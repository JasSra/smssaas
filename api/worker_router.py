"""Endpoints called by the Android app worker."""
from fastapi import APIRouter, Header, HTTPException
from datetime import datetime, timezone
import os
import httpx

from db import db
from models import ReceiptPost, InboundPost, DiagnosticPost, HeartbeatPost, CallStatePost
from redis_telemetry import (
    mark_phone_online, push_diagnostic, push_recent_sms, push_recent_call,
)

router = APIRouter(prefix="/worker", tags=["worker"])

DEVICE_SECRET = "smssaas-worker-secret"  # set via env in production

# Optional: inboxr-cloud routing webhook. Set CLOUD_WEBHOOK_URL + CLOUD_WEBHOOK_KEY in .env
# e.g. CLOUD_WEBHOOK_URL=https://app.getinboxr.app/api/internal/sms/inbound
#      CLOUD_WEBHOOK_KEY=<same value as SMS_ADMIN_KEY in inboxr-cloud>
CLOUD_WEBHOOK_URL = os.environ.get("CLOUD_WEBHOOK_URL", "")
CLOUD_WEBHOOK_KEY = os.environ.get("CLOUD_WEBHOOK_KEY", "")


def _require_device(x_device_secret: str = Header(...)):
    if x_device_secret != DEVICE_SECRET:
        raise HTTPException(status_code=401, detail="invalid device secret")


# ── Poll for outbound jobs ────────────────────────────────────────────────────

@router.get("/poll")
def poll(device_id: str, limit: int = 10):
    """Phone calls this every ~3 seconds to get pending SMS jobs."""
    with db() as conn:
        rows = conn.execute(
            """
            SELECT id, to_number, body, media_url
            FROM outbound_queue
            WHERE device_id = ? AND status = 'pending'
            ORDER BY created_at
            LIMIT ?
            """,
            (device_id, limit),
        ).fetchall()

        # mark as sending so another poll won't double-pick
        ids = [r["id"] for r in rows]
        if ids:
            conn.execute(
                f"UPDATE outbound_queue SET status='sending' WHERE id IN ({','.join('?'*len(ids))})",
                ids,
            )

    return {"jobs": [dict(r) for r in rows]}


# ── Poll for device commands (dial, hangup, ...) ─────────────────────────────

@router.get("/poll/commands")
def poll_commands(device_id: str, limit: int = 5):
    """Phone polls this alongside /poll to get pending dial/hangup commands."""
    with db() as conn:
        rows = conn.execute(
            """
            SELECT id, command, payload_json
            FROM device_commands
            WHERE device_id=? AND status='pending'
            ORDER BY created_at
            LIMIT ?
            """,
            (device_id, limit),
        ).fetchall()
        ids = [r["id"] for r in rows]
        if ids:
            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                f"UPDATE device_commands SET status='picked', picked_at=? "
                f"WHERE id IN ({','.join('?'*len(ids))})",
                [now, *ids],
            )
    return {"commands": [dict(r) for r in rows]}


@router.post("/command/ack")
def command_ack(cmd_id: int, result: str = "done"):
    """Phone reports a command as completed or failed."""
    with db() as conn:
        conn.execute(
            "UPDATE device_commands SET status=? WHERE id=?",
            (result if result in ("done", "failed") else "done", cmd_id),
        )
    return {"ok": True}


# ── Delivery receipt ──────────────────────────────────────────────────────────

@router.post("/receipt")
def receipt(body: ReceiptPost):
    now = datetime.now(timezone.utc).isoformat()
    with db() as conn:
        conn.execute(
            "INSERT INTO delivery_receipts (message_id, result, error_code) VALUES (?,?,?)",
            (body.message_id, body.result, body.error_code),
        )
        if body.result in ("SENT", "DELIVERED"):
            conn.execute(
                "UPDATE outbound_queue SET status=?, sent_at=? WHERE id=?",
                (body.result.lower(), now, body.message_id),
            )
        else:
            conn.execute(
                "UPDATE outbound_queue SET status='failed', error_msg=? WHERE id=?",
                (f"error_code={body.error_code}", body.message_id),
            )
    return {"ok": True}


# ── Inbound SMS ───────────────────────────────────────────────────────────────

@router.post("/inbound")
async def inbound(body: InboundPost):
    with db() as conn:
        # resolve tenant from phone
        row = conn.execute(
            "SELECT tenant_id FROM phones WHERE device_id=?", (body.device_id,)
        ).fetchone()
        tenant_id = row["tenant_id"] if row else None

        conn.execute(
            """
            INSERT INTO inbound_messages
              (tenant_id, device_id, from_number, body, media_json, received_at)
            VALUES (?,?,?,?,?,?)
            """,
            (
                tenant_id,
                body.device_id,
                body.from_number,
                body.body,
                body.media_json,
                body.received_at.isoformat(),
            ),
        )
        msg_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        webhook_url = None
        if tenant_id:
            t = conn.execute(
                "SELECT webhook_url FROM tenants WHERE id=?", (tenant_id,)
            ).fetchone()
            webhook_url = t["webhook_url"] if t else None

    # Telemetry — inbound SMS into the live feed
    push_recent_sms(
        tenant_id=tenant_id,
        device_id=body.device_id,
        direction="inbound",
        from_number=body.from_number,
        to_number="",
        body=body.body,
        message_id=msg_id,
    )

    # fire webhooks outside DB transaction
    async with httpx.AsyncClient(timeout=5) as client:
        # 1. Legacy per-tenant webhook (SmsSaaS-native)
        if webhook_url:
            legacy_payload = {
                "event": "inbound_sms",
                "from": body.from_number,
                "body": body.body,
                "received_at": body.received_at.isoformat(),
            }
            try:
                await client.post(webhook_url, json=legacy_payload)
                with db() as conn:
                    conn.execute(
                        "UPDATE inbound_messages SET webhook_delivered=TRUE WHERE id=?",
                        (msg_id,),
                    )
            except Exception:
                pass  # non-fatal

        # 2. Inboxr-cloud routing webhook (multi-tenant inbox routing)
        if CLOUD_WEBHOOK_URL and CLOUD_WEBHOOK_KEY:
            cloud_payload = {
                "device_id":   body.device_id,
                "from_number": body.from_number,
                "body":        body.body,
                "received_at": body.received_at.isoformat(),
                "smssaas_id":  msg_id,
            }
            try:
                await client.post(
                    CLOUD_WEBHOOK_URL,
                    json=cloud_payload,
                    headers={"x-smssaas-key": CLOUD_WEBHOOK_KEY},
                )
            except Exception as e:
                # Log but don't fail — inbound message is already stored locally
                print(f"[worker/inbound] cloud webhook error: {e}")

    return {"ok": True}


# ── Diagnostic ────────────────────────────────────────────────────────────────

@router.post("/diagnostic")
def diagnostic(body: DiagnosticPost):
    queue_depth = None
    with db() as conn:
        conn.execute(
            """
            INSERT INTO diagnostics
              (device_id, signal_dbm, battery_pct, carrier, network_type, data_state)
            VALUES (?,?,?,?,?,?)
            """,
            (
                body.device_id,
                body.signal_dbm,
                body.battery_pct,
                body.carrier,
                body.network_type,
                body.data_state,
            ),
        )
        # prune old rows — keep last 1000 per device
        conn.execute(
            """
            DELETE FROM diagnostics WHERE device_id=? AND id NOT IN (
              SELECT id FROM diagnostics WHERE device_id=?
              ORDER BY timestamp DESC LIMIT 1000
            )
            """,
            (body.device_id, body.device_id),
        )
        # Capture current queue depth so the time series shows backpressure
        row = conn.execute(
            "SELECT COUNT(*) AS qd FROM outbound_queue WHERE device_id=? AND status IN ('pending','sending')",
            (body.device_id,),
        ).fetchone()
        queue_depth = row["qd"] if row else None

    push_diagnostic(
        body.device_id,
        battery_pct=body.battery_pct,
        signal_dbm=body.signal_dbm,
        network_type=body.network_type,
        queue_depth=queue_depth,
    )
    return {"ok": True}


# ── Heartbeat ─────────────────────────────────────────────────────────────────

@router.post("/heartbeat")
def heartbeat(body: HeartbeatPost):
    now = datetime.now(timezone.utc).isoformat()
    phone_meta = None
    with db() as conn:
        updated = conn.execute(
            "UPDATE phones SET status='online', last_seen=? WHERE device_id=?",
            (now, body.device_id),
        ).rowcount
        if updated:
            row = conn.execute(
                "SELECT phone_number, carrier, country_code FROM phones WHERE device_id=?",
                (body.device_id,),
            ).fetchone()
            if row:
                phone_meta = dict(row)
    if not updated:
        raise HTTPException(status_code=404, detail="unknown device_id")

    # Telemetry
    mark_phone_online(
        body.device_id,
        phone_number=phone_meta and phone_meta.get("phone_number"),
        carrier=phone_meta and phone_meta.get("carrier"),
        country=phone_meta and phone_meta.get("country_code"),
    )
    return {"ok": True}


# ── Sensor telemetry ─────────────────────────────────────────────────────────

import os as _os
import json as _json
from pathlib import Path as _Path
from fastapi import UploadFile, File, Form

SNAPSHOTS_DIR = _Path(_os.environ.get("DATA_DIR", "/data")) / "snapshots"
SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)


@router.post("/sensor")
async def post_sensor(
    device_id: str = Form(...),
    captured_at: str = Form(...),
    payload_json: str = Form(...),
    snapshot: UploadFile | None = File(None),
):
    """Bulk sensor + camera snapshot upload from the SensorWorkerService."""
    snapshot_path: str | None = None
    if snapshot is not None:
        # Path: /data/snapshots/{device_id}/{YYYYMMDD}/{epoch_ms}.jpg
        ts_dir = SNAPSHOTS_DIR / device_id / captured_at[:10].replace("-", "")
        ts_dir.mkdir(parents=True, exist_ok=True)
        epoch_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        snapshot_path = str(ts_dir / f"{epoch_ms}.jpg")
        with open(snapshot_path, "wb") as f:
            f.write(await snapshot.read())

    try:
        p = _json.loads(payload_json)
    except Exception:
        p = {}

    with db() as conn:
        conn.execute(
            """
            INSERT INTO sensor_readings (
              device_id, captured_at,
              audio_rms, audio_db,
              battery_pct, battery_temp_c, battery_charging,
              light_lux, pressure_hpa,
              accel_x, accel_y, accel_z, accel_magnitude,
              magnet_x, magnet_y, magnet_z,
              cell_dbm, cell_network, wifi_json,
              snapshot_path
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                device_id, captured_at,
                p.get("audio_rms"), p.get("audio_db"),
                p.get("battery_pct"), p.get("battery_temp_c"), p.get("battery_charging"),
                p.get("light_lux"), p.get("pressure_hpa"),
                p.get("accel_x"), p.get("accel_y"), p.get("accel_z"), p.get("accel_magnitude"),
                p.get("magnet_x"), p.get("magnet_y"), p.get("magnet_z"),
                p.get("cell_dbm"), p.get("cell_network"),
                _json.dumps(p.get("wifi", [])) if p.get("wifi") is not None else None,
                snapshot_path,
            ),
        )
        sid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        # Prune: keep last 5000 readings per device
        conn.execute(
            """
            DELETE FROM sensor_readings WHERE device_id=? AND id NOT IN (
              SELECT id FROM sensor_readings WHERE device_id=?
              ORDER BY id DESC LIMIT 5000
            )
            """,
            (device_id, device_id),
        )
    return {"ok": True, "id": sid, "snapshot_path": snapshot_path}


# ── Call state ────────────────────────────────────────────────────────────────

@router.post("/call/state")
def call_state(body: CallStatePost):
    now = datetime.now(timezone.utc).isoformat()
    tenant_id = None
    with db() as conn:
        existing = conn.execute(
            "SELECT id FROM calls WHERE id=?", (body.call_id,)
        ).fetchone()

        if existing:
            updates = {"status": body.status}
            if body.status == "active":
                updates["answered_at"] = now
            elif body.status in ("completed", "failed", "missed"):
                updates["ended_at"] = now
                if body.duration_sec is not None:
                    updates["duration_sec"] = body.duration_sec

            set_clause = ", ".join(f"{k}=?" for k in updates)
            conn.execute(
                f"UPDATE calls SET {set_clause} WHERE id=?",
                [*updates.values(), body.call_id],
            )
        else:
            # first report for this call — create the record
            row = conn.execute(
                "SELECT tenant_id FROM phones WHERE device_id=?", (body.device_id,)
            ).fetchone()
            tenant_id = row["tenant_id"] if row else None

            conn.execute(
                """
                INSERT INTO calls
                  (id, tenant_id, device_id, direction, from_number, to_number, status)
                VALUES (?,?,?,?,?,?,?)
                """,
                (
                    body.call_id,
                    tenant_id,
                    body.device_id,
                    body.direction or "inbound",
                    body.from_number or "",
                    body.to_number or "",
                    body.status,
                ),
            )

    push_recent_call(
        tenant_id=tenant_id,
        device_id=body.device_id,
        call_id=body.call_id,
        direction=body.direction,
        status=body.status,
        from_number=body.from_number or "",
        to_number=body.to_number or "",
        duration_sec=body.duration_sec,
    )
    return {"ok": True}
