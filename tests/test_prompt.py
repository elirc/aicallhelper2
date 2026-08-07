"""Prompt byte-stability and the cache-split rule."""

from __future__ import annotations

from app_core.llm.prompt import ROLE_INSTRUCTIONS, STYLE_SUFFIXES, build_prompt


class TestSections:
    def test_bare_prompt_is_role_only(self) -> None:
        parts = build_prompt(resume="", job_description="", style="balanced", transcript="q")
        assert parts.cached_prefix == ROLE_INSTRUCTIONS
        assert "Ground every answer" not in parts.cached_prefix

    def test_resume_section_appended_with_grounding(self) -> None:
        parts = build_prompt(
            resume="10 years of Python", job_description="", style="balanced", transcript="q"
        )
        assert "--- THE USER'S RESUME ---\n10 years of Python" in parts.cached_prefix
        assert parts.cached_prefix.endswith(
            "Ground every answer in the resume and target role above. Never invent "
            "experience the resume does not support."
        )

    def test_jd_section_appended(self) -> None:
        parts = build_prompt(
            resume="", job_description="Senior SRE", style="balanced", transcript="q"
        )
        assert "--- THE JOB THEY ARE INTERVIEWING FOR ---\nSenior SRE" in parts.cached_prefix
        assert "RESUME" not in parts.cached_prefix

    def test_edge_trim_only_interior_formatting_survives(self) -> None:
        resume = "  line1\n\n  * bullet\nline3  "
        parts = build_prompt(resume=resume, job_description="", style="brief", transcript="q")
        assert "line1\n\n  * bullet\nline3" in parts.cached_prefix

    def test_user_message_wrapper_verbatim(self) -> None:
        parts = build_prompt(resume="", job_description="", style="brief", transcript="Why us?")
        assert parts.user_message == (
            'The other person on the call just said:\n"""\nWhy us?\n"""\n\nWhat should I say?'
        )


class TestStyles:
    def test_three_styles(self) -> None:
        for style in ("brief", "balanced", "detailed"):
            parts = build_prompt(resume="", job_description="", style=style, transcript="q")
            assert parts.style_suffix == STYLE_SUFFIXES[style]

    def test_unknown_style_falls_back_to_balanced(self) -> None:
        for bad in ("", "verbose", "BRIEF", "42"):
            parts = build_prompt(resume="", job_description="", style=bad, transcript="q")
            assert parts.style_suffix == STYLE_SUFFIXES["balanced"]


class TestCacheContract:
    def test_byte_stable_across_calls(self) -> None:
        kwargs = {
            "resume": "R" * 500,
            "job_description": "J" * 300,
            "style": "detailed",
            "transcript": "Tell me about a challenge.",
        }
        a = build_prompt(**kwargs)
        b = build_prompt(**kwargs)
        assert a == b
        assert a.cached_prefix.encode() == b.cached_prefix.encode()

    def test_style_flip_never_touches_cached_prefix(self) -> None:
        # The style policy sits AFTER the cache breakpoint: flipping styles
        # must be latency-free, never invalidating the cached resume+JD.
        prefixes = {
            build_prompt(
                resume="res", job_description="jd", style=style, transcript="q"
            ).cached_prefix
            for style in ("brief", "balanced", "detailed")
        }
        assert len(prefixes) == 1

    def test_transcript_lives_outside_the_system_prompt(self) -> None:
        a = build_prompt(resume="res", job_description="jd", style="brief", transcript="q1")
        b = build_prompt(resume="res", job_description="jd", style="brief", transcript="q2")
        assert a.cached_prefix == b.cached_prefix
        assert a.style_suffix == b.style_suffix
        assert a.user_message != b.user_message
