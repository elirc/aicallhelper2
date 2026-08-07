"""Provider conformance suite + per-provider error-mapping matrices.

The conformance class is the harness any future AnswerProvider must pass —
clone the parametrize list when registering a new provider (see README
"How to add an answer provider").

All wire traffic is scripted bytes through httpx.MockTransport — no network.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable

import httpx
import pytest

from app_core.llm.anthropic import AnthropicProvider
from app_core.llm.base import AnswerProvider, ProviderFailure
from app_core.llm.groq import GroqProvider, extract_openai_delta, is_done_sentinel
from app_core.llm.prompt import build_prompt

PROMPT = build_prompt(
    resume="Python engineer", job_description="SRE role", style="brief", transcript="Why us?"
)


def anthropic_sse(texts: list[str]) -> bytes:
    lines = [b'data: {"type":"message_start","message":{}}\n\n']
    for text in texts:
        payload = json.dumps(
            {"type": "content_block_delta", "delta": {"type": "text_delta", "text": text}}
        )
        lines.append(f"data: {payload}\n\n".encode())
    lines.append(b'data: {"type":"message_stop"}\n\n')
    return b"".join(lines)


def groq_sse(texts: list[str]) -> bytes:
    lines = []
    for text in texts:
        payload = json.dumps({"choices": [{"delta": {"content": text}}]})
        lines.append(f"data: {payload}\n\n".encode())
    lines.append(b"data: [DONE]\n\n")
    return b"".join(lines)


SSE_BUILDERS: dict[str, Callable[[list[str]], bytes]] = {
    "anthropic": anthropic_sse,
    "groq": groq_sse,
}


def client_returning(
    response_factory: Callable[[httpx.Request], httpx.Response],
) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(response_factory))


def client_with_bytes(body: bytes, chunk_size: int = 0) -> httpx.AsyncClient:
    async def chunks() -> AsyncIterator[bytes]:
        if chunk_size <= 0:
            yield body
        else:
            for i in range(0, len(body), chunk_size):
                yield body[i : i + chunk_size]

    return client_returning(lambda request: httpx.Response(200, content=chunks()))


async def collect(provider: AnswerProvider, http: httpx.AsyncClient) -> list[str]:
    request = provider.build_request(PROMPT, "test-key")
    return [d async for d in provider.stream(request, http)]


@pytest.mark.parametrize(
    "provider", [AnthropicProvider(), GroqProvider()], ids=lambda p: p.id
)
class TestProviderConformance:
    """Every registered AnswerProvider must pass these."""

    def test_identity(self, provider: AnswerProvider) -> None:
        assert provider.id and provider.display_name
        assert provider.origin.startswith("https://")
        assert not provider.origin.endswith("/")

    def test_build_request_is_deterministic(self, provider: AnswerProvider) -> None:
        a = provider.build_request(PROMPT, "k")
        b = provider.build_request(PROMPT, "k")
        assert a.url == b.url and a.headers == b.headers and a.body == b.body

    def test_request_carries_the_key_and_json_content_type(
        self, provider: AnswerProvider
    ) -> None:
        request = provider.build_request(PROMPT, "sekret-key-123")
        headers = request.header_dict()
        assert any("sekret-key-123" in v for v in headers.values())
        assert headers.get("content-type") == "application/json"

    async def test_stream_yields_expected_deltas(self, provider: AnswerProvider) -> None:
        body = SSE_BUILDERS[provider.id](["Hello ", "world", "!"])
        deltas = await collect(provider, client_with_bytes(body))
        assert "".join(deltas) == "Hello world!"

    async def test_stream_survives_hostile_chunking(self, provider: AnswerProvider) -> None:
        body = SSE_BUILDERS[provider.id](["café ", "€50", " done"])
        for chunk_size in (1, 2, 3, 7):
            deltas = await collect(provider, client_with_bytes(body, chunk_size))
            assert "".join(deltas) == "café €50 done"

    async def test_non_200_raises_status_failure(self, provider: AnswerProvider) -> None:
        http = client_returning(lambda r: httpx.Response(418, text="teapot"))
        with pytest.raises(ProviderFailure) as info:
            await collect(provider, http)
        assert info.value.kind == "status" and info.value.status == 418

    async def test_200_with_empty_body_raises_not_crashes(
        self, provider: AnswerProvider
    ) -> None:
        http = client_returning(lambda r: httpx.Response(200, content=b""))
        with pytest.raises(ProviderFailure) as info:
            await collect(provider, http)
        assert info.value.kind == "empty_body"

    async def test_connection_failure_is_connect_kind(self, provider: AnswerProvider) -> None:
        def raise_connect(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("boom", request=request)

        with pytest.raises(ProviderFailure) as info:
            await collect(provider, client_returning(raise_connect))
        assert info.value.kind == "connect"

    async def test_mid_stream_drop_is_stream_drop_kind(
        self, provider: AnswerProvider
    ) -> None:
        body = SSE_BUILDERS[provider.id](["partial"])

        async def dying() -> AsyncIterator[bytes]:
            yield body
            raise httpx.ReadError("connection reset")

        http = client_returning(lambda r: httpx.Response(200, content=dying()))
        request = provider.build_request(PROMPT, "k")
        got: list[str] = []
        with pytest.raises(ProviderFailure) as info:
            async for delta in provider.stream(request, http):
                got.append(delta)
        assert info.value.kind == "stream_drop"
        assert "".join(got) == "partial"  # deltas before the drop still made it out

    def test_abort_classified_first_never_a_scary_http_error(
        self, provider: AnswerProvider
    ) -> None:
        assert provider.classify_error(asyncio.CancelledError()).code == "aborted"

    def test_401_maps_to_llm_auth(self, provider: AnswerProvider) -> None:
        err = provider.classify_error(ProviderFailure("status", status=401))
        assert err.code == "llm_auth"
        assert "401" in err.message

    def test_429_maps_to_rate_limit(self, provider: AnswerProvider) -> None:
        assert (
            provider.classify_error(ProviderFailure("status", status=429)).code
            == "llm_rate_limit"
        )

    def test_connect_maps_to_llm_http_with_actionable_message(
        self, provider: AnswerProvider
    ) -> None:
        err = provider.classify_error(ProviderFailure("connect"))
        assert err.code == "llm_http"
        assert "internet" in err.message.lower() or "reach" in err.message.lower()

    def test_unknown_exception_maps_to_internal(self, provider: AnswerProvider) -> None:
        assert provider.classify_error(ValueError("x")).code == "internal"

    def test_only_connect_is_retryable(self, provider: AnswerProvider) -> None:
        assert provider.is_retryable(ProviderFailure("connect")) is True
        assert provider.is_retryable(ProviderFailure("status", status=500)) is False
        assert provider.is_retryable(ProviderFailure("stream_drop")) is False
        assert provider.is_retryable(ProviderFailure("empty_body")) is False
        assert provider.is_retryable(ValueError()) is False


class TestDefaultRegistry:
    def test_ships_both_providers_anthropic_first(self) -> None:
        from app_core.llm.base import default_registry

        registry = default_registry()
        assert registry.ids() == ["anthropic", "groq"]
        choices = registry.choices()
        assert choices[0] == {
            "id": "anthropic",
            "displayName": "Claude Haiku 4.5 (recommended)",
        }
        assert registry.get("groq") is not None
        assert registry.get("nope") is None


class TestAnthropicSpecifics:
    provider = AnthropicProvider()

    def test_body_shape_two_system_blocks_cache_breakpoint_on_first(self) -> None:
        request = self.provider.build_request(PROMPT, "k")
        body = json.loads(request.body)
        assert body["model"] == "claude-haiku-4-5"
        assert body["max_tokens"] == 1024
        assert body["stream"] is True
        system = body["system"]
        assert len(system) == 2
        assert system[0]["text"] == PROMPT.cached_prefix
        assert system[0]["cache_control"] == {"type": "ephemeral"}
        assert system[1]["text"] == PROMPT.style_suffix
        assert "cache_control" not in system[1]
        assert body["messages"] == [{"role": "user", "content": PROMPT.user_message}]

    def test_headers(self) -> None:
        headers = self.provider.build_request(PROMPT, "k").header_dict()
        assert headers["x-api-key"] == "k"
        assert headers["anthropic-version"] == "2023-06-01"

    async def test_multiple_content_blocks_joined_with_nothing(self) -> None:
        # Deltas across ALL content blocks concatenate with NOTHING between.
        body = (
            anthropic_sse(["block one"])[: -len(b'data: {"type":"message_stop"}\n\n')]
            + b'data: {"type":"content_block_stop"}\n\n'
            + anthropic_sse(["block two"])
        )
        deltas = await collect(self.provider, client_with_bytes(body))
        assert "".join(deltas) == "block oneblock two"

    def test_403_is_llm_auth(self) -> None:
        err = self.provider.classify_error(ProviderFailure("status", status=403))
        assert err.code == "llm_auth" and "403" in err.message

    def test_529_overloaded(self) -> None:
        err = self.provider.classify_error(ProviderFailure("status", status=529))
        assert err.code == "llm_http" and "529" in err.message

    def test_other_status_includes_snippet(self) -> None:
        err = self.provider.classify_error(
            ProviderFailure("status", status=500, body_snippet='{"error":"oops"}')
        )
        assert err.code == "llm_http" and "500" in err.message and "oops" in err.message


class TestGroqSpecifics:
    provider = GroqProvider()

    def test_body_shape_single_system_string_and_reasoning_knobs(self) -> None:
        request = self.provider.build_request(PROMPT, "k")
        body = json.loads(request.body)
        assert body["model"] == "openai/gpt-oss-120b"
        assert body["temperature"] == 0.7
        assert body["max_completion_tokens"] == 1024
        assert body["reasoning_effort"] == "low"
        assert body["include_reasoning"] is False
        assert "reasoning_format" not in body  # Qwen-family knob — never sent
        system = body["messages"][0]
        assert system["role"] == "system"
        assert system["content"] == PROMPT.cached_prefix + "\n\n" + PROMPT.style_suffix

    def test_bearer_auth(self) -> None:
        headers = self.provider.build_request(PROMPT, "gk").header_dict()
        assert headers["Authorization"] == "Bearer gk"

    async def test_done_sentinel_skipped_bytes_after_still_count(self) -> None:
        body = (
            b'data: {"choices":[{"delta":{"content":"before"}}]}\n\n'
            b"data: [DONE]\n\n"
            b'data: {"choices":[{"delta":{"content":" after"}}]}\n\n'
        )
        deltas = await collect(self.provider, client_with_bytes(body))
        assert "".join(deltas) == "before after"

    async def test_truncated_stream_flushes_last_words(self) -> None:
        # No trailing blank line: the final un-terminated data line must flush.
        body = b'data: {"choices":[{"delta":{"content":"last words"}}]}'
        deltas = await collect(self.provider, client_with_bytes(body))
        assert deltas == ["last words"]

    def test_404_points_at_the_pinned_model_constant(self) -> None:
        err = self.provider.classify_error(ProviderFailure("status", status=404))
        assert err.code == "llm_http"
        assert "retired" in err.message and "constant" in err.message

    def test_403_reports_actual_status(self) -> None:
        err = self.provider.classify_error(ProviderFailure("status", status=403))
        assert err.code == "llm_auth" and "403" in err.message and "401" not in err.message

    def test_5xx_unavailable(self) -> None:
        err = self.provider.classify_error(ProviderFailure("status", status=503))
        assert err.code == "llm_http" and "unavailable" in err.message

    def test_stream_drop_message(self) -> None:
        err = self.provider.classify_error(ProviderFailure("stream_drop"))
        assert err.code == "llm_http" and "streaming" in err.message

    def test_helpers_are_importable_template_pieces(self) -> None:
        assert is_done_sentinel(" [DONE] ")
        assert not is_done_sentinel('{"choices":[]}')
        assert extract_openai_delta('{"choices":[{"delta":{"content":"x"}}]}') == "x"
        assert extract_openai_delta('{"choices":[]}') is None
        assert extract_openai_delta("not json") is None
        assert extract_openai_delta('{"choices":[{"delta":{"content":5}}]}') is None


class TestPreResponseTimeoutIsNotRetryable:
    """A read timeout waiting for response headers means the server may
    already be generating our answer; retrying could produce a second answer
    and burns the latency budget. Only a genuine connect failure is safe."""

    @pytest.mark.parametrize(
        "provider", [AnthropicProvider(), GroqProvider()], ids=lambda p: p.id
    )
    async def test_read_timeout_before_response_is_kind_timeout(
        self, provider: AnswerProvider
    ) -> None:
        def raise_read_timeout(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("timed out waiting for headers", request=request)

        with pytest.raises(ProviderFailure) as info:
            await collect(provider, client_returning(raise_read_timeout))
        assert info.value.kind == "timeout"
        assert provider.is_retryable(info.value) is False
        err = provider.classify_error(info.value)
        assert err.code == "llm_http" and "time" in err.message.lower()

    @pytest.mark.parametrize(
        "provider", [AnthropicProvider(), GroqProvider()], ids=lambda p: p.id
    )
    async def test_connect_error_stays_retryable(self, provider: AnswerProvider) -> None:
        def raise_connect(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        with pytest.raises(ProviderFailure) as info:
            await collect(provider, client_returning(raise_connect))
        assert info.value.kind == "connect"
        assert provider.is_retryable(info.value) is True
