"""
WebSocket voice audio handler.

Frame wire format (binary, 4-byte header + payload):
  byte 0 — direction: 0x01 phone→server  0x02 server→phone
  byte 1 — type:      0x01 audio  0x02 dtmf  0x03 hangup  0x04 hold
  byte 2-3 — seq: uint16 big-endian
  bytes 4.. — payload: raw PCM16LE (audio) or ASCII digit (dtmf) or empty
"""
import asyncio
import os
import struct
import uuid
from pathlib import Path
from datetime import datetime, timezone

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from db import db
from ivr import IvrSession

router = APIRouter(tags=["voice-ws"])

RECORDINGS_DIR = Path(os.environ.get("DATA_DIR", "/data")) / "recordings"
VOICEMAIL_DIR  = Path(os.environ.get("DATA_DIR", "/data")) / "voicemail"
RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
VOICEMAIL_DIR.mkdir(parents=True, exist_ok=True)

# dir byte values
DIR_FROM_PHONE  = 0x01
DIR_TO_PHONE    = 0x02

# type byte values
TYPE_AUDIO  = 0x01
TYPE_DTMF   = 0x02
TYPE_HANGUP = 0x03
TYPE_HOLD   = 0x04


def _pack(direction: int, type_: int, seq: int, payload: bytes) -> bytes:
    return struct.pack(">BBH", direction, type_, seq) + payload


def _unpack(data: bytes) -> tuple[int, int, int, bytes]:
    direction, type_, seq = struct.unpack_from(">BBH", data)
    return direction, type_, seq, data[4:]


# ── Per-call pub/sub broker ───────────────────────────────────────────────────
# Each active call gets a CallBroker. The phone's WS feeds inbound audio frames
# into broker.publish(); browser /listen WSes subscribe via broker.subscribe()
# and receive copies. Browser /listen also enqueues DTMF/hangup commands that
# the phone WS reads via broker.next_inject() and forwards to the device.

class CallBroker:
    def __init__(self, call_id: str) -> None:
        self.call_id = call_id
        self._subscribers: set[asyncio.Queue[bytes]] = set()
        self._inject: asyncio.Queue[bytes] = asyncio.Queue(maxsize=64)

    def publish(self, frame: bytes) -> None:
        for q in list(self._subscribers):
            if q.full():
                # listener too slow — drop oldest to keep latency bounded
                try: q.get_nowait()
                except asyncio.QueueEmpty: pass
            try: q.put_nowait(frame)
            except asyncio.QueueFull: pass

    def subscribe(self) -> asyncio.Queue[bytes]:
        q: asyncio.Queue[bytes] = asyncio.Queue(maxsize=200)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[bytes]) -> None:
        self._subscribers.discard(q)

    def inject(self, frame: bytes) -> None:
        try: self._inject.put_nowait(frame)
        except asyncio.QueueFull: pass

    async def next_inject(self, timeout: float) -> bytes | None:
        try:
            return await asyncio.wait_for(self._inject.get(), timeout=timeout)
        except asyncio.TimeoutError:
            return None


_BROKERS: dict[str, CallBroker] = {}


def _get_broker(call_id: str) -> CallBroker:
    b = _BROKERS.get(call_id)
    if b is None:
        b = CallBroker(call_id)
        _BROKERS[call_id] = b
    return b


def _drop_broker(call_id: str) -> None:
    _BROKERS.pop(call_id, None)


def _enqueue_dial(device_id: str, source_call_id: str, to_number: str) -> None:
    """When an IVR transfer node fires, queue a 'dial' device command so the
    phone places an outbound call to the target after the inbound IVR leg
    hangs up. Effectively a blind transfer.

    Payload shape matches Android VoiceWorkerService.handleCommand("dial"):
      { call_id: <new outbound uuid>, to_number: <E.164> }
    """
    if not to_number:
        return
    import json as _json
    new_call_id = str(uuid.uuid4())
    try:
        with db() as conn:
            # Pre-insert a calls row so traces and the active-calls UI reflect
            # the outbound leg even if the worker hasn't yet posted state.
            row = conn.execute("SELECT tenant_id FROM calls WHERE id=?", (source_call_id,)).fetchone()
            tenant_id = row["tenant_id"] if row else None
            conn.execute(
                """
                INSERT OR IGNORE INTO calls
                  (id, tenant_id, device_id, direction, from_number, to_number, status, started_at)
                VALUES (?,?,?,?,?,?, 'pending', ?)
                """,
                (new_call_id, tenant_id, device_id, "outbound", "", to_number,
                 datetime.now(timezone.utc).isoformat()),
            )
            conn.execute(
                "INSERT INTO device_commands (device_id, command, payload_json) VALUES (?,?,?)",
                (device_id, "dial", _json.dumps({
                    "call_id": new_call_id,
                    "to_number": to_number,
                    "source_call_id": source_call_id,
                })),
            )
    except Exception:
        pass


@router.websocket("/ws/voice/{device_id}/{call_id}")
async def voice_ws(websocket: WebSocket, device_id: str, call_id: str):
    await websocket.accept()
    broker = _get_broker(call_id)

    rec_path = RECORDINGS_DIR / f"{call_id}.pcm"
    rec_file = open(rec_path, "wb")

    # Look up IVR flow for this call. If not yet bound, fall back to the
    # inbox-default flow keyed by device's phone number.
    flow_id: str | None = None
    flow_json: str | None = None
    tenant_id: str | None = None
    with db() as conn:
        call_row = conn.execute(
            "SELECT tenant_id, ivr_flow_id, direction FROM calls WHERE id=?",
            (call_id,),
        ).fetchone()
        if call_row:
            tenant_id = call_row["tenant_id"]
            flow_id = call_row["ivr_flow_id"]

        if flow_id:
            r = conn.execute("SELECT flow_json FROM ivr_flows WHERE id=?", (flow_id,)).fetchone()
            if r:
                flow_json = r["flow_json"]
        else:
            # Fallback path: resolve via sms_inboxes by device_id
            r = conn.execute(
                """
                SELECT i.id AS inbox_id, i.tenant_id, i.default_ivr_flow_id, f.flow_json
                  FROM sms_inboxes i
             LEFT JOIN ivr_flows f ON f.id = i.default_ivr_flow_id
                 WHERE i.device_id = ?
                 LIMIT 1
                """,
                (device_id,),
            ).fetchone()
            if r and r["default_ivr_flow_id"] and r["flow_json"]:
                flow_id = r["default_ivr_flow_id"]
                flow_json = r["flow_json"]
                tenant_id = tenant_id or r["tenant_id"]
                # Stamp the call so traces and queries align
                conn.execute(
                    "UPDATE calls SET ivr_flow_id=? WHERE id=?",
                    (flow_id, call_id),
                )

    # System-default fallback if still nothing
    if not flow_json:
        flow_id = flow_id or "system_default"
        flow_json = '{"start_node":"g","nodes":{"g":{"type":"play","audio":"tts:This number is unattended. Please leave a message after the beep.","on_dtmf":{"timeout":"vm"},"timeout_sec":3},"vm":{"type":"voicemail","prompt":"tts:Leave your message now."}}}'

    def _on_ivr_action(event: str, detail: dict) -> None:
        if event == "transfer":
            _enqueue_dial(device_id, call_id, detail.get("to") or "")

    ivr: IvrSession | None = IvrSession(
        call_id, flow_json,
        flow_id=flow_id or "",
        tenant_id=tenant_id,
        on_action=_on_ivr_action,
    )

    seq_out = 0
    pcm_buffer = bytearray()  # buffer for DTMF detection
    DTMF_WINDOW = 160 * 20    # 20ms frames × 20 = 400ms window

    async def send_audio(pcm: bytes):
        nonlocal seq_out
        chunk_size = 320  # 20ms @ 8kHz PCM16
        for i in range(0, len(pcm), chunk_size):
            frame = _pack(DIR_TO_PHONE, TYPE_AUDIO, seq_out, pcm[i:i+chunk_size])
            seq_out += 1
            await websocket.send_bytes(frame)

    async def send_hangup():
        nonlocal seq_out
        await websocket.send_bytes(_pack(DIR_TO_PHONE, TYPE_HANGUP, seq_out, b""))
        seq_out += 1

    # Start IVR greeting
    if ivr:
        greeting_pcm = await ivr.start()
        if greeting_pcm:
            await send_audio(greeting_pcm)

    async def drain_inject() -> None:
        """Pump browser-injected frames (DTMF/hangup) to the phone."""
        while True:
            frame = await broker.next_inject(timeout=30)
            if frame is None:
                continue
            try:
                await websocket.send_bytes(frame)
            except Exception:
                return

    inject_task = asyncio.create_task(drain_inject())

    try:
        while True:
            data = await asyncio.wait_for(websocket.receive_bytes(), timeout=30)
            direction, type_, seq, payload = _unpack(data)

            # Fan out every inbound frame to /listen subscribers (read-only).
            broker.publish(data)

            if type_ == TYPE_AUDIO:
                rec_file.write(payload)
                pcm_buffer.extend(payload)

                # DTMF detection on accumulated buffer
                if len(pcm_buffer) >= DTMF_WINDOW and ivr:
                    digit = _detect_dtmf(bytes(pcm_buffer[-DTMF_WINDOW:]))
                    if digit:
                        response = await ivr.on_dtmf(digit)
                        if response.get("audio"):
                            await send_audio(response["audio"])
                        if response.get("hangup"):
                            await send_hangup()
                            break
                        if response.get("voicemail"):
                            vm_path = await _record_voicemail(
                                websocket, device_id, call_id,
                                call_row["tenant_id"] if call_row else None,
                                response.get("voicemail_prompt"),
                            )
                            await send_hangup()
                            break

            elif type_ == TYPE_DTMF:
                digit = payload.decode("ascii", errors="ignore").strip()
                if digit and ivr:
                    response = await ivr.on_dtmf(digit)
                    if response.get("audio"):
                        await send_audio(response["audio"])
                    if response.get("hangup"):
                        await send_hangup()
                        break

            elif type_ == TYPE_HANGUP:
                break

    except (WebSocketDisconnect, asyncio.TimeoutError):
        pass
    finally:
        inject_task.cancel()
        rec_file.close()
        await _finalize_recording(call_id, rec_path)
        if ivr:
            await ivr.cleanup()
        _drop_broker(call_id)


# ── /listen — read-only audio fanout for browser monitoring ──────────────────
# Auth: short-lived HMAC token in ?t=... that the cloud mints with ADMIN_KEY.
# Token format: hex(hmac_sha256(secret, "{call_id}|{exp_unix}")) + "." + str(exp)

import hmac
import hashlib
import time as _time

ADMIN_KEY = os.environ.get("ADMIN_KEY", "changeme")


def _verify_listen_token(call_id: str, token: str | None) -> bool:
    if not token or "." not in token:
        return False
    sig, _, exp = token.rpartition(".")
    try:
        exp_i = int(exp)
    except ValueError:
        return False
    if exp_i < int(_time.time()):
        return False
    expected = hmac.new(
        ADMIN_KEY.encode("utf-8"),
        f"{call_id}|{exp_i}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(sig, expected)


@router.websocket("/ws/voice/{device_id}/{call_id}/listen")
async def voice_listen(websocket: WebSocket, device_id: str, call_id: str):
    """Browser monitors a live call. Receives every audio frame from the phone WS;
    can also send DTMF (type=0x02) and hangup (type=0x03) frames which are
    forwarded to the device via the broker's inject queue.
    """
    token = websocket.query_params.get("t")
    if not _verify_listen_token(call_id, token):
        await websocket.close(code=1008)  # policy violation
        return
    await websocket.accept()
    broker = _get_broker(call_id)
    q = broker.subscribe()

    async def fanout() -> None:
        try:
            while True:
                frame = await q.get()
                await websocket.send_bytes(frame)
        except Exception:
            return

    fanout_task = asyncio.create_task(fanout())

    try:
        while True:
            data = await asyncio.wait_for(websocket.receive_bytes(), timeout=60)
            if len(data) < 4:
                continue
            _, type_, _, _ = _unpack(data)
            # Only DTMF or hangup are accepted from the browser side.
            if type_ in (TYPE_DTMF, TYPE_HANGUP):
                broker.inject(data)
            if type_ == TYPE_HANGUP:
                break
    except (WebSocketDisconnect, asyncio.TimeoutError):
        pass
    finally:
        fanout_task.cancel()
        broker.unsubscribe(q)


async def _record_voicemail(
    ws: WebSocket,
    device_id: str,
    call_id: str,
    tenant_id: str | None,
    prompt_pcm: bytes | None,
):
    vm_path = VOICEMAIL_DIR / f"{call_id}.pcm"

    if prompt_pcm:
        seq = 0
        chunk = 320
        for i in range(0, len(prompt_pcm), chunk):
            frame = _pack(DIR_TO_PHONE, TYPE_AUDIO, seq, prompt_pcm[i:i+chunk])
            seq += 1
            await ws.send_bytes(frame)

    vm_file = open(vm_path, "wb")
    try:
        while True:
            data = await asyncio.wait_for(ws.receive_bytes(), timeout=60)
            _, type_, _, payload = _unpack(data)
            if type_ == TYPE_AUDIO:
                vm_file.write(payload)
            elif type_ == TYPE_HANGUP:
                break
    except (WebSocketDisconnect, asyncio.TimeoutError):
        pass
    finally:
        vm_file.close()

    # store voicemail record + kick off async transcription
    from_number = ""
    with db() as conn:
        call_row = conn.execute(
            "SELECT from_number FROM calls WHERE id=?", (call_id,)
        ).fetchone()
        if call_row:
            from_number = call_row["from_number"]
        conn.execute(
            """
            INSERT INTO voicemails
              (call_id, tenant_id, device_id, from_number, audio_path)
            VALUES (?,?,?,?,?)
            """,
            (call_id, tenant_id, device_id, from_number, str(vm_path)),
        )
        vm_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    asyncio.create_task(_transcribe_voicemail(vm_id, str(vm_path)))
    return str(vm_path)


async def _transcribe_voicemail(vm_id: int, pcm_path: str):
    """Run Whisper in a thread so we don't block the event loop."""
    loop = asyncio.get_event_loop()
    transcript = await loop.run_in_executor(None, _whisper_transcribe, pcm_path)
    if transcript:
        with db() as conn:
            conn.execute(
                "UPDATE voicemails SET transcript=? WHERE id=?",
                (transcript, vm_id),
            )


def _whisper_transcribe(pcm_path: str) -> str:
    try:
        from faster_whisper import WhisperModel
        import wave, os

        wav_path = pcm_path.replace(".pcm", ".wav")
        with open(pcm_path, "rb") as f:
            pcm_data = f.read()
        with wave.open(wav_path, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(8000)
            wf.writeframes(pcm_data)

        model_name = os.environ.get("WHISPER_MODEL", "small")
        model = WhisperModel(model_name, device="cpu", compute_type="int8")
        segments, _ = model.transcribe(wav_path, language="en")
        text = " ".join(seg.text.strip() for seg in segments)
        os.unlink(wav_path)
        return text
    except Exception as e:
        return f"[transcription error: {e}]"


async def _finalize_recording(call_id: str, pcm_path: Path):
    """Convert raw PCM to opus, update the calls table, kick off transcription."""
    if not pcm_path.exists() or pcm_path.stat().st_size == 0:
        pcm_path.unlink(missing_ok=True)
        return

    opus_path = pcm_path.with_suffix(".opus")
    # Keep the PCM around just long enough for Whisper, then drop it.
    pcm_for_transcribe = str(pcm_path)
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y",
        "-f", "s16le", "-ar", "8000", "-ac", "1",
        "-i", str(pcm_path),
        "-c:a", "libopus", "-b:a", "16k",
        str(opus_path),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()

    if opus_path.exists():
        with db() as conn:
            conn.execute(
                "UPDATE calls SET recording_path=? WHERE id=?",
                (str(opus_path), call_id),
            )
        asyncio.create_task(_transcribe_call(call_id, pcm_for_transcribe))
    else:
        pcm_path.unlink(missing_ok=True)


async def _transcribe_call(call_id: str, pcm_path: str):
    """Run Whisper on the call recording, then drop the PCM."""
    loop = asyncio.get_event_loop()
    transcript = await loop.run_in_executor(None, _whisper_transcribe, pcm_path)
    if transcript:
        with db() as conn:
            # Add a transcript column on the fly if older deployments don't have it.
            try:
                conn.execute("ALTER TABLE calls ADD COLUMN transcript TEXT")
            except Exception:
                pass
            conn.execute(
                "UPDATE calls SET transcript=? WHERE id=?",
                (transcript, call_id),
            )
    try:
        Path(pcm_path).unlink(missing_ok=True)
    except Exception:
        pass


# ── Goertzel DTMF detection ───────────────────────────────────────────────────
# Detects standard DTMF digits from 8kHz PCM16LE audio.

import math

_DTMF_FREQS = {
    "1": (697, 1209), "2": (697, 1336), "3": (697, 1477),
    "4": (770, 1209), "5": (770, 1336), "6": (770, 1477),
    "7": (852, 1209), "8": (852, 1336), "9": (852, 1477),
    "*": (941, 1209), "0": (941, 1336), "#": (941, 1477),
}
_SAMPLE_RATE = 8000
_THRESHOLD = 1e7


def _goertzel(samples: list[float], freq: float) -> float:
    n = len(samples)
    k = round(n * freq / _SAMPLE_RATE)
    w = 2 * math.pi * k / n
    coeff = 2 * math.cos(w)
    s_prev, s_prev2 = 0.0, 0.0
    for s in samples:
        s_cur = s + coeff * s_prev - s_prev2
        s_prev2, s_prev = s_prev, s_cur
    return s_prev2**2 + s_prev**2 - coeff * s_prev * s_prev2


def _detect_dtmf(pcm: bytes) -> str | None:
    samples = [
        struct.unpack_from("<h", pcm, i)[0]
        for i in range(0, len(pcm) - 1, 2)
    ]
    best_digit, best_power = None, 0.0
    for digit, (row_f, col_f) in _DTMF_FREQS.items():
        p = min(_goertzel(samples, row_f), _goertzel(samples, col_f))
        if p > _THRESHOLD and p > best_power:
            best_power = p
            best_digit = digit
    return best_digit
