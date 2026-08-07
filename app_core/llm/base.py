"""The answer-provider abstraction — swappability is a product requirement.

Adding or swapping a provider = write one module implementing
`AnswerProvider`, call `register()`, done. The session machine, retry
helper, metrics, events, and every view are provider-agnostic and must not
change. See README "How to add an answer provider".
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from typing import Literal, Protocol, runtime_checkable

import httpx

from app_core.errors import AppError
from app_core.llm.prompt import PromptParts

FailureKind = Literal["connect", "status", "stream_drop", "empty_body"]


@dataclass
class ProviderFailure(Exception):
    """Internal wire-level failure, classified into an AppError by the provider."""

    kind: FailureKind
    status: int | None = None
    body_snippet: str = ""
    detail: str = ""


@dataclass(frozen=True)
class ProviderRequest:
    """The FULL wire request, assembled once and immutable.

    The retry policy (§ retry.py) reuses this object verbatim, which is what
    makes "the retried request is byte-identical" true by construction.
    """

    url: str
    headers: tuple[tuple[str, str], ...]
    body: bytes

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
    ) -> AsyncIterator[str]:
        """Yield answer text deltas; raise ProviderFailure on wire trouble."""
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
