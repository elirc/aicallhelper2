"""Deepgram live-transcription WebSocket client.

Wire behavior pinned by the spec:
- auth via WebSocket subprotocol ["token", <api key>];
- binary frames of raw little-endian i16 PCM, 16 kHz mono;
- KeepAlive every 8 s while open (Deepgram kills idle sockets ~10 s after
  the last audio — NET-0001 — and silence during a call is normal), stopped
  the MOMENT a close is requested: a KeepAlive after CloseStream can error
  on the CLOSING socket and fabricate a "lost connection" during a stop
  that is succeeding;
- finalize = CloseStream, wait for the close (or the 5 s cap), return the
  full transcript; idempotent (a second call joins the first); a stream
  that never opened or already died finalizes immediately;
- frames captured before the socket opens buffer in order (cap ~15 s, drop
  oldest) and flush the instant it opens;
- Deepgram rejects bad keys by CLOSING the socket, often without an error
  frame — a close before any Results frame surfaces as a connect failure
  with the code+reason detail.

The error handler is registered at construction, so an early stream death
always has somewhere to go — the v2 "error before handler registration was
dropped" hazard cannot occur by construction (rule 6). Exactly one error is
ever reported, and never after abort.

Deliberately NOT tuned: `endpointing`/`no_delay` — this client never waits
on the endpointer, so those knobs only cost smart_format quality.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections import deque
from typing import cast

import websockets
from websockets.asyncio.client import ClientConnection
from websockets.exceptions import ConnectionClosed, ConnectionClosedError
from websockets.typing import Subprotocol

from app_core.errors import AppError
from app_core.session.machine import OnSttError, OnSttUpdate
from app_core.stt.frames import (
    SttErrorDetail,
    TranscriptAccumulator,
    TranscriptSegment,
    parse_frame,
)

DEEPGRAM_URL = (
    "wss://api.deepgram.com/v1/listen"
    "?model=nova-3&encoding=linear16&sample_rate=16000&channels=1"
    "&interim_results=true&smart_format=true"
)
CONNECT_TIMEOUT_S = 5.0
FINALIZE_TIMEOUT_S = 5.0
KEEPALIVE_INTERVAL_S = 8.0
# ~15 s of ~128 ms frames buffered while the socket opens; oldest dropped.
PREOPEN_MAX_FRAMES = 120

KEEPALIVE_MSG = '{"type":"KeepAlive"}'
CLOSESTREAM_MSG = '{"type":"CloseStream"}'

# Queue sentinel: CloseStream travels the SAME path as audio so it can never
# overtake frames still queued behind a stalled send (Deepgram discards audio
# that arrives after CloseStream, so overtaking silently truncates the tail).
_CLOSE_SENTINEL = object()


class DeepgramStream:
    def __init__(
        self,
        api_key: str,
        on_update: OnSttUpdate,
        on_error: OnSttError,
        *,
        url: str = DEEPGRAM_URL,
        connect_timeout: float = CONNECT_TIMEOUT_S,
        finalize_timeout: float = FINALIZE_TIMEOUT_S,
        keepalive_interval: float = KEEPALIVE_INTERVAL_S,
    ) -> None:
        self._api_key = api_key
        self._on_update = on_update
        self._on_error = on_error
        self._url = url
        self._connect_timeout = connect_timeout
        self._finalize_timeout = finalize_timeout
        self._keepalive_interval = keepalive_interval

        self._acc = TranscriptAccumulator()
        self._preopen: deque[bytes] = deque(maxlen=PREOPEN_MAX_FRAMES)
        self._queue: asyncio.Queue[bytes | object] = asyncio.Queue()
        self._ws: ClientConnection | None = None
        self._tasks: list[asyncio.Task[None]] = []
        self._closed_evt = asyncio.Event()
        self._closed = False
        self._close_requested = False
        self._aborted = False
        self._error_reported = False
        self._got_results = False
        self._finalize_task: asyncio.Task[str] | None = None
        self._keepalive_task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------ lifecycle

    async def connect(self) -> None:
        subprotocols = [cast(Subprotocol, "token"), cast(Subprotocol, self._api_key)]
        try:
            self._ws = await asyncio.wait_for(
                websockets.connect(self._url, subprotocols=subprotocols, max_size=2**22),
                timeout=self._connect_timeout,
            )
        except TimeoutError as exc:
            raise AppError(
                "stt_connect",
                "Timed out connecting to Deepgram. Check the API key and your network.",
            ) from exc
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise AppError(
                "stt_connect",
                "Could not connect to Deepgram. Check the API key and your network.",
            ) from exc
        loop = asyncio.get_running_loop()
        self._tasks.append(loop.create_task(self._reader()))
        self._tasks.append(loop.create_task(self._sender()))
        self._keepalive_task = loop.create_task(self._keepalive())
        self._tasks.append(self._keepalive_task)
        # Flush frames captured while the socket was opening, in order.
        while self._preopen:
            self._queue.put_nowait(self._preopen.popleft())

    def send(self, pcm: bytes) -> None:
        if self._close_requested or self._aborted or self._closed:
            return
        if self._ws is None:
            self._preopen.append(pcm)  # deque maxlen drops oldest
        else:
            self._queue.put_nowait(pcm)

    async def finalize(self) -> str:
        if self._finalize_task is None:
            self._finalize_task = asyncio.get_running_loop().create_task(self._do_finalize())
        return await self._finalize_task

    async def abort(self) -> None:
        """Silent teardown. The socket death this causes must never be
        reported as an error."""
        self._aborted = True
        self._close_requested = True
        for task in self._tasks:
            if task is not asyncio.current_task():
                task.cancel()
        ws = self._ws
        if ws is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(ws.close(), timeout=2.0)
        self._closed = True
        self._closed_evt.set()

    # ------------------------------------------------------------ internals

    async def _do_finalize(self) -> str:
        self._close_requested = True
        ws = self._ws
        if ws is None or self._closed:
            # Never opened or already died: return what we have immediately
            # rather than burning the timeout.
            return self._acc.text
        if self._keepalive_task is not None:
            self._keepalive_task.cancel()  # nothing may touch a CLOSING socket
        # Behind any frames still in flight, never ahead of them.
        self._queue.put_nowait(_CLOSE_SENTINEL)
        with contextlib.suppress(TimeoutError):
            # The server flushes its held-back tail (smart_format entity
            # hold-back included) then closes; 5 s cap.
            await asyncio.wait_for(self._closed_evt.wait(), self._finalize_timeout)
        # The sender exits after writing CloseStream; if the cap fired first it
        # is still parked on the queue, so cancel it rather than leak a task
        # pinning the socket for the process lifetime.
        for task in self._tasks:
            if task is not asyncio.current_task() and not task.done():
                task.cancel()
        return self._acc.text

    async def _reader(self) -> None:
        ws = self._ws
        assert ws is not None
        error: AppError | None = None
        close_detail = ""
        abnormal_close = False
        try:
            try:
                async for message in ws:
                    if isinstance(message, bytes | bytearray):
                        continue
                    parsed = parse_frame(message)
                    if isinstance(parsed, TranscriptSegment):
                        self._got_results = True
                        text = self._acc.apply(parsed)
                        if not self._aborted:
                            self._on_update(text, parsed.is_final)
                    elif isinstance(parsed, SttErrorDetail):
                        error = AppError(
                            "stt_error", f"Deepgram reported an error: {parsed.detail}"
                        )
                        break
            except ConnectionClosedError as exc:
                abnormal_close = True
                close_detail = _close_detail(exc)
            except ConnectionClosed:
                pass
            except asyncio.CancelledError:
                raise
            except Exception:
                abnormal_close = True
            self._closed = True
            self._classify_close(error, abnormal_close, close_detail)
        finally:
            # Set LAST, after classification. finalize() wakes on this event
            # and cancels the remaining tasks, so setting it earlier would
            # race this task's own error reporting.
            self._closed = True
            self._closed_evt.set()

    def _classify_close(
        self, error: AppError | None, abnormal_close: bool, close_detail: str
    ) -> None:
        if error is not None:
            self._report_error(error)
            return
        if self._close_requested or self._aborted:
            # Expected close during finalize/abort — but an ABNORMAL close
            # mid-finalize is a stream death and must surface (rule 5; the
            # machine ignores it once the transcript is finalized).
            if not abnormal_close or self._aborted:
                return
            if not self._got_results:
                # Never transcribed anything: this is the bad-key rejection
                # (1008 DATA-xxxx), which must keep its connect-failure
                # classification even though a stop was already requested.
                self._report_error(
                    AppError(
                        "stt_connect",
                        f"Deepgram closed the connection{close_detail}. "
                        "Check the API key and your network.",
                    )
                )
                return
            self._report_error(
                AppError(
                    "stt_error",
                    f"Lost the Deepgram connection while finalizing{close_detail}.",
                )
            )
            return
        if not self._got_results:
            # Close before any Results frame = connect-level rejection
            # (Deepgram closes with 1008 DATA-xxxx for bad keys/requests,
            # 1011 NET-xxxx for server faults, often with no error frame).
            self._report_error(
                AppError(
                    "stt_connect",
                    f"Deepgram closed the connection{close_detail}. "
                    "Check the API key and your network.",
                )
            )
        else:
            self._report_error(
                AppError(
                    "stt_error",
                    f"Lost the Deepgram connection mid-recording{close_detail}. "
                    "The transcript would be silently truncated, so this "
                    "recording was stopped.",
                )
            )

    async def _sender(self) -> None:
        ws = self._ws
        assert ws is not None
        while True:
            item = await self._queue.get()
            try:
                if item is _CLOSE_SENTINEL:
                    await ws.send(CLOSESTREAM_MSG)
                    return  # drained: every queued frame was written first
                assert isinstance(item, bytes)
                await ws.send(item)
            except Exception:
                return  # the reader observes and classifies the death

    async def _keepalive(self) -> None:
        ws = self._ws
        assert ws is not None
        while not self._close_requested and not self._closed:
            await asyncio.sleep(self._keepalive_interval)
            # Checked immediately before sending: a KeepAlive after
            # CloseStream errors on the CLOSING socket and fabricates a
            # "lost connection" during a stop that is succeeding.
            if self._close_requested or self._closed:
                return
            try:
                await ws.send(KEEPALIVE_MSG)
            except Exception:
                return

    def _report_error(self, err: AppError) -> None:
        # Exactly one error per stream, and never after abort (rule 6).
        if self._error_reported or self._aborted:
            return
        self._error_reported = True
        self._on_error(err)


def _close_detail(exc: ConnectionClosedError) -> str:
    frame = exc.rcvd or exc.sent
    if frame is None:
        return ""
    reason = f" {frame.reason}" if frame.reason else ""
    return f" (code {frame.code}{reason})"
