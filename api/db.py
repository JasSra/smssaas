import sqlite3
import os
from contextlib import contextmanager

DB_PATH = os.environ.get("DB_PATH", "/data/smssaas.db")


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def db():
    conn = get_conn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS tenants (
    id          TEXT PRIMARY KEY,
    api_key     TEXT UNIQUE NOT NULL,
    plan        TEXT DEFAULT 'starter',
    webhook_url TEXT,
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS phones (
    device_id     TEXT PRIMARY KEY,
    phone_number  TEXT NOT NULL,
    country_code  TEXT NOT NULL DEFAULT 'AU',
    carrier       TEXT,
    tenant_id     TEXT REFERENCES tenants(id),
    status        TEXT DEFAULT 'offline',
    last_seen     DATETIME,
    registered_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS outbound_queue (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id   TEXT NOT NULL REFERENCES tenants(id),
    device_id   TEXT REFERENCES phones(device_id),
    to_number   TEXT NOT NULL,
    body        TEXT NOT NULL,
    media_url   TEXT,
    status      TEXT DEFAULT 'pending',
    retry_count INTEGER DEFAULT 0,
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
    sent_at     DATETIME,
    error_msg   TEXT
);

CREATE TABLE IF NOT EXISTS inbound_messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       TEXT REFERENCES tenants(id),
    device_id       TEXT NOT NULL REFERENCES phones(device_id),
    from_number     TEXT NOT NULL,
    body            TEXT NOT NULL,
    media_json      TEXT,
    received_at     DATETIME NOT NULL,
    webhook_delivered BOOLEAN DEFAULT FALSE
);

CREATE TABLE IF NOT EXISTS delivery_receipts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id  INTEGER NOT NULL REFERENCES outbound_queue(id),
    result      TEXT NOT NULL,
    error_code  INTEGER,
    timestamp   DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS diagnostics (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id    TEXT NOT NULL REFERENCES phones(device_id),
    signal_dbm   INTEGER,
    battery_pct  INTEGER,
    carrier      TEXT,
    network_type TEXT,
    data_state   TEXT,
    timestamp    DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS usage_ledger (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id   TEXT NOT NULL REFERENCES tenants(id),
    message_id  INTEGER REFERENCES outbound_queue(id),
    direction   TEXT NOT NULL,
    credits     REAL NOT NULL,
    billed_at   DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS calls (
    id             TEXT PRIMARY KEY,
    tenant_id      TEXT REFERENCES tenants(id),
    device_id      TEXT REFERENCES phones(device_id),
    direction      TEXT NOT NULL,
    from_number    TEXT NOT NULL,
    to_number      TEXT NOT NULL,
    caller_id      TEXT,
    status         TEXT DEFAULT 'ringing',
    ivr_flow_id    TEXT,
    recording_path TEXT,
    transcript     TEXT,
    duration_sec   INTEGER,
    started_at     DATETIME DEFAULT CURRENT_TIMESTAMP,
    answered_at    DATETIME,
    ended_at       DATETIME
);

CREATE TABLE IF NOT EXISTS voicemails (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    call_id      TEXT REFERENCES calls(id),
    tenant_id    TEXT REFERENCES tenants(id),
    device_id    TEXT REFERENCES phones(device_id),
    from_number  TEXT NOT NULL,
    audio_path   TEXT NOT NULL,
    duration_sec INTEGER,
    transcript   TEXT,
    read         BOOLEAN DEFAULT FALSE,
    created_at   DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS sensor_readings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id       TEXT NOT NULL REFERENCES phones(device_id),
    captured_at     DATETIME NOT NULL,
    -- Audio
    audio_rms       REAL,                -- 0..1 normalized
    audio_db        REAL,                -- ~ -120..0 dB
    -- Battery / thermal
    battery_pct     INTEGER,
    battery_temp_c  REAL,
    battery_charging BOOLEAN,
    -- Ambient
    light_lux       REAL,
    pressure_hpa    REAL,
    -- Motion
    accel_x         REAL,
    accel_y         REAL,
    accel_z         REAL,
    accel_magnitude REAL,                -- sqrt(x^2+y^2+z^2)
    -- Magnetic (door)
    magnet_x        REAL,
    magnet_y        REAL,
    magnet_z        REAL,
    -- Network
    cell_dbm        INTEGER,
    cell_network    TEXT,
    wifi_json       TEXT,                -- JSON [{ssid,bssid,rssi,freq}]
    -- Snapshot
    snapshot_path   TEXT
);

CREATE TABLE IF NOT EXISTS device_commands (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id    TEXT NOT NULL REFERENCES phones(device_id),
    command      TEXT NOT NULL,           -- 'dial' | 'hangup' | future
    payload_json TEXT NOT NULL DEFAULT '{}',
    status       TEXT NOT NULL DEFAULT 'pending',  -- pending | picked | done | failed
    created_at   DATETIME DEFAULT CURRENT_TIMESTAMP,
    picked_at    DATETIME
);

CREATE TABLE IF NOT EXISTS ivr_flows (
    id         TEXT PRIMARY KEY,
    tenant_id  TEXT REFERENCES tenants(id),
    name       TEXT NOT NULL,
    flow_json  TEXT NOT NULL,
    active     BOOLEAN DEFAULT TRUE,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS ivr_call_traces (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    call_id     TEXT NOT NULL,
    flow_id     TEXT NOT NULL,
    tenant_id   TEXT REFERENCES tenants(id),
    node_id     TEXT NOT NULL,
    event       TEXT NOT NULL,
    detail      TEXT,
    timestamp   DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS tts_voices (
    id          TEXT PRIMARY KEY,
    label       TEXT NOT NULL,
    provider    TEXT NOT NULL,
    provider_id TEXT NOT NULL,
    description TEXT,
    is_default  BOOLEAN DEFAULT FALSE
);

CREATE TABLE IF NOT EXISTS ivr_flow_templates (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    description TEXT,
    flow_json   TEXT NOT NULL,
    sort_order  INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS sms_inboxes (
    id                   TEXT PRIMARY KEY,
    tenant_id            TEXT REFERENCES tenants(id),
    device_id            TEXT REFERENCES phones(device_id),
    phone_number         TEXT NOT NULL,
    label                TEXT,
    default_ivr_flow_id  TEXT REFERENCES ivr_flows(id),
    created_at           DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_outbound_status   ON outbound_queue(status, device_id);
CREATE INDEX IF NOT EXISTS idx_outbound_tenant   ON outbound_queue(tenant_id, created_at);
CREATE INDEX IF NOT EXISTS idx_inbound_tenant    ON inbound_messages(tenant_id, received_at);
CREATE INDEX IF NOT EXISTS idx_diagnostics_dev   ON diagnostics(device_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_calls_tenant      ON calls(tenant_id, started_at);
CREATE INDEX IF NOT EXISTS idx_devcmds_pending   ON device_commands(device_id, status, created_at);
CREATE INDEX IF NOT EXISTS idx_sensors_dev_time  ON sensor_readings(device_id, captured_at);
CREATE INDEX IF NOT EXISTS idx_ivr_traces_call   ON ivr_call_traces(call_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_ivr_traces_flow   ON ivr_call_traces(flow_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_inboxes_tenant    ON sms_inboxes(tenant_id);
CREATE INDEX IF NOT EXISTS idx_inboxes_phone     ON sms_inboxes(phone_number);
"""


# OpenAI tts-1 voices: alloy, echo, fable, onyx, nova, shimmer
SEED_VOICES = [
    ("nova",    "Sarah - friendly",        "openai", "nova",    "Warm, conversational",      1),
    ("onyx",    "Marcus - authoritative",  "openai", "onyx",    "Deep, confident",           0),
    ("alloy",   "Linda - corporate",       "openai", "alloy",   "Polished, neutral",         0),
    ("echo",    "Daniel - news anchor",    "openai", "echo",    "Clear, articulate",         0),
    ("shimmer", "Rachel - casual",         "openai", "shimmer", "Relaxed, approachable",     0),
    ("fable",   "Oliver - storyteller",    "openai", "fable",   "Expressive, British",       0),
]

SEED_TEMPLATES = [
    ("tpl_support_hours", "Customer support hours",
     "Press 1 for hours, 2 for emergency, 3 for callback",
     '{"start_node":"greeting","nodes":{"greeting":{"type":"play","audio":"tts:Thanks for calling. Press 1 for current hours, 2 for an emergency, or 3 to request a callback.","on_dtmf":{"1":"hours","2":"emerg","3":"vm","timeout":"greeting"},"timeout_sec":8},"hours":{"type":"play","audio":"tts:We are open Monday to Friday, 9am to 5pm.","on_dtmf":{"timeout":"hangup"},"timeout_sec":4},"emerg":{"type":"transfer","to":"+10000000000"},"vm":{"type":"voicemail","prompt":"tts:Leave your name, number, and reason for the callback."},"hangup":{"type":"hangup"}}}',
     1),
    ("tpl_real_estate", "Real estate inbound",
     "Sales / property mgmt / voicemail",
     '{"start_node":"greeting","nodes":{"greeting":{"type":"play","audio":"tts:Welcome. Press 1 for sales, 2 for property management, 3 to leave a message.","on_dtmf":{"1":"sales","2":"pm","3":"vm","timeout":"greeting"},"timeout_sec":8},"sales":{"type":"transfer","to":"+10000000001"},"pm":{"type":"transfer","to":"+10000000002"},"vm":{"type":"voicemail","prompt":"tts:Leave your message after the beep."}}}',
     2),
    ("tpl_restaurant", "Restaurant takeaway",
     "Order / hours / directions",
     '{"start_node":"greeting","nodes":{"greeting":{"type":"play","audio":"tts:Press 1 to place an order, 2 for hours, 3 for directions.","on_dtmf":{"1":"order","2":"hours","3":"dirs","timeout":"greeting"},"timeout_sec":8},"order":{"type":"transfer","to":"+10000000003"},"hours":{"type":"play","audio":"tts:We are open daily, 11am to 10pm.","on_dtmf":{"timeout":"hangup"},"timeout_sec":4},"dirs":{"type":"play","audio":"tts:We are at 123 Main Street.","on_dtmf":{"timeout":"hangup"},"timeout_sec":4},"hangup":{"type":"hangup"}}}',
     3),
    ("tpl_otp_fallback", "OTP / 2FA fallback",
     "Repeat code or fall back to SMS",
     '{"start_node":"greeting","nodes":{"greeting":{"type":"play","audio":"tts:Your verification code is being read. Press 1 to repeat, 2 to receive via SMS instead.","on_dtmf":{"1":"greeting","2":"sms","timeout":"hangup"},"timeout_sec":10},"sms":{"type":"play","audio":"tts:A text message has been sent.","on_dtmf":{"timeout":"hangup"},"timeout_sec":3},"hangup":{"type":"hangup"}}}',
     4),
    ("tpl_anti_spam", "Anti-spam screening",
     "Caller states reason, then routes",
     '{"start_node":"greeting","nodes":{"greeting":{"type":"play","audio":"tts:Please state your name and reason for calling at the beep, then press hash.","on_dtmf":{"#":"route","timeout":"vm"},"timeout_sec":15},"route":{"type":"transfer","to":"+10000000000"},"vm":{"type":"voicemail","prompt":"tts:Sorry we missed you. Leave a message."}}}',
     5),
]


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r["name"] == column for r in rows)


def _migrate(conn: sqlite3.Connection):
    if not _column_exists(conn, "ivr_flows", "updated_at"):
        conn.execute("ALTER TABLE ivr_flows ADD COLUMN updated_at DATETIME")
    if not _column_exists(conn, "calls", "transcript"):
        conn.execute("ALTER TABLE calls ADD COLUMN transcript TEXT")


def _seed(conn: sqlite3.Connection):
    for vid, label, provider, pid, desc, is_def in SEED_VOICES:
        conn.execute(
            "INSERT OR IGNORE INTO tts_voices (id,label,provider,provider_id,description,is_default) VALUES (?,?,?,?,?,?)",
            (vid, label, provider, pid, desc, is_def),
        )
    for tid, name, desc, flow_json, order in SEED_TEMPLATES:
        conn.execute(
            "INSERT OR IGNORE INTO ivr_flow_templates (id,name,description,flow_json,sort_order) VALUES (?,?,?,?,?)",
            (tid, name, desc, flow_json, order),
        )

    # Backfill: synthesize one sms_inboxes row per phone that doesn't have one yet.
    # Inbox id format 'inbox_<device_id>' is a placeholder; inboxr-cloud upserts the
    # canonical row via PUT /v1/voice/inboxes/{id} on first sync.
    rows = conn.execute(
        """
        SELECT p.device_id, p.phone_number, p.tenant_id
          FROM phones p
         WHERE p.tenant_id IS NOT NULL
           AND NOT EXISTS (SELECT 1 FROM sms_inboxes i WHERE i.device_id = p.device_id)
        """
    ).fetchall()
    for r in rows:
        conn.execute(
            "INSERT OR IGNORE INTO sms_inboxes (id, tenant_id, device_id, phone_number, label) VALUES (?,?,?,?,?)",
            (f"inbox_{r['device_id']}", r["tenant_id"], r["device_id"], r["phone_number"], None),
        )


def init_db():
    with db() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)
        _seed(conn)
