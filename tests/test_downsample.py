"""Audio math: downsampling, mono mixdown, RMS."""

from __future__ import annotations

import numpy as np

from app_core.audio.downsample import (
    FRAME_SAMPLES,
    downsample_chunk,
    float_to_i16,
    interleaved_i16_to_float,
    resample_to_16k,
    rms_level,
    to_mono,
)


class TestConversion:
    def test_interleaved_stereo_unpacks(self) -> None:
        raw = np.array([1000, -1000, 2000, -2000], dtype="<i2").tobytes()
        out = interleaved_i16_to_float(raw, channels=2)
        assert out.shape == (2, 2)
        assert abs(out[0, 0] - 1000 / 32768) < 1e-6

    def test_stereo_to_mono_averages(self) -> None:
        samples = np.array([[0.5, -0.5], [1.0, 0.0]], dtype=np.float32)
        mono = to_mono(samples)
        assert np.allclose(mono, [0.0, 0.5])

    def test_float_to_i16_clips(self) -> None:
        out = float_to_i16(np.array([2.0, -2.0, 0.0], dtype=np.float32))
        assert out.tolist() == [32767, -32767, 0]


class TestResample:
    def test_48k_to_16k_reduces_by_three(self) -> None:
        src = np.zeros(4800, dtype=np.float32)
        out = resample_to_16k(src, 48_000)
        assert abs(len(out) - 1600) <= 1

    def test_16k_passthrough(self) -> None:
        src = np.ones(100, dtype=np.float32)
        assert resample_to_16k(src, 16_000) is src

    def test_sine_survives_resampling(self) -> None:
        t = np.arange(48_000, dtype=np.float32) / 48_000
        sine = np.sin(2 * np.pi * 440 * t).astype(np.float32)
        out = resample_to_16k(sine, 48_000)
        # 440 Hz is far below Nyquist at 16 kHz; energy must be preserved.
        assert 0.6 < float(np.sqrt(np.mean(out**2))) < 0.8

    def test_empty_input(self) -> None:
        out = resample_to_16k(np.empty(0, dtype=np.float32), 48_000)
        assert out.size == 0


class TestRms:
    def test_silence_is_zero(self) -> None:
        assert rms_level(np.zeros(100, dtype=np.int16)) == 0.0

    def test_full_scale_near_one(self) -> None:
        frame = np.full(100, 32767, dtype=np.int16)
        assert 0.99 < rms_level(frame) <= 1.0

    def test_empty_frame(self) -> None:
        assert rms_level(np.empty(0, dtype=np.int16)) == 0.0


class TestChunkPipeline:
    def test_device_chunk_to_16k_mono_i16(self) -> None:
        # 480 stereo samples at 48 kHz -> ~160 mono samples at 16 kHz.
        t = np.arange(480)
        left = (np.sin(2 * np.pi * 440 * t / 48_000) * 10_000).astype("<i2")
        raw = np.column_stack([left, left]).astype("<i2").tobytes()
        out = downsample_chunk(raw, src_rate=48_000, channels=2)
        assert out.dtype == np.int16
        assert abs(len(out) - 160) <= 1

    def test_frame_samples_constant_matches_spec(self) -> None:
        assert FRAME_SAMPLES == 2048  # ~128 ms at 16 kHz
