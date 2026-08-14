# 06 — Exercises: learn by changing the code

Graded, hands-on, each with a definition of done. Do them on a branch.
The rule for every exercise: **write or update the tests as part of the
change** — the suites (`pytest`, `npm test`, `ruff`, `mypy --strict`,
`tsc`) must be green at the end, and `docs/TESTING.md` gets a bullet for
any new test. That rule is not busywork; it's the workflow this repo was
built with.

---

## Level 1 — Read and verify (30 min each)

### 1.1 Break it on purpose
Change `is True` to `== True`… wait — `1 == True` is `True` in Python, so
change it to `bool(data.get("is_final"))` in `frames.py`. Run pytest.
**Done when:** you can name the exact test that catches it
(`test_is_final_must_be_literally_true`) and explain the user-visible
symptom it prevents.

### 1.2 Watch a race fail
In `machine.py`, move `self._active = session` in `start_session` to after
`self._spawn(...)` returns — actually, move the claim INTO `_run_record`
after the connect await. Run the machine tests.
**Done when:** you can list every test that fails and map each to a §5
rule number.

### 1.3 Trace one delta
Write (on paper) the full path of one answer token: httpx chunk →
`SSEParser.feed` → `extract_anthropic_delta` → `stream_answer`'s
`on_delta` → `_emit` → queue → `evaluate_js` → CustomEvent →
`processEvent` → rAF flush → reducer → `<Markdown>`.
**Done when:** your trace names the thread/loop each hop runs on.

## Level 2 — Small features (1–2 h each)

### 2.1 Add a fourth answer style
Add `"interview-story"`: a STAR-format style suffix. Touch:
`prompt.py` (suffix), `settings.py` (`ANSWER_STYLES`), the frontend chips
and Settings select, and every test that enumerates styles.
**Done when:** the chip appears, persists, and — the real point — the
byte-stability test still proves a style flip never touches the cached
prefix.

### 2.2 Add an `llm:usage` event
After a done answer, emit token usage if the provider surfaced it (extend
`stream()` to also capture a final usage object — note this changes the
provider seam, so start in `base.py` and let the type checker walk you to
every site). Render it as a subtle chip next to the latency chip.
**Done when:** both fake-provider machine tests and a frontend test cover
it, and a provider WITHOUT usage data degrades to "no chip", not a crash.

### 2.3 Make the level meter honest about clipping
`rms_level` can't tell "loud" from "clipping". Add a `clipped: bool` to
the audio:level payload (true if any sample is at int16 min/max), style
the meter red when clipping.
**Done when:** a numpy test proves detection at exactly ±32767/-32768 and
a frontend test proves the class flip.

## Level 3 — The provider exercise (3–4 h)

### 3.1 Add an OpenAI provider end-to-end
Follow README "How to add an answer provider" for real:
`app_core/llm/openai.py` (`id="openai"`, chat-completions API — clone
`groq.py`, reuse `wire.py` + the exported helpers), register it, add it to
the conformance parametrize list, write its specifics class (status
matrix; no reasoning knobs — `gpt-4.1-mini` isn't a reasoning model).
**Done when:** conformance passes for all THREE providers; the Settings
select shows it; `hasOpenaiKey` appears in the view WITHOUT you touching
settings code (that's the registry doing its job); and you never edited
`machine.py`, `retry.py`, or any frontend file except none at all — if you
had to, the seam leaked and THAT's the lesson.

## Level 4 — Invariant work (the deep end, 3–6 h)

### 4.1 Add a "pause recording" feature without breaking §5
Design first, code second: a `pause_session`/`resume_session` command
pair. Write the invariants BEFORE implementing (what happens to frames
while paused? keepalive? the 120 s cap? stop-while-paused?
supersede-while-paused?). Then implement + test each invariant.
**Done when:** you have a written invariant list reviewed against every
existing §5 rule, and machine tests for each — including at least one
race (pause during connect).

### 4.2 Kill a flake before it exists
The machine tests use real sleeps for watchdogs (small but real). Convert
the timeout tests to a fake clock/controllable timer injected via
`Timeouts` — without changing production behavior.
**Done when:** the timeout tests pass with zero real sleeping and the
production default path is untouched (`clock` already injects; the timers
are the hard part — you'll need to abstract `loop.call_later` /
`asyncio.sleep` behind a small Protocol).

## Level 5 — Systems work (the expert end, 4–8 h each)

These are the exercises that would be real tickets on a real team. Each
one either extends a seam or builds tooling the project genuinely lacks.
Design on paper first; the definition of done is the spec.

### 5.1 Property-test the resampler against a reference
`TestStreamingResampler` pins four chunk sizes and a handful of tones.
Generalize it into a seeded property test: random signals (white noise
plus a few random-frequency sines), random source rates (16000, 44100,
48000, 96000), and random chunkings — including 1-sample chunks and
prime-sized runs. The reference implementation is the resampler itself
fed the WHOLE signal through one instance; the property is that any
chunking is byte-identical to that. Add two analytic properties on top:
an in-band sine (≤3 kHz) survives with <1% RMS error, and an out-of-band
tone (≥10 kHz at a 48 kHz source) is suppressed by ≥50 dB.
**Done when:** the test is seeded (zero flake risk), runs in under ~2 s,
and you have VERIFIED it goes red under two hand-applied mutations:
(a) break the phase carry (`self._pos = next_pos - keep_from` →
`self._pos = 0.0`) and (b) stop carrying `self._fir_state` between
chunks. If either mutation survives, the property is weaker than you
think — tighten it before merging.

### 5.2 Surface a mid-recording device change as an error
Today, unplugging the output device (or Windows switching the default)
mid-recording leaves the capture stream bound to a dead endpoint
delivering silence — only the silence hint notices, and only from the
symptom. Add real detection: a watcher (polling the default render
device's identity ~1 s on a background thread is fine) that reports a
device change to the machine. This extends the `AudioSource` seam in
`machine.py`, so design the Protocol change first and let
`mypy --strict` walk you to every production implementation (it checks
`app_core` and `app.py` only, so the test fakes are on you). Decide the
policy BEFORE coding, in §5 terms: a device death mid-recording should
behave like an STT mid-recording death — one actionable `session:error`
("The audio device changed — start the recording again"), teardown, slot
released — and must be IGNORED after `stop_requested` (the transcript is
already complete; killing the finalize would destroy user work, which is
exactly the Drill-10 shape).
**Done when:** a machine test with a fake AudioSource fires the change
mid-recording and asserts one error + released slot + nothing painted
after; a second test fires it after `stop_requested` and asserts the
answer still arrives; the silence hint still covers the cases the watcher
can't see (muted call, wrong device from the start); and `docs/TESTING.md`
documents both tests with their failure modes.

### 5.3 Make the markdown parser incremental — behind the invariant
`parseBlocks` re-parses the full source on every rAF flush. Make it
incremental: cache the blocks that can no longer change and re-parse only
from the last STABLE boundary. The hard part is defining "stable" — a
trailing list can be extended by a later line (loose lists), an
unterminated fence consumes everything after it, and a paragraph can
absorb the next line. Derive the rule (hint: a blank line followed by
already-parsed non-tail blocks is a safe cut; the last one or two blocks
never are), then hide the whole thing behind the existing streaming
invariant: every cut point of every corpus document must render
byte-identically to a batch render, with the suite UNTOUCHED.
**Done when:** the invariant suite passes unmodified (it is the fence —
if you had to edit it, the optimization changed behavior); a new test
proves the work is O(tail) by injecting a line-scan counter and streaming
a ~20 KB document (total scanned lines must grow linearly, not
quadratically); and `blockSignature`-based memoization still gets DOM
identity across updates (the "completed blocks keep their DOM nodes"
test stays green).

### 5.4 Add a latency trace to the metrics
The chip reports three numbers; a slow answer needs more resolution. Add
a `trace` object to the `llm:done` metrics with monotonic stage stamps
measured by the machine's injected clock: `stopRequested`,
`transcriptFinal`, `requestStarted`, `firstDelta`, `done` (the ask path
reports no STT stages). Additive only — the event shape grows, nothing
existing moves, and the prompt stays byte-stable (timestamps live in
metrics, NEVER in the prompt; `test_byte_stable_across_calls` is the
tripwire you must not trip).
**Done when:** a machine test with an injected fake clock asserts exact
stage values and monotonic ordering for both the record and ask paths;
the chip tooltip renders the split behind a frontend test; a provider
that never streams a delta produces a trace whose `firstDelta` equals
`done` (the Q67 honesty rule, extended); and `docs/TESTING.md` gets the
bullets.

### 5.5 Build a replay harness for the whole pipeline
The tests exercise every piece; nothing exercises the whole pipe with
REAL audio. Write `tools/replay.py`: read a WAV file (any rate/channels),
convert with the real `downsample_chunk` + `StreamingResampler` in
~125 ms device-style chunks, and drive a real `SessionManager` wired to
the scripted loopback Deepgram server from `tests/test_deepgram_client.py`
and a scripted provider over `httpx.MockTransport` — then print the live
partials, the final transcript, the answer, and the metrics. Everything
runs offline; the point is that ONLY composition code is new. You are
rebuilding app.py's wiring with fakes at the two network edges — which
makes this exercise a direct probe of the seam that shipped broken for
four commits (case study 1).
**Done when:** `.venv\Scripts\python tools\replay.py fixture.wav` runs
offline and deterministically (scripted transcript out of scripted
Results frames); it uses the production `SessionManager`,
`DeepgramStream`, retry, and SSE classes unmodified; the output shows all
three metrics; and if you had to edit anything under `app_core/` to
compose it, you stop and write down which seam leaked — that finding is
worth more than the tool.

---

## Working rules (apply to every exercise)

1. Red first: see the new test fail before making it pass.
2. `ruff` + `mypy --strict` + `tsc` stay clean — the type checker walking
   you through a seam change (2.2, 3.1, 5.2) is a feature, not friction.
3. Every behavior you add gets a `docs/TESTING.md` bullet saying WHY it
   exists, matching the house style.
4. If an exercise forces you to touch a file the exercise says you
   shouldn't need, stop and figure out which seam leaked — that insight is
   worth more than finishing.
5. Level 5 only: before coding, write the invariant list or the
   definition of "stable"/"done" in prose and check it against §5 and the
   existing tests. The mutation pass proved this codebase's guards can
   LOOK tested while protecting nothing — your new ones start with the
   same burden of proof.
