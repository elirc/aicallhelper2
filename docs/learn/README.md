# Learn this codebase — a curriculum for a junior software engineer

AI Call Assistant v3 is a small app with senior-grade problems inside it:
real-time streaming, cross-thread async, race conditions that were each
"purchased with a real bug", untrusted input at three boundaries, and a
latency budget measured in milliseconds. That makes it an unusually good
training ground: every pattern here exists for a reason you can trace to a
concrete failure — and the failures are real, proved by measurement, a
mutation-testing pass, and four adversarial audit rounds recorded in the
git log.

> **Which version this describes.** The curriculum was written against the
> 2026-09-18 code (v3.1.0, commit `c6191b5` plus that pass). The
> 2026-09-22 production-readiness pass changed several mechanisms it
> teaches; where a chapter and `docs/ARCHITECTURE.md` disagree, ARCHITECTURE
> is current. Most chapters quote the older code deliberately (the bugs and
> their fixes are the lesson), so they were not rewritten; the factual
> Q&A (flashcards, performance, security) was updated. What changed:
>
> - **Stop drains audio.** "Rule 4" is now a *capture cutoff*: frames are
>   accepted until the Stop drain has delivered every pre-Stop sample (on a
>   dedicated audio worker thread, bounded at 2 s), then rejected. The
>   latency clock still starts at Stop acceptance, before the drain; a new
>   `audioDrainMs` metric splits the drain out of `sttFinalizeMs`.
> - **The 120 s cap starts when capture is running**, and the new
>   `session:recording {deadlineMs, capMs}` event drives the UI countdown.
> - **Provider completion is validated.** Eight `FailureKind`s (added
>   `provider_error`, `incomplete`, `empty_answer`); `llm:done` carries
>   `finish`; an empty answer is a `session:error`, never a blank success.
>   The Groq 404 message no longer asks users to edit source.
> - **Keys fail closed.** DPAPI failure fails the save; there is no new
>   `plain:` fallback (legacy values still decode).
> - **Event dispatch** runs on bounded dispatch threads (not
>   `asyncio.to_thread`) with `seq`/`pageGen` stamps, a bounded queue and
>   re-sent reserved events; the page resynchronizes from `get_status()`.
> - **Protection status** is tri-state and shown in every view.
> - **Refused-stop recovery** is disarmed by progress and scoped to its
>   session (the fixed 20 s recovery described in 02/07/10 is history).
> - **Settings** preserve an unreadable file as a `.bak` before any write
>   and support a save-revision precondition.

The curriculum is thirteen documents in two layers: a **core sequence**
(01–06, six different learning methods — do these in order the first time
through) and **deep dives** (07–13, one topic each — read on demand, in
any order, after the tour).

## The core sequence

| # | Document | Method | Time |
|---|----------|--------|------|
| 1 | [01-guided-tour.md](01-guided-tour.md) | **Guided code reading** — walk the pipeline end to end, file by file, in dependency order | 2–3 h |
| 2 | [02-design-decisions.md](02-design-decisions.md) | **ADR study** — 16 architecture decisions: context, alternatives, consequences, plus how to talk about each in a job interview | 1–2 h |
| 3 | [03-flashcards.md](03-flashcards.md) | **Spaced recall** — 84 Q/A cards over the concepts, grouped by topic; quiz yourself across several days | ongoing |
| 4 | [04-spot-the-bug.md](04-spot-the-bug.md) | **Spot-the-bug** — 19 plausible-looking broken versions of real code; find the flaw before reading the answer | 1–2 h |
| 5 | [05-fire-drills.md](05-fire-drills.md) | **Incident simulation** — 15 "user reports X" scenarios; practice forming a diagnosis path before reading the resolution | 1–2 h |
| 6 | [06-exercises.md](06-exercises.md) | **Learning by building** — graded hands-on changes, from "add a style" to "add a whole provider", each with a definition of done | 4–8 h |

## The deep dives

| # | Document | Topic | Time |
|---|----------|-------|------|
| 7 | [07-case-studies.md](07-case-studies.md) | The audits' biggest finds as full post-mortems — symptom, hunt, root cause, fix, and the test that now guards each | 1–2 h |
| 8 | [08-testing-craft.md](08-testing-craft.md) | What a wiring gap that survived a green suite and 7-of-18 surviving mutations taught about writing tests that actually hold | 1–2 h |
| 9 | [09-audio-and-dsp.md](09-audio-and-dsp.md) | The audio path and the signal processing behind it, taught from zero against the real resampler — every number measured | 2 h |
| 10 | [10-concurrency.md](10-concurrency.md) | The single-owner loop model in depth: every thread, every crossing, and the rule protecting each — the deep version of ADR-1 | 1–2 h |
| 11 | [11-security-model.md](11-security-model.md) | The threat model: what the app protects, against what, by which mechanism — and, just as plainly, what it does not | 1 h |
| 12 | [12-performance.md](12-performance.md) | The ~1 second stop-to-first-word budget: where the milliseconds go and which decisions exist to protect them | 1 h |
| 13 | [13-review-checklist.md](13-review-checklist.md) | A review checklist distilled from what the audits caught — what to check before merging a change to this codebase | 30 min, then reference |

Prerequisites: comfortable Python, basic React/TypeScript, and `asyncio`
vocabulary (task, await, cancellation). You do NOT need audio or WebSocket
experience — teaching those through this codebase is the point.

Four companion documents live outside this folder:

- [`../TESTING.md`](../TESTING.md) — every test documented with the failure
  mode it guards. Read a test's bullet BEFORE reading the test; guessing
  the implementation from the guarantee is itself a great exercise.
- [`../ARCHITECTURE.md`](../ARCHITECTURE.md) — the system-level map of the
  pipeline, the module boundaries, and the seams between them.
- [`../TROUBLESHOOTING.md`](../TROUBLESHOOTING.md) — symptom-first
  diagnosis for a running app; the operational counterpart to the fire
  drills.
- [`../../README.md`](../../README.md) — setup, the latency architecture,
  and the "How to add an answer provider" recipe used by exercise 3.1.

## Reading paths

**"I have one hour."** Read the mental model below, then three stops of
[01-guided-tour.md](01-guided-tour.md): Stop 0 (the latency constraint),
Stop 8 (the session machine), and Stop 12 (the shell seam that shipped
broken for four commits). Close with the "After the tour" questions at
the end of that file — a wrong answer tells you exactly where to come
back.

**"I am joining this project."** The core sequence in order, 01 through
06, with 03's flashcards running alongside as a daily habit. Read
[`../ARCHITECTURE.md`](../ARCHITECTURE.md) between 01 and 02, keep
[`../TESTING.md`](../TESTING.md) open while doing 06's exercises (its
per-test bullets are the acceptance criteria culture here), and read
[13-review-checklist.md](13-review-checklist.md) before your first real
change.

**"I want to get better at testing."** Start with
[08-testing-craft.md](08-testing-craft.md) — this repo is a case study in
how a fully green suite — 414 tests at the time — coexisted with a
shipped app that displayed nothing.
Then [04-spot-the-bug.md](04-spot-the-bug.md) — for each flaw you find,
also name the test that would have caught it — then
[07-case-studies.md](07-case-studies.md) for the full arcs, then do 06's
exercises under its rule that every change updates the tests and
`../TESTING.md`.

**"I want to understand the concurrency."** ADR-1 in
[02-design-decisions.md](02-design-decisions.md) for the decision, then
[10-concurrency.md](10-concurrency.md) for the full thread map, then
Stops 8 and 9 of [01-guided-tour.md](01-guided-tour.md) with
`app_core/session/machine.py` and `app_core/stt/client.py` open —
tracing the supersession and stop-vs-cap races by hand is the exercise.
Finish with the Concurrency group of [03-flashcards.md](03-flashcards.md)
and drills from [05-fire-drills.md](05-fire-drills.md).

**"I am preparing for an interview."** The "say it in an interview"
sections of [02-design-decisions.md](02-design-decisions.md) out loud,
all 84 cards of [03-flashcards.md](03-flashcards.md) spaced over a week,
and [05-fire-drills.md](05-fire-drills.md) spoken as if walking an
interviewer through the diagnosis. Then mine
[07-case-studies.md](07-case-studies.md) for stories with real numbers in
them — "our suites were green while the app shipped broken, here is why"
and "our resampler dropped 15 of every 16,000 samples and I can explain
the fix" are the kind of specific, true stories interviews reward.

## The one-paragraph mental model

Everything is one pipeline with a state machine in the middle. Audio
frames flow from a Windows loopback device (PortAudio thread) through a
phase-continuous streaming resampler into an asyncio loop (dedicated
thread), out over a WebSocket to Deepgram, which streams text back; when
the user presses Stop, the machine finalizes the transcript, builds a
byte-stable prompt, and streams an answer from an LLM provider over a
pre-warmed HTTPS connection into the UI (a React app in a WebView2
window). The session machine (`app_core/session/machine.py`) owns every
rule about what may happen when; everything around it is either a parser
(pure, hostile-input-safe) or an adapter (Protocol-typed, swappable,
faked in tests). The UI is reached over exactly two one-way channels:
commands travel frontend → core through pywebview's `js_api` and return
Result envelopes; events travel core → frontend through one serialized,
batched queue — and the single line attaching that queue to the window is
the line the shipped app was missing for four commits, which is why the
seam between the channels has a test suite of its own.
