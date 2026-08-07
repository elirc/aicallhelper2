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

---

## Working rules (apply to every exercise)

1. Red first: see the new test fail before making it pass.
2. `ruff` + `mypy --strict` + `tsc` stay clean — the type checker walking
   you through a seam change (2.2, 3.1) is a feature, not friction.
3. Every behavior you add gets a `docs/TESTING.md` bullet saying WHY it
   exists, matching the house style.
4. If an exercise forces you to touch a file the exercise says you
   shouldn't need, stop and figure out which seam leaked — that insight is
   worth more than finishing.
