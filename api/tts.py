"""
TTS service.

Primary: OpenAI tts-1 (lifelike, 6 named voices: alloy, echo, fable, onyx, nova, shimmer).
Fallback: local pyttsx3 (robotic) when OPENAI_API_KEY is unset or the call fails.

Cache layout under {DATA_DIR}/tts_cache/:
    {sha256(provider|voice|text)}.mp3   — for browser preview / hosted playback
    {sha256(provider|voice|text)}.pcm   — 8kHz s16le mono, fed straight into the audio_ws path

Public API:
    synthesize(text, voice_id) -> {"key": ..., "mp3_path": ..., "pcm_path": ..., "url": ...}
    text_to_pcm(text, voice_id="nova") -> bytes   (used by ivr.py)
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import subprocess
import tempfile
from pathlib import Path

import httpx

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
TTS_CACHE = DATA_DIR / "tts_cache"
TTS_CACHE.mkdir(parents=True, exist_ok=True)

# Public URL prefix that maps to TTS_CACHE — wired in main.py via StaticFiles.
PUBLIC_URL_PREFIX = os.environ.get("TTS_PUBLIC_URL_PREFIX", "/v1/voice/tts/audio")

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()
OPENAI_TTS_MODEL = os.environ.get("OPENAI_TTS_MODEL", "tts-1")
OPENAI_VOICES = {"alloy", "echo", "fable", "onyx", "nova", "shimmer"}

DEFAULT_VOICE = "nova"


def _key(provider: str, voice: str, text: str) -> str:
    return hashlib.sha256(f"{provider}|{voice}|{text}".encode()).hexdigest()


def _paths(key: str) -> tuple[Path, Path]:
    return TTS_CACHE / f"{key}.mp3", TTS_CACHE / f"{key}.pcm"


def _ffmpeg_to_pcm(src: Path, dst: Path) -> bool:
    try:
        subprocess.run(
            [
                "ffmpeg", "-y",
                "-i", str(src),
                "-f", "s16le", "-ar", "8000", "-ac", "1",
                str(dst),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return dst.exists() and dst.stat().st_size > 0
    except Exception:
        return False


async def _openai_synthesize(text: str, voice: str, mp3_path: Path) -> bool:
    if not OPENAI_API_KEY:
        return False
    if voice not in OPENAI_VOICES:
        voice = DEFAULT_VOICE
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.post(
                "https://api.openai.com/v1/audio/speech",
                headers={
                    "Authorization": f"Bearer {OPENAI_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": OPENAI_TTS_MODEL,
                    "voice": voice,
                    "input": text,
                    "response_format": "mp3",
                },
            )
            r.raise_for_status()
            mp3_path.write_bytes(r.content)
            return mp3_path.stat().st_size > 0
    except Exception:
        return False


def _pyttsx3_render(text: str, mp3_path: Path) -> bool:
    try:
        import pyttsx3
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            wav_path = Path(f.name)
        engine = pyttsx3.init()
        engine.setProperty("rate", 155)
        engine.save_to_file(text, str(wav_path))
        engine.runAndWait()
        ok = subprocess.run(
            ["ffmpeg", "-y", "-i", str(wav_path), "-codec:a", "libmp3lame", "-qscale:a", "4", str(mp3_path)],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        ).returncode == 0
        try: wav_path.unlink()
        except OSError: pass
        return ok and mp3_path.exists()
    except Exception:
        return False


async def synthesize(text: str, voice_id: str | None = None) -> dict:
    """Synthesize text. Returns paths + URL. Cached by (provider, voice, text)."""
    text = (text or "").strip()
    if not text:
        return {"key": "", "mp3_path": None, "pcm_path": None, "url": None}

    voice = voice_id or DEFAULT_VOICE
    provider = "openai" if (OPENAI_API_KEY and voice in OPENAI_VOICES) else "pyttsx3"
    key = _key(provider, voice, text)
    mp3_path, pcm_path = _paths(key)

    if not mp3_path.exists():
        ok = False
        if provider == "openai":
            ok = await _openai_synthesize(text, voice, mp3_path)
        if not ok:
            provider = "pyttsx3"
            key = _key(provider, voice, text)
            mp3_path, pcm_path = _paths(key)
            if not mp3_path.exists():
                loop = asyncio.get_event_loop()
                ok = await loop.run_in_executor(None, _pyttsx3_render, text, mp3_path)
                if not ok:
                    return {"key": "", "mp3_path": None, "pcm_path": None, "url": None}

    if not pcm_path.exists():
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _ffmpeg_to_pcm, mp3_path, pcm_path)

    return {
        "key": key,
        "provider": provider,
        "voice": voice,
        "mp3_path": str(mp3_path),
        "pcm_path": str(pcm_path) if pcm_path.exists() else None,
        "url": f"{PUBLIC_URL_PREFIX}/{key}.mp3",
    }


async def text_to_pcm(text: str, voice_id: str = DEFAULT_VOICE) -> bytes:
    res = await synthesize(text, voice_id)
    p = res.get("pcm_path")
    if not p:
        return b""
    return Path(p).read_bytes()
