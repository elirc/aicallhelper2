"""The answer-provider abstraction — swappability is a product requirement.

Adding or swapping a provider = write one module implementing
`AnswerProvider`, call `register()`, done. The session machine, retry
helper, metrics, events, and every view are provider-agnostic and must not
change. See README "How to add an answer provider".
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, Protocol, runtime_checkable

from app_core.errors import AppError
from app_core.llm.prompt import PromptParts

if TYPE_CHECKING:
    import httpx

# "connect" is the ONLY retryable kind: it means the request never reached the
# server. "timeout" is deliberately separate — a read timeout waiting for
# response headers means the server may already be working on our request, so
# retrying could produce a second answer and would burn the latency budget.
# The last three are PROVIDER-level failures on an HTTP 200 stream (review
# R03): "provider_error" is an error event inside the stream, "incomplete" is
# a stream that ended without the provider's terminal signal (a clean EOF is
# not completion — the SSE parser deliberately flushes a truncated last
# line), and "empty_answer" is a finished stream with no usable text.
FailureKind = Literal[
    "connect",
    "timeout",
    "status",
    "stream_drop",
    "empty_body",
    "provider_error",
    "incomplete",
    "empty_answer",
]

# How a provider says its answer ended. "truncated" = the output-token cap
# (or context window) cut it off; "refused" = the provider's safety layer
# stopped it. Both still carry whatever text streamed.
Finish = Literal["complete", "truncated", "refused"]


@dataclass
class ProviderFailure(Exception):
    """Internal wire-level failure, classified into an AppError by the provider."""

    kind: FailureKind
    status: int | None = None
    body_snippet: str = ""
    detail: str = ""
    # For "empty_answer": how the (empty) stream ended, so the message can
    # say "cut off" or "declined" instead of a bare "try again".
    finish: Finish | None = None


@dataclass(frozen=True)
class StreamEnd:
    """The provider's terminal signal, yielded LAST by `AnswerProvider.stream`.

    Yielded only after the adapter saw its provider's real end-of-message
    event (Anthropic `message_stop`; OpenAI-style `finish_reason` or
    `[DONE]`). A provider that never yields one is trusted as "complete" by
    retry.py — the shipped adapters always yield one or raise "incomplete".
    """

    finish: Finish = "complete"
    stop_reason: str | None = None  # the provider's raw value, for diagnostics


@dataclass(frozen=True)
class AnswerResult:
    """What `stream_answer` returns: the full text plus how it ended."""

    text: str
    finish: Finish = "complete"
    stop_reason: str | None = None


def empty_answer_message(provider_label: str, failure: ProviderFailure) -> str:
    """User copy for a finished stream that produced no usable text."""
    if failure.finish == "truncated":
        return (
            f"{provider_label} hit its length limit before writing any answer text. "
            "Try again, or ask a shorter question."
        )
    if failure.finish == "refused":
        return f"{provider_label} declined to answer this one. Try rephrasing the question."
    return f"{provider_label} finished without writing an answer. Try again."


@dataclass(frozen=True)
class ProviderRequest:
    """The FULL wire request, assembled once and immutable.

    The retry policy (§ retry.py) reuses this object verbatim, which is what
    makes "the retried request is byte-identical" true by construction.
    """

    url: str
    # repr=False: the headers carry the API key and the body the user's
    # profile; a dataclass repr lands in tracebacks, pytest output and logs.
    headers: tuple[tuple[str, str], ...] = field(repr=False)
    body: bytes = field(repr=False)

    def header_dict(self) -> dict[str, str]:
        return dict(self.headers)


@runtime_checkable
class AnswerProvider(Protocol):
    id: str
    display_name: str
    origin: str

    def build_request(self, prompt: PromptParts, api_key: str) -> ProviderRequest:
        """Assemble the full wire request once. Must be deterministic (byte-stable)."""
        ...

    def stream(
        self, request: ProviderRequest, http: httpx.AsyncClient
    ) -> AsyncIterator[str | StreamEnd]:
        """Yield text deltas, then one StreamEnd once the provider's terminal
        event arrived. Raise ProviderFailure on wire trouble, on an in-stream
        provider error, or when the stream ends without that terminal event."""
        ...

    def classify_error(self, failure: BaseException) -> AppError:
        """Map a failure to the closed error-code set with an actionable message."""
        ...

    def is_retryable(self, failure: BaseException) -> bool:
        """True only for connection-level failures where the server never heard us."""
        ...


@dataclass
class ProviderRegistry:
    """Settings validation and the Settings UI's provider <select> both read this."""

    _providers: dict[str, AnswerProvider] = field(default_factory=dict)

    def register(self, provider: AnswerProvider) -> None:
        self._providers[provider.id] = provider

    def get(self, provider_id: str) -> AnswerProvider | None:
        return self._providers.get(provider_id)

    def ids(self) -> list[str]:
        return list(self._providers)

    def as_mapping(self) -> Mapping[str, AnswerProvider]:
        return dict(self._providers)

    def choices(self) -> list[dict[str, str]]:
        return [
            {"id": p.id, "displayName": p.display_name} for p in self._providers.values()
        ]


def default_registry() -> ProviderRegistry:
    """The shipped providers, in UI order (Anthropic first = recommended default)."""
    from app_core.llm.anthropic import AnthropicProvider
    from app_core.llm.groq import GroqProvider

    registry = ProviderRegistry()
    registry.register(AnthropicProvider())
    registry.register(GroqProvider())
    return registry
