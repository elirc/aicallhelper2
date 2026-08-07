"""Deepgram frame parsing under hostile input, and transcript accumulation."""

from __future__ import annotations

import json

from app_core.stt.frames import (
    SttErrorDetail,
    TranscriptAccumulator,
    TranscriptSegment,
    parse_frame,
)


def results(transcript: object, is_final: object = False) -> str:
    return json.dumps(
        {
            "type": "Results",
            "is_final": is_final,
            "channel": {"alternatives": [{"transcript": transcript}]},
        }
    )


class TestParseResults:
    def test_interim_and_final(self) -> None:
        assert parse_frame(results("hello", False)) == TranscriptSegment("hello", False)
        assert parse_frame(results("hello", True)) == TranscriptSegment("hello", True)

    def test_is_final_must_be_literally_true(self) -> None:
        # Truthy imposters are interim — 1, "true", [] would corrupt commits.
        for imposter in (1, "true", [1], {"a": 1}, 1.0):
            frame = parse_frame(results("hi", imposter))
            assert frame == TranscriptSegment("hi", False)

    def test_non_string_transcript_ignored(self) -> None:
        for bad in (None, 5, ["x"], {"t": "x"}):
            assert parse_frame(results(bad, True)) is None

    def test_missing_or_null_channel_ignored(self) -> None:
        assert parse_frame(json.dumps({"type": "Results", "is_final": True})) is None
        assert (
            parse_frame(json.dumps({"type": "Results", "channel": None})) is None
        )
        assert (
            parse_frame(json.dumps({"type": "Results", "channel": "nope"})) is None
        )

    def test_empty_alternatives_ignored(self) -> None:
        frame = json.dumps({"type": "Results", "channel": {"alternatives": []}})
        assert parse_frame(frame) is None
        frame = json.dumps({"type": "Results", "channel": {"alternatives": ["str"]}})
        assert parse_frame(frame) is None

    def test_malformed_json_ignored_never_crash(self) -> None:
        for garbage in ("", "{", "not json", "\x00\x01", "[1,2", '{"type":'):
            assert parse_frame(garbage) is None

    def test_pathologically_nested_json_ignored(self) -> None:
        nested = "[" * 50_000 + "]" * 50_000
        assert parse_frame(nested) is None

    def test_non_dict_payloads_ignored(self) -> None:
        for payload in ("[]", "3", '"Results"', "null", "true"):
            assert parse_frame(payload) is None

    def test_other_frame_types_ignored(self) -> None:
        assert parse_frame(json.dumps({"type": "Metadata", "duration": 4})) is None
        assert parse_frame(json.dumps({"type": "UtteranceEnd"})) is None
        assert parse_frame(json.dumps({"type": "SpeechStarted"})) is None


class TestParseError:
    def test_v1_listen_shape(self) -> None:
        frame = json.dumps(
            {"type": "Error", "description": "bad thing", "message": "detail", "variant": "x"}
        )
        parsed = parse_frame(frame)
        assert isinstance(parsed, SttErrorDetail)
        assert "bad thing" in parsed.detail
        assert "detail" in parsed.detail

    def test_newer_code_description_shape(self) -> None:
        parsed = parse_frame(json.dumps({"type": "Error", "code": "DATA-0001", "description": "d"}))
        assert isinstance(parsed, SttErrorDetail)
        assert "DATA-0001" in parsed.detail

    def test_error_with_no_detail(self) -> None:
        parsed = parse_frame(json.dumps({"type": "Error"}))
        assert isinstance(parsed, SttErrorDetail)
        assert parsed.detail == "unknown error"


class TestAccumulator:
    def test_committed_prefix_plus_interim(self) -> None:
        acc = TranscriptAccumulator()
        assert acc.apply(TranscriptSegment("tell me", False)) == "tell me"
        assert acc.apply(TranscriptSegment("tell me about", False)) == "tell me about"
        assert acc.apply(TranscriptSegment("Tell me about yourself.", True)) == (
            "Tell me about yourself."
        )
        assert acc.apply(TranscriptSegment("What", False)) == "Tell me about yourself. What"
        assert acc.apply(TranscriptSegment("What else?", True)) == (
            "Tell me about yourself. What else?"
        )

    def test_empty_final_clears_interim_but_commits_nothing(self) -> None:
        acc = TranscriptAccumulator()
        acc.apply(TranscriptSegment("stray interim", False))
        assert acc.apply(TranscriptSegment("", True)) == ""
        assert acc.text == ""

    def test_whitespace_final_ignored(self) -> None:
        acc = TranscriptAccumulator()
        acc.apply(TranscriptSegment("   ", True))
        assert acc.text == ""

    def test_final_supersedes_interim(self) -> None:
        acc = TranscriptAccumulator()
        acc.apply(TranscriptSegment("hel", False))
        acc.apply(TranscriptSegment("hello there", True))
        assert acc.text == "hello there"
