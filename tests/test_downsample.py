"""Audio math: downsampling, mono mixdown, RMS."""

from __future__ import annotations

import numpy as np

from app_core.audio.downsample import (
    FRAME_SAMPLES,
    TARGET_RATE,
    StreamingResampler,
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

    def test_16k_passthrough_preserves_samples(self) -> None:
        # Values, not object identity: the resampler is stateful now, so it
        # returns its own buffer even when no rate change is needed.
        src = np.ones(100, dtype=np.float32)
        assert np.allclose(resample_to_16k(src, 16_000), src)

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


class TestStreamingResampler:
    """The device hands us arbitrary chunks. Resampling each one standalone
    made the output depend on that chunking, dropped samples when a chunk did
    not divide evenly, and folded everything above 8 kHz into the speech band
    — all of it on the audio Deepgram transcribes."""

    SRC = 48_000

    @staticmethod
    def tone(freq: float, seconds: float, rate: int) -> np.ndarray:
        t = np.arange(int(rate * seconds), dtype=np.float64) / rate
        return np.sin(2 * np.pi * freq * t).astype(np.float32)

    @staticmethod
    def band_rms(sig: np.ndarray, rate: int, lo: float, hi: float) -> float:
        spec = np.abs(np.fft.rfft(sig * np.hanning(sig.size)))
        freqs = np.fft.rfftfreq(sig.size, 1.0 / rate)
        band = spec[(freqs >= lo) & (freqs <= hi)]
        return float(np.sqrt(np.sum(band**2)) / sig.size)

    def test_output_is_independent_of_how_the_device_chunks_the_audio(self) -> None:
        signal = self.tone(1000, 1.0, self.SRC)
        whole = StreamingResampler(self.SRC).process(signal)
        for size in (480, 1024, 6000, 7777):
            stream = StreamingResampler(self.SRC)
            pieces = [stream.process(signal[i : i + size]) for i in range(0, signal.size, size)]
            chunked = np.concatenate(pieces)
            assert chunked.size == whole.size, size
            assert np.array_equal(chunked, whole), size

    def test_no_samples_are_lost_over_a_long_stream(self) -> None:
        # A chunk length that does not divide evenly used to silently drop
        # ~15 samples per second.
        chunk = 1024
        stream = StreamingResampler(self.SRC)
        signal = self.tone(440, 5.0, self.SRC)
        produced = sum(
            stream.process(signal[i : i + chunk]).size
            for i in range(0, signal.size, chunk)
        )
        expected = signal.size // 3
        assert abs(produced - expected) <= 1, (produced, expected)

    def test_content_above_the_target_nyquist_is_filtered_not_folded(self) -> None:
        # A 10 kHz tone decimated to 16 kHz aliases to 6 kHz — the middle of
        # the speech band — unless it is filtered out first.
        hf = self.tone(10_000, 1.0, self.SRC)
        out = StreamingResampler(self.SRC).process(hf)
        alias = self.band_rms(out, TARGET_RATE, 5500, 6500)
        reference = self.band_rms(self.tone(6000, 1.0, TARGET_RATE), TARGET_RATE, 5500, 6500)
        assert alias < reference / 100, (alias, reference)

    def test_speech_band_content_passes_through_intact(self) -> None:
        for freq in (300, 1000, 3000):
            src = self.tone(freq, 1.0, self.SRC)
            out = StreamingResampler(self.SRC).process(src)
            src_rms = float(np.sqrt(np.mean(src.astype(np.float64) ** 2)))
            out_rms = float(np.sqrt(np.mean(out.astype(np.float64) ** 2)))
            assert abs(out_rms - src_rms) < 0.01, (freq, src_rms, out_rms)

    def test_a_44100_device_also_resamples_without_drift(self) -> None:
        stream = StreamingResampler(44_100)
        signal = self.tone(440, 2.0, 44_100)
        produced = sum(
            stream.process(signal[i : i + 5512]).size
            for i in range(0, signal.size, 5512)
        )
        expected = round(signal.size * TARGET_RATE / 44_100)
        assert abs(produced - expected) <= 2, (produced, expected)

    def test_empty_and_tiny_chunks_are_safe(self) -> None:
        stream = StreamingResampler(self.SRC)
        assert stream.process(np.empty(0, dtype=np.float32)).size == 0
        for _ in range(10):
            stream.process(np.zeros(1, dtype=np.float32))  # must not raise

    def test_cost_stays_negligible_on_the_callback_thread(self) -> None:
        import time

        stream = StreamingResampler(self.SRC)
        chunk = self.tone(440, 0.125, self.SRC)
        stream.process(chunk)  # warm
        started = time.perf_counter()
        for _ in range(20):
            stream.process(chunk)
        per_chunk = (time.perf_counter() - started) / 20
        # This runs inside the GIL on PortAudio's thread; 125 ms of audio must
        # cost a small fraction of 125 ms.
        assert per_chunk < 0.0125, per_chunk

    def test_downsample_chunk_threads_the_resampler_through(self) -> None:
        stream = StreamingResampler(self.SRC)
        mono = self.tone(1000, 0.125, self.SRC)
        raw = (mono * 32767).astype("<i2").tobytes()
        first = downsample_chunk(raw, src_rate=self.SRC, channels=1, resampler=stream)
        second = downsample_chunk(raw, src_rate=self.SRC, channels=1, resampler=stream)
        assert first.dtype == np.int16 and second.dtype == np.int16
        # Same input twice through ONE resampler is NOT identical output: the
        # second call continues the filter/phase state rather than restarting.
        assert not np.array_equal(first, second)
