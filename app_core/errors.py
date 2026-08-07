"""Structured errors shared across the core.

Every failure that crosses the core -> frontend boundary is one of these
codes; the UI keys behavior off the code, so the set is closed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ErrorCode = Literal[
    "no_stt_key",
    "no_llm_key",
    "stt_connect",
    "stt_error",
    "stt_timeout",
    "no_speech",
    "llm_auth",
    "llm_http",
    "llm_rate_limit",
    "llm_first_token_timeout",
    "llm_timeout",
    "aborted",
    "internal",
]


@dataclass(frozen=True)
class AppError(Exception):
    """A user-facing, structured error. `message` is actionable copy, never a raw traceback."""

    code: ErrorCode
    message: str

    def to_payload(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}
