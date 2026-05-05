"""Tenant-facing API endpoints — require Bearer API key."""
from fastapi import APIRouter, Header, HTTPException, Depends
from datetime import datetime, timezone
import uuid

from db import db
from models import SmsSend, VoiceCall, IvrFlowCreate

router = APIRouter(prefix="/v1", tags=["tenant"])


def _tenant(authorization: str = Header(...)):
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    api_key = authorization.removeprefix("Bearer ").strip()
    with db() as conn:
        row = conn.execute(
            "SELECT id, plan FROM tenants WHERE api_key=?", (api_key,)
        ).fetchone()
    if not row:
        raise HTTPException(status_code=401, detail="invalid api key")
    return dict(row)


def _pick_phone(conn, tenant_id: str, country_code: str, device_id: str | None):
    """Return a device_id for this send job, or raise 503."""
    if device_id:
        row = conn.execute(
            "SELECT device_id FROM phones WHERE device_id=? AND status='online'",
            (device_id,),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=503, detail="requested phone is offline")
        return device_id

    # auto-assign: prefer dedicated phone for this tenant, else shared pool
    row = conn.execute(
        """
        SELECT p.device_id FROM phones p
        LEFT JOIN (
          SELECT device_id, COUNT(*) AS depth
          FROM outbound_queue WHERE status IN ('pending','sending')
          GROUP BY device_id
        ) q ON p.device_id = q.device_id
        WHERE p.status='online' AND p.country_code=?
          AND (p.tenant_id=? OR p.tenant_id IS NULL)
        ORDER BY p.tenant_id DESC, COALESCE(q.depth,0) ASC
        LIMIT 1
        """,
        (country_code, tenant_id),
    ).fetchone()

    if not row:
        raise HTTPException(
            status_code=503,
            detail=f"no online phone available for country {country_code}",
        )
    return row["device_id"]


# ── SMS ───────────────────────────────────────────────────────────────────────

@router.post("/sms/send")
def sms_send(body: SmsSend, tenant=Depends(_tenant)):
    with db() as conn:
        device_id = _pick_phone(conn, tenant["id"], body.country_code, body.device_id)
        conn.execute(
            """
            INSERT INTO outbound_queue (tenant_id, device_id, to_number, body, media_url)
            VALUES (?,?,?,?,?)
            """,
            (tenant["id"], device_id, body.to, body.body, body.media_url if hasattr(body, "media_url") else None),
        )
        msg_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "INSERT INTO usage_ledger (tenant_id, message_id, direction, credits) VALUES (?,?,?,?)",
            (tenant["id"], msg_id, "outbound", 1.0),
        )
    return {"message_id": msg_id, "device_id": device_id, "status": "pending"}


@router.get("/sms/messages")
def sms_messages(
    limit: int = 50,
    offset: int = 0,
    status: str | None = None,
    tenant=Depends(_tenant),
):
    with db() as conn:
        where = "tenant_id=?"
        params: list = [tenant["id"]]
        if status:
            where += " AND status=?"
            params.append(status)
        rows = conn.execute(
            f"SELECT * FROM outbound_queue WHERE {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()
    return {"messages": [dict(r) for r in rows]}


@router.get("/sms/inbound")
def sms_inbound(limit: int = 50, offset: int = 0, tenant=Depends(_tenant)):
    with db() as conn:
        rows = conn.execute(
            """
            SELECT * FROM inbound_messages
            WHERE tenant_id=? ORDER BY received_at DESC LIMIT ? OFFSET ?
            """,
            (tenant["id"], limit, offset),
        ).fetchall()
    return {"messages": [dict(r) for r in rows]}


# ── Usage ─────────────────────────────────────────────────────────────────────

PLAN_LIMITS = {
    "starter":  {"sms": 500,   "voice_min": 100,  "voicemail": 10},
    "pro":      {"sms": 2000,  "voice_min": 500,  "voicemail": 50},
    "business": {"sms": 10000, "voice_min": 2000, "voicemail": 999999},
}

@router.get("/usage")
def usage(tenant=Depends(_tenant)):
    with db() as conn:
        sms_used = conn.execute(
            """
            SELECT COALESCE(SUM(credits),0) FROM usage_ledger
            WHERE tenant_id=? AND direction='outbound'
              AND strftime('%Y-%m', billed_at) = strftime('%Y-%m', 'now')
            """,
            (tenant["id"],),
        ).fetchone()[0]
    limits = PLAN_LIMITS.get(tenant["plan"], PLAN_LIMITS["starter"])
    return {
        "plan": tenant["plan"],
        "sms_used": int(sms_used),
        "sms_limit": limits["sms"],
        "sms_remaining": max(0, limits["sms"] - int(sms_used)),
    }


# ── Numbers ───────────────────────────────────────────────────────────────────

@router.get("/numbers")
def numbers(tenant=Depends(_tenant)):
    with db() as conn:
        rows = conn.execute(
            """
            SELECT device_id, phone_number, country_code, carrier, status
            FROM phones WHERE tenant_id=?
            """,
            (tenant["id"],),
        ).fetchall()
    return {"numbers": [dict(r) for r in rows]}


# ── Voice ─────────────────────────────────────────────────────────────────────

@router.post("/voice/call")
def voice_call(body: VoiceCall, tenant=Depends(_tenant)):
    call_id = str(uuid.uuid4())
    with db() as conn:
        device_id = _pick_phone(conn, tenant["id"], body.country_code, body.device_id)
        conn.execute(
            """
            INSERT INTO calls
              (id, tenant_id, device_id, direction, from_number, to_number, caller_id, status, ivr_flow_id)
            VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (
                call_id,
                tenant["id"],
                device_id,
                "outbound",
                "",  # filled in by phone after dial
                body.to,
                body.caller_id,
                "pending_dial",
                body.ivr_flow_id,
            ),
        )
    # The phone will pick up the dial command on next WebSocket heartbeat
    # (push command channel is a phase-2 enhancement)
    return {"call_id": call_id, "device_id": device_id, "status": "pending_dial"}


@router.get("/voice/calls")
def voice_calls(limit: int = 50, offset: int = 0, tenant=Depends(_tenant)):
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM calls WHERE tenant_id=? ORDER BY started_at DESC LIMIT ? OFFSET ?",
            (tenant["id"], limit, offset),
        ).fetchall()
    return {"calls": [dict(r) for r in rows]}


@router.get("/voice/voicemails")
def voicemails(limit: int = 50, offset: int = 0, tenant=Depends(_tenant)):
    with db() as conn:
        rows = conn.execute(
            "SELECT id,call_id,from_number,duration_sec,transcript,read,created_at FROM voicemails WHERE tenant_id=? ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (tenant["id"], limit, offset),
        ).fetchall()
    return {"voicemails": [dict(r) for r in rows]}


@router.get("/voice/voicemails/{vm_id}/audio")
def voicemail_audio(vm_id: int, tenant=Depends(_tenant)):
    from fastapi.responses import FileResponse
    with db() as conn:
        row = conn.execute(
            "SELECT audio_path FROM voicemails WHERE id=? AND tenant_id=?",
            (vm_id, tenant["id"]),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404)
    return FileResponse(row["audio_path"], media_type="audio/ogg")


@router.get("/voice/calls/{call_id}/recording")
def call_recording(call_id: str, tenant=Depends(_tenant)):
    from fastapi.responses import FileResponse
    with db() as conn:
        row = conn.execute(
            "SELECT recording_path FROM calls WHERE id=? AND tenant_id=?",
            (call_id, tenant["id"]),
        ).fetchone()
    if not row or not row["recording_path"]:
        raise HTTPException(status_code=404)
    return FileResponse(row["recording_path"], media_type="audio/ogg")


# ── IVR flows ─────────────────────────────────────────────────────────────────

@router.post("/voice/ivr")
def ivr_create(body: IvrFlowCreate, tenant=Depends(_tenant)):
    flow_id = str(uuid.uuid4())
    with db() as conn:
        conn.execute(
            "INSERT INTO ivr_flows (id, tenant_id, name, flow_json) VALUES (?,?,?,?)",
            (flow_id, tenant["id"], body.name, body.flow_json),
        )
    return {"id": flow_id}


@router.get("/voice/ivr")
def ivr_list(tenant=Depends(_tenant)):
    with db() as conn:
        rows = conn.execute(
            "SELECT id,name,active,created_at FROM ivr_flows WHERE tenant_id=? ORDER BY created_at DESC",
            (tenant["id"],),
        ).fetchall()
    return {"flows": [dict(r) for r in rows]}
