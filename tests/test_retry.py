"""The retry policy matrix: retry once pre-stream; never on status; never
after delta; never after abort; byte-identical request reuse."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from app_core.llm.base import AnswerResult, ProviderFailure, ProviderRequest, StreamEnd
from app_core.llm.retry import stream_answer
from tests.conftest import FakeProvider


def http_stub() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500)))


async def run(provider: FakeProvider) -> tuple[str, list[str]]:
    deltas: list[str] = []
    request = provider.build_request(
        provider_prompt(), "key"
    )
    result = await stream_answer(provider, request, http_stub(), deltas.append)
    return result.text, deltas


def provider_prompt() -> object:
    from app_core.llm.prompt import build_prompt

    return build_prompt(resume="", job_description="", style="brief", transcript="q")


class TestRetryOnce:
    async def test_connect_failure_before_any_delta_retries_once(self) -> None:
        provider = FakeProvider()
        provider.failures = [ProviderFailure("connect")]
        answer, deltas = await run(provider)
        assert answer == "Hello world."
        assert deltas == ["Hello ", "world."]
        assert provider.stream_calls == 2

    async def test_two_connect_failures_do_not_retry_twice(self) -> None:
        provider = FakeProvider()
        provider.failures = [ProviderFailure("connect"), ProviderFailure("connect")]
        with pytest.raises(ProviderFailure):
            await run(provider)
        assert provider.stream_calls == 2

    async def test_http_status_never_retried(self) -> None:
        # The server heard us and said no — an instant retry burns the
        # first-token budget.
        provider = FakeProvider()
        provider.failures = [ProviderFailure("status", status=429)]
        with pytest.raises(ProviderFailure):
            await run(provider)
        assert provider.stream_calls == 1

    async def test_never_after_a_delta(self) -> None:
        # A second attempt would concatenate two answers in the UI.
        # The failure kind must be RETRYABLE ("connect"), or the policy skips
        # the retry via is_retryable and this test proves nothing about the
        # got_delta guard — a mutation test caught exactly that.
        provider = FakeProvider()
        provider.fail_after_first_delta = ProviderFailure("connect")
        deltas: list[str] = []
        request = provider.build_request(provider_prompt(), "key")
        with pytest.raises(ProviderFailure):
            await stream_answer(provider, request, http_stub(), deltas.append)
        assert provider.stream_calls == 1
        assert deltas == ["Hello "]  # the partial stays painted; no second answer

    async def test_never_after_abort(self) -> None:
        provider = FakeProvider()
        provider.pre_delta_hang = asyncio.Event()  # never set — hangs
        request = provider.build_request(provider_prompt(), "key")
        task = asyncio.get_running_loop().create_task(
            stream_answer(provider, request, http_stub(), lambda d: None)
        )
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert provider.stream_calls == 1

    async def test_retried_request_is_the_same_object(self) -> None:
        # Byte-identical by construction: build_request ran once, the
        # immutable ProviderRequest is reused verbatim.
        seen: list[ProviderRequest] = []

        class Recording(FakeProvider):
            async def stream(self, request, http):  # type: ignore[override]
                seen.append(request)
                async for d in super().stream(request, http):
                    yield d

        provider = Recording()
        provider.failures = [ProviderFailure("connect")]
        request = provider.build_request(provider_prompt(), "key")
        await stream_answer(provider, request, http_stub(), lambda d: None)
        assert len(seen) == 2
        assert seen[0] is seen[1]
        assert provider.requests_built == 1


class TestAnswerResult:
    """R03: stream_answer reports HOW the answer ended and never returns a
    blank success."""

    async def test_a_provider_without_a_stream_end_is_trusted_as_complete(self) -> None:
        provider = FakeProvider()  # yields bare strings only
        request = provider.build_request(provider_prompt(), "key")
        result = await stream_answer(provider, request, http_stub(), lambda d: None)
        assert result == AnswerResult("Hello world.", "complete", None)

    async def test_stream_end_metadata_is_carried_into_the_result(self) -> None:
        class Truncating(FakeProvider):
            async def stream(self, request, http):  # type: ignore[override]
                async for d in super().stream(request, http):
                    yield d
                yield StreamEnd("truncated", "max_tokens")

        provider = Truncating()
        request = provider.build_request(provider_prompt(), "key")
        deltas: list[str] = []
        result = await stream_answer(provider, request, http_stub(), deltas.append)
        assert result == AnswerResult("Hello world.", "truncated", "max_tokens")
        assert deltas == ["Hello ", "world."]  # the StreamEnd never reaches the panel

    async def test_no_text_is_an_empty_answer_failure_and_never_retried(self) -> None:
        provider = FakeProvider()
        provider.deltas = []
        request = provider.build_request(provider_prompt(), "key")
        with pytest.raises(ProviderFailure) as info:
            await stream_answer(provider, request, http_stub(), lambda d: None)
        assert info.value.kind == "empty_answer" and info.value.finish == "complete"
        assert provider.stream_calls == 1

    async def test_a_raising_on_delta_closes_the_provider_stream_immediately(self) -> None:
        # Without an explicit aclose the provider generator (and the HTTP
        # response inside it) stays open until garbage collection.
        closed: list[bool] = []

        class Tracking(FakeProvider):
            async def stream(self, request, http):  # type: ignore[override]
                try:
                    async for d in super().stream(request, http):
                        yield d
                finally:
                    closed.append(True)

        def boom(delta: str) -> None:
            raise RuntimeError("sink failed")

        provider = Tracking()
        request = provider.build_request(provider_prompt(), "key")
        with pytest.raises(RuntimeError):
            await stream_answer(provider, request, http_stub(), boom)
        assert closed == [True]
