"""Vectorized audio math — numpy only, never per-sample Python loops.

The device callback runs on PortAudio's thread; this module is the "minimal
numpy work" it is allowed to do before posting finished frames to the
asyncio loop. Burning CPU inside the GIL here would starve the pipeline.
"""

from __future__ import annotations

import numpy as np
import numpy.typing as npt

TARGET_RATE = 16_000
# ~128 ms at 16 kHz — the frame size Deepgram receives.
FRAME_SAMPLES = 2048


def interleaved_i16_to_float(raw: bytes, channels: int) -> npt.NDArray[np.float32]:
    """Interleaved little-endian i16 -> (n, channels) float32 in [-1, 1]."""
    data = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if channels > 1:
        usable = len(data) - (len(data) % channels)
        data = data[:usable].reshape(-1, channels)
    else:
        data = data.reshape(-1, 1)
    return data


def to_mono(samples: npt.NDArray[np.float32]) -> npt.NDArray[np.float32]:
    if samples.shape[1] == 1:
        return samples[:, 0]
    result: npt.NDArray[np.float32] = samples.mean(axis=1, dtype=np.float32)
    return result


def resample_to_16k(mono: npt.NDArray[np.float32], src_rate: int) -> npt.NDArray[np.float32]:
    """Linear-interpolation resample. Vectorized (np.interp)."""
    if src_rate == TARGET_RATE or mono.size == 0:
        return mono
    out_len = max(1, round(mono.size * TARGET_RATE / src_rate))
    src_positions = np.linspace(0.0, mono.size - 1, num=out_len, dtype=np.float64)
    resampled = np.interp(src_positions, np.arange(mono.size, dtype=np.float64), mono)
    return resampled.astype(np.float32)


def float_to_i16(mono: npt.NDArray[np.float32]) -> npt.NDArray[np.int16]:
    clipped = np.clip(mono, -1.0, 1.0)
    return (clipped * 32767.0).astype(np.int16)


def rms_level(frame: npt.NDArray[np.int16]) -> float:
    """0..1 RMS for the level meter."""
    if frame.size == 0:
        return 0.0
    scaled = frame.astype(np.float64) / 32768.0
    return float(np.sqrt(np.mean(np.square(scaled))))


def downsample_chunk(
    raw: bytes, *, src_rate: int, channels: int
) -> npt.NDArray[np.int16]:
    """One device-callback chunk -> 16 kHz mono i16 samples."""
    return float_to_i16(resample_to_16k(to_mono(interleaved_i16_to_float(raw, channels)), src_rate))
