"""Prompt byte-stability and the cache-split rule."""

from __future__ import annotations

from app_core.llm.prompt import (
    CALL_TYPES,
    DEFAULT_CALL_TYPE,
    ROLE_INSTRUCTIONS,
    STYLE_SUFFIXES,
    build_prompt,
    call_type_choices,
)


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


class TestCallTypes:
    """Call-type tailoring lives in the CACHED prefix; the style suffix stays
    call-type independent so it can sit after the cache breakpoint."""

    def test_default_call_type_is_behavioral_and_is_the_original_prompt(self) -> None:
        implicit = build_prompt(resume="", job_description="", style="brief", transcript="q")
        explicit = build_prompt(
            resume="", job_description="", style="brief", transcript="q", call_type="behavioral"
        )
        assert implicit == explicit
        assert DEFAULT_CALL_TYPE == "behavioral"
        assert CALL_TYPES["behavioral"].role == ROLE_INSTRUCTIONS

    def test_unknown_call_type_falls_back_to_behavioral(self) -> None:
        for bad in ("", "SALES", "42", "technical "):
            parts = build_prompt(
                resume="", job_description="", style="brief", transcript="q", call_type=bad
            )
            assert parts.cached_prefix == ROLE_INSTRUCTIONS

    def test_every_call_type_has_a_distinct_role_and_grounding(self) -> None:
        roles = {spec.role for spec in CALL_TYPES.values()}
        groundings = {spec.grounding for spec in CALL_TYPES.values()}
        assert len(roles) == len(CALL_TYPES) == 6
        assert len(groundings) == len(CALL_TYPES)
        # test_jd_section_appended asserts the literal "RESUME" is absent from a
        # JD-only prefix; no role or grounding text may reintroduce it.
        for spec in CALL_TYPES.values():
            assert "RESUME" not in spec.role and "RESUME" not in spec.grounding

    def test_choices_follow_definition_order_with_labels(self) -> None:
        choices = call_type_choices()
        assert [c["id"] for c in choices] == list(CALL_TYPES)
        assert choices[0] == {"id": "behavioral", "label": "Behavioral interview"}
        assert all(c["label"] for c in choices)

    def test_sections_land_in_a_fixed_order(self) -> None:
        parts = build_prompt(
            resume="RES",
            job_description="JD",
            style="brief",
            transcript="q",
            call_type="technical",
            focus="FOC",
            notes="NOT",
        )
        prefix = parts.cached_prefix
        order = [
            prefix.index(CALL_TYPES["technical"].role),
            prefix.index("--- THE USER'S RESUME ---\nRES"),
            prefix.index("--- THE JOB THEY ARE INTERVIEWING FOR ---\nJD"),
            prefix.index("--- FOCUS FOR THIS CALL ---\nFOC"),
            prefix.index("--- THE USER'S NOTES ---\nNOT"),
            prefix.index(CALL_TYPES["technical"].grounding.strip()),
        ]
        assert order == sorted(order)
        assert prefix.endswith(CALL_TYPES["technical"].grounding)

    def test_grounding_appended_when_only_focus_or_only_notes_present(self) -> None:
        only_focus = build_prompt(
            resume="", job_description="", style="brief", transcript="q", focus="React"
        )
        only_notes = build_prompt(
            resume="", job_description="", style="brief", transcript="q", notes="say hi"
        )
        assert only_focus.cached_prefix.endswith(CALL_TYPES["behavioral"].grounding)
        assert only_notes.cached_prefix.endswith(CALL_TYPES["behavioral"].grounding)

    def test_focus_and_notes_are_edge_trimmed_only(self) -> None:
        parts = build_prompt(
            resume="",
            job_description="",
            style="brief",
            transcript="q",
            focus="  a\n  b  ",
            notes="\n* x\n",
        )
        assert "--- FOCUS FOR THIS CALL ---\na\n  b\n" in parts.cached_prefix
        assert "--- THE USER'S NOTES ---\n* x\n" in parts.cached_prefix

    def test_sales_and_meeting_use_the_call_context_header(self) -> None:
        sales = build_prompt(
            resume="",
            job_description="Acme renewal",
            style="brief",
            transcript="q",
            call_type="sales",
        )
        meeting = build_prompt(
            resume="",
            job_description="Sprint plan",
            style="brief",
            transcript="q",
            call_type="meeting",
        )
        assert "--- ABOUT THIS CALL ---\nAcme renewal" in sales.cached_prefix
        assert "--- ABOUT THIS MEETING ---\nSprint plan" in meeting.cached_prefix
        assert "INTERVIEWING" not in sales.cached_prefix

    def test_call_type_and_focus_live_in_the_prefix_never_the_suffix(self) -> None:
        suffixes = set()
        prefixes = set()
        for call_type in CALL_TYPES:
            for focus in ("", "Python"):
                parts = build_prompt(
                    resume="r",
                    job_description="j",
                    style="detailed",
                    transcript="q",
                    call_type=call_type,
                    focus=focus,
                )
                suffixes.add(parts.style_suffix)
                prefixes.add(parts.cached_prefix)
        assert len(suffixes) == 1, "the style suffix must not vary with the call type"
        assert len(prefixes) == len(CALL_TYPES) * 2

    def test_detailed_suffix_defers_structure_to_the_call_guidance(self) -> None:
        assert "call guidance above" in STYLE_SUFFIXES["detailed"]
        assert "situation" not in STYLE_SUFFIXES["detailed"]
        assert "situation" in CALL_TYPES["behavioral"].role  # STAR stays behavioral
