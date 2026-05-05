from pydantic import BaseModel, Field
from typing import Optional
from datetime import datetime


# ── Phone registration ───────────────────────────────────────────────────────

class PhoneRegister(BaseModel):
    device_id: str
    phone_number: str
    country_code: str = "AU"
    carrier: Optional[str] = None
    tenant_id: Optional[str] = None


# ── Worker → server ──────────────────────────────────────────────────────────

class ReceiptPost(BaseModel):
    device_id: str
    message_id: int
    result: str                  # SENT | DELIVERED | FAILED
    error_code: Optional[int] = None


class InboundPost(BaseModel):
    device_id: str
    from_number: str
    body: str
    received_at: datetime
    media_json: Optional[str] = None  # JSON string of [{mime, base64}]


class DiagnosticPost(BaseModel):
    device_id: str
    signal_dbm: Optional[int] = None
    battery_pct: Optional[int] = None
    carrier: Optional[str] = None
    network_type: Optional[str] = None
    data_state: Optional[str] = None


class HeartbeatPost(BaseModel):
    device_id: str


class CallStatePost(BaseModel):
    device_id: str
    call_id: str
    status: str                  # ringing | active | completed | missed | failed
    from_number: Optional[str] = None
    to_number: Optional[str] = None
    direction: Optional[str] = None   # inbound | outbound
    duration_sec: Optional[int] = None


# ── Tenant → server ──────────────────────────────────────────────────────────

class SmsSend(BaseModel):
    to: str
    body: str
    from_number: Optional[str] = None
    country_code: str = "AU"
    device_id: Optional[str] = None  # override auto-assign


class VoiceCall(BaseModel):
    to: str
    country_code: str = "AU"
    device_id: Optional[str] = None
    caller_id: Optional[str] = None  # masked CLI for SIP outbound
    ivr_flow_id: Optional[str] = None


class IvrFlowCreate(BaseModel):
    name: str
    flow_json: str               # JSON string of the flow tree


class IvrFlowUpdate(BaseModel):
    name: Optional[str] = None
    flow_json: Optional[str] = None
    active: Optional[bool] = None


class TtsGenerateReq(BaseModel):
    text: str
    voice_id: Optional[str] = None        # 'nova' | 'onyx' | 'alloy' | 'echo' | 'shimmer' | 'fable'


class TtsScriptReq(BaseModel):
    description: str                       # plain-English description
    max_words: int = 60
    company: Optional[str] = None
    tone: Optional[str] = None             # 'professional' | 'friendly' | 'casual' | 'urgent'


class FlowFromDescriptionReq(BaseModel):
    name: str
    description: str                       # plain-English description of the whole flow


class InboxAssignFlow(BaseModel):
    flow_id: Optional[str] = None          # null → unassign


class InboundCallStart(BaseModel):
    device_id: str
    call_id: str
    from_number: str
    to_number: str


# ── Admin ────────────────────────────────────────────────────────────────────

class AdminSmsSend(BaseModel):
    to: str
    body: str
    device_id: Optional[str] = None
    country_code: str = "AU"


class AdminVoiceCall(BaseModel):
    device_id: str
    to_number: str
    ivr_flow_id: Optional[str] = None
    caller_id: Optional[str] = None
    tenant_id: Optional[str] = None       # defaults to phone's tenant if omitted
