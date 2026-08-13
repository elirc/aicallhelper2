"""WASAPI loopback capture of the default render device via PyAudioWPatch.

Capture is a core concern (v2's Electron getDisplayMedia hack is gone). The
device callback runs on PortAudio's thread: it does the minimal numpy work
(downsample + RMS) and posts finished 2048-sample frames to the asyncio
loop via call_soon_threadsafe — no ad-hoc cross-thread state mutation.

PyAudioWPatch is imported lazily so the core (and every test) loads without
an audio stack present.
"""

from __future__ import annotations

import asyncio
import contextlib
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
        self._pending: npt.NDArray[np.int16] = np.empty(0, dtype=np.int16)
        self._resampler: StreamingResampler | None = None

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
            self._pending = np.empty(0, dtype=np.int16)
            # One resampler per stream: it carries filter memory and the
            # fractional read position across device chunks.
            self._resampler = StreamingResampler(src_rate)

            def callback(
                in_data: bytes | None,
                frame_count: int,
                time_info: object,
                status: int,
            ) -> tuple[None, int]:
                if in_data:
                    self._on_device_chunk(in_data, src_rate, channels, on_frame)
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

    def _on_device_chunk(
        self, raw: bytes, src_rate: int, channels: int, on_frame: OnFrame
    ) -> None:
        # PortAudio's thread: numpy-only work, then hand frames to the loop.
        # An exception here would propagate into PortAudio's C callback,
        # which kills the stream with no Python-visible error — the user
        # would just see a recording that captures nothing.
        try:
            samples = downsample_chunk(
                raw, src_rate=src_rate, channels=channels, resampler=self._resampler
            )
            self._pending = np.concatenate([self._pending, samples])
            while self._pending.size >= FRAME_SAMPLES:
                frame = self._pending[:FRAME_SAMPLES]
                self._pending = self._pending[FRAME_SAMPLES:]
                level = rms_level(frame)
                self._loop.call_soon_threadsafe(on_frame, frame.tobytes(), level)
        except Exception:
            # Drop this chunk rather than let it escape; a stream killed here
            # stops delivering with no error anyone can catch.
            self._pending = np.empty(0, dtype=np.int16)

    def stop(self) -> None:
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
        self._pending = np.empty(0, dtype=np.int16)
