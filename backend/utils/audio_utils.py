"""
Audio utilities for format conversion between browser and Gemini Live API.

Gemini Live API expects:
- Input: 16-bit PCM, 16kHz, mono, little-endian
- Output: 16-bit PCM, 24kHz, mono, little-endian

Browser typically sends:
- 8kHz sample rate PCM (for web audio)
"""

import audioop
import struct
from typing import Tuple


# Audio conversion states for continuous streaming (reduces glitches)
_upsample_state = None
_downsample_state = None


def resample_pcm(data: bytes, from_rate: int, to_rate: int, sample_width: int = 2) -> bytes:
    """
    Resample PCM audio data from one sample rate to another.
    
    Args:
        data: Raw PCM audio bytes (16-bit, little-endian)
        from_rate: Source sample rate (e.g., 8000)
        to_rate: Target sample rate (e.g., 16000)
        sample_width: Bytes per sample (2 for 16-bit)
    
    Returns:
        Resampled PCM audio bytes
    """
    if from_rate == to_rate:
        return data
    
    if len(data) == 0:
        return data
    
    # Use audioop.ratecv for resampling
    # Parameters: (data, width, nchannels, inrate, outrate, state)
    converted, _ = audioop.ratecv(data, sample_width, 1, from_rate, to_rate, None)
    return converted


def convert_browser_to_gemini(pcm_data: bytes, input_rate: int = 16000) -> bytes:
    """
    Convert browser PCM audio to Gemini format (16kHz).
    
    Args:
        pcm_data: 16-bit PCM audio at input_rate sample rate
        input_rate: Sample rate of input audio (16000 or 8000)
    
    Returns:
        16-bit PCM audio at 16kHz sample rate
    """
    global _upsample_state
    
    if len(pcm_data) == 0:
        return pcm_data
    
    # If already at 16kHz (Gemini's native input rate), passthrough with zero conversion
    if input_rate == 16000:
        return pcm_data
    
    # Upsample 8kHz -> 16kHz (factor of 2) using state for continuity
    converted, _upsample_state = audioop.ratecv(
        pcm_data, 2, 1, input_rate, 16000, _upsample_state
    )
    return converted


def convert_gemini_to_browser(pcm_24khz: bytes) -> bytes:
    """
    Convert Gemini output audio (24kHz) to browser format (8kHz).
    
    Args:
        pcm_24khz: 16-bit PCM audio at 24kHz sample rate
    
    Returns:
        16-bit PCM audio at 8kHz sample rate
    """
    global _downsample_state
    
    if len(pcm_24khz) == 0:
        return pcm_24khz
    
    # Downsample 24kHz -> 8kHz (factor of 3) using state for continuity
    converted, _downsample_state = audioop.ratecv(
        pcm_24khz, 2, 1, 24000, 8000, _downsample_state
    )
    return converted


def reset_audio_states():
    """Reset the audio conversion states for a new session."""
    global _upsample_state, _downsample_state
    _upsample_state = None
    _downsample_state = None


def convert_mulaw_to_pcm(mulaw_data: bytes) -> bytes:
    """
    Convert G.711 mu-law to linear PCM.
    
    Args:
        mulaw_data: G.711 mu-law encoded audio
    
    Returns:
        16-bit linear PCM audio
    """
    return audioop.ulaw2lin(mulaw_data, 2)


def convert_pcm_to_mulaw(pcm_data: bytes) -> bytes:
    """
    Convert linear PCM to G.711 mu-law.
    
    Args:
        pcm_data: 16-bit linear PCM audio
    
    Returns:
        G.711 mu-law encoded audio
    """
    return audioop.lin2ulaw(pcm_data, 2)


def get_audio_duration_ms(pcm_data: bytes, sample_rate: int, sample_width: int = 2) -> float:
    """
    Calculate the duration of PCM audio in milliseconds.
    
    Args:
        pcm_data: Raw PCM audio bytes
        sample_rate: Sample rate in Hz
        sample_width: Bytes per sample
    
    Returns:
        Duration in milliseconds
    """
    num_samples = len(pcm_data) // sample_width
    return (num_samples / sample_rate) * 1000


def generate_silence_pcm(duration_ms: float, sample_rate: int = 8000) -> bytes:
    """
    Generate silent PCM audio.
    
    Args:
        duration_ms: Duration in milliseconds
        sample_rate: Sample rate in Hz
    
    Returns:
        Silent 16-bit PCM audio
    """
    num_samples = int((duration_ms / 1000) * sample_rate)
    return b'\x00\x00' * num_samples

