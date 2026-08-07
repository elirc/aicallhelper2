"""Prompt construction — provider-independent, byte-stable product behavior.

The prompt is split so Anthropic's cache breakpoint lands after the
resume+JD block: `cached_prefix` (role + profile) | `style_suffix` (style
policy) | `user_message` (the transcript wrapper). Flipping the answer style
must never invalidate the cached prefix, and identical inputs must produce
byte-identical prompts (caching is a byte-prefix match — no timestamps, no
unordered joins).
"""

from __future__ import annotations

from dataclasses import dataclass

ROLE_INSTRUCTIONS = (
    "You are a real-time call assistant helping the user answer questions asked "
    "of them during a live interview or call. You are given a transcript of what "
    "the other person just said. Reply with the answer the user should say, "
    "written in first person, in natural spoken English. Do not add meta "
    "commentary, greetings, or quotation marks — output only the answer itself. "
    "If the transcript contains no real question, briefly suggest what the user "
    "could say next."
)

RESUME_HEADER = "\n\n--- THE USER'S RESUME ---\n"
JOB_HEADER = "\n\n--- THE JOB THEY ARE INTERVIEWING FOR ---\n"

GROUNDING = (
    "\n\nGround every answer in the resume and target role above. Never invent "
    "experience the resume does not support."
)

STYLE_SUFFIXES: dict[str, str] = {
    "brief": (
        "Answer in one or two spoken sentences — the shortest reply that "
        "fully answers the question. No lists, no headings, no lead-in."
    ),
    "balanced": (
        "Be concise and confident: a few sentences for simple questions, "
        "short structured points for complex ones."
    ),
    "detailed": (
        "Give a structured answer: one sentence that answers directly, then "
        "three to five short supporting points (what the situation was, what "
        "you did, what the result was). Keep every point short enough to say "
        "in one breath — this is spoken aloud, not read."
    ),
}


@dataclass(frozen=True)
class PromptParts:
    cached_prefix: str
    style_suffix: str
    user_message: str


def build_prompt(*, resume: str, job_description: str, style: str, transcript: str) -> PromptParts:
    """Build the three prompt parts. Unknown/corrupt style falls back to balanced.

    Resume and JD are trimmed at the edges only — interior formatting
    survives verbatim.
    """
    prefix = ROLE_INSTRUCTIONS
    resume_trimmed = resume.strip()
    jd_trimmed = job_description.strip()
    if resume_trimmed:
        prefix += RESUME_HEADER + resume_trimmed
    if jd_trimmed:
        prefix += JOB_HEADER + jd_trimmed
    if resume_trimmed or jd_trimmed:
        prefix += GROUNDING
    suffix = STYLE_SUFFIXES.get(style, STYLE_SUFFIXES["balanced"])
    user_message = (
        "The other person on the call just said:\n"
        '"""\n'
        f"{transcript}\n"
        '"""\n'
        "\n"
        "What should I say?"
    )
    return PromptParts(cached_prefix=prefix, style_suffix=suffix, user_message=user_message)
