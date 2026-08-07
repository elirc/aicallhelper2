"""The session state machine — one live question/answer pipeline at a time.

Every invariant here was purchased with a real bug in v2; the numbered
comments reference the spec's §5 rules. All state is touched ONLY on the
core's asyncio loop; everything crossing a thread boundary hands off first.
Dependencies are Protocol-injected so pytest exercises every rule with
fakes — no network, no audio device.

`asyncio.CancelledError` is control flow here, not an error: abort paths
cancel tasks, and handlers must never swallow or remap cancellation.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Callable, Coroutine, Mapping
from dataclasses import dataclass, field
from typing import Literal, Protocol

import httpx

from app_core.errors import AppError, ErrorCode
from app_core.llm.base import AnswerProvider
from app_core.llm.prompt import build_prompt
from app_core.llm.retry import stream_answer

OnSttUpdate = Callable[[str, bool], None]
OnSttError = Callable[[AppError], None]


class SttStream(Protocol):
    async def connect(self) -> None: ...

    def send(self, pcm: bytes) -> None: ...

    async def finalize(self) -> str: ...

    async def abort(self) -> None: ...


SttFactory = Callable[[str, OnSttUpdate, OnSttError], SttStream]


class AudioSource(Protocol):
    def start(self, on_frame: Callable[[bytes, float], None]) -> None: ...

    def stop(self) -> None: ...


class EventSink(Protocol):
    def emit(self, name: str, payload: dict[str, object]) -> None: ...


class Warmer(Protocol):
    def warm(self, origin: str) -> object: ...


@dataclass(frozen=True)
class AnswerConfig:
    resume: str
    job_description: str
    style: str
    provider_id: str


class SettingsReader(Protocol):
    def answer_config(self) -> AnswerConfig:
        """Memory-only read — safe to call on the loop."""
        ...

    def get_secret(self, secret_id: str) -> str | None:
        """May hit DPAPI — callers wrap in asyncio.to_thread."""
        ...


@dataclass(frozen=True)
class Timeouts:
    stt_finalize_s: float = 5.0
    llm_first_token_s: float = 10.0
    llm_total_s: float = 60.0
    record_cap_s: float = 120.0


Phase = Literal["connecting", "recording", "finalizing", "answering", "done"]

DEEPGRAM_SECRET_ID = "deepgram"


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
    stop_time: float | None = None
    timeout_code: ErrorCode | None = None
    stt: SttStream | None = None
    tasks: set[asyncio.Task[None]] = field(default_factory=set)
    cap_handle: asyncio.TimerHandle | None = None


async def _quiet_abort(stream: SttStream) -> None:
    # Teardown of a dead stream must never surface.
    with contextlib.suppress(Exception):
        await stream.abort()


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
            raise AppError(
                "no_llm_key",
                f"No API key for {provider.display_name}. "
                "Open Settings (gear icon) and add it.",
            )
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
        isn't the live, recording, not-yet-stopping session. Every other
        outcome arrives as an event; this error return is the only way the
        UI learns its stop went nowhere."""
        session = self._active
        if (
            session is None
            or session.id != session_id
            or session.aborted
            or session.kind != "record"
            or session.phase != "recording"
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
            raise AppError(
                "no_llm_key",
                f"No API key for {provider.display_name}. "
                "Open Settings (gear icon) and add it.",
            )
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

    # ------------------------------------------------------------ pipelines

    async def _run_record(self, session: _Session, stt_key: str) -> None:
        stream = self._stt_factory(
            stt_key, self._make_on_update(session), self._make_on_error(session)
        )
        session.stt = stream
        # Capture and STT connect start IN PARALLEL; frames captured before
        # the socket opens buffer inside the stream (cap ~15 s, drop oldest).
        session.owns_audio = True
        try:
            self._audio.start(lambda pcm, rms: self._on_frame(session, pcm, rms))
        except Exception:
            # A missing/vanished loopback device must surface actionably, not
            # as a generic pipeline error.
            session.owns_audio = False
            self._fail(
                session,
                AppError(
                    "internal",
                    "Could not open the system audio device. Check that a "
                    "default output device exists, then try again.",
                ),
            )
            return
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
        session.phase = "recording"
        loop = asyncio.get_running_loop()
        session.cap_handle = loop.call_later(
            self._timeouts.record_cap_s, self._on_record_cap, session
        )

    def _on_record_cap(self, session: _Session) -> None:
        # 120 s hard cap: auto-stop, then answer normally. The event lets the
        # UI transition to finalizing without a user stop press.
        if (
            self._active is not session
            or session.aborted
            or session.phase != "recording"
            or session.stop_requested
        ):
            return
        self._emit(session, "session:autostopped", {})
        self._begin_stop(session)

    def _begin_stop(self, session: _Session) -> None:
        session.stop_requested = True
        session.stop_time = self._clock()  # THE LATENCY CLOCK STARTS HERE
        if session.cap_handle is not None:
            session.cap_handle.cancel()
        self._audio.stop()
        session.owns_audio = False
        session.phase = "finalizing"
        # Provider misconfig surfaces at answer time with a real message.
        with contextlib.suppress(AppError):
            self._warmer.warm(self._active_provider().origin)
        self._spawn(session, self._finalize_and_answer(session))

    async def _finalize_and_answer(self, session: _Session) -> None:
        assert session.stt is not None and session.stop_time is not None
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
        stt_ms = int((self._clock() - session.stop_time) * 1000)
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
            self._fail(
                session,
                AppError("internal", "Unknown answer provider — pick one in Settings."),
            )
            return
        api_key = await asyncio.to_thread(self._settings.get_secret, provider.id)
        if session.aborted or session.errored:
            return
        if not api_key:
            self._fail(
                session,
                AppError(
                    "no_llm_key",
                    f"No API key for {provider.display_name}. "
                    "Open Settings (gear icon) and add it.",
                ),
            )
            return
        prompt = build_prompt(
            resume=cfg.resume,
            job_description=cfg.job_description,
            style=cfg.style,
            transcript=transcript,
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
        answer = stream_task.result()
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
                "answer": answer,
                "metrics": {
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
            raise AppError("internal", "Unknown answer provider — pick one in Settings.")
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

    def _on_frame(self, session: _Session, pcm: bytes, rms: float) -> None:
        # Rule 4: frames only for the live, not-yet-stopped session. Frames
        # after stop would race the CloseStream flush; stale ids are dropped.
        if self._active is not session or session.aborted or session.stop_requested:
            return
        if session.phase not in ("connecting", "recording"):
            return
        if session.stt is not None:
            session.stt.send(pcm)
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

    def _teardown(self, session: _Session) -> None:
        if session.owns_audio:
            self._audio.stop()
            session.owns_audio = False
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
        if session.owns_audio:
            self._audio.stop()
            session.owns_audio = False
        for task in list(session.tasks):
            task.cancel()
        if session.stt is not None:
            stream = session.stt
            # Aborting CAUSES the socket to die; the stream's abort path
            # suppresses that death so it is never reported as an error.
            asyncio.get_running_loop().create_task(_quiet_abort(stream))
        self._release(session)
