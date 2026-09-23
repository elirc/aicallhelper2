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
from app_core.llm.base import (
    AnswerProvider,
    AnswerResult,
    ProviderFailure,
    ProviderRequest,
    StreamEnd,
)
from app_core.llm.groq import MODEL as GROQ_MODEL
from app_core.llm.groq import (
    GroqProvider,
    extract_openai_delta,
    extract_openai_finish_reason,
    is_done_sentinel,
)
from app_core.llm.prompt import build_prompt
from app_core.llm.retry import stream_answer

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


async def collect_all(
    provider: AnswerProvider, http: httpx.AsyncClient
) -> list[str | StreamEnd]:
    request = provider.build_request(PROMPT, "test-key")
    return [d async for d in provider.stream(request, http)]


async def collect(provider: AnswerProvider, http: httpx.AsyncClient) -> list[str]:
    """Text deltas only (the terminal StreamEnd is checked separately)."""
    return [d for d in await collect_all(provider, http) if isinstance(d, str)]


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
        # A drop happens before the terminal event (Anthropic stops reading
        # at message_stop, so a completed body would never see the drop).
        body = _sse(WIRES[provider.id].text("partial"))

        async def dying() -> AsyncIterator[bytes]:
            yield body
            raise httpx.ReadError("connection reset")

        http = client_returning(lambda r: httpx.Response(200, content=dying()))
        request = provider.build_request(PROMPT, "k")
        got: list[str] = []
        with pytest.raises(ProviderFailure) as info:
            async for delta in provider.stream(request, http):
                assert isinstance(delta, str)
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
        # No trailing blank line: the final un-terminated data line must
        # flush (the words reach the panel) — but with neither finish_reason
        # nor [DONE] the stream was cut short, so it is NOT a completion.
        body = b'data: {"choices":[{"delta":{"content":"last words"}}]}'
        request = self.provider.build_request(PROMPT, "k")
        got: list[str | StreamEnd] = []
        with pytest.raises(ProviderFailure) as info:
            async for item in self.provider.stream(request, client_with_bytes(body)):
                got.append(item)
        assert got == ["last words"]
        assert info.value.kind == "incomplete"

    async def test_unterminated_final_done_line_still_flushes_and_completes(self) -> None:
        body = b'data: {"choices":[{"delta":{"content":"last words"}}]}\n\ndata: [DONE]'
        items = await collect_all(self.provider, client_with_bytes(body))
        assert items == ["last words", StreamEnd("complete", None)]

    def test_404_gives_an_actionable_message_never_an_edit_the_source_one(self) -> None:
        # R13: an installed-app user cannot edit app_core/llm/groq.py.
        err = self.provider.classify_error(ProviderFailure("status", status=404))
        assert err.code == "llm_http"
        assert "retire" in err.message and GROQ_MODEL in err.message
        assert "Settings" in err.message
        assert ".py" not in err.message and "constant" not in err.message
        assert "Claude" in err.message  # the concrete way out: switch provider

    def test_400_model_decommissioned_gets_the_same_actionable_message(self) -> None:
        err = self.provider.classify_error(
            ProviderFailure(
                "status",
                status=400,
                body_snippet='{"error":{"code":"model_decommissioned","message":"x"}}',
            )
        )
        assert err.code == "llm_http" and GROQ_MODEL in err.message
        assert ".py" not in err.message

    def test_other_400_is_not_misreported_as_a_retired_model(self) -> None:
        err = self.provider.classify_error(
            ProviderFailure("status", status=400, body_snippet='{"error":"bad"}')
        )
        assert GROQ_MODEL not in err.message and "400" in err.message

    def test_finish_reason_helper(self) -> None:
        assert extract_openai_finish_reason('{"choices":[{"finish_reason":"length"}]}') == (
            "length"
        )
        assert extract_openai_finish_reason('{"choices":[{"finish_reason":null}]}') is None
        assert extract_openai_finish_reason("nope") is None

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


# ---------------------------------------------------------------------------
# R03: provider-level completion, not HTTP 200 / EOF, decides success.


def _sse(*payloads: str) -> bytes:
    return b"".join(f"data: {p}\n\n".encode() for p in payloads)


class _AnthropicWire:
    normal, limit, refusal = "end_turn", "max_tokens", "refusal"

    @staticmethod
    def text(t: str) -> str:
        return json.dumps(
            {"type": "content_block_delta", "delta": {"type": "text_delta", "text": t}}
        )

    @staticmethod
    def error() -> list[str]:
        return [
            '{"type":"error","error":{"type":"api_error","message":"Internal server error"}}'
        ]

    @staticmethod
    def end(reason: str) -> list[str]:
        return [
            json.dumps({"type": "message_delta", "delta": {"stop_reason": reason}}),
            '{"type":"message_stop"}',
        ]


class _GroqWire:
    normal, limit, refusal = "stop", "length", "content_filter"

    @staticmethod
    def text(t: str) -> str:
        return json.dumps({"choices": [{"delta": {"content": t}, "finish_reason": None}]})

    @staticmethod
    def error() -> list[str]:
        return ['{"error":{"message":"Internal server error","type":"internal_error"}}']

    @staticmethod
    def end(reason: str) -> list[str]:
        return [
            json.dumps({"choices": [{"delta": {}, "finish_reason": reason}]}),
            "[DONE]",
        ]


WIRES: dict[str, type[_AnthropicWire] | type[_GroqWire]] = {
    "anthropic": _AnthropicWire,
    "groq": _GroqWire,
}


class _Counting:
    def __init__(self, body: bytes) -> None:
        self.requests = 0
        self._body = body
        self.http = client_returning(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests += 1
        return httpx.Response(200, content=self._body)


async def _answer(
    provider: AnswerProvider, body: bytes
) -> tuple[AnswerResult | ProviderFailure, list[str], int]:
    wire = _Counting(body)
    deltas: list[str] = []
    request = provider.build_request(PROMPT, "k")
    result: AnswerResult | ProviderFailure
    try:
        result = await stream_answer(provider, request, wire.http, deltas.append)
    except ProviderFailure as failure:
        result = failure
    return result, deltas, wire.requests


@pytest.mark.parametrize("provider", [AnthropicProvider(), GroqProvider()], ids=lambda p: p.id)
class TestProviderCompletionValidation:
    """The R03 acceptance scenarios, for both providers, end to end through
    stream_answer (so the no-retry-after-delta rule is exercised too)."""

    async def test_error_event_before_any_text_fails_and_is_not_retried(
        self, provider: AnswerProvider
    ) -> None:
        w = WIRES[provider.id]
        result, deltas, requests = await _answer(provider, _sse(*w.error()))
        assert isinstance(result, ProviderFailure) and result.kind == "provider_error"
        assert deltas == [] and requests == 1
        err = provider.classify_error(result)
        assert err.code == "llm_http" and "error during the answer" in err.message
        assert "Internal server error" in err.message

    async def test_error_event_after_text_fails_and_keeps_the_partial(
        self, provider: AnswerProvider
    ) -> None:
        w = WIRES[provider.id]
        body = _sse(w.text("Partial "), *w.error())
        result, deltas, requests = await _answer(provider, body)
        assert isinstance(result, ProviderFailure) and result.kind == "provider_error"
        assert deltas == ["Partial "] and requests == 1

    async def test_valid_but_empty_output_is_an_error_not_a_blank_success(
        self, provider: AnswerProvider
    ) -> None:
        w = WIRES[provider.id]
        result, _, requests = await _answer(provider, _sse(*w.end(w.normal)))
        assert isinstance(result, ProviderFailure) and result.kind == "empty_answer"
        assert requests == 1
        assert "without writing an answer" in provider.classify_error(result).message

    async def test_whitespace_only_output_is_an_empty_answer(
        self, provider: AnswerProvider
    ) -> None:
        w = WIRES[provider.id]
        body = _sse(w.text("  \n"), *w.end(w.normal))
        result, _, _ = await _answer(provider, body)
        assert isinstance(result, ProviderFailure) and result.kind == "empty_answer"

    async def test_clean_but_premature_eof_is_incomplete(
        self, provider: AnswerProvider
    ) -> None:
        w = WIRES[provider.id]
        # Unterminated final line: the SSE flush still delivers the words.
        body = _sse(w.text("Half an ")) + f"data: {w.text('answer')}".encode()
        result, deltas, requests = await _answer(provider, body)
        assert isinstance(result, ProviderFailure) and result.kind == "incomplete"
        assert deltas == ["Half an ", "answer"] and requests == 1
        err = provider.classify_error(result)
        assert err.code == "llm_http" and "stopped before" in err.message

    async def test_token_limit_completion_is_reported_as_truncated(
        self, provider: AnswerProvider
    ) -> None:
        w = WIRES[provider.id]
        body = _sse(w.text("A long answer that"), *w.end(w.limit))
        result, _, _ = await _answer(provider, body)
        assert result == AnswerResult("A long answer that", "truncated", w.limit)

    async def test_empty_output_at_the_token_limit_says_so(
        self, provider: AnswerProvider
    ) -> None:
        w = WIRES[provider.id]
        result, _, _ = await _answer(provider, _sse(*w.end(w.limit)))
        assert isinstance(result, ProviderFailure) and result.finish == "truncated"
        assert "length limit" in provider.classify_error(result).message

    async def test_refusal_is_reported_as_refused(self, provider: AnswerProvider) -> None:
        w = WIRES[provider.id]
        body = _sse(w.text("I can not help"), *w.end(w.refusal))
        result, _, _ = await _answer(provider, body)
        assert isinstance(result, AnswerResult) and result.finish == "refused"

    async def test_normal_completion_is_complete(self, provider: AnswerProvider) -> None:
        w = WIRES[provider.id]
        body = _sse(w.text("Hello "), w.text("world."), *w.end(w.normal))
        result, deltas, _ = await _answer(provider, body)
        assert result == AnswerResult("Hello world.", "complete", w.normal)
        assert deltas == ["Hello ", "world."]


class TestProviderSpecificCompletion:
    async def test_anthropic_ping_only_stream_is_incomplete_not_success(self) -> None:
        # `ping` is a data line, so the transport's "produced" flag flips on
        # it; completion must still require message_stop.
        body = _sse('{"type":"message_start","message":{}}', '{"type":"ping"}')
        result, _, _ = await _answer(AnthropicProvider(), body)
        assert isinstance(result, ProviderFailure) and result.kind == "incomplete"

    async def test_anthropic_message_stop_without_message_delta_is_complete(self) -> None:
        body = _sse(_AnthropicWire.text("ok"), '{"type":"message_stop"}')
        result, _, _ = await _answer(AnthropicProvider(), body)
        assert result == AnswerResult("ok", "complete", None)

    async def test_anthropic_context_window_stop_is_truncated(self) -> None:
        body = _sse(
            _AnthropicWire.text("ok"), *_AnthropicWire.end("model_context_window_exceeded")
        )
        result, _, _ = await _answer(AnthropicProvider(), body)
        assert isinstance(result, AnswerResult) and result.finish == "truncated"

    def test_anthropic_overloaded_and_rate_limit_stream_errors_map_like_http(self) -> None:
        p = AnthropicProvider()
        over = p.classify_error(ProviderFailure("provider_error", detail="overloaded_error"))
        assert over.code == "llm_http" and "overloaded" in over.message
        rate = p.classify_error(ProviderFailure("provider_error", detail="rate_limit_error"))
        assert rate.code == "llm_rate_limit"

    async def test_groq_finish_reason_without_done_is_complete(self) -> None:
        body = _sse(
            _GroqWire.text("ok"),
            json.dumps({"choices": [{"delta": {}, "finish_reason": "stop"}]}),
        )
        result, _, _ = await _answer(GroqProvider(), body)
        assert result == AnswerResult("ok", "complete", "stop")

    def test_groq_rate_limit_stream_error_maps_to_rate_limit(self) -> None:
        err = GroqProvider().classify_error(
            ProviderFailure("provider_error", detail="rate_limit_exceeded")
        )
        assert err.code == "llm_rate_limit"


class TestSecretsStayOutOfReprs:
    @pytest.mark.parametrize(
        "provider", [AnthropicProvider(), GroqProvider()], ids=lambda p: p.id
    )
    def test_request_repr_never_contains_the_key_or_profile(
        self, provider: AnswerProvider
    ) -> None:
        request = provider.build_request(PROMPT, "sk-super-secret-123")
        assert isinstance(request, ProviderRequest)
        text = repr(request)
        assert "sk-super-secret-123" not in text
        assert "Python engineer" not in text  # the resume rides in the body
        assert request.url in text
