"""
IVR state machine.

Flow JSON schema:
{
  "start_node": "greeting",
  "nodes": {
    "greeting": {
      "type": "play",
      "audio": "tts:Welcome. Press 1 for sales.",
      "on_dtmf": {"1": "sales", "timeout": "greeting"},
      "timeout_sec": 10
    },
    "sales": {"type": "transfer", "to": "+61412345678"},
    "voicemail_node": {
      "type": "voicemail",
      "prompt": "tts:Leave a message."
    },
    "hangup_node": {"type": "hangup"},
    "loop_node": {"type": "goto", "node": "greeting", "max_loops": 3, "on_exceeded": "hangup_node"}
  }
}

Node types: play | transfer | voicemail | hangup | goto
"""
import json
import asyncio
import os
from pathlib import Path


TTS_CACHE = Path(os.environ.get("DATA_DIR", "/data")) / "tts_cache"
TTS_CACHE.mkdir(parents=True, exist_ok=True)
SAMPLE_RATE = 8000  # PCM16LE mono output rate


class IvrSession:
    def __init__(
        self,
        call_id: str,
        flow_json: str,
        *,
        flow_id: str = "",
        tenant_id: str | None = None,
        on_action=None,  # callable(event: str, detail: dict) — 'transfer' | 'hangup_async'
    ):
        self.call_id = call_id
        self.flow_id = flow_id
        self.tenant_id = tenant_id
        self.flow: dict = json.loads(flow_json)
        self.nodes: dict = self.flow.get("nodes", {})
        self.current: str = self.flow.get("start_node", "")
        self._loop_counts: dict[str, int] = {}
        self._timeout_task: asyncio.Task | None = None
        self._on_action = on_action

    def _trace(self, node_id: str, event: str, detail: str | None = None) -> None:
        try:
            from db import db
            with db() as conn:
                conn.execute(
                    "INSERT INTO ivr_call_traces (call_id, flow_id, tenant_id, node_id, event, detail) VALUES (?,?,?,?,?,?)",
                    (self.call_id, self.flow_id, self.tenant_id, node_id, event, detail),
                )
        except Exception:
            pass

    async def start(self) -> bytes | None:
        """Called when call is answered. Returns initial audio PCM."""
        self._trace(self.current, "start")
        return await self._enter_node(self.current)

    async def on_dtmf(self, digit: str) -> dict:
        """Called when a DTMF digit is detected. Returns {audio, hangup, voicemail}."""
        if self._timeout_task:
            self._timeout_task.cancel()
            self._timeout_task = None

        self._trace(self.current, "dtmf", digit)
        node = self.nodes.get(self.current, {})
        on_dtmf: dict = node.get("on_dtmf", {})
        next_node = on_dtmf.get(digit) or on_dtmf.get("default")

        if not next_node:
            # unrecognised digit — re-play current node
            self._trace(self.current, "dtmf_ignored", digit)
            audio = await self._enter_node(self.current)
            return {"audio": audio}

        return await self._goto(next_node)

    async def _goto(self, node_name: str) -> dict:
        node = self.nodes.get(node_name, {})
        ntype = node.get("type", "hangup")

        if ntype == "goto":
            count = self._loop_counts.get(node_name, 0) + 1
            self._loop_counts[node_name] = count
            max_loops = node.get("max_loops", 3)
            if count > max_loops:
                exceeded = node.get("on_exceeded", "hangup_node")
                self._trace(node_name, "loop_exceeded", str(count))
                return await self._goto(exceeded)
            target = node.get("node", "")
            self._trace(node_name, "goto", target)
            return await self._goto(target)

        if ntype == "hangup":
            self._trace(node_name, "hangup")
            return {"hangup": True}

        if ntype == "voicemail":
            self._trace(node_name, "voicemail")
            prompt = node.get("prompt", "")
            prompt_pcm = await self._resolve_audio(prompt) if prompt else None
            return {"voicemail": True, "voicemail_prompt": prompt_pcm}

        if ntype == "transfer":
            target = node.get("to")
            self._trace(node_name, "transfer", target)
            if self._on_action:
                try: self._on_action("transfer", {"to": target})
                except Exception: pass
            return {"hangup": True, "transfer_to": target}

        if ntype == "play":
            self.current = node_name
            audio = await self._enter_node(node_name)
            return {"audio": audio}

        self._trace(node_name, "unknown_type", ntype)
        return {"hangup": True}

    async def _enter_node(self, node_name: str) -> bytes | None:
        node = self.nodes.get(node_name, {})
        self.current = node_name
        self._trace(node_name, "enter", node.get("type"))
        audio_src = node.get("audio", "")
        pcm = await self._resolve_audio(audio_src) if audio_src else None

        timeout_sec = node.get("timeout_sec", 10)
        timeout_dest = node.get("on_dtmf", {}).get("timeout", node_name)
        self._timeout_task = asyncio.create_task(
            self._timeout(timeout_sec, timeout_dest)
        )
        return pcm

    async def _timeout(self, sec: float, dest: str):
        await asyncio.sleep(sec)
        await self._goto(dest)

    async def _resolve_audio(self, src: str) -> bytes | None:
        # tts:<text>                  — default voice
        # tts:<voice_id>:<text>       — specific OpenAI voice (alloy/echo/fable/onyx/nova/shimmer)
        # file:/abs/path.pcm          — pre-rendered audio
        # url:<https_mp3_url>         — fetched + transcoded on demand (cached)
        if src.startswith("tts:"):
            payload = src[4:]
            voice = "nova"
            text = payload
            if ":" in payload:
                head, tail = payload.split(":", 1)
                if head in {"alloy", "echo", "fable", "onyx", "nova", "shimmer"}:
                    voice, text = head, tail
            from tts import text_to_pcm
            return await text_to_pcm(text, voice)
        if src.startswith("file:"):
            path = Path(src[5:])
            if path.exists():
                return path.read_bytes()
        return None

    async def cleanup(self):
        if self._timeout_task:
            self._timeout_task.cancel()
