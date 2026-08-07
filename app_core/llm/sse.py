"""Incremental server-sent-events parser operating on raw bytes.

Requirements this parser exists to meet (each one a real failure mode):
- chunks may split at ANY byte boundary — mid-line, mid-JSON, between
  ``\\r`` and ``\\n``, or mid multi-byte UTF-8 character;
- CR, LF, and CRLF line endings all occur;
- comment/keep-alive lines (``: ...``) must be skipped;
- a final un-terminated ``data:`` line at end of stream must be flushed —
  a truncated stream otherwise silently loses the answer's last words.

We parse from bytes with an incremental UTF-8 decoder rather than relying on
httpx's text iteration, so a multi-byte character split across chunks
survives.
"""

from __future__ import annotations

import codecs


class SSEParser:
    """Feed raw bytes, get back completed event data payloads (in order)."""

    def __init__(self) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._line_buf = ""
        self._data_lines: list[str] = []
        self._pending_cr = False

    def feed(self, chunk: bytes) -> list[str]:
        text = self._decoder.decode(chunk)
        if not text:
            return []
        return self._feed_text(text)

    def flush(self) -> list[str]:
        """End of stream: flush the decoder, any partial line, and any open event."""
        events: list[str] = []
        tail = self._decoder.decode(b"", True)
        if tail:
            events.extend(self._feed_text(tail))
        self._pending_cr = False
        if self._line_buf:
            self._handle_line(self._line_buf, events)
            self._line_buf = ""
        if self._data_lines:
            events.append("\n".join(self._data_lines))
            self._data_lines = []
        return events

    def _feed_text(self, text: str) -> list[str]:
        events: list[str] = []
        if self._pending_cr:
            # The CR already terminated a line last chunk; a leading LF now is
            # the second half of that CRLF, not a new (empty) line.
            if text.startswith("\n"):
                text = text[1:]
            self._pending_cr = False
            if not text:
                return events
        buf = self._line_buf + text
        start = 0
        i = 0
        n = len(buf)
        while i < n:
            ch = buf[i]
            if ch == "\n":
                self._handle_line(buf[start:i], events)
                i += 1
                start = i
            elif ch == "\r":
                self._handle_line(buf[start:i], events)
                if i + 1 < n:
                    i += 2 if buf[i + 1] == "\n" else 1
                else:
                    self._pending_cr = True
                    i += 1
                start = i
            else:
                i += 1
        self._line_buf = buf[start:]
        return events

    def _handle_line(self, line: str, events: list[str]) -> None:
        if line == "":
            # Blank line dispatches the accumulated event, if any.
            if self._data_lines:
                events.append("\n".join(self._data_lines))
                self._data_lines = []
            return
        if line.startswith(":"):
            return  # comment / keep-alive
        field, sep, value = line.partition(":")
        if not sep:
            field, value = line, ""
        if field != "data":
            return  # event/id/retry are irrelevant to these APIs
        if value.startswith(" "):
            value = value[1:]
        self._data_lines.append(value)
