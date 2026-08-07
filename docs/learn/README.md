# Learn this codebase — a curriculum for a junior software engineer

AI Call Assistant v3 is a small app with senior-grade problems inside it:
real-time streaming, cross-thread async, race conditions that were each
"purchased with a real bug", untrusted input at two boundaries, and a
latency budget measured in milliseconds. That makes it an unusually good
training ground: every pattern here exists for a reason you can trace to a
concrete failure.

This folder teaches the project through **six different learning methods**.
Use them in this order the first time through; return to individual pieces
as needed.

| # | Document | Method | Time |
|---|----------|--------|------|
| 1 | [01-guided-tour.md](01-guided-tour.md) | **Guided code reading** — walk the pipeline end to end, file by file, in dependency order | 2–3 h |
| 2 | [02-design-decisions.md](02-design-decisions.md) | **ADR study** — 10 architecture decisions: context, alternatives, consequences, plus how to talk about each in a job interview | 1–2 h |
| 3 | [03-flashcards.md](03-flashcards.md) | **Spaced recall** — 40 Q/A cards over the concepts; quiz yourself across several days | ongoing |
| 4 | [04-spot-the-bug.md](04-spot-the-bug.md) | **Spot-the-bug** — 10 plausible-looking broken versions of real code; find the flaw before reading the answer | 1–2 h |
| 5 | [05-fire-drills.md](05-fire-drills.md) | **Incident simulation** — 8 "user reports X" scenarios; practice forming a diagnosis path before reading the resolution | 1–2 h |
| 6 | [06-exercises.md](06-exercises.md) | **Learning by building** — graded hands-on changes, from "add a style" to "add a whole provider", each with a definition of done | 4–8 h |

Prerequisites: comfortable Python, basic React/TypeScript, and `asyncio`
vocabulary (task, await, cancellation). You do NOT need audio or WebSocket
experience — teaching those through this codebase is the point.

Two companion documents live outside this folder:

- [`../TESTING.md`](../TESTING.md) — every test documented with the failure
  mode it guards. Read a test's bullet BEFORE reading the test; guessing the
  implementation from the guarantee is itself a great exercise.
- [`../../README.md`](../../README.md) — setup, the latency architecture,
  and the "How to add an answer provider" recipe used by exercise 6.

## The one-paragraph mental model

Everything is one pipeline with a state machine in the middle. Audio frames
flow from a Windows loopback device (PortAudio thread) into an asyncio loop
(dedicated thread), out over a WebSocket to Deepgram, which streams text
back; when the user presses Stop, the machine finalizes the transcript,
builds a prompt, and streams an answer from an LLM provider over a
pre-warmed HTTPS connection into the UI (a React app in a WebView2 window,
reached only via a serialized event queue). The session machine
(`app_core/session/machine.py`) owns every rule about what may happen when;
everything around it is either a parser (pure, hostile-input-safe) or an
adapter (Protocol-typed, swappable, faked in tests).
