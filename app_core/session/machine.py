"""The session state machine — one live question/answer pipeline at a time.

Every invariant here was purchased with a real bug in v2; the numbered
comments reference the spec's §5 rules. All state is touched ONLY on the
core's asyncio loop; everything crossing a thread boundary hands off first.
Dependencies are Protocol-injected so pytest exercises every rule with
fakes — no network, no audio device.

`asyncio.CancelledError` is control flow here, not an error: abort paths
cancel tasks, and handlers must never swallow or remap cancellation.

The Protocols and value types this machine depends on live in
app_core/contracts.py (re-exported here for compatibility) so that the
settings store and the STT client can import them without importing this
module and, through it, httpx.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Callable, Coroutine, Mapping
from concurrent.futures import Executor, ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from app_core.contracts import (
    DEEPGRAM_SECRET_ID,
    AnswerConfig,
    AudioSource,
    EventSink,
    OnSttError,
    OnSttUpdate,
    SettingsReader,
    SttFactory,
    SttStream,
    Warmer,
)
from app_core.errors import AppError, ErrorCode
from app_core.llm.base import AnswerProvider
from app_core.llm.prompt import build_prompt
from app_core.llm.retry import stream_answer

if TYPE_CHECKING:
    import httpx

__all__ = [
    "DEEPGRAM_SECRET_ID",
    "AnswerConfig",
    "AudioSource",
    "EventSink",
    "OnSttError",
    "OnSttUpdate",
    "SessionManager",
    "SessionSnapshot",
    "SettingsReader",
    "SttFactory",
    "SttStream",
    "Timeouts",
    "Warmer",
]


@dataclass(frozen=True)
class Timeouts:
    stt_finalize_s: float = 5.0
    llm_first_token_s: float = 10.0
    llm_total_s: float = 60.0
    record_cap_s: float = 120.0
    # Bound on stopping the device and delivering the pre-Stop tail. A driver
    # that hangs in stop_stream() must not park the session in "Finalizing…".
    audio_drain_s: float = 2.0


Phase = Literal["connecting", "recording", "finalizing", "answering", "done"]


@dataclass(frozen=True)
class SessionSnapshot:
    """Public, immutable copy of the live slot (see `active_snapshot`)."""

    id: str
    kind: Literal["record", "ask"]
    phase: Phase


@dataclass
class _Session:
    id: str
    kind: Literal["record", "ask"]
    phase: Phase = "connecting"
    aborted: bool = False
    errored: bool = False
    released: bool = False
    stop_requested: bool = False
    stt_done: bool = False
    owns_audio: bool = False
    # The device open failed after Stop was already taken; the empty
    # transcript that follows is reported as the device error, not no_speech.
    device_failed: bool = False
    # Rule 4's cutoff: frames are accepted until the Stop drain has delivered
    # every pre-Stop sample, and rejected from then on (see _drain_audio).
    capture_closed: bool = False
    drain: asyncio.Future[None] | None = None
    audio_drain_ms: int = 0
    stop_time: float | None = None
    timeout_code: ErrorCode | None = None
    stt: SttStream | None = None
    tasks: set[asyncio.Task[None]] = field(default_factory=set)
    cap_handle: asyncio.TimerHandle | None = None


def _device_open_error() -> AppError:
    # A missing/vanished loopback device must surface actionably, not as a
    # generic pipeline error.
    return AppError(
        "internal",
        "Could not open the system audio device. Check that a "
        "default output device exists, then try again.",
    )


async def _quiet_abort(stream: SttStream) -> None:
    # Teardown of a dead stream must never surface.
    with contextlib.suppress(Exception):
        await stream.abort()


def _missing_key_error(provider: AnswerProvider) -> AppError:
    return AppError(
        "no_llm_key",
        f"No API key for {provider.display_name}. Open Settings (gear icon) and add it.",
    )


def _unknown_provider_error() -> AppError:
    return AppError("internal", "Unknown answer provider — pick one in Settings.")


class SessionManager:
    def __init__(
        self,
        *,
        settings: SettingsReader,
        providers: Mapping[str, AnswerProvider],
        stt_factory: SttFactory,
        audio: AudioSource,
        http: httpx.AsyncClient,
        warmer: Warmer,
        events: EventSink,
        timeouts: Timeouts | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        audio_executor: Executor | None = None,
    ) -> None:
        self._settings = settings
        self._providers = providers
        self._stt_factory = stt_factory
        self._audio = audio
        self._http = http
        self._warmer = warmer
        self._events = events
        self._timeouts = timeouts or Timeouts()
        self._clock = clock
        self._wall_clock = wall_clock
        # R10: every device call (open, stop, drain, terminate) blocks, so it
        # runs here — ONE worker thread, so start/stop/drain are serialized in
        # submission order (a new session's start can never overtake the old
        # session's stop) and the core loop never blocks on the device.
        self._audio_executor: Executor = audio_executor or ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="aica-audio"
        )
        self._active: _Session | None = None
        self._counter = 0
        # Rule 2 claim ticket. The slot itself can only be claimed after the
        # key reads (they must fail the COMMAND, not a session), so the claim
        # of "newest" is this counter, taken synchronously at command entry —
        # before any await. Without it a command whose DPAPI read stalls could
        # resume and supersede a command the user issued LATER.
        self._command_seq = 0

    # ------------------------------------------------------------- commands

    async def start_session(self) -> str:
        ticket = self._claim_ticket()
        stt_key = await asyncio.to_thread(self._settings.get_secret, DEEPGRAM_SECRET_ID)
        if not stt_key:
            raise AppError(
                "no_stt_key",
                "No Deepgram API key saved. Open Settings (gear icon) and add it.",
            )
        provider = self._active_provider()
        llm_key = await asyncio.to_thread(self._settings.get_secret, provider.id)
        if not llm_key:
            raise _missing_key_error(provider)
        # Rule 1 (supersession) + rule 2 (latest-start-wins): the ticket was
        # claimed before the awaits above; if a newer command took one since,
        # this start lost and must not install itself over the winner.
        self._require_ticket(ticket)
        self._supersede()
        session = self._new_session("record")
        self._active = session
        self._warmer.warm(provider.origin)
        self._spawn(session, self._run_record(session, stt_key))
        return session.id

    async def stop_session(self, session_id: str) -> None:
        """Rule 3 — the stop contract: raises "not taken" for anything that
        isn't the live, not-yet-stopping record session. Every other outcome
        arrives as an event; this error return is the only way the UI learns
        its stop went nowhere.

        A stop that lands while the STT socket is still CONNECTING is taken,
        not refused: the audio captured so far is buffered inside the stream
        and the finalize is deferred until the socket opens. Refusing it left
        the UI parked in "Finalizing…" for 20 s and threw the recording away.
        """
        session = self._active
        if (
            session is None
            or session.id != session_id
            or session.aborted
            or session.kind != "record"
            or session.phase not in ("connecting", "recording")
            or session.stop_requested
        ):
            raise AppError("internal", "Stop not taken — that session is not recording.")
        self._begin_stop(session)

    async def ask(self, text: str) -> str:
        # Rule 8: validate BEFORE superseding — garbage input must not kill a
        # live session.
        trimmed = text.strip()
        if not trimmed:
            raise AppError("internal", "Type a question first.")
        if len(trimmed) > 8000:
            raise AppError("internal", "Question is too long (max 8000 characters).")
        ticket = self._claim_ticket()  # after validation (rule 8), before any await
        provider = self._active_provider()
        llm_key = await asyncio.to_thread(self._settings.get_secret, provider.id)
        if not llm_key:
            raise _missing_key_error(provider)
        self._require_ticket(ticket)
        self._supersede()
        session = self._new_session("ask")
        session.phase = "answering"
        session.stt_done = True
        session.stop_time = self._clock()  # the metrics clock starts at ask-accept
        self._active = session
        self._warmer.warm(provider.origin)
        # The id resolves to the caller before any event fires: events start
        # inside the spawned task, which runs after this coroutine returns.
        self._spawn(session, self._run_ask(session, trimmed))
        return session.id

    def cancel_session(self, session_id: str) -> None:
        """Rule 10 — cancel is silent: no done, no error, work aborted, slot
        released. Invalid ids do nothing (never an error)."""
        session = self._active
        if session is None or session.id != session_id or session.aborted:
            return
        self._supersede()

    def active_snapshot(self) -> SessionSnapshot | None:
        """Read-only view of the live slot for the bridge's status snapshot and
        the shell's shutdown: None when idle or when the slot holds a session
        already superseded. Call it ON the loop — the slot is mutated only there."""
        session = self._active
        if session is None or session.aborted or session.released:
            return None
        return SessionSnapshot(id=session.id, kind=session.kind, phase=session.phase)

    # ------------------------------------------------------------ pipelines

    async def _run_record(self, session: _Session, stt_key: str) -> None:
        stream = self._stt_factory(
            stt_key, self._make_on_update(session), self._make_on_error(session)
        )
        session.stt = stream
        # Capture and STT connect start IN PARALLEL (R10: the device opens on
        # the audio worker, so the handshake below runs meanwhile); frames
        # captured before the socket opens buffer inside the stream (cap
        # ~15 s, drop oldest). The recording cap arms when capture is
        # actually running — see _on_audio_started.
        session.owns_audio = True
        loop = asyncio.get_running_loop()
        started = loop.run_in_executor(
            self._audio_executor,
            self._audio.start,
            lambda pcm, rms: self._on_frame(session, pcm, rms),
        )
        started.add_done_callback(lambda f: self._on_audio_started(session, f))
        try:
            await stream.connect()
        except asyncio.CancelledError:
            await _quiet_abort(stream)
            raise
        except AppError as err:
            if session.aborted or self._active is not session:
                await _quiet_abort(stream)  # lost the race — rule 2, silent
                self._release(session)
            else:
                self._fail(session, err)
            return
        except Exception:
            if session.aborted or self._active is not session:
                await _quiet_abort(stream)
                self._release(session)
            else:
                self._fail(
                    session,
                    AppError(
                        "stt_connect",
                        "Could not connect to Deepgram. "
                        "Check the API key and your network.",
                    ),
                )
            return
        if session.aborted or self._active is not session:
            # Rule 2: a start that resolves and discovers it lost tears its
            # stream down — it must never install itself over the winner.
            await _quiet_abort(stream)
            self._release(session)
            return
        if session.stop_requested:
            # The user pressed Stop while the socket was opening. The frames
            # captured meanwhile were buffered in the stream and flushed on
            # open; finalize now, behind them (it waits for the drain first).
            self._spawn(session, self._finalize_and_answer(session))
            return
        session.phase = "recording"

    def _on_audio_started(self, session: _Session, started: asyncio.Future[None]) -> None:
        # Runs on the loop once the device open on the audio worker resolved.
        if started.cancelled():
            return
        failed = started.exception() is not None  # always retrieved: no stray warning
        if session.aborted or session.released or self._active is not session:
            return  # lost the race; teardown already queued the device stop
        if failed:
            # A missing/vanished loopback device must surface actionably, not
            # as a generic pipeline error.
            if session.stop_requested:
                # The drain owns the (failed) device now; nothing to stop. The
                # finalize that follows reports this instead of "no speech".
                session.device_failed = True
                return
            self._fail(session, _device_open_error())
            return
        if session.stop_requested:
            return  # Stop landed while the device was opening: no cap to arm
        # R10: the cap is armed at capture acceptance, not after the STT
        # connect, and the UI counts down to the SAME deadline instead of
        # running its own interval from an earlier moment. The deadline is
        # wall-clock epoch ms because the page compares it with Date.now().
        cap_s = self._timeouts.record_cap_s
        session.cap_handle = asyncio.get_running_loop().call_later(
            cap_s, self._on_record_cap, session
        )
        self._emit(
            session,
            "session:recording",
            {
                "deadlineMs": int((self._wall_clock() + cap_s) * 1000),
                "capMs": int(cap_s * 1000),
            },
        )

    def _on_record_cap(self, session: _Session) -> None:
        # 120 s hard cap: auto-stop, then answer normally. The event lets the
        # UI transition to finalizing without a user stop press. The cap runs
        # from capture start, so it can fire while the socket still connects.
        if (
            self._active is not session
            or session.aborted
            or session.phase not in ("connecting", "recording")
            or session.stop_requested
        ):
            return
        self._emit(session, "session:autostopped", {})
        self._begin_stop(session)

    def _begin_stop(self, session: _Session) -> None:
        was_connecting = session.phase == "connecting"
        session.stop_requested = True
        # THE LATENCY CLOCK STARTS HERE — at Stop acceptance, BEFORE the drain
        # and device teardown: that time is part of what the user waits for,
        # so firstTokenMs/totalMs include it (review §8, R10).
        session.stop_time = self._clock()
        if session.cap_handle is not None:
            session.cap_handle.cancel()
        # R06: stop at the capture cutoff and deliver the pre-Stop tail. Runs
        # on the audio worker; submitted synchronously so it is ordered
        # before any later session's start.
        session.drain = asyncio.get_running_loop().run_in_executor(
            self._audio_executor, self._audio.stop_and_drain
        )
        # Retrieve a failure even when nobody awaits (the session was
        # cancelled mid-drain) — the finalize path treats it as "drained".
        session.drain.add_done_callback(
            lambda f: None if f.cancelled() else f.exception()
        )
        session.owns_audio = False
        session.phase = "finalizing"
        # Provider misconfig surfaces at answer time with a real message.
        with contextlib.suppress(AppError):
            self._warmer.warm(self._active_provider().origin)
        if was_connecting:
            return  # _run_record finalizes the moment the socket opens
        self._spawn(session, self._finalize_and_answer(session))

    async def _finalize_and_answer(self, session: _Session) -> None:
        assert session.stt is not None and session.stop_time is not None
        # R06: every pre-Stop frame reaches the stream BEFORE finalize queues
        # CloseStream behind it (Deepgram discards audio after CloseStream).
        await self._drain_audio(session)
        if session.aborted or session.errored:
            return
        drained_at = self._clock()
        audio_drain_ms = int((drained_at - session.stop_time) * 1000)
        try:
            # The stream enforces the 5 s flush cap itself and returns what it
            # has; this outer guard only catches a finalize that truly hangs.
            transcript = await asyncio.wait_for(
                session.stt.finalize(), self._timeouts.stt_finalize_s + 2.0
            )
        except TimeoutError:
            self._fail(
                session,
                AppError("stt_timeout", "Timed out finalizing the transcript. Try again."),
            )
            return
        if session.aborted or session.errored:
            return
        # Stage timings are split so neither hides the other; the headline
        # firstTokenMs/totalMs still run from Stop acceptance and include both.
        stt_ms = int((self._clock() - drained_at) * 1000)
        session.audio_drain_ms = audio_drain_ms
        if not transcript.strip() and session.device_failed:
            self._fail(session, _device_open_error())
            return
        if not transcript.strip():
            # Rule 7: never an LLM call on an empty prompt.
            self._fail(
                session,
                AppError(
                    "no_speech",
                    "No speech detected in the recording. "
                    "Make sure call audio is playing.",
                ),
            )
            return
        # Rule 5: once the transcript is final the STT stream's job is done —
        # a late socket close must NOT kill an answer that is streaming.
        session.stt_done = True
        await self._answer(session, transcript, stt_ms)

    async def _run_ask(self, session: _Session, question: str) -> None:
        # Rule 8: the trimmed question renders through the same event shape
        # as a recorded transcript.
        self._emit(session, "stt:partial", {"text": question, "isFinal": True})
        # sttFinalizeMs is exactly 0 for typed questions: there was no STT
        # stage, and billing one would be a lie.
        await self._answer(session, question, stt_ms=0)

    async def _answer(self, session: _Session, transcript: str, stt_ms: int) -> None:
        assert session.stop_time is not None
        cfg = self._settings.answer_config()
        provider = self._providers.get(cfg.provider_id)
        if provider is None:
            self._fail(session, _unknown_provider_error())
            return
        api_key = await asyncio.to_thread(self._settings.get_secret, provider.id)
        if session.aborted or session.errored:
            return
        if not api_key:
            self._fail(session, _missing_key_error(provider))
            return
        prompt = build_prompt(
            resume=cfg.resume,
            job_description=cfg.job_description,
            style=cfg.style,
            transcript=transcript,
            call_type=cfg.call_type,
            focus=cfg.focus,
            notes=cfg.notes,
        )
        request = provider.build_request(prompt, api_key)
        session.phase = "answering"
        first_token_ms: int | None = None

        def on_delta(delta: str) -> None:
            nonlocal first_token_ms
            # Rule 9: deltas racing in after a timeout/error are suppressed —
            # nothing paints after the error.
            if session.aborted or session.errored:
                return
            if first_token_ms is None:
                assert session.stop_time is not None
                first_token_ms = int((self._clock() - session.stop_time) * 1000)
            self._emit(session, "llm:delta", {"delta": delta})

        loop = asyncio.get_running_loop()
        stream_task = loop.create_task(
            stream_answer(provider, request, self._http, on_delta)
        )

        async def watchdog(delay: float, code: ErrorCode, disarmed: Callable[[], bool]) -> None:
            await asyncio.sleep(delay)
            if disarmed() or stream_task.done() or session.timeout_code is not None:
                return
            session.timeout_code = code
            session.errored = True  # suppress deltas the instant the timer fires
            stream_task.cancel()

        # Rule 9: the first-token timer is disarmed by the first delta; the
        # total timer runs to completion.
        wd_first = loop.create_task(
            watchdog(
                self._timeouts.llm_first_token_s,
                "llm_first_token_timeout",
                lambda: first_token_ms is not None,
            )
        )
        wd_total = loop.create_task(
            watchdog(self._timeouts.llm_total_s, "llm_timeout", lambda: False)
        )
        try:
            await asyncio.wait({stream_task})
        except asyncio.CancelledError:
            stream_task.cancel()
            raise
        finally:
            wd_first.cancel()
            wd_total.cancel()

        if stream_task.cancelled():
            if session.timeout_code == "llm_first_token_timeout":
                self._fail(
                    session,
                    AppError(
                        "llm_first_token_timeout",
                        "The answer didn't start streaming within 10 seconds. Try again.",
                    ),
                )
            elif session.timeout_code == "llm_timeout":
                self._fail(
                    session,
                    AppError(
                        "llm_timeout",
                        "The answer took longer than 60 seconds and was stopped.",
                    ),
                )
            else:
                self._release(session)  # cancelled by an abort — silent
            return
        exc = stream_task.exception()
        if exc is not None:
            if session.aborted:
                self._release(session)
                return
            error = provider.classify_error(exc)
            if error.code == "aborted":
                self._release(session)
            else:
                self._fail(session, error)
            return
        result = stream_task.result()
        if session.aborted or session.errored:
            return
        total_ms = int((self._clock() - session.stop_time) * 1000)
        # A provider that never streamed a delta reports firstTokenMs =
        # totalMs — never 0, because 0 renders as "instant" and lies about
        # the one number this app is judged on.
        first_ms = first_token_ms if first_token_ms is not None else total_ms
        session.phase = "done"
        self._emit(
            session,
            "llm:done",
            {
                "transcript": transcript,
                "answer": result.text,
                # R03: "complete" | "truncated" (hit the output-token cap) |
                # "refused". Only non-empty answers get here — an empty one
                # is a session:error (retry.py). See CONTRACT.md.
                "finish": result.finish,
                "callType": cfg.call_type,
                "metrics": {
                    "audioDrainMs": session.audio_drain_ms,
                    "sttFinalizeMs": stt_ms,
                    "firstTokenMs": first_ms,
                    "totalMs": total_ms,
                },
            },
        )
        self._teardown(session)

    # ------------------------------------------------------------- plumbing

    def _active_provider(self) -> AnswerProvider:
        cfg = self._settings.answer_config()
        provider = self._providers.get(cfg.provider_id)
        if provider is None:
            raise _unknown_provider_error()
        return provider

    def _claim_ticket(self) -> int:
        self._command_seq += 1
        return self._command_seq

    def _require_ticket(self, ticket: int) -> None:
        if self._command_seq != ticket:
            # A newer command claimed while we were awaiting. Report `aborted`,
            # which the UI never shows — the user's newer action is in charge.
            raise AppError("aborted", "Superseded by a newer request.")

    def _new_session(self, kind: Literal["record", "ask"]) -> _Session:
        self._counter += 1
        return _Session(id=f"s{self._counter}", kind=kind)

    def _spawn(self, session: _Session, coro: Coroutine[object, object, None]) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        session.tasks.add(task)

        def _done(t: asyncio.Task[None]) -> None:
            session.tasks.discard(t)
            if t.cancelled():
                return
            exc = t.exception()
            if exc is None:
                return
            # A failure in one pipeline must never take the process down —
            # the worst case is one failed answer and a structured error.
            if isinstance(exc, AppError):
                self._fail(session, exc)
            else:
                self._fail(
                    session, AppError("internal", "Internal error in the session pipeline.")
                )

        task.add_done_callback(_done)

    async def _drain_audio(self, session: _Session) -> None:
        """Wait for the Stop drain, then close the capture cutoff.

        The drain posts every pre-Stop frame to the loop BEFORE it returns,
        and the executor's completion is posted after them, so by the time
        this resumes each of those frames has already gone through _on_frame
        into the stream. Only then does Rule 4 start rejecting frames."""
        drain = session.drain
        try:
            if drain is not None:
                with contextlib.suppress(Exception):  # a failed stop drained nothing more
                    await asyncio.wait_for(
                        asyncio.shield(drain), self._timeouts.audio_drain_s
                    )
        finally:
            session.capture_closed = True

    def _on_frame(self, session: _Session, pcm: bytes, rms: float) -> None:
        # Rule 4: frames only for the live session, up to the capture cutoff.
        # Frames captured BEFORE Stop but still queued (or drained as the
        # final short frame) are accepted while the drain runs — dropping them
        # cut the last syllable (R06). Once _drain_audio closes the cutoff,
        # nothing more is accepted: finalize is about to queue CloseStream.
        if self._active is not session or session.aborted or session.capture_closed:
            return
        if session.phase not in ("connecting", "recording", "finalizing"):
            return
        if session.stt is not None:
            session.stt.send(pcm)
        if not session.stop_requested:
            self._emit(session, "audio:level", {"rms": rms})

    def _make_on_update(self, session: _Session) -> OnSttUpdate:
        def cb(text: str, is_final: bool) -> None:
            if session.aborted or session.errored or session.phase == "done":
                return
            self._emit(session, "stt:partial", {"text": text, "isFinal": is_final})

        return cb

    def _make_on_error(self, session: _Session) -> OnSttError:
        def cb(err: AppError) -> None:
            # Rule 5: one stt_error tears the session down — but never after
            # the transcript finalized, and never for an abort-caused death.
            if session.stt_done or session.aborted or session.errored:
                return
            self._fail(session, err)

        return cb

    def _emit(self, session: _Session, name: str, payload: dict[str, object]) -> None:
        # Rule 1: events from a superseded session are dropped — including
        # its done.
        if session.aborted:
            return
        # Rule 9, structurally: once a session has failed, the only thing it
        # may still emit is its own error. Nothing paints after the error.
        if session.errored and name != "session:error":
            return
        self._events.emit(name, {"sessionId": session.id, **payload})

    def _fail(self, session: _Session, err: AppError) -> None:
        # Rule 6 (one error per stream, never after abort) + rule 3's silent
        # cases: errors after done/abort/release change nothing.
        if session.released or session.aborted:
            return
        if session.errored and session.phase == "done":
            return  # already failed once — one error per stream
        session.errored = True
        session.phase = "done"
        if err.code != "aborted":
            self._emit(session, "session:error", {"error": err.to_payload()})
        self._teardown(session)

    def _stop_audio(self, session: _Session) -> None:
        # Discard-stop on the audio worker: queued behind this session's own
        # start (so a slow device open is still closed) and ahead of any
        # later session's start. LoopbackCapture.stop swallows device errors.
        if session.owns_audio:
            session.owns_audio = False
            session.capture_closed = True
            self._audio_executor.submit(self._audio.stop)

    def _teardown(self, session: _Session) -> None:
        self._stop_audio(session)
        if session.cap_handle is not None:
            session.cap_handle.cancel()
        if session.stt is not None:
            stream = session.stt
            asyncio.get_running_loop().create_task(_quiet_abort(stream))
        self._release(session)

    def _release(self, session: _Session) -> None:
        # Rule 11: the slot is released exactly once, whatever the outcome —
        # the next session never supersedes a ghost.
        if session.released:
            return
        session.released = True
        if session.cap_handle is not None:
            session.cap_handle.cancel()
        if self._active is session:
            self._active = None

    def _supersede(self) -> None:
        session = self._active
        if session is None:
            return
        session.aborted = True  # from here every event from it is dropped
        if session.cap_handle is not None:
            session.cap_handle.cancel()
        self._stop_audio(session)
        for task in list(session.tasks):
            task.cancel()
        if session.stt is not None:
            stream = session.stt
            # Aborting CAUSES the socket to die; the stream's abort path
            # suppresses that death so it is never reported as an error.
            asyncio.get_running_loop().create_task(_quiet_abort(stream))
        self._release(session)
