"""The seams every core module agrees on — and nothing else.

This module exists so the light parts of the core (the settings store, the
STT client, the bridge) can import the Protocols and value types they need
WITHOUT pulling in the session state machine, which in turn pulls in httpx
and the provider stack. Keeping this module dependency-free is what lets
the shell create the window before any heavy import has run; see app.py
`App._build_core`.

Nothing here may import httpx, websockets, numpy, or webview.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from app_core.errors import AppError
from app_core.llm.prompt import DEFAULT_CALL_TYPE

OnSttUpdate = Callable[[str, bool], None]
OnSttError = Callable[[AppError], None]

DEEPGRAM_SECRET_ID = "deepgram"


class SttStream(Protocol):
    async def connect(self) -> None: ...

    def send(self, pcm: bytes) -> None: ...

    async def finalize(self) -> str: ...

    async def abort(self) -> None: ...


SttFactory = Callable[[str, OnSttUpdate, OnSttError], SttStream]


class AudioSource(Protocol):
    """Every method may block on the device: the session machine calls them
    on its single audio worker thread, never on the core loop."""

    def start(self, on_frame: Callable[[bytes, float], None]) -> None: ...

    def stop(self) -> None:
        """Stop and discard undelivered audio (cancel/supersede/teardown)."""
        ...

    def stop_and_drain(self) -> None:
        """Stop at the capture cutoff and deliver every pre-cutoff sample
        (including a final short frame) to `on_frame` before returning."""
        ...


class EventSink(Protocol):
    def emit(self, name: str, payload: dict[str, object]) -> None: ...


class Warmer(Protocol):
    def warm(self, origin: str) -> object: ...


@dataclass(frozen=True)
class AnswerConfig:
    """Everything the answer pipeline reads from settings, snapshotted once.

    `call_type`, `focus` and `notes` come from the ACTIVE profile (see
    app_core/store/settings.py) and land in the cached prompt prefix; the
    style is global and lands after the cache breakpoint.
    """

    resume: str
    job_description: str
    style: str
    provider_id: str
    call_type: str = DEFAULT_CALL_TYPE
    focus: str = ""
    notes: str = ""


class SettingsReader(Protocol):
    def answer_config(self) -> AnswerConfig:
        """Memory-only read — safe to call on the loop."""
        ...

    def get_secret(self, secret_id: str) -> str | None:
        """May hit DPAPI — callers wrap in asyncio.to_thread."""
        ...
