"""The retry policy matrix: retry once pre-stream; never on status; never
after delta; never after abort; byte-identical request reuse."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from app_core.llm.base import ProviderFailure, ProviderRequest
from app_core.llm.retry import stream_answer
from tests.conftest import FakeProvider


def http_stub() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500)))


async def run(provider: FakeProvider) -> tuple[str, list[str]]:
    deltas: list[str] = []
    request = provider.build_request(
        provider_prompt(), "key"
    )
    answer = await stream_answer(provider, request, http_stub(), deltas.append)
    return answer, deltas


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
