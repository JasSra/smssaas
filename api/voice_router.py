"""
Voice / IVR API surface — /v1/voice/*

Public routes (Bearer auth):
  POST   /v1/voice/ivr-flows
  GET    /v1/voice/ivr-flows
  GET    /v1/voice/ivr-flows/{id}
  PATCH  /v1/voice/ivr-flows/{id}
  DELETE /v1/voice/ivr-flows/{id}
  POST   /v1/voice/ivr-flows/from-description
  POST   /v1/voice/ivr-flows/{id}/dry-run
  GET    /v1/voice/ivr-flows/{id}/traces

  POST   /v1/voice/tts/generate
  POST   /v1/voice/tts/script
  GET    /v1/voice/tts/voices
  GET    /v1/voice/tts/audio/{filename}        (no auth — cached audio)

  GET    /v1/voice/templates
  POST   /v1/voice/templates/{id}/clone

  GET    /v1/voice/inboxes
  PUT    /v1/voice/inboxes/{id}/flow
  POST   /v1/voice/inboxes/{id}/test-call

Worker routes (no auth — device polls):
  POST   /v1/voice/incoming                    (InCallService announces an inbound ring)
  POST   /v1/voice/trace                       (IVR engine writes a transition)
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Header
from fastapi.responses import FileResponse

from db import db
from models import (
    IvrFlowCreate,
    IvrFlowUpdate,
    TtsGenerateReq,
    TtsScriptReq,
    FlowFromDescriptionReq,
    InboxAssignFlow,
    InboundCallStart,
)
from pydantic import BaseModel
import tts
import ai

router = APIRouter(prefix="/v1/voice", tags=["voice"])


# ── Auth helper (mirrors tenant_router) ───────────────────────────────────────

_ADMIN_KEY = os.environ.get("ADMIN_KEY") or os.environ.get("SMS_ADMIN_KEY") or "changeme"


def _tenant(
    authorization: str | None = Header(default=None),
    x_admin_key: str | None = Header(default=None, alias="X-Admin-Key"),
    x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
):
    """Dual-mode: phones use Bearer <api_key>; inboxr-cloud server uses X-Admin-Key + X-Tenant-Id."""
    if authorization and authorization.startswith("Bearer "):
        api_key = authorization.removeprefix("Bearer ").strip()
        with db() as conn:
            row = conn.execute(
                "SELECT id, plan FROM tenants WHERE api_key=?", (api_key,)
            ).fetchone()
        if not row:
            raise HTTPException(status_code=401, detail="invalid api key")
        return dict(row)

    if x_admin_key and x_admin_key == _ADMIN_KEY and x_tenant_id:
        with db() as conn:
            row = conn.execute(
                "SELECT id, plan FROM tenants WHERE id=?", (x_tenant_id,)
            ).fetchone()
        if not row:
            # Auto-create a row if missing — inboxr-cloud is canonical for tenant IDs.
            with db() as conn:
                conn.execute(
                    "INSERT OR IGNORE INTO tenants (id, api_key, plan) VALUES (?,?,?)",
                    (x_tenant_id, f"auto_{x_tenant_id}", "starter"),
                )
                row = conn.execute(
                    "SELECT id, plan FROM tenants WHERE id=?", (x_tenant_id,)
                ).fetchone()
        return dict(row) if row else (_ for _ in ()).throw(HTTPException(401, "tenant resolve failed"))

    raise HTTPException(status_code=401, detail="missing auth (Bearer or X-Admin-Key+X-Tenant-Id)")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Flow validation ───────────────────────────────────────────────────────────

VALID_NODE_TYPES = {"play", "transfer", "voicemail", "hangup", "goto"}


def validate_flow(flow: dict) -> list[str]:
    errors: list[str] = []
    if not isinstance(flow, dict):
        return ["flow must be an object"]
    nodes = flow.get("nodes")
    start = flow.get("start_node")
    if not isinstance(nodes, dict) or not nodes:
        return ["flow.nodes must be a non-empty object"]
    if start not in nodes:
        errors.append(f"start_node '{start}' not in nodes")
    for nid, node in nodes.items():
        if not isinstance(node, dict):
            errors.append(f"node '{nid}' must be an object")
            continue
        ntype = node.get("type")
        if ntype not in VALID_NODE_TYPES:
            errors.append(f"node '{nid}' has invalid type '{ntype}'")
            continue
        on_dtmf = node.get("on_dtmf", {}) or {}
        for digit, target in on_dtmf.items():
            if target not in nodes:
                errors.append(f"node '{nid}' on_dtmf['{digit}'] → unknown node '{target}'")
        if ntype == "goto":
            tgt = node.get("node")
            if tgt and tgt not in nodes:
                errors.append(f"goto node '{nid}' → unknown node '{tgt}'")
            ex = node.get("on_exceeded")
            if ex and ex not in nodes:
                errors.append(f"goto node '{nid}' on_exceeded → unknown node '{ex}'")
    return errors


def simulate_flow(flow: dict, digits: list[str]) -> dict:
    """Walk the flow given a sequence of DTMF digits. No audio rendering."""
    nodes = flow["nodes"]
    cur = flow["start_node"]
    path = [{"node": cur, "event": "enter"}]
    loops: dict[str, int] = {}

    def step_to(nid: str):
        nonlocal cur
        cur = nid
        path.append({"node": nid, "event": "enter"})

    for d in digits:
        node = nodes.get(cur, {})
        ntype = node.get("type")
        if ntype != "play":
            break
        target = (node.get("on_dtmf") or {}).get(d)
        path.append({"node": cur, "event": f"dtmf:{d}"})
        if not target:
            path.append({"node": cur, "event": "ignored"})
            continue
        # walk through goto/terminal nodes
        while True:
            tnode = nodes.get(target, {})
            ttype = tnode.get("type")
            if ttype == "goto":
                count = loops.get(target, 0) + 1
                loops[target] = count
                if count > tnode.get("max_loops", 3):
                    target = tnode.get("on_exceeded")
                    if not target:
                        break
                    continue
                target = tnode.get("node")
                continue
            step_to(target)
            if ttype in ("hangup", "transfer", "voicemail"):
                path.append({"node": target, "event": ttype})
                return {"path": path, "outcome": ttype, "final_node": target}
            break

    # ran out of digits
    node = nodes.get(cur, {})
    return {"path": path, "outcome": node.get("type", "unknown"), "final_node": cur}


# ── Flow CRUD ────────────────────────────────────────────────────────────────

@router.post("/ivr-flows")
def flow_create(body: IvrFlowCreate, tenant=Depends(_tenant)):
    try:
        flow = json.loads(body.flow_json)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="flow_json is not valid JSON")
    errs = validate_flow(flow)
    if errs:
        raise HTTPException(status_code=400, detail={"validation_errors": errs})
    flow_id = str(uuid.uuid4())
    with db() as conn:
        conn.execute(
            "INSERT INTO ivr_flows (id, tenant_id, name, flow_json, updated_at) VALUES (?,?,?,?,?)",
            (flow_id, tenant["id"], body.name, body.flow_json, _now()),
        )
    return {"id": flow_id}


@router.get("/ivr-flows")
def flow_list(tenant=Depends(_tenant)):
    with db() as conn:
        rows = conn.execute(
            "SELECT id,name,active,created_at,updated_at FROM ivr_flows WHERE tenant_id=? ORDER BY COALESCE(updated_at,created_at) DESC",
            (tenant["id"],),
        ).fetchall()
    return {"flows": [dict(r) for r in rows]}


@router.get("/ivr-flows/{flow_id}")
def flow_get(flow_id: str, tenant=Depends(_tenant)):
    with db() as conn:
        row = conn.execute(
            "SELECT id,name,flow_json,active,created_at,updated_at FROM ivr_flows WHERE id=? AND tenant_id=?",
            (flow_id, tenant["id"]),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="flow not found")
    return dict(row)


@router.patch("/ivr-flows/{flow_id}")
def flow_update(flow_id: str, body: IvrFlowUpdate, tenant=Depends(_tenant)):
    sets: list[str] = []
    args: list = []
    if body.name is not None:
        sets.append("name=?"); args.append(body.name)
    if body.flow_json is not None:
        try:
            flow = json.loads(body.flow_json)
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="flow_json is not valid JSON")
        errs = validate_flow(flow)
        if errs:
            raise HTTPException(status_code=400, detail={"validation_errors": errs})
        sets.append("flow_json=?"); args.append(body.flow_json)
    if body.active is not None:
        sets.append("active=?"); args.append(1 if body.active else 0)
    if not sets:
        raise HTTPException(status_code=400, detail="no fields to update")
    sets.append("updated_at=?"); args.append(_now())
    args.extend([flow_id, tenant["id"]])
    with db() as conn:
        cur = conn.execute(
            f"UPDATE ivr_flows SET {', '.join(sets)} WHERE id=? AND tenant_id=?",
            args,
        )
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="flow not found")
    return {"ok": True}


@router.delete("/ivr-flows/{flow_id}")
def flow_delete(flow_id: str, tenant=Depends(_tenant)):
    with db() as conn:
        # detach inboxes that point at this flow
        conn.execute(
            "UPDATE sms_inboxes SET default_ivr_flow_id=NULL WHERE default_ivr_flow_id=? AND tenant_id=?",
            (flow_id, tenant["id"]),
        )
        cur = conn.execute(
            "DELETE FROM ivr_flows WHERE id=? AND tenant_id=?",
            (flow_id, tenant["id"]),
        )
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="flow not found")
    return {"ok": True}


@router.post("/ivr-flows/from-description")
async def flow_from_description(body: FlowFromDescriptionReq, tenant=Depends(_tenant)):
    try:
        flow = await ai.generate_flow_tree(body.name, body.description)
    except ai.AiError as e:
        raise HTTPException(status_code=502, detail=f"ai: {e}")
    errs = validate_flow(flow)
    if errs:
        raise HTTPException(status_code=422, detail={"validation_errors": errs, "flow": flow})
    flow_id = str(uuid.uuid4())
    flow_json = json.dumps(flow)
    with db() as conn:
        conn.execute(
            "INSERT INTO ivr_flows (id, tenant_id, name, flow_json, updated_at) VALUES (?,?,?,?,?)",
            (flow_id, tenant["id"], body.name, flow_json, _now()),
        )
    return {"id": flow_id, "flow": flow}


@router.post("/ivr-flows/{flow_id}/dry-run")
def flow_dry_run(flow_id: str, digits: list[str] | None = None, tenant=Depends(_tenant)):
    digits = digits or []
    with db() as conn:
        row = conn.execute(
            "SELECT flow_json FROM ivr_flows WHERE id=? AND tenant_id=?",
            (flow_id, tenant["id"]),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="flow not found")
    flow = json.loads(row["flow_json"])
    return simulate_flow(flow, digits)


@router.get("/calls")
def calls_list(limit: int = 50, offset: int = 0, direction: str | None = None,
               device_id: str | None = None, tenant=Depends(_tenant)):
    """Paginated call history with optional filters."""
    where = ["c.tenant_id=?"]
    args: list = [tenant["id"]]
    if direction in ("inbound", "outbound"):
        where.append("c.direction=?"); args.append(direction)
    if device_id:
        where.append("c.device_id=?"); args.append(device_id)
    args.extend([limit, offset])
    sql = f"""
        SELECT c.id, c.direction, c.from_number, c.to_number, c.status, c.duration_sec,
               c.ivr_flow_id, c.recording_path IS NOT NULL AS has_recording,
               c.transcript, c.started_at, c.answered_at, c.ended_at,
               f.name AS flow_name
          FROM calls c
     LEFT JOIN ivr_flows f ON f.id = c.ivr_flow_id
         WHERE {' AND '.join(where)}
         ORDER BY c.started_at DESC
         LIMIT ? OFFSET ?
    """
    with db() as conn:
        rows = conn.execute(sql, args).fetchall()
    return {"calls": [dict(r) for r in rows]}


@router.get("/calls/{call_id}")
def call_detail(call_id: str, tenant=Depends(_tenant)):
    with db() as conn:
        row = conn.execute(
            """
            SELECT c.id, c.direction, c.from_number, c.to_number, c.status, c.duration_sec,
                   c.ivr_flow_id, c.recording_path IS NOT NULL AS has_recording,
                   c.transcript, c.started_at, c.answered_at, c.ended_at,
                   f.name AS flow_name
              FROM calls c
         LEFT JOIN ivr_flows f ON f.id = c.ivr_flow_id
             WHERE c.id=? AND c.tenant_id=?
            """,
            (call_id, tenant["id"]),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="call not found")
        traces = conn.execute(
            "SELECT node_id, event, detail, timestamp FROM ivr_call_traces WHERE call_id=? ORDER BY id",
            (call_id,),
        ).fetchall()
    return {"call": dict(row), "traces": [dict(t) for t in traces]}


@router.get("/calls/{call_id}/recording")
def call_recording(call_id: str, tenant=Depends(_tenant)):
    with db() as conn:
        row = conn.execute(
            "SELECT recording_path FROM calls WHERE id=? AND tenant_id=?",
            (call_id, tenant["id"]),
        ).fetchone()
    if not row or not row["recording_path"]:
        raise HTTPException(status_code=404, detail="recording not available")
    p = Path(row["recording_path"])
    if not p.exists():
        raise HTTPException(status_code=404, detail="recording file missing")
    media = "audio/opus" if p.suffix == ".opus" else "audio/mpeg"
    return FileResponse(str(p), media_type=media)


@router.get("/calls/active")
def calls_active(tenant=Depends(_tenant)):
    """List currently-ringing/active calls for live-listen UI."""
    with db() as conn:
        rows = conn.execute(
            """
            SELECT id, device_id, direction, from_number, to_number, status,
                   ivr_flow_id, started_at, answered_at
              FROM calls
             WHERE tenant_id=? AND status IN ('ringing','active')
             ORDER BY started_at DESC
             LIMIT 50
            """,
            (tenant["id"],),
        ).fetchall()
    return {"calls": [dict(r) for r in rows]}


@router.get("/ivr-flows/{flow_id}/traces")
def flow_traces(flow_id: str, limit: int = 100, tenant=Depends(_tenant)):
    with db() as conn:
        # confirm ownership
        own = conn.execute(
            "SELECT 1 FROM ivr_flows WHERE id=? AND tenant_id=?",
            (flow_id, tenant["id"]),
        ).fetchone()
        if not own:
            raise HTTPException(status_code=404, detail="flow not found")
        rows = conn.execute(
            "SELECT call_id,node_id,event,detail,timestamp FROM ivr_call_traces "
            "WHERE flow_id=? AND tenant_id=? ORDER BY id DESC LIMIT ?",
            (flow_id, tenant["id"], limit),
        ).fetchall()
    return {"traces": [dict(r) for r in rows]}


# ── TTS ──────────────────────────────────────────────────────────────────────

@router.get("/tts/voices")
def tts_voices(tenant=Depends(_tenant)):
    with db() as conn:
        rows = conn.execute(
            "SELECT id,label,provider,provider_id,description,is_default FROM tts_voices ORDER BY is_default DESC, label"
        ).fetchall()
    return {"voices": [dict(r) for r in rows]}


@router.post("/tts/generate")
async def tts_generate(body: TtsGenerateReq, tenant=Depends(_tenant)):
    if not body.text.strip():
        raise HTTPException(status_code=400, detail="text is empty")
    res = await tts.synthesize(body.text, body.voice_id)
    if not res.get("url"):
        raise HTTPException(status_code=502, detail="tts synthesis failed")
    return {"url": res["url"], "key": res["key"], "voice": res["voice"], "provider": res["provider"]}


@router.post("/tts/script")
async def tts_script(body: TtsScriptReq, tenant=Depends(_tenant)):
    try:
        text = await ai.generate_voice_script(
            body.description,
            max_words=body.max_words,
            company=body.company,
            tone=body.tone,
        )
    except ai.AiError as e:
        raise HTTPException(status_code=502, detail=f"ai: {e}")
    return {"text": text}


@router.get("/tts/audio/{filename}")
def tts_audio(filename: str):
    # unauthenticated — keys are sha256, not enumerable. Used by browser preview + IVR engine.
    if "/" in filename or ".." in filename or not filename.endswith(".mp3"):
        raise HTTPException(status_code=400, detail="invalid filename")
    path = tts.TTS_CACHE / filename
    if not path.exists():
        raise HTTPException(status_code=404)
    return FileResponse(str(path), media_type="audio/mpeg")


# ── Templates ────────────────────────────────────────────────────────────────

@router.get("/templates")
def templates_list(tenant=Depends(_tenant)):
    with db() as conn:
        rows = conn.execute(
            "SELECT id,name,description,sort_order FROM ivr_flow_templates ORDER BY sort_order, name"
        ).fetchall()
    return {"templates": [dict(r) for r in rows]}


@router.post("/templates/{tpl_id}/clone")
def template_clone(tpl_id: str, name: str | None = None, tenant=Depends(_tenant)):
    with db() as conn:
        row = conn.execute(
            "SELECT name, flow_json FROM ivr_flow_templates WHERE id=?", (tpl_id,)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="template not found")
        flow_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO ivr_flows (id, tenant_id, name, flow_json, updated_at) VALUES (?,?,?,?,?)",
            (flow_id, tenant["id"], name or row["name"], row["flow_json"], _now()),
        )
    return {"id": flow_id}


# ── Inbox assignment ─────────────────────────────────────────────────────────

@router.get("/inboxes")
def inbox_list(tenant=Depends(_tenant)):
    with db() as conn:
        rows = conn.execute(
            """
            SELECT i.id, i.phone_number, i.label, i.default_ivr_flow_id, f.name AS flow_name
              FROM sms_inboxes i
              LEFT JOIN ivr_flows f ON f.id = i.default_ivr_flow_id
             WHERE i.tenant_id=?
             ORDER BY i.created_at DESC
            """,
            (tenant["id"],),
        ).fetchall()
    return {"inboxes": [dict(r) for r in rows]}


@router.put("/inboxes/{inbox_id}/flow")
def inbox_assign_flow(inbox_id: str, body: InboxAssignFlow, tenant=Depends(_tenant)):
    with db() as conn:
        if body.flow_id is not None:
            own = conn.execute(
                "SELECT 1 FROM ivr_flows WHERE id=? AND tenant_id=?",
                (body.flow_id, tenant["id"]),
            ).fetchone()
            if not own:
                raise HTTPException(status_code=404, detail="flow not found")
        cur = conn.execute(
            "UPDATE sms_inboxes SET default_ivr_flow_id=? WHERE id=? AND tenant_id=?",
            (body.flow_id, inbox_id, tenant["id"]),
        )
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="inbox not found")
    return {"ok": True}


@router.post("/inboxes/{inbox_id}/test-call")
def inbox_test_call(inbox_id: str, tenant=Depends(_tenant)):
    """Queue a 'self-dial' command — phone places a call to its own number to exercise the IVR."""
    with db() as conn:
        row = conn.execute(
            "SELECT phone_number, device_id FROM sms_inboxes WHERE id=? AND tenant_id=?",
            (inbox_id, tenant["id"]),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="inbox not found")
        if not row["device_id"]:
            raise HTTPException(status_code=400, detail="inbox has no device assigned")
        new_call_id = str(uuid.uuid4())
        conn.execute(
            """
            INSERT INTO calls (id, tenant_id, device_id, direction, from_number, to_number, status, started_at)
            VALUES (?,?,?,?,?,?, 'pending', ?)
            """,
            (new_call_id, tenant["id"], row["device_id"], "outbound", "test",
             row["phone_number"], _now()),
        )
        conn.execute(
            "INSERT INTO device_commands (device_id, command, payload_json) VALUES (?,?,?)",
            (row["device_id"], "dial", json.dumps({
                "call_id": new_call_id,
                "to_number": row["phone_number"],
                "test": True,
            })),
        )
    return {"ok": True, "call_id": new_call_id}


class InboxUpsert(BaseModel):
    id: str                       # inboxr-cloud inbox id (canonical)
    phone_number: str
    device_id: str | None = None
    label: str | None = None


@router.put("/inboxes/{inbox_id}")
def inbox_upsert(inbox_id: str, body: InboxUpsert, tenant=Depends(_tenant)):
    """Sync an inbox row from inboxr-cloud. Idempotent. Inbox id is canonical."""
    if body.id != inbox_id:
        raise HTTPException(status_code=400, detail="path/body id mismatch")
    with db() as conn:
        conn.execute(
            """
            INSERT INTO sms_inboxes (id, tenant_id, device_id, phone_number, label)
                 VALUES (?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
                tenant_id   = excluded.tenant_id,
                device_id   = excluded.device_id,
                phone_number= excluded.phone_number,
                label       = COALESCE(excluded.label, sms_inboxes.label)
            """,
            (inbox_id, tenant["id"], body.device_id, body.phone_number, body.label),
        )
    return {"ok": True}


# ── Worker / inbound-call endpoints (Bearer auth — phones use the tenant key) ─

@router.post("/incoming")
def incoming(body: InboundCallStart, tenant=Depends(_tenant)):
    """Phone's InCallService announces an inbound ring → server resolves the IVR flow.

    Returns the flow_json (or null) so the audio_ws layer can spin up an IvrSession.
    """
    with db() as conn:
        # Look up inbox by to_number; pick this device's default_ivr_flow_id.
        ibx = conn.execute(
            """
            SELECT i.id AS inbox_id, i.tenant_id, i.default_ivr_flow_id, f.flow_json
              FROM sms_inboxes i
              LEFT JOIN ivr_flows f ON f.id = i.default_ivr_flow_id
             WHERE i.phone_number = ? AND i.device_id = ? AND i.tenant_id = ?
             LIMIT 1
            """,
            (body.to_number, body.device_id, tenant["id"]),
        ).fetchone()

        flow_id = None
        flow_json = None
        tenant_id = tenant["id"]
        if ibx:
            flow_id = ibx["default_ivr_flow_id"]
            flow_json = ibx["flow_json"]

        # Fallback: any system flow named 'system_default'? Otherwise build a minimal voicemail flow.
        if not flow_json:
            flow_json = json.dumps({
                "start_node": "g",
                "nodes": {
                    "g":  {"type": "play", "audio": "tts:This number is unattended. Please leave a message after the beep.",
                           "on_dtmf": {"timeout": "vm"}, "timeout_sec": 3},
                    "vm": {"type": "voicemail", "prompt": "tts:Leave your message now."},
                },
            })
            flow_id = "system_default"

        # Persist call row + initial trace
        conn.execute(
            """
            INSERT OR REPLACE INTO calls
              (id, tenant_id, device_id, direction, from_number, to_number, status, ivr_flow_id, started_at)
            VALUES (?,?,?,?,?,?, 'ringing', ?, ?)
            """,
            (body.call_id, tenant_id, body.device_id, "inbound",
             body.from_number, body.to_number, flow_id, _now()),
        )
        conn.execute(
            "INSERT INTO ivr_call_traces (call_id, flow_id, tenant_id, node_id, event, detail) VALUES (?,?,?,?,?,?)",
            (body.call_id, flow_id or "", tenant_id, "_start_", "incoming",
             json.dumps({"from": body.from_number, "to": body.to_number})),
        )

    return {"flow_id": flow_id, "flow_json": flow_json}


@router.post("/trace")
def trace(call_id: str, flow_id: str, node_id: str, event: str, detail: str | None = None, tenant=Depends(_tenant)):
    """IVR engine logs a transition. Scoped to bearer's tenant."""
    with db() as conn:
        # ensure call belongs to this tenant (or is unbound)
        row = conn.execute("SELECT tenant_id FROM calls WHERE id=?", (call_id,)).fetchone()
        if row and row["tenant_id"] and row["tenant_id"] != tenant["id"]:
            raise HTTPException(status_code=403, detail="cross-tenant trace")
        conn.execute(
            "INSERT INTO ivr_call_traces (call_id, flow_id, tenant_id, node_id, event, detail) VALUES (?,?,?,?,?,?)",
            (call_id, flow_id, tenant["id"], node_id, event, detail),
        )
    return {"ok": True}
