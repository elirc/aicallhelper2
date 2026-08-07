"""The SSE parser under hostile chunking.

The invariant test feeds a corpus of streams cut at EVERY byte boundary and
asserts the parsed events always equal the batch parse — including a
multi-byte UTF-8 character split across chunks.
"""

from __future__ import annotations

import typing

from app_core.llm.sse import SSEParser


def parse_all(chunks: list[bytes]) -> list[str]:
    parser = SSEParser()
    events: list[str] = []
    for chunk in chunks:
        events.extend(parser.feed(chunk))
    events.extend(parser.flush())
    return events


class TestBasics:
    def test_single_event_lf(self) -> None:
        assert parse_all([b"data: hello\n\n"]) == ["hello"]

    def test_single_event_crlf(self) -> None:
        assert parse_all([b"data: hello\r\n\r\n"]) == ["hello"]

    def test_single_event_cr_only(self) -> None:
        assert parse_all([b"data: hello\r\r"]) == ["hello"]

    def test_multiple_events(self) -> None:
        assert parse_all([b"data: a\n\ndata: b\n\n"]) == ["a", "b"]

    def test_comment_lines_skipped(self) -> None:
        assert parse_all([b": keep-alive\n\ndata: x\n\n"]) == ["x"]

    def test_non_data_fields_ignored(self) -> None:
        assert parse_all([b"event: message\nid: 3\nretry: 100\ndata: x\n\n"]) == ["x"]

    def test_multi_line_data_joined_with_newline(self) -> None:
        assert parse_all([b"data: a\ndata: b\n\n"]) == ["a\nb"]

    def test_only_one_leading_space_stripped(self) -> None:
        assert parse_all([b"data:  two spaces\n\n"]) == [" two spaces"]
        assert parse_all([b"data:nospace\n\n"]) == ["nospace"]

    def test_final_unterminated_data_line_flushed(self) -> None:
        # A truncated stream must not silently lose the answer's last words.
        assert parse_all([b"data: complete\n\ndata: last words"]) == [
            "complete",
            "last words",
        ]

    def test_unterminated_event_without_blank_line_flushed(self) -> None:
        assert parse_all([b"data: tail\n"]) == ["tail"]

    def test_empty_stream(self) -> None:
        assert parse_all([]) == []
        assert parse_all([b""]) == []


class TestHostileChunking:
    CORPUS: typing.ClassVar[list[bytes]] = [
        b"data: hello\n\ndata: world\n\n",
        b"data: a\r\ndata: b\r\n\r\ndata: c\r\n\r\n",
        b"data: mixed\n\r\ndata: endings\r\r",
        b": comment\r\ndata: x\r\n\r\n",
        b'data: {"delta":{"content":"caf\xc3\xa9 \xe2\x82\xac"}}\n\n',  # multi-byte UTF-8
        b"data: [DONE]\n\ndata: after\n\n",
        b"event: e\ndata: 1\ndata: 2\n\ndata: tail",
        "data: 日本語のテキスト\n\ndata: 終わり\n\n".encode(),
    ]

    def test_every_cut_point_matches_batch(self) -> None:
        for doc in self.CORPUS:
            expected = parse_all([doc])
            for cut in range(len(doc) + 1):
                got = parse_all([doc[:cut], doc[cut:]])
                assert got == expected, f"cut at {cut} in {doc!r}"

    def test_three_way_cuts_on_crlf_heavy_doc(self) -> None:
        doc = b"data: a\r\ndata: b\r\n\r\ndata: caf\xc3\xa9\r\n\r\n"
        expected = parse_all([doc])
        for i in range(0, len(doc) + 1, 3):
            for j in range(i, len(doc) + 1, 5):
                assert parse_all([doc[:i], doc[i:j], doc[j:]]) == expected

    def test_byte_at_a_time(self) -> None:
        doc = b'data: {"text":"\xe2\x82\xac euro"}\r\n\r\ndata: end'
        expected = parse_all([doc])
        got = parse_all([bytes([b]) for b in doc])
        assert got == expected

    def test_split_between_cr_and_lf_no_phantom_blank_line(self) -> None:
        # CR|LF split across chunks must not dispatch the event twice or
        # fabricate an empty line that dispatches early.
        assert parse_all([b"data: a\r", b"\ndata: b\r\n", b"\r\n"]) == ["a\nb"]
