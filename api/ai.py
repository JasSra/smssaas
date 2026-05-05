"""
AI helpers powered by Anthropic.

- generate_voice_script(description): plain-English brief → ~60-word voice script
- generate_flow_tree(name, description): plain-English brief → IVR flow JSON
"""
from __future__ import annotations

import json
import os
import re
from typing import Any

import httpx

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")


class AiError(RuntimeError):
    pass


async def _anthropic(system: str, user: str, max_tokens: int = 1024) -> str:
    if not ANTHROPIC_API_KEY:
        raise AiError("ANTHROPIC_API_KEY not set")
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": ANTHROPIC_MODEL,
                "max_tokens": max_tokens,
                "system": system,
                "messages": [{"role": "user", "content": user}],
            },
        )
        r.raise_for_status()
        data = r.json()
        parts = data.get("content", [])
        for p in parts:
            if p.get("type") == "text":
                return p.get("text", "").strip()
    raise AiError("no text in response")


SCRIPT_SYSTEM = (
    "You write concise, professional voice prompts for phone IVR systems. "
    "Output ONLY the spoken text — no quotes, no stage directions, no preamble. "
    "Avoid abbreviations a TTS engine would mispronounce. "
    "Use plain words and short sentences. "
    "Default length is ~60 words unless told otherwise."
)


async def generate_voice_script(
    description: str,
    *,
    max_words: int = 60,
    company: str | None = None,
    tone: str | None = None,
) -> str:
    parts = [f"Brief: {description.strip()}"]
    if company:
        parts.append(f"Company name: {company}")
    if tone:
        parts.append(f"Tone: {tone}")
    parts.append(f"Target length: {max_words} words. Output the script only.")
    user = "\n".join(parts)
    return await _anthropic(SCRIPT_SYSTEM, user, max_tokens=400)


FLOW_SYSTEM = """You design phone IVR flow trees. Output ONLY a single JSON object — no commentary, no markdown fences.

Schema:
{
  "start_node": "<node_id>",
  "nodes": {
    "<node_id>": <node>
    ...
  }
}

Node types:
- play       : { "type": "play", "audio": "tts:<spoken text>", "on_dtmf": { "<digit>": "<node_id>", "timeout": "<node_id>" }, "timeout_sec": 8 }
- transfer   : { "type": "transfer", "to": "+10000000000" }
- voicemail  : { "type": "voicemail", "prompt": "tts:<spoken text>" }
- hangup     : { "type": "hangup" }
- goto       : { "type": "goto", "node": "<node_id>", "max_loops": 3, "on_exceeded": "<node_id>" }

Rules:
- All node ids referenced in transitions must exist in "nodes".
- Every flow has at least one play node and one hangup node.
- Use natural-sounding spoken text in the "audio" field after the "tts:" prefix.
- Keep each spoken prompt under 60 words.
- For unknown phone numbers (e.g. for transfers when the description does not specify), use "+10000000000" as a placeholder — the user will edit before saving.
"""


async def generate_flow_tree(name: str, description: str) -> dict[str, Any]:
    user = f"Flow name: {name}\nDescription: {description.strip()}\n\nReturn the JSON flow tree only."
    raw = await _anthropic(FLOW_SYSTEM, user, max_tokens=2048)
    # tolerate stray markdown fences
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.MULTILINE).strip()
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError as e:
        raise AiError(f"model did not return JSON: {e}") from e
    if "start_node" not in obj or "nodes" not in obj:
        raise AiError("flow JSON missing start_node or nodes")
    return obj
