"""Prompt construction — provider-independent, byte-stable product behavior.

The prompt is split so Anthropic's cache breakpoint lands after the
profile block: `cached_prefix` (call-type role + resume + JD + focus +
notes + grounding) | `style_suffix` (style policy) | `user_message` (the
transcript wrapper). Flipping the answer style must never invalidate the
cached prefix, and identical inputs must produce byte-identical prompts
(caching is a byte-prefix match — no timestamps, no unordered joins).

Call types: the role instructions, the heading over the job-description
block, and the grounding rule all depend on WHAT KIND of call this is. A
behavioral interview wants STAR stories grounded in the resume; a technical
screen wants the fact answered on its merits (the resume cannot ground "how
does React reconciliation work"); a sales call must never invent pricing.
Everything call-type-specific lives in the cached prefix because it is
stable per profile; the style suffix stays call-type-independent so it can
sit after the breakpoint.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CallTypeSpec:
    label: str
    role: str
    jd_header: str
    grounding: str


_PREAMBLE = (
    "You are a real-time call assistant helping the user during a live call. You "
    "are given a transcript of what the other person just said. Reply with exactly "
    "what the user should say next, written in first person, in natural spoken "
    "English. Do not add meta commentary, greetings, or quotation marks — output "
    "only the words to say."
)

_BEHAVIORAL_ROLE = (
    "You are a real-time call assistant helping the user answer questions asked "
    "of them during a live interview or call. You are given a transcript of what "
    "the other person just said. Reply with the answer the user should say, "
    "written in first person, in natural spoken English. Do not add meta "
    "commentary, greetings, or quotation marks — output only the answer itself. "
    "If the transcript contains no real question, briefly suggest what the user "
    "could say next. Answer with a specific example from the user's own "
    "experience whenever the question invites one, and make the outcome concrete "
    "when the resume gives one. Structure for longer answers: what the situation "
    "was, what you did, what the result was."
)

CALL_TYPES: dict[str, CallTypeSpec] = {
    "behavioral": CallTypeSpec(
        label="Behavioral interview",
        role=_BEHAVIORAL_ROLE,
        jd_header="THE JOB THEY ARE INTERVIEWING FOR",
        grounding=(
            "\n\nGround every answer in the resume and target role above. Never invent "
            "experience the resume does not support."
        ),
    ),
    "technical": CallTypeSpec(
        label="Technical screen",
        role=(
            _PREAMBLE + " This is a technical screening interview. Answer the technical "
            "question directly and correctly first, using the precise names of the "
            "APIs, data structures, or language features involved, then add the one "
            "tradeoff or edge case a senior engineer would mention. Prefer the tools "
            "and stack named in the focus section; if a question is about a "
            "technology you have not used, say how you would approach it rather than "
            "bluffing. If the transcript is a coding problem, state the approach and "
            "its time and space complexity, not a full code listing. If the "
            "transcript contains no real question, briefly suggest a clarifying "
            "question the user could ask. Structure for longer answers: the direct "
            "answer, how it works, when you would and would not use it."
        ),
        jd_header="THE JOB THEY ARE INTERVIEWING FOR",
        grounding=(
            "\n\nUse the resume, role, and focus above to choose which technologies and "
            "examples to lead with, but answer the technical question on its merits — "
            "technical facts do not need to come from the resume. Never claim hands-on "
            "experience the resume does not support."
        ),
    ),
    "system_design": CallTypeSpec(
        label="System design",
        role=(
            _PREAMBLE + " This is a system design interview. Treat the transcript as a "
            "design prompt or a follow-up on one. Start by naming the one or two "
            "requirements or constraints that drive the design, then propose the "
            "components and how data flows between them, then name the main tradeoff "
            "and what you would change at ten times the scale. Ask one clarifying "
            "question when the requirements are genuinely ambiguous rather than "
            "assuming. If the transcript contains no real question, briefly suggest "
            "the next part of the design the user could walk through. Structure for "
            "longer answers: requirements and constraints, core components and data "
            "flow, the key tradeoff, how it scales or fails."
        ),
        jd_header="THE JOB THEY ARE INTERVIEWING FOR",
        grounding=(
            "\n\nDraw on the systems and scale described in the resume and focus above "
            "for concrete examples, and never claim to have built something the resume "
            "does not support."
        ),
    ),
    "recruiter": CallTypeSpec(
        label="Recruiter screen",
        role=(
            _PREAMBLE + " This is a recruiter or HR screening call. Keep answers short, "
            "warm, and positive: one or two sentences that confirm interest, summarize "
            "fit, or give a straight logistics answer (availability, location, work "
            "authorization, notice period). For compensation questions give a range "
            "or defer to the full process — never a single number unless the notes "
            "say otherwise. If the transcript contains no real question, briefly "
            "suggest a question the user could ask the recruiter about the process or "
            "the team. Structure for longer answers: the direct answer, one sentence "
            "of relevant background, one sentence of enthusiasm for the role."
        ),
        jd_header="THE JOB THEY ARE INTERVIEWING FOR",
        grounding=(
            "\n\nGround every answer in the resume and target role above. Never invent "
            "experience or credentials the resume does not support."
        ),
    ),
    "sales": CallTypeSpec(
        label="Sales or customer call",
        role=(
            _PREAMBLE + " This is a sales, customer, or client call where the user "
            "represents their company or product. Work out what the other person is "
            "really asking for — a feature, a price, reassurance, a next step — and "
            "reply with the answer that moves the conversation forward: address an "
            "objection with a specific benefit, answer a factual question plainly, or "
            "propose the next concrete step. Never invent pricing, capabilities, or "
            "commitments; when the notes do not cover a detail, say you will confirm "
            "it and move on. If the transcript contains no real question, briefly "
            "suggest a discovery question the user could ask. Structure for longer "
            "answers: acknowledge their point, the specific answer or benefit, the "
            "next step."
        ),
        jd_header="ABOUT THIS CALL",
        grounding=(
            "\n\nGround every claim in the notes and background above. Never invent "
            "pricing, features, customers, or commitments they do not support."
        ),
    ),
    "meeting": CallTypeSpec(
        label="General meeting",
        role=(
            _PREAMBLE + " This is a general work meeting or discussion, not an "
            "interview. Reply with the most useful contribution the user could make "
            "right now: answer the question if one was asked, otherwise offer the one "
            "clarifying question, decision, or next step the discussion needs. Keep it "
            "collegial and concrete. If the transcript contains no real question, "
            "briefly suggest what the user could say next. Structure for longer "
            "answers: the point, the reason, the proposed next step."
        ),
        jd_header="ABOUT THIS MEETING",
        grounding=(
            "\n\nUse the background and notes above for context. Never invent facts, "
            "decisions, or commitments they do not support."
        ),
    ),
}

DEFAULT_CALL_TYPE = "behavioral"

# Backward-compatible aliases: the behavioral call type IS the original
# single-purpose prompt, so the old names still point at the shipped text.
ROLE_INSTRUCTIONS = CALL_TYPES[DEFAULT_CALL_TYPE].role
GROUNDING = CALL_TYPES[DEFAULT_CALL_TYPE].grounding

RESUME_HEADER = "\n\n--- THE USER'S RESUME ---\n"
JOB_HEADER = "\n\n--- THE JOB THEY ARE INTERVIEWING FOR ---\n"
FOCUS_HEADER = "\n\n--- FOCUS FOR THIS CALL ---\n"
NOTES_HEADER = "\n\n--- THE USER'S NOTES ---\n"

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
        "three to five short supporting points following the structure for "
        "longer answers given in the call guidance above. Keep every point short "
        "enough to say in one breath — this is spoken aloud, not read."
    ),
}


@dataclass(frozen=True)
class PromptParts:
    cached_prefix: str
    style_suffix: str
    user_message: str


def call_type_choices() -> list[dict[str, str]]:
    """The call types in UI order, for the settings view (never hard-coded in the UI)."""
    return [{"id": key, "label": spec.label} for key, spec in CALL_TYPES.items()]


def build_prompt(
    *,
    resume: str,
    job_description: str,
    style: str,
    transcript: str,
    call_type: str = DEFAULT_CALL_TYPE,
    focus: str = "",
    notes: str = "",
) -> PromptParts:
    """Build the three prompt parts. Unknown/corrupt style or call type falls back.

    Profile text is trimmed at the edges only — interior formatting survives
    verbatim. Section order is fixed (role, resume, JD, focus, notes,
    grounding) so identical inputs produce identical bytes.
    """
    spec = CALL_TYPES.get(call_type, CALL_TYPES[DEFAULT_CALL_TYPE])
    prefix = spec.role
    resume_trimmed = resume.strip()
    jd_trimmed = job_description.strip()
    focus_trimmed = focus.strip()
    notes_trimmed = notes.strip()
    if resume_trimmed:
        prefix += RESUME_HEADER + resume_trimmed
    if jd_trimmed:
        prefix += "\n\n--- " + spec.jd_header + " ---\n" + jd_trimmed
    if focus_trimmed:
        prefix += FOCUS_HEADER + focus_trimmed
    if notes_trimmed:
        prefix += NOTES_HEADER + notes_trimmed
    if resume_trimmed or jd_trimmed or focus_trimmed or notes_trimmed:
        prefix += spec.grounding
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
