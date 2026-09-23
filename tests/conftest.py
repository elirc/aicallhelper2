"""Shared fakes for the core test suite.

No network, no audio devices, no live providers anywhere in the suite: the
session machine is exercised through these Protocol-typed fakes at the same
seams production uses (SttStream, AnswerProvider, AudioSource, EventSink).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Callable
from concurrent.futures import Executor, Future
from typing import Any

import httpx
import pytest

from app_core.errors import AppError
from app_core.llm.base import ProviderFailure, ProviderRequest
from app_core.llm.prompt import PromptParts
from app_core.session.machine import (
    AnswerConfig,
    OnSttError,
    OnSttUpdate,
    SessionManager,
    Timeouts,
)


class FakeSettings:
    """SettingsReader fake with directly assignable state."""

    def __init__(self) -> None:
        self.resume = ""
        self.job_description = ""
        self.style = "balanced"
        self.provider_id = "fake"
        self.call_type = "behavioral"
        self.focus = ""
        self.notes = ""
        self.secrets: dict[str, str] = {"deepgram": "dg-key", "fake": "llm-key"}

    def answer_config(self) -> AnswerConfig:
        return AnswerConfig(
            resume=self.resume,
            job_description=self.job_description,
            style=self.style,
            provider_id=self.provider_id,
            call_type=self.call_type,
            focus=self.focus,
            notes=self.notes,
        )

    def get_secret(self, secret_id: str) -> str | None:
        return self.secrets.get(secret_id)


class CollectSink:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def emit(self, name: str, payload: dict[str, Any]) -> None:
        self.events.append((name, payload))

    def named(self, name: str) -> list[dict[str, Any]]:
        return [p for n, p in self.events if n == name]

    def for_session(self, session_id: str) -> list[tuple[str, dict[str, Any]]]:
        return [(n, p) for n, p in self.events if p.get("sessionId") == session_id]


class InlineExecutor(Executor):
    """Runs audio-worker calls synchronously at submit time, so the suite's
    FakeAudio stays deterministic. Tests that need a real worker thread (loop
    responsiveness, drain latency) pass a ThreadPoolExecutor instead."""

    def submit(self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Future[Any]:
        future: Future[Any] = Future()
        try:
            future.set_result(fn(*args, **kwargs))
        except BaseException as exc:
            future.set_exception(exc)
        return future


class FakeAudio:
    def __init__(self) -> None:
        self.on_frame: Callable[[bytes, float], None] | None = None
        self.starts = 0
        self.stops = 0
        self.drains = 0
        # stop_and_drain knobs: a pre-Stop remainder to deliver, a blocking
        # device-teardown delay, and the loop to post to (set when the drain
        # runs on a real worker thread, like LoopbackCapture's posting).
        self.tail: bytes | None = None
        self.drain_delay_s = 0.0
        self.loop: asyncio.AbstractEventLoop | None = None

    def start(self, on_frame: Callable[[bytes, float], None]) -> None:
        self.starts += 1
        self.on_frame = on_frame

    def stop(self) -> None:
        self.stops += 1
        self.on_frame = None

    def stop_and_drain(self) -> None:
        self.drains += 1
        self.stops += 1
        on_frame, self.on_frame = self.on_frame, None
        if self.drain_delay_s:
            time.sleep(self.drain_delay_s)
        if on_frame is not None and self.tail is not None:
            if self.loop is not None:
                self.loop.call_soon_threadsafe(on_frame, self.tail, 0.1)
            else:
                on_frame(self.tail, 0.1)

    def feed(self, pcm: bytes, rms: float = 0.5) -> None:
        if self.on_frame is not None:
            self.on_frame(pcm, rms)


class FakeWarmer:
    def __init__(self) -> None:
        self.warmed: list[str] = []

    def warm(self, origin: str) -> None:
        self.warmed.append(origin)


class FakeStt:
    """Scriptable SttStream. Tests drive it via the exposed knobs."""

    def __init__(self, api_key: str, on_update: OnSttUpdate, on_error: OnSttError) -> None:
        self.api_key = api_key
        self.on_update = on_update
        self.on_error = on_error
        self.frames: list[bytes] = []
        self.aborted = False
        self.finalize_called = 0
        self.transcript = "What is your greatest strength?"
        self.connect_gate: asyncio.Event | None = None  # test holds connect open
        self.connect_error: AppError | None = None
        self.finalize_gate: asyncio.Event | None = None  # test holds finalize open
        self.connected = False

    async def connect(self) -> None:
        if self.connect_gate is not None:
            await self.connect_gate.wait()
        if self.connect_error is not None:
            raise self.connect_error
        self.connected = True

    def send(self, pcm: bytes) -> None:
        self.frames.append(pcm)

    async def finalize(self) -> str:
        self.finalize_called += 1
        if self.finalize_gate is not None:
            await self.finalize_gate.wait()
        return self.transcript

    async def abort(self) -> None:
        self.aborted = True


class FakeSttFactory:
    def __init__(self) -> None:
        self.streams: list[FakeStt] = []
        self.prepare: Callable[[FakeStt], None] | None = None

    def __call__(
        self, api_key: str, on_update: OnSttUpdate, on_error: OnSttError
    ) -> FakeStt:
        stream = FakeStt(api_key, on_update, on_error)
        if self.prepare is not None:
            self.prepare(stream)
        self.streams.append(stream)
        return stream

    @property
    def last(self) -> FakeStt:
        return self.streams[-1]


class FakeProvider:
    """Scriptable AnswerProvider."""

    id = "fake"
    display_name = "Fake Provider"
    origin = "https://fake.example"

    def __init__(self) -> None:
        self.deltas: list[str] = ["Hello ", "world."]
        self.delta_gap_s = 0.0
        self.pre_delta_hang: asyncio.Event | None = None  # never yields until set
        self.post_delta_hang: asyncio.Event | None = None  # hangs after first delta
        self.failures: list[BaseException] = []  # popped per attempt, before deltas
        self.fail_after_first_delta: BaseException | None = None
        self.requests_built = 0
        self.stream_calls = 0
        self.prompts: list[PromptParts] = []

    def build_request(self, prompt: PromptParts, api_key: str) -> ProviderRequest:
        self.requests_built += 1
        self.prompts.append(prompt)
        body = (prompt.cached_prefix + prompt.style_suffix + prompt.user_message).encode()
        return ProviderRequest(
            url="https://fake.example/v1/answer",
            headers=(("authorization", api_key),),
            body=body,
        )

    async def stream(
        self, request: ProviderRequest, http: httpx.AsyncClient
    ) -> AsyncIterator[str]:
        self.stream_calls += 1
        if self.failures:
            raise self.failures.pop(0)
        if self.pre_delta_hang is not None:
            await self.pre_delta_hang.wait()
        for i, delta in enumerate(self.deltas):
            if self.delta_gap_s:
                await asyncio.sleep(self.delta_gap_s)
            yield delta
            if i == 0:
                if self.fail_after_first_delta is not None:
                    raise self.fail_after_first_delta
                if self.post_delta_hang is not None:
                    await self.post_delta_hang.wait()

    def classify_error(self, failure: BaseException) -> AppError:
        if isinstance(failure, asyncio.CancelledError):
            return AppError("aborted", "Cancelled.")
        if isinstance(failure, ProviderFailure):
            if failure.kind == "status" and failure.status == 401:
                return AppError("llm_auth", "Fake rejected the API key (401).")
            if failure.kind == "connect":
                return AppError("llm_http", "Could not reach Fake.")
            return AppError("llm_http", "Fake HTTP trouble.")
        return AppError("internal", "Unexpected error.")

    def is_retryable(self, failure: BaseException) -> bool:
        return isinstance(failure, ProviderFailure) and failure.kind == "connect"


class Harness:
    def __init__(self, timeouts: Timeouts, audio_executor: Executor | None = None) -> None:
        self.settings = FakeSettings()
        self.sink = CollectSink()
        self.audio = FakeAudio()
        self.warmer = FakeWarmer()
        self.stt = FakeSttFactory()
        self.provider = FakeProvider()
        self.http = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(500))
        )
        self.machine = SessionManager(
            settings=self.settings,
            providers={"fake": self.provider},
            stt_factory=self.stt,
            audio=self.audio,
            http=self.http,
            warmer=self.warmer,
            events=self.sink,
            timeouts=timeouts,
            audio_executor=audio_executor or InlineExecutor(),
        )

    async def settle(self, rounds: int = 40) -> None:
        for _ in range(rounds):
            await asyncio.sleep(0)

    async def drain(self, timeout: float = 2.0) -> None:
        """Let short real-time waits (watchdogs, to_thread hops) resolve."""
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.01)
            if self.machine._active is None:
                return


@pytest.fixture
def harness() -> Harness:
    return Harness(
        Timeouts(
            stt_finalize_s=0.5,
            llm_first_token_s=0.35,
            llm_total_s=0.8,
            record_cap_s=30.0,
        )
    )
