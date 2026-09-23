"""WASAPI loopback capture of the default render device via PyAudioWPatch.

Capture is a core concern (v2's Electron getDisplayMedia hack is gone). The
device callback runs on PortAudio's thread: it does the minimal numpy work
(downsample + RMS) and posts finished 2048-sample frames to the asyncio
loop via call_soon_threadsafe — no ad-hoc cross-thread state mutation.

Threads: `start`/`stop`/`stop_and_drain` block on the device, so the
session machine runs them on ONE dedicated audio worker thread (never on
the core loop — R10). The PortAudio callback thread shares `_pending`,
`_resampler` and `_on_frame` with that worker; `_lock` guards all three,
so a stop can never interleave with a half-processed chunk.

Stop has two flavors (R06):
  * `stop()` DISCARDS: cancel/supersede/teardown throw away whatever has not
    been delivered yet.
  * `stop_and_drain()` is the user's Stop. The CAPTURE CUTOFF is the moment
    `stop_stream()` returns: PortAudio has then finished every callback, so
    nothing captured after it exists. Everything captured before it — the
    frames already posted to the loop and the sub-frame `_pending`
    remainder, delivered as one final SHORT frame — reaches `on_frame`, in
    capture order, before this method returns. Because every frame is posted
    with `call_soon_threadsafe` BEFORE this returns, a caller that awaits it
    via `run_in_executor` resumes strictly after those frames ran (the
    executor's completion is itself a later `call_soon_threadsafe`). The
    FIR's ~31-sample group delay (<1 ms at 48 kHz) and the resampler's
    sub-sample carry are not zero-padded out: sub-millisecond, and padding
    would append synthetic samples to the recording.

PyAudioWPatch is imported lazily so the core (and every test) loads without
an audio stack present.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
from collections.abc import Callable
from typing import Any

import numpy as np
import numpy.typing as npt

from app_core.audio.downsample import (
    FRAME_SAMPLES,
    StreamingResampler,
    downsample_chunk,
    rms_level,
)

OnFrame = Callable[[bytes, float], None]


class LoopbackCapture:
    """AudioSource implementation over the default WASAPI loopback device."""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._pa: Any = None
        self._stream: Any = None
        self._lock = threading.Lock()
        self._pending: npt.NDArray[np.int16] = np.empty(0, dtype=np.int16)
        self._resampler: StreamingResampler | None = None
        # The live stream's sink. None = no delivery: a callback that races
        # a stop must not post into whichever session starts next.
        self._on_frame: OnFrame | None = None

    def start(self, on_frame: OnFrame) -> None:
        import pyaudiowpatch as pyaudio

        self.stop()
        self._pa = pyaudio.PyAudio()
        try:
            wasapi = self._pa.get_host_api_info_by_type(pyaudio.paWASAPI)
            default_speakers = self._pa.get_device_info_by_index(
                wasapi["defaultOutputDevice"]
            )
            if not default_speakers.get("isLoopbackDevice"):
                for candidate in self._pa.get_loopback_device_info_generator():
                    if default_speakers["name"] in candidate["name"]:
                        default_speakers = candidate
                        break
                else:
                    raise RuntimeError("No WASAPI loopback device for the default output")
            src_rate = int(default_speakers["defaultSampleRate"])
            channels = int(default_speakers["maxInputChannels"])
            with self._lock:
                self._pending = np.empty(0, dtype=np.int16)
                # One resampler per stream: it carries filter memory and the
                # fractional read position across device chunks.
                self._resampler = StreamingResampler(src_rate)
                self._on_frame = on_frame

            def callback(
                in_data: bytes | None,
                frame_count: int,
                time_info: object,
                status: int,
            ) -> tuple[None, int]:
                if in_data:
                    self._on_device_chunk(in_data, src_rate, channels)
                return (None, pyaudio.paContinue)

            self._stream = self._pa.open(
                format=pyaudio.paInt16,
                channels=channels,
                rate=src_rate,
                input=True,
                input_device_index=int(default_speakers["index"]),
                frames_per_buffer=src_rate // 8,  # ~125 ms device-side chunks
                stream_callback=callback,
            )
        except Exception:
            self.stop()
            raise

    def _on_device_chunk(self, raw: bytes, src_rate: int, channels: int) -> None:
        # PortAudio's thread: numpy-only work, then hand frames to the loop.
        # An exception here would propagate into PortAudio's C callback,
        # which kills the stream with no Python-visible error — the user
        # would just see a recording that captures nothing.
        with self._lock:
            on_frame = self._on_frame
            if on_frame is None:
                return  # stopped: this chunk is past the cutoff
            try:
                samples = downsample_chunk(
                    raw, src_rate=src_rate, channels=channels, resampler=self._resampler
                )
                self._pending = np.concatenate([self._pending, samples])
                while self._pending.size >= FRAME_SAMPLES:
                    frame = self._pending[:FRAME_SAMPLES]
                    self._pending = self._pending[FRAME_SAMPLES:]
                    self._post(on_frame, frame)
            except Exception:
                # Drop this chunk rather than let it escape; a stream killed
                # here stops delivering with no error anyone can catch.
                self._pending = np.empty(0, dtype=np.int16)

    def _post(self, on_frame: OnFrame, frame: npt.NDArray[np.int16]) -> None:
        self._loop.call_soon_threadsafe(on_frame, frame.tobytes(), rms_level(frame))

    def stop(self) -> None:
        """Stop and DISCARD anything not yet delivered (cancel semantics)."""
        with self._lock:
            self._on_frame = None
        self._close_device()
        with self._lock:
            self._pending = np.empty(0, dtype=np.int16)

    def stop_and_drain(self) -> None:
        """Stop at the capture cutoff and deliver every pre-cutoff sample.

        See the module docstring for the ordering guarantee."""
        stream = self._stream
        if stream is not None:
            # Blocks until the in-flight callback (if any) has returned; its
            # frames are already posted. Nothing is captured after this.
            with contextlib.suppress(Exception):
                stream.stop_stream()
        with self._lock:
            on_frame, self._on_frame = self._on_frame, None
            tail, self._pending = self._pending, np.empty(0, dtype=np.int16)
            if on_frame is not None and tail.size:
                with contextlib.suppress(Exception):  # loop closed at shutdown
                    self._post(on_frame, tail)
        self._close_device()

    def _close_device(self) -> None:
        stream, self._stream = self._stream, None
        pa, self._pa = self._pa, None
        if stream is not None:
            with contextlib.suppress(Exception):
                stream.stop_stream()
            with contextlib.suppress(Exception):
                stream.close()
        if pa is not None:
            with contextlib.suppress(Exception):
                pa.terminate()
