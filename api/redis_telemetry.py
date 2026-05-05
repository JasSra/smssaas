"""
Redis-backed telemetry + hot indices.

We persist phone diagnostics, online presence, recent SMS/call activity, and
rate-limit buckets in Redis so the inboxr-cloud admin pages can render live
charts without hitting SQLite.

All writes are best-effort — if Redis is unreachable the request still
succeeds, telemetry just goes missing. We never block on it.

Keys:
  phone:{device_id}:diag       Stream — XADD per heartbeat/diagnostic, MAXLEN ~ 20160 (~7d × 30s)
  phone:online                 Sorted set — score = last-seen ms epoch
  phone:meta:{device_id}       Hash — carrier, model, country, phone_number (set once at register)
  recent:sms                   Stream — capped at 1000 entries, fleet-wide
  recent:calls                 Stream — capped at 1000 entries
  rl:tenant:{tenant_id}:{action}  Sorted set — sliding window for rate limits
"""
import os
import json
import time
import logging
from typing import Optional

log = logging.getLogger("redis_telemetry")

REDIS_URL = os.environ.get("REDIS_URL", "")  # e.g. redis://:pass@10.10.0.21:6379/0
_client = None  # set on first use

DIAG_MAXLEN  = 20160  # ~7 days of 30s ticks
RECENT_MAXLEN = 1000


def _get_client():
    """Lazy-init the Redis client. Returns None if disabled or import fails."""
    global _client
    if _client is not None or not REDIS_URL:
        return _client
    try:
        import redis  # type: ignore
        _client = redis.from_url(REDIS_URL, decode_responses=True, socket_connect_timeout=2, socket_timeout=2)
        _client.ping()
        log.info(f"redis_telemetry connected to {REDIS_URL.split('@')[-1]}")
    except Exception as e:
        log.warning(f"redis_telemetry disabled: {e}")
        _client = False  # poison sentinel so we don't retry on every call
    return _client if _client else None


def _safe_call(fn, *args, **kwargs):
    """Run a Redis op; swallow any exception so callers never break."""
    c = _get_client()
    if c is None:
        return None
    try:
        return fn(c, *args, **kwargs)
    except Exception as e:
        log.warning(f"redis op failed: {e}")
        return None


# ── Phone presence + meta ────────────────────────────────────────────────────

def mark_phone_online(device_id: str, *, phone_number: Optional[str] = None,
                      carrier: Optional[str] = None, country: Optional[str] = None,
                      model: Optional[str] = None) -> None:
    def _do(c):
        now_ms = int(time.time() * 1000)
        c.zadd("phone:online", {device_id: now_ms})
        meta = {k: v for k, v in {
            "phone_number": phone_number,
            "carrier": carrier,
            "country": country,
            "model": model,
            "updated_at": str(now_ms),
        }.items() if v is not None}
        if meta:
            c.hset(f"phone:meta:{device_id}", mapping=meta)
    _safe_call(_do)


def mark_phone_offline(device_id: str) -> None:
    _safe_call(lambda c: c.zrem("phone:online", device_id))


# ── Diagnostics stream + numeric metrics ─────────────────────────────────────

def push_diagnostic(device_id: str, *, battery_pct: Optional[int] = None,
                    signal_dbm: Optional[int] = None, network_type: Optional[str] = None,
                    queue_depth: Optional[int] = None) -> None:
    """
    Append one diagnostic tick to the phone's stream. Stream auto-trims to
    ~7 days of 30s ticks. Numeric fields are stored as strings (Redis Streams
    only take strings); read side will parse.
    """
    fields = {k: str(v) for k, v in {
        "battery_pct": battery_pct,
        "signal_dbm": signal_dbm,
        "network_type": network_type,
        "queue_depth": queue_depth,
    }.items() if v is not None}
    if not fields:
        return
    fields["ts"] = str(int(time.time() * 1000))

    def _do(c):
        c.xadd(
            f"phone:{device_id}:diag",
            fields,
            maxlen=DIAG_MAXLEN,
            approximate=True,
        )
    _safe_call(_do)


# ── Recent activity (fleet-wide, capped) ─────────────────────────────────────

def push_recent_sms(*, tenant_id: Optional[str], device_id: str, direction: str,
                    from_number: str, to_number: str, body: str,
                    message_id: Optional[int] = None) -> None:
    body_preview = (body or "")[:80]

    def _do(c):
        c.xadd(
            "recent:sms",
            {
                "tenant_id": tenant_id or "",
                "device_id": device_id,
                "direction": direction,
                "from": from_number,
                "to": to_number,
                "body": body_preview,
                "message_id": str(message_id) if message_id is not None else "",
                "ts": str(int(time.time() * 1000)),
            },
            maxlen=RECENT_MAXLEN,
            approximate=True,
        )
    _safe_call(_do)


def push_recent_call(*, tenant_id: Optional[str], device_id: str, call_id: str,
                     direction: Optional[str], status: str,
                     from_number: str = "", to_number: str = "",
                     duration_sec: Optional[int] = None) -> None:
    def _do(c):
        c.xadd(
            "recent:calls",
            {
                "tenant_id": tenant_id or "",
                "device_id": device_id,
                "call_id": call_id,
                "direction": direction or "",
                "status": status,
                "from": from_number,
                "to": to_number,
                "duration_sec": str(duration_sec) if duration_sec is not None else "",
                "ts": str(int(time.time() * 1000)),
            },
            maxlen=RECENT_MAXLEN,
            approximate=True,
        )
    _safe_call(_do)


# ── Sliding-window rate limit ────────────────────────────────────────────────

def check_rate_limit(*, tenant_id: str, action: str, limit: int,
                     window_ms: int = 1000) -> dict:
    """
    Sliding-window counter. Returns:
      { ok: bool, remaining: int, retry_after_ms: int }
    Never blocks send if Redis is down — returns ok:True silently.
    """
    def _do(c):
        key = f"rl:tenant:{tenant_id}:{action}"
        now_ms = int(time.time() * 1000)
        cutoff = now_ms - window_ms
        pipe = c.pipeline()
        pipe.zremrangebyscore(key, 0, cutoff)
        pipe.zadd(key, {f"{now_ms}-{action}-{c.incr(f'{key}:seq', 1)}": now_ms})
        pipe.zcard(key)
        pipe.expire(key, max(2, window_ms // 1000 + 2))
        _, _, count, _ = pipe.execute()
        ok = count <= limit
        remaining = max(0, limit - count)
        # for retry hint: oldest score still in window
        if not ok:
            oldest = c.zrange(key, 0, 0, withscores=True)
            retry_after = int(oldest[0][1] + window_ms - now_ms) if oldest else window_ms
        else:
            retry_after = 0
        return {"ok": ok, "remaining": remaining, "retry_after_ms": retry_after}

    res = _safe_call(_do)
    if res is None:
        return {"ok": True, "remaining": limit, "retry_after_ms": 0}
    return res
