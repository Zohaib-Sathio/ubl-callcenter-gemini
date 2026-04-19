"""Primary-speaker voiceprint consistency check for a live call.

One SpeakerVerifier instance per call. The first ~3 seconds of voiced caller
audio are enrolled as the reference voiceprint; every ~1.5 s of voiced audio
thereafter is compared to it via cosine similarity on resemblyzer embeddings.
A mismatch is only "confirmed" after 2 consecutive windows fall below the
similarity threshold, to absorb single-window noise.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Optional

import numpy as np
from resemblyzer import VoiceEncoder, preprocess_wav

_SAMPLE_WIDTH_BYTES = 2  # int16 PCM
_ENERGY_RMS_MIN = 300.0  # int16 RMS gate to skip silence from enrollment window


@dataclass
class SpeakerCheckResult:
    kind: str  # "enrolled" | "match" | "mismatch"
    similarity: Optional[float]
    mismatch_confirmed: bool


_encoder: Optional[VoiceEncoder] = None
_encoder_lock = asyncio.Lock()


async def get_encoder() -> VoiceEncoder:
    """Return the shared VoiceEncoder, loading it on first call.
    Safe to call repeatedly; the model load (≈1 s on CPU) happens once."""
    global _encoder
    if _encoder is not None:
        return _encoder
    async with _encoder_lock:
        if _encoder is None:
            print("🧠 [SECURITY] Loading resemblyzer VoiceEncoder...")
            _encoder = await asyncio.to_thread(VoiceEncoder)
            print("✅ [SECURITY] VoiceEncoder ready")
    return _encoder


def _sync_embed(encoder: VoiceEncoder, int16_clip: np.ndarray, sample_rate: int) -> np.ndarray:
    float_clip = int16_clip.astype(np.float32) / 32768.0
    processed = preprocess_wav(float_clip, source_sr=sample_rate)
    return encoder.embed_utterance(processed).astype(np.float32)


class SpeakerVerifier:
    def __init__(
        self,
        call_id: Optional[str] = None,
        sample_rate: int = 16000,
        enrollment_seconds: float = 3.0,
        window_seconds: float = 1.5,
        similarity_threshold: float = 0.70,
        consecutive_failures_to_flag: int = 2,
    ):
        self.call_id = call_id
        self.sample_rate = sample_rate
        self.enrollment_samples = int(enrollment_seconds * sample_rate)
        self.window_samples = int(window_seconds * sample_rate)
        self.similarity_threshold = similarity_threshold
        self.consecutive_failures_to_flag = consecutive_failures_to_flag

        self._voiced_buffer = np.empty(0, dtype=np.int16)
        self._reference_embedding: Optional[np.ndarray] = None
        self._enrolled_consumed = 0
        self._consecutive_below = 0
        self._mismatch_confirmed = False
        self._check_in_flight = False
        self._chunks_since_last_log = 0

        self._first_audio_at: Optional[float] = None
        self._enrollment_ms: Optional[float] = None
        self._total_embed_ms = 0.0
        self._embed_count = 0
        self._total_add_audio_ms = 0.0
        self._add_audio_count = 0

    @property
    def mismatch_confirmed(self) -> bool:
        return self._mismatch_confirmed

    def add_audio(self, pcm_bytes: bytes) -> None:
        """Append 16 kHz mono int16 PCM. Silent frames are dropped so the
        enrollment/window thresholds count *voiced* audio only."""
        if self._mismatch_confirmed or not pcm_bytes:
            return
        t0 = time.perf_counter()
        arr = np.frombuffer(pcm_bytes, dtype=np.int16)
        if arr.size == 0:
            return
        rms = float(np.sqrt(np.mean(arr.astype(np.float32) ** 2)))
        if rms < _ENERGY_RMS_MIN:
            return
        if self._first_audio_at is None:
            self._first_audio_at = time.time()
        self._voiced_buffer = np.concatenate([self._voiced_buffer, arr])
        self._total_add_audio_ms += (time.perf_counter() - t0) * 1000.0
        self._add_audio_count += 1

    async def maybe_check(self) -> Optional[SpeakerCheckResult]:
        """Run enrollment or a comparison window if enough new voiced audio
        has accumulated. Returns None if nothing was done."""
        if self._mismatch_confirmed or self._check_in_flight:
            return None

        encoder = await get_encoder()

        if self._reference_embedding is None:
            if self._voiced_buffer.size < self.enrollment_samples:
                return None
            self._check_in_flight = True
            try:
                clip = self._voiced_buffer[: self.enrollment_samples].copy()
                embed_start = time.perf_counter()
                embedding = await asyncio.to_thread(
                    _sync_embed, encoder, clip, self.sample_rate
                )
                embed_ms = (time.perf_counter() - embed_start) * 1000.0
                self._reference_embedding = embedding
                self._enrolled_consumed = self.enrollment_samples
                self._enrollment_ms = embed_ms
                self._total_embed_ms += embed_ms
                self._embed_count += 1
                print(
                    f"🔒 [SECURITY] call={self.call_id} primary speaker enrolled "
                    f"(voiced_samples={self._voiced_buffer.size}, emb_dim={embedding.shape[0]})"
                )
                print(
                    f"⏱️  [SECURITY TIMING] call={self.call_id} enrollment_embed_ms={embed_ms:.1f} "
                    f"clip_samples={clip.size}"
                )
                return SpeakerCheckResult(
                    kind="enrolled", similarity=None, mismatch_confirmed=False
                )
            finally:
                self._check_in_flight = False

        available = self._voiced_buffer.size - self._enrolled_consumed
        if available < self.window_samples:
            return None

        self._check_in_flight = True
        try:
            start = self._enrolled_consumed
            end = start + self.window_samples
            clip = self._voiced_buffer[start:end].copy()
            self._enrolled_consumed = end

            embed_start = time.perf_counter()
            embedding = await asyncio.to_thread(
                _sync_embed, encoder, clip, self.sample_rate
            )
            embed_ms = (time.perf_counter() - embed_start) * 1000.0
            self._total_embed_ms += embed_ms
            self._embed_count += 1

            sim_start = time.perf_counter()
            similarity = float(np.dot(self._reference_embedding, embedding))
            sim_ms = (time.perf_counter() - sim_start) * 1000.0

            avg_embed = self._total_embed_ms / max(self._embed_count, 1)
            avg_add = self._total_add_audio_ms / max(self._add_audio_count, 1)
            print(
                f"⏱️  [SECURITY TIMING] call={self.call_id} window_embed_ms={embed_ms:.1f} "
                f"cosine_ms={sim_ms:.2f} embeds_run={self._embed_count} "
                f"avg_embed_ms={avg_embed:.1f} avg_add_audio_ms={avg_add:.3f} "
                f"total_embed_ms={self._total_embed_ms:.1f}"
            )

            if similarity < self.similarity_threshold:
                self._consecutive_below += 1
                print(
                    f"⚠️  [SECURITY] call={self.call_id} similarity={similarity:.3f} "
                    f"< {self.similarity_threshold} (strike {self._consecutive_below}/"
                    f"{self.consecutive_failures_to_flag})"
                )
                if self._consecutive_below >= self.consecutive_failures_to_flag:
                    self._mismatch_confirmed = True
                    detection_latency_s = (
                        time.time() - self._first_audio_at
                        if self._first_audio_at is not None
                        else float("nan")
                    )
                    print(
                        f"⏱️  [SECURITY TIMING] call={self.call_id} MISMATCH_CONFIRMED "
                        f"detection_latency_s={detection_latency_s:.2f} "
                        f"embeds_run={self._embed_count} "
                        f"total_embed_ms={self._total_embed_ms:.1f} "
                        f"enrollment_ms={self._enrollment_ms or 0.0:.1f}"
                    )
                    return SpeakerCheckResult(
                        kind="mismatch",
                        similarity=similarity,
                        mismatch_confirmed=True,
                    )
                return SpeakerCheckResult(
                    kind="mismatch",
                    similarity=similarity,
                    mismatch_confirmed=False,
                )

            self._consecutive_below = 0
            print(
                f"✅ [SECURITY] call={self.call_id} similarity={similarity:.3f} "
                f"(same speaker)"
            )
            return SpeakerCheckResult(
                kind="match", similarity=similarity, mismatch_confirmed=False
            )
        finally:
            self._check_in_flight = False


async def warm_encoder() -> None:
    """Call once on app startup to pay the model-load *and* first-forward-pass
    cost early. Loading weights alone leaves torch's JIT/CPU kernels uninitialised,
    so the first real embed (caller enrollment) would otherwise take tens of
    seconds. A throwaway embed on a 1-second silent-ish clip forces that
    initialisation to happen before any call starts."""
    encoder = await get_encoder()
    dummy = np.zeros(16000, dtype=np.int16)
    t0 = time.perf_counter()
    await asyncio.to_thread(_sync_embed, encoder, dummy, 16000)
    warmup_ms = (time.perf_counter() - t0) * 1000.0
    print(f"🔥 [SECURITY] VoiceEncoder warmup embed completed in {warmup_ms:.0f}ms")
