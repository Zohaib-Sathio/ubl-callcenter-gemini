"""Pre-rendered farewell audio for the speaker-change handoff.

The message must always reach the customer, even if the Gemini Live session
has stopped emitting audio (blocked, rate-limited, mid-tool-call, etc.). We
synthesize the fixed line once at startup via OpenAI TTS and cache the raw
PCM. `_handle_speaker_mismatch` then streams the cached bytes straight to the
browser over the existing `media` event contract.
"""

from __future__ import annotations

import asyncio
import os
from typing import Optional

from openai import AsyncOpenAI

FAREWELL_TEXT = (
    "We have detected second person with you, thats why we are redirecting "
    "you to human agent"
)

FAREWELL_SAMPLE_RATE = 24000  # matches the browser's agent-audio AudioContext
FAREWELL_VOICE = "alloy"
FAREWELL_MODEL = "gpt-4o-mini-tts"

_pcm_cache: Optional[bytes] = None
_lock = asyncio.Lock()


async def _render_pcm() -> bytes:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not set — cannot pre-render farewell audio")
    client = AsyncOpenAI(api_key=api_key)
    buf = bytearray()
    async with client.audio.speech.with_streaming_response.create(
        model=FAREWELL_MODEL,
        voice=FAREWELL_VOICE,
        input=FAREWELL_TEXT,
        response_format="pcm",
    ) as response:
        async for chunk in response.iter_bytes():
            buf.extend(chunk)
    if not buf:
        raise RuntimeError("OpenAI TTS returned empty PCM for farewell")
    return bytes(buf)


async def prewarm_farewell_audio() -> None:
    """Render the farewell line once at startup and stash the PCM in memory."""
    global _pcm_cache
    if _pcm_cache is not None:
        return
    async with _lock:
        if _pcm_cache is not None:
            return
        try:
            print("🗣️ [SECURITY] Pre-rendering farewell audio via OpenAI TTS...")
            _pcm_cache = await _render_pcm()
            duration_s = len(_pcm_cache) / (FAREWELL_SAMPLE_RATE * 2)
            print(
                f"✅ [SECURITY] Farewell PCM cached "
                f"({len(_pcm_cache)} bytes, ~{duration_s:.2f}s)"
            )
        except Exception as e:
            print(f"❌ [SECURITY] Failed to pre-render farewell audio: {e}")


async def get_farewell_pcm() -> Optional[bytes]:
    """Return the cached farewell PCM. Lazily renders if startup render failed."""
    if _pcm_cache is not None:
        return _pcm_cache
    try:
        await prewarm_farewell_audio()
    except Exception as e:
        print(f"❌ [SECURITY] Lazy farewell render failed: {e}")
    return _pcm_cache
