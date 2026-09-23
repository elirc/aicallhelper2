"""LoopbackCapture without a device: the stop/drain contract (R06).

The PortAudio stream is replaced by a stub; chunks are fed through the same
`_on_device_chunk` entry the device callback uses. Frames are posted with
call_soon_threadsafe, so each test yields to the loop before asserting.
"""

from __future__ import annotations

import asyncio

import numpy as np
import pytest

from app_core.audio.capture import LoopbackCapture
from app_core.audio.downsample import FRAME_SAMPLES, StreamingResampler

RATE = 16_000  # identity resample: sample counts in == sample counts out


def pcm(n: int, value: int = 1000) -> bytes:
    return np.full(n, value, dtype="<i2").tobytes()


class StubStream:
    """Stands in for the PortAudio stream; stop_stream can deliver a final
    in-flight callback, as PortAudio does before it returns."""

    def __init__(self, capture: LoopbackCapture, in_flight: bytes | None = None) -> None:
        self.capture = capture
        self.in_flight = in_flight
        self.calls: list[str] = []

    def stop_stream(self) -> None:
        self.calls.append("stop_stream")
        if self.in_flight is not None:
            chunk, self.in_flight = self.in_flight, None
            self.capture._on_device_chunk(chunk, RATE, 1)

    def close(self) -> None:
        self.calls.append("close")


def armed(loop: asyncio.AbstractEventLoop) -> tuple[LoopbackCapture, list[int]]:
    """A capture wired as start() would leave it, minus the device."""
    cap = LoopbackCapture(loop)
    got: list[int] = []
    cap._resampler = StreamingResampler(RATE)
    cap._on_frame = lambda frame, _rms: got.append(len(frame) // 2)
    return cap, got


async def flush() -> None:
    for _ in range(3):
        await asyncio.sleep(0)


class TestStopAndDrain:
    @pytest.mark.parametrize("n", [1, 100, 1600, FRAME_SAMPLES - 1, FRAME_SAMPLES, 5000])
    async def test_every_captured_sample_is_delivered(self, n: int) -> None:
        # Previously stop() cleared up to 2,047 samples (~128 ms) of the
        # question's last syllable; a recording shorter than one frame
        # delivered nothing at all.
        cap, got = armed(asyncio.get_running_loop())
        cap._on_device_chunk(pcm(n), RATE, 1)
        cap.stop_and_drain()
        await flush()
        assert sum(got) == n
        assert all(size == FRAME_SAMPLES for size in got[:-1])

    async def test_the_in_flight_callback_lands_before_the_tail_and_close(self) -> None:
        # Cutoff = stop_stream() returning. A callback PortAudio finishes
        # inside stop_stream is pre-cutoff audio and must be included.
        cap, got = armed(asyncio.get_running_loop())
        stream = StubStream(cap, in_flight=pcm(3000))
        cap._stream = stream
        cap._on_device_chunk(pcm(100), RATE, 1)
        cap.stop_and_drain()
        await flush()
        assert got == [FRAME_SAMPLES, 3100 - FRAME_SAMPLES]
        assert stream.calls[0] == "stop_stream" and "close" in stream.calls
        assert cap._stream is None

    async def test_nothing_is_delivered_after_the_cutoff(self) -> None:
        cap, got = armed(asyncio.get_running_loop())
        cap._on_device_chunk(pcm(10), RATE, 1)
        cap.stop_and_drain()
        cap._on_device_chunk(pcm(FRAME_SAMPLES * 2), RATE, 1)  # a straggler
        await flush()
        assert got == [10]


class TestStopDiscards:
    async def test_stop_discards_the_remainder(self) -> None:
        # Cancel/supersede semantics are unchanged: nothing more is delivered.
        cap, got = armed(asyncio.get_running_loop())
        cap._on_device_chunk(pcm(FRAME_SAMPLES + 5), RATE, 1)
        cap.stop()
        await flush()
        assert got == [FRAME_SAMPLES]
        assert cap._pending.size == 0

    async def test_a_callback_after_stop_never_reaches_the_old_sink(self) -> None:
        # The next session's start() installs a new sink; a late callback from
        # the stopped stream must not post into either.
        cap, got = armed(asyncio.get_running_loop())
        cap.stop()
        cap._on_device_chunk(pcm(FRAME_SAMPLES), RATE, 1)
        await flush()
        assert got == []

    async def test_a_failed_chunk_is_dropped_without_escaping(self) -> None:
        cap, got = armed(asyncio.get_running_loop())
        cap._resampler = None
        cap._on_device_chunk(b"\x01", 0, 1)  # src_rate 0: resampler blows up
        await flush()
        assert got == []
