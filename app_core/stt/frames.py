"""Pure parsing of Deepgram live-transcription frames.

Deepgram's stream is hostile input as far as this app is concerned: frames
may be malformed, fields may hold the wrong type, and `is_final` may be a
truthy imposter (1, "true"). A wrong parse silently corrupts the transcript
the answer is grounded in, so every rule here is strict.
"""

from __future__ import annotations

import json
from dataclasses import dataclass


@dataclass(frozen=True)
class TranscriptSegment:
    """One Results frame's transcript. `is_final` means Deepgram committed it."""

    text: str
    is_final: bool


@dataclass(frozen=True)
class SttErrorDetail:
    """One Error frame's human-readable detail, quoted into the surfaced message."""

    detail: str


ParsedFrame = TranscriptSegment | SttErrorDetail | None


def parse_frame(raw: str | bytes) -> ParsedFrame:
    """Parse one text frame. Returns None for anything that must be ignored.

    Ignored: Metadata, UtteranceEnd, unknown types, malformed JSON,
    pathologically nested JSON (RecursionError), non-dict payloads, Results
    frames whose transcript is missing/non-string or whose channel shape is
    wrong. Ignoring (not crashing) is the contract: one weird frame must not
    kill a recording.
    """
    try:
        data = json.loads(raw)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    frame_type = data.get("type")
    if frame_type == "Results":
        return _parse_results(data)
    if frame_type == "Error":
        return _parse_error(data)
    return None


def _parse_results(data: dict[str, object]) -> TranscriptSegment | None:
    channel = data.get("channel")
    if not isinstance(channel, dict):
        return None
    alternatives = channel.get("alternatives")
    if not isinstance(alternatives, list) or not alternatives:
        return None
    first = alternatives[0]
    if not isinstance(first, dict):
        return None
    transcript = first.get("transcript")
    if not isinstance(transcript, str):
        return None
    # `is_final` must be literally True — truthy imposters (1, "true") are
    # interim. `x is True` rejects 1 because bool identity, not equality.
    is_final = data.get("is_final") is True
    return TranscriptSegment(text=transcript, is_final=is_final)


def _parse_error(data: dict[str, object]) -> SttErrorDetail:
    # Two shapes exist in the wild: v1 listen {description, message, variant}
    # and newer {code, description}. Quote whatever detail is present.
    parts: list[str] = []
    for key in ("code", "description", "message", "variant"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
        elif isinstance(value, int | float) and not isinstance(value, bool):
            parts.append(str(value))
    return SttErrorDetail(detail=" — ".join(parts) if parts else "unknown error")


class TranscriptAccumulator:
    """Full transcript = committed prefix (each non-empty final appended) + latest interim.

    The committed prefix grows incrementally — O(appended text) per final,
    never a re-join of the whole recording per message.
    """

    def __init__(self) -> None:
        self._committed = ""
        self._interim = ""

    def apply(self, segment: TranscriptSegment) -> str:
        if segment.is_final:
            text = segment.text.strip()
            if text:
                self._committed = f"{self._committed} {text}" if self._committed else text
            # A final (even an empty one) supersedes whatever interim preceded it.
            self._interim = ""
        else:
            self._interim = segment.text.strip()
        return self.text

    @property
    def text(self) -> str:
        if self._interim:
            return f"{self._committed} {self._interim}" if self._committed else self._interim
        return self._committed
