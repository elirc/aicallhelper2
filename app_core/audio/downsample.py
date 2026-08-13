"""Vectorized audio math — numpy only, never per-sample Python loops.

The device callback runs on PortAudio's thread; this module is the "minimal
numpy work" it is allowed to do before posting finished frames to the
asyncio loop. Burning CPU inside the GIL here would starve the pipeline.

Resampling is STATEFUL and per-stream (`StreamingResampler`). Resampling
each device chunk independently — which is what a bare `np.interp` over
`linspace(0, n-1)` does — is wrong in three measurable ways:
  * it pins both endpoints of every chunk, so the output depends on how the
    device happened to slice the audio (measured: up to 2.0 of waveform
    error on a unit-amplitude 1 kHz sine, versus resampling the same signal
    whole);
  * it silently drops samples when the chunk length does not divide evenly
    (measured: 15 985 output samples where 16 000 were owed, per second);
  * decimating 48 kHz to 16 kHz with no low-pass folds everything above
    8 kHz straight back into the speech band (measured: a 10 kHz tone
    arriving at near-full strength around 6 kHz).
All three land on the audio Deepgram transcribes, so they cost accuracy.
"""

from __future__ import annotations

import numpy as np
import numpy.typing as npt

TARGET_RATE = 16_000
# ~128 ms at 16 kHz — the frame size Deepgram receives.
FRAME_SAMPLES = 2048

# Windowed-sinc anti-aliasing filter. 63 taps costs ~1.5% of real time on the
# callback thread and buys ~59 dB of alias suppression; the cut sits below the
# 8 kHz target Nyquist with room for the transition band, and above the band
# that carries speech intelligibility.
FIR_TAPS = 63
CUTOFF_HZ = 7200.0


def design_lowpass(num_taps: int, cutoff_norm: float) -> npt.NDArray[np.float64]:
    """Windowed-sinc FIR. `cutoff_norm` is cutoff / source rate, in (0, 0.5)."""
    n = np.arange(num_taps, dtype=np.float64) - (num_taps - 1) / 2.0
    taps = 2.0 * cutoff_norm * np.sinc(2.0 * cutoff_norm * n)
    taps *= np.hamming(num_taps)
    return taps / taps.sum()  # unity DC gain


class StreamingResampler:
    """Phase-continuous resample to 16 kHz, with anti-aliasing.

    One instance per capture stream. Feeding a signal through it in ANY chunk
    sizes produces byte-identical output to feeding it whole — that property
    is what the per-chunk version lacked.
    """

    def __init__(self, src_rate: int) -> None:
        self._src_rate = src_rate
        self._step = src_rate / TARGET_RATE
        self._pos = 0.0  # fractional read position within the carried buffer
        self._tail: npt.NDArray[np.float64] = np.zeros(0, dtype=np.float64)
        if src_rate > TARGET_RATE:
            self._fir: npt.NDArray[np.float64] | None = design_lowpass(
                FIR_TAPS, CUTOFF_HZ / src_rate
            )
            self._fir_state = np.zeros(FIR_TAPS - 1, dtype=np.float64)
        else:
            # Upsampling (or already 16 kHz) cannot alias.
            self._fir = None
            self._fir_state = np.zeros(0, dtype=np.float64)

    def process(self, mono: npt.NDArray[np.float32]) -> npt.NDArray[np.float32]:
        if mono.size == 0:
            return np.empty(0, dtype=np.float32)
        samples = mono.astype(np.float64)
        if self._fir is not None:
            # Overlap-save: exact, with the filter's memory carried across
            # chunks so no seam appears at a chunk boundary.
            buf = np.concatenate([self._fir_state, samples])
            samples = np.convolve(buf, self._fir, mode="valid")
            self._fir_state = buf[-(self._fir.size - 1) :]
        if self._src_rate == TARGET_RATE:
            return samples.astype(np.float32)

        buf = np.concatenate([self._tail, samples])
        last = buf.size - 1
        if buf.size < 2 or self._pos > last:
            self._tail = buf  # not enough yet to place a sample
            return np.empty(0, dtype=np.float32)
        count = int(np.floor((last - self._pos) / self._step)) + 1
        positions = self._pos + np.arange(count, dtype=np.float64) * self._step
        out: npt.NDArray[np.float64] = np.interp(
            positions, np.arange(buf.size, dtype=np.float64), buf
        )
        # Carry the fraction, not just the leftover samples: this is what
        # keeps consecutive chunks on one continuous time base.
        next_pos = self._pos + count * self._step
        keep_from = min(int(np.floor(next_pos)), buf.size)
        self._tail = buf[keep_from:]
        self._pos = next_pos - keep_from
        return out.astype(np.float32)


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


def resample_to_16k(
    mono: npt.NDArray[np.float32], src_rate: int
) -> npt.NDArray[np.float32]:
    """One-shot resample of a complete signal. Streaming callers must hold a
    `StreamingResampler` instead, so state carries across chunks."""
    return StreamingResampler(src_rate).process(mono)


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
    raw: bytes,
    *,
    src_rate: int,
    channels: int,
    resampler: StreamingResampler | None = None,
) -> npt.NDArray[np.int16]:
    """One device-callback chunk -> 16 kHz mono i16 samples.

    Pass the stream's `resampler` so phase carries across chunks; omitting it
    resamples the chunk standalone, which is only correct for a whole signal.
    """
    mono = to_mono(interleaved_i16_to_float(raw, channels))
    stream = resampler if resampler is not None else StreamingResampler(src_rate)
    return float_to_i16(stream.process(mono))
