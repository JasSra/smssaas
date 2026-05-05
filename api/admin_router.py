"""Admin endpoints — internal use, protected by ADMIN_KEY env var."""
import os
import json
import uuid
import secrets
from pathlib import Path
from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import FileResponse
from datetime import datetime, timezone

from db import db
from models import PhoneRegister, AdminSmsSend, AdminVoiceCall
from redis_telemetry import push_recent_sms, mark_phone_online

router = APIRouter(prefix="/admin", tags=["admin"])

ADMIN_KEY = os.environ.get("ADMIN_KEY", "changeme")


def _require_admin(x_admin_key: str = Header(...)):
    if x_admin_key != ADMIN_KEY:
        raise HTTPException(status_code=401, detail="invalid admin key")


# ── Phones ────────────────────────────────────────────────────────────────────

@router.get("/phones")
def list_phones(_=_require_admin):
    with db() as conn:
        rows = conn.execute(
            """
            SELECT p.*, d.signal_dbm, d.battery_pct, d.network_type,
                   (SELECT COUNT(*) FROM outbound_queue q
                    WHERE q.device_id=p.device_id AND q.status IN ('pending','sending')
                   ) AS queue_depth
            FROM phones p
            LEFT JOIN diagnostics d ON d.device_id=p.device_id
              AND d.id = (SELECT MAX(id) FROM diagnostics WHERE device_id=p.device_id)
            ORDER BY p.registered_at
            """
        ).fetchall()
    return {"phones": [dict(r) for r in rows]}


@router.post("/phones/register")
def register_phone(body: PhoneRegister, _=_require_admin):
    with db() as conn:
        existing = conn.execute(
            "SELECT device_id FROM phones WHERE device_id=?", (body.device_id,)
        ).fetchone()
        if existing:
            # update carrier/number if phone re-registers
            # but don't overwrite a known phone number with 'unknown'
            existing_row = conn.execute(
                "SELECT phone_number FROM phones WHERE device_id=?", (body.device_id,)
            ).fetchone()
            stored_number = existing_row["phone_number"] if existing_row else None
            new_number = body.phone_number
            if new_number in ("unknown", "", None) and stored_number not in ("unknown", "", None):
                new_number = stored_number  # keep the good number
            conn.execute(
                """
                UPDATE phones SET phone_number=?, country_code=?, carrier=?, status='online', last_seen=?
                WHERE device_id=?
                """,
                (
                    new_number,
                    body.country_code,
                    body.carrier,
                    datetime.now(timezone.utc).isoformat(),
                    body.device_id,
                ),
            )
            return {"registered": False, "updated": True, "device_id": body.device_id}

        conn.execute(
            """
            INSERT INTO phones (device_id, phone_number, country_code, carrier, tenant_id, status, last_seen)
            VALUES (?,?,?,?,?,?,?)
            """,
            (
                body.device_id,
                body.phone_number,
                body.country_code,
                body.carrier,
                body.tenant_id,
                "online",
                datetime.now(timezone.utc).isoformat(),
            ),
        )
    return {"registered": True, "device_id": body.device_id}


@router.delete("/phones/{device_id}")
def remove_phone(device_id: str, _=_require_admin):
    with db() as conn:
        conn.execute("DELETE FROM phones WHERE device_id=?", (device_id,))
    return {"ok": True}


# ── SMS test ──────────────────────────────────────────────────────────────────

@router.post("/sms/test")
def test_sms(body: AdminSmsSend, _=_require_admin):
    """Enqueue a test SMS without requiring a tenant API key."""
    with db() as conn:
        # use or create a __admin__ tenant
        admin_tenant = conn.execute(
            "SELECT id FROM tenants WHERE id='__admin__'"
        ).fetchone()
        if not admin_tenant:
            conn.execute(
                "INSERT INTO tenants (id, api_key, plan) VALUES ('__admin__',?,?)",
                (secrets.token_hex(32), "business"),
            )

        # pick phone
        if body.device_id:
            device_id = body.device_id
        else:
            row = conn.execute(
                "SELECT device_id FROM phones WHERE status='online' AND country_code=? LIMIT 1",
                (body.country_code,),
            ).fetchone()
            if not row:
                raise HTTPException(status_code=503, detail="no online phone available")
            device_id = row["device_id"]

        conn.execute(
            "INSERT INTO outbound_queue (tenant_id, device_id, to_number, body) VALUES (?,?,?,?)",
            ("__admin__", device_id, body.to, body.body),
        )
        msg_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    push_recent_sms(
        tenant_id="__admin__",
        device_id=device_id,
        direction="outbound",
        from_number="",
        to_number=body.to,
        body=body.body,
        message_id=msg_id,
    )
    return {"message_id": msg_id, "device_id": device_id, "status": "pending"}


# ── Usage overview ────────────────────────────────────────────────────────────

@router.get("/usage")
def admin_usage(_=_require_admin):
    with db() as conn:
        rows = conn.execute(
            """
            SELECT t.id, t.plan,
              SUM(CASE WHEN l.direction='outbound' THEN l.credits ELSE 0 END) AS sms_out,
              SUM(CASE WHEN l.direction='inbound'  THEN l.credits ELSE 0 END) AS sms_in
            FROM tenants t
            LEFT JOIN usage_ledger l ON l.tenant_id=t.id
              AND strftime('%Y-%m', l.billed_at) = strftime('%Y-%m', 'now')
            GROUP BY t.id
            ORDER BY sms_out DESC
            """
        ).fetchall()
    return {"usage": [dict(r) for r in rows]}


# ── Tenant management ─────────────────────────────────────────────────────────

@router.post("/tenants")
def create_tenant(plan: str = "starter", webhook_url: str | None = None, _=_require_admin):
    tenant_id = str(uuid.uuid4())
    api_key = secrets.token_urlsafe(32)
    with db() as conn:
        conn.execute(
            "INSERT INTO tenants (id, api_key, plan, webhook_url) VALUES (?,?,?,?)",
            (tenant_id, api_key, plan, webhook_url),
        )
    return {"tenant_id": tenant_id, "api_key": api_key, "plan": plan}


@router.get("/tenants")
def list_tenants(_=_require_admin):
    with db() as conn:
        rows = conn.execute("SELECT id, plan, webhook_url, created_at FROM tenants WHERE id != '__admin__'").fetchall()
    return {"tenants": [dict(r) for r in rows]}


# ── Admin message/inbound/call/voicemail/diagnostic views ─────────────────────

@router.get("/messages")
def admin_messages(
    limit: int = 100,
    status: str | None = None,
    device_id: str | None = None,
    _=_require_admin,
):
    with db() as conn:
        where = "1=1"
        params: list = []
        if status:
            where += " AND status=?"
            params.append(status)
        if device_id:
            where += " AND device_id=?"
            params.append(device_id)
        rows = conn.execute(
            f"SELECT * FROM outbound_queue WHERE {where} ORDER BY created_at DESC LIMIT ?",
            [*params, limit],
        ).fetchall()
    return {"messages": [dict(r) for r in rows]}


@router.get("/inbound")
def admin_inbound(limit: int = 100, device_id: str | None = None, _=_require_admin):
    with db() as conn:
        where = "1=1"
        params: list = []
        if device_id:
            where += " AND device_id=?"
            params.append(device_id)
        rows = conn.execute(
            f"SELECT * FROM inbound_messages WHERE {where} ORDER BY received_at DESC LIMIT ?",
            [*params, limit],
        ).fetchall()
    return {"messages": [dict(r) for r in rows]}


@router.get("/calls")
def admin_calls(limit: int = 100, device_id: str | None = None, _=_require_admin):
    with db() as conn:
        where = "1=1"
        params: list = []
        if device_id:
            where += " AND device_id=?"
            params.append(device_id)
        rows = conn.execute(
            f"SELECT * FROM calls WHERE {where} ORDER BY started_at DESC LIMIT ?",
            [*params, limit],
        ).fetchall()
    return {"calls": [dict(r) for r in rows]}


@router.get("/voicemails")
def admin_voicemails(limit: int = 100, _=_require_admin):
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM voicemails ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return {"voicemails": [dict(r) for r in rows]}


@router.post("/voice/call")
def admin_voice_call(body: AdminVoiceCall, _=_require_admin):
    """Place an outbound call from a managed phone.

    Inserts a `calls` row in 'dialing' state and a `device_commands` row of
    type='dial' that the phone picks up via /worker/poll/commands.
    """
    call_id = str(uuid.uuid4())
    with db() as conn:
        phone = conn.execute(
            "SELECT tenant_id, status FROM phones WHERE device_id=?",
            (body.device_id,),
        ).fetchone()
        if not phone:
            raise HTTPException(status_code=404, detail="unknown device_id")
        if phone["status"] != "online":
            raise HTTPException(status_code=503, detail="device offline")

        tenant_id = body.tenant_id or phone["tenant_id"]

        conn.execute(
            """
            INSERT INTO calls
              (id, tenant_id, device_id, direction, from_number, to_number,
               caller_id, ivr_flow_id, status)
            VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (
                call_id, tenant_id, body.device_id, "outbound",
                "", body.to_number, body.caller_id, body.ivr_flow_id,
                "dialing",
            ),
        )
        conn.execute(
            "INSERT INTO device_commands (device_id, command, payload_json) VALUES (?,?,?)",
            (
                body.device_id,
                "dial",
                json.dumps({"call_id": call_id, "to_number": body.to_number}),
            ),
        )

    return {"call_id": call_id, "device_id": body.device_id, "status": "dialing"}


@router.get("/voice/calls/{call_id}")
def admin_voice_call_get(call_id: str, _=_require_admin):
    with db() as conn:
        row = conn.execute("SELECT * FROM calls WHERE id=?", (call_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="call not found")
    return {"call": dict(row)}


@router.post("/voice/calls/{call_id}/hangup")
def admin_voice_call_hangup(call_id: str, _=_require_admin):
    """Enqueue a 'hangup' device_command for the call's device."""
    with db() as conn:
        row = conn.execute(
            "SELECT device_id, status FROM calls WHERE id=?", (call_id,)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="call not found")
        if row["status"] in ("completed", "failed", "missed"):
            return {"ok": True, "noop": True}
        conn.execute(
            "INSERT INTO device_commands (device_id, command, payload_json) VALUES (?,?,?)",
            (row["device_id"], "hangup", json.dumps({"call_id": call_id})),
        )
    return {"ok": True}


@router.post("/voice/calls/{call_id}/dtmf")
def admin_voice_call_dtmf(call_id: str, digit: str, _=_require_admin):
    """Inject a DTMF digit into the live call via the per-call broker."""
    if digit not in "0123456789*#":
        raise HTTPException(status_code=400, detail="invalid digit")
    # Import inline to avoid circular import at module load.
    from audio_ws import _BROKERS, _pack, DIR_TO_PHONE, TYPE_DTMF
    broker = _BROKERS.get(call_id)
    if broker is None:
        raise HTTPException(status_code=409, detail="call not active")
    broker.inject(_pack(DIR_TO_PHONE, TYPE_DTMF, 0, digit.encode("ascii")))
    return {"ok": True}


@router.get("/voice/calls/{call_id}/recording")
def admin_voice_call_recording(call_id: str, _=_require_admin):
    with db() as conn:
        row = conn.execute(
            "SELECT recording_path FROM calls WHERE id=?", (call_id,)
        ).fetchone()
    if not row or not row["recording_path"]:
        raise HTTPException(status_code=404, detail="recording not ready")
    p = Path(row["recording_path"])
    if not p.exists():
        raise HTTPException(status_code=404, detail="recording missing on disk")
    return FileResponse(
        path=str(p),
        media_type="audio/ogg",
        filename=f"{call_id}.opus",
        headers={"Cache-Control": "private, max-age=300"},
    )


@router.get("/sensors/{device_id}/latest")
def admin_sensor_latest(device_id: str, _=_require_admin):
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM sensor_readings WHERE device_id=? ORDER BY id DESC LIMIT 1",
            (device_id,),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="no readings yet")
    return {"reading": dict(row)}


@router.get("/sensors/{device_id}/timeline")
def admin_sensor_timeline(device_id: str, limit: int = 200, _=_require_admin):
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM sensor_readings WHERE device_id=? ORDER BY id DESC LIMIT ?",
            (device_id, min(limit, 2000)),
        ).fetchall()
    return {"readings": [dict(r) for r in rows]}


@router.get("/sensors/{device_id}/snapshot/{reading_id}")
def admin_sensor_snapshot(device_id: str, reading_id: int, _=_require_admin):
    with db() as conn:
        row = conn.execute(
            "SELECT snapshot_path FROM sensor_readings WHERE id=? AND device_id=?",
            (reading_id, device_id),
        ).fetchone()
    if not row or not row["snapshot_path"]:
        raise HTTPException(status_code=404, detail="snapshot missing")
    p = Path(row["snapshot_path"])
    if not p.exists():
        raise HTTPException(status_code=404, detail="snapshot missing on disk")
    return FileResponse(str(p), media_type="image/jpeg",
                        headers={"Cache-Control": "private, max-age=86400"})


@router.get("/diagnostics")
def admin_diagnostics(device_id: str, limit: int = 50, _=_require_admin):
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM diagnostics WHERE device_id=? ORDER BY timestamp DESC LIMIT ?",
            (device_id, limit),
        ).fetchall()
    return {"diagnostics": [dict(r) for r in rows]}


@router.get("/stats")
def admin_stats(_=_require_admin):
    """Overview counts for the dashboard."""
    with db() as conn:
        online = conn.execute("SELECT COUNT(*) FROM phones WHERE status='online'").fetchone()[0]
        total_phones = conn.execute("SELECT COUNT(*) FROM phones").fetchone()[0]
        pending = conn.execute(
            "SELECT COUNT(*) FROM outbound_queue WHERE status IN ('pending','sending')"
        ).fetchone()[0]
        sent_today = conn.execute(
            "SELECT COUNT(*) FROM outbound_queue WHERE status IN ('sent','delivered') AND date(sent_at)=date('now')"
        ).fetchone()[0]
        inbound_today = conn.execute(
            "SELECT COUNT(*) FROM inbound_messages WHERE date(received_at)=date('now')"
        ).fetchone()[0]
        calls_today = conn.execute(
            "SELECT COUNT(*) FROM calls WHERE date(started_at)=date('now')"
        ).fetchone()[0]
        unread_vm = conn.execute(
            "SELECT COUNT(*) FROM voicemails WHERE read=FALSE"
        ).fetchone()[0]
    return {
        "phones_online": online,
        "phones_total": total_phones,
        "queue_depth": pending,
        "sent_today": sent_today,
        "inbound_today": inbound_today,
        "calls_today": calls_today,
        "unread_voicemails": unread_vm,
    }


# ── Redis telemetry pass-through ─────────────────────────────────────────────
# These endpoints read from Redis (populated by worker_router) and let
# inboxr-cloud render live charts without hitting SQLite.

@router.get("/diag/{device_id}")
def admin_diag_stream(device_id: str, since_ms: int = 0, limit: int = 500, _=_require_admin):
    """
    Read the diagnostics stream for one phone. Each entry has battery_pct,
    signal_dbm, network_type, queue_depth, ts.
    """
    from redis_telemetry import _get_client
    c = _get_client()
    if c is None:
        return {"items": [], "redis": "unavailable"}
    try:
        start = f"{since_ms}-0" if since_ms else "-"
        entries = c.xrange(f"phone:{device_id}:diag", min=start, count=limit)
        items = [
            {"id": eid, **fields} for eid, fields in entries
        ]
        return {"items": items}
    except Exception as e:
        return {"items": [], "error": str(e)}


@router.get("/recent/sms")
def admin_recent_sms(limit: int = 50, _=_require_admin):
    from redis_telemetry import _get_client
    c = _get_client()
    if c is None:
        return {"items": [], "redis": "unavailable"}
    try:
        entries = c.xrevrange("recent:sms", count=limit)
        items = [{"id": eid, **fields} for eid, fields in entries]
        return {"items": items}
    except Exception as e:
        return {"items": [], "error": str(e)}


@router.get("/recent/calls")
def admin_recent_calls(limit: int = 50, _=_require_admin):
    from redis_telemetry import _get_client
    c = _get_client()
    if c is None:
        return {"items": [], "redis": "unavailable"}
    try:
        entries = c.xrevrange("recent:calls", count=limit)
        items = [{"id": eid, **fields} for eid, fields in entries]
        return {"items": items}
    except Exception as e:
        return {"items": [], "error": str(e)}


@router.get("/phones/online")
def admin_phones_online(_=_require_admin):
    """Live presence — phones that heartbeat-ed within the last 90s."""
    from redis_telemetry import _get_client
    import time as _time
    c = _get_client()
    if c is None:
        return {"items": [], "redis": "unavailable"}
    try:
        now_ms = int(_time.time() * 1000)
        cutoff = now_ms - 90_000
        ids = c.zrangebyscore("phone:online", cutoff, "+inf", withscores=True)
        result = []
        for device_id, last_seen_ms in ids:
            meta = c.hgetall(f"phone:meta:{device_id}") or {}
            result.append({
                "device_id": device_id,
                "last_seen_ms": int(last_seen_ms),
                "phone_number": meta.get("phone_number"),
                "carrier": meta.get("carrier"),
                "country": meta.get("country"),
                "model": meta.get("model"),
            })
        return {"items": result, "now_ms": now_ms}
    except Exception as e:
        return {"items": [], "error": str(e)}
