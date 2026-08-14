# 08 — Testing craft: tests that actually hold

This codebase runs 297 core + 148 frontend tests, all green. It also
shipped for four commits with the line that delivers every event to the
page missing, and a mutation pass later proved that 7 of 18 deliberate
defects survived the entire green suite. Both facts are in the git log.
The suites were good — and they still had holes shaped exactly like the
bugs that got through. This document is what closing those holes taught,
with every lesson tied to a test you can open.

## A green test proves nothing until you have seen it fail

A test earns trust the moment it fails *for the right reason* — not
before. A test can pass because the setup never reaches the code under
test, because the assertion is vacuous, or because a sibling check masks
the guard you think you are testing. From the outside all of those look
identical to a test that works.

So this repo's practice is: after writing a fence, re-apply the break and
watch the fence stop it.

- `tests/test_app_wiring.py:60` guards the `sink.attach(window)` call that
  was missing in production. It was verified to fail with the attach
  removed (docs/TESTING.md records this next to the test).
- `TestCloseStreamUnderBackpressure` (`tests/test_deepgram_client.py:500`)
  guards CloseStream ordering. It was verified to fail when the queue
  sentinel is replaced by a direct `ws.send` — the exact bug it exists to
  prevent.
- The mutation-pass commit closes seven holes and says of each fix:
  "verified by re-applying the mutation."

If you cannot make a test fail by breaking the code it names, you have
not written a test for that code. You have written a test near it.

## Mutation testing: eighteen mutations, seven survivors

Mutation testing inverts the usual question. Instead of "does the suite
pass?", it asks "if I plant a specific defect, does anything notice?"
Here it was done by hand: eighteen single defects — a deleted guard, a
reverted fix — applied one at a time against the green suite, the suite
run, the tree restored and verified pristine. Eleven mutations were
caught. Seven were not: with the defect in place, all 288 then-current
core tests stayed green. Every survivor was a guard that *looked*
tested. Two are worth dwelling on.

### The retry guard the test named but never reached

The retry policy in `app_core/llm/retry.py:50` is one conjunction:

```python
if attempt == 1 and not got_delta and provider.is_retryable(exc):
    continue
```

`test_never_after_a_delta` existed precisely to protect `not got_delta` —
a retry after a painted delta concatenates two answers in the UI. But the
test made the stream fail with a `stream_drop` failure, and
`is_retryable` returns True only for `connect`. So when the mutation
deleted `not got_delta`, the retry was still skipped — by `is_retryable`,
for a different reason — and the test passed while proving nothing about
the guard it was named after.

The fixed test (`tests/test_retry.py:60`) fails after the first delta
with a `connect` failure: every other condition in the conjunction is now
satisfied *toward* retrying, so `got_delta` is the only thing standing
between the suite and a doubled answer. That is the general rule for
testing one leg of a conjunction: choose inputs that satisfy all the
other legs, or a sibling condition will absorb the mutation and lie to
you.

### The ordering test the loopback socket was passing for you

CloseStream must leave the process after every queued audio frame,
because Deepgram discards audio that arrives after it — the tail of the
question, lost at the exact moment the user pressed Stop. A test asserted
that ordering and passed. It also passed when the code was mutated to
write CloseStream directly on the socket, bypassing the queue entirely.

Why: on a fast loopback socket the sender coroutine drains the queue
synchronously before `finalize()` even runs. By the time CloseStream
could jump the queue, the queue was always empty. The environment
resolved the race the same way every time, so the test exercised the
scheduler's habits, not the code's ordering guarantee.

The replacement (`tests/test_deepgram_client.py:500`) manufactures the
contention: it monkeypatches `ws.send` to stall the *first* write until
released, queues twelve frames behind it, starts `finalize()` while the
sender is parked, then releases. In that window a direct send would
visibly overtake — a comment even marks the line where it would
escape — and the assertion `order[-1] == "close"` catches it. A race test
that does not construct the race is testing your machine, not your code.

### The other five, compressed

- **`ask()`'s `_require_ticket` was deletable** with all 288 tests green.
  The only ticket test stalls `start_session`, so `ask`'s copy of the
  check was never load-bearing. `tests/test_machine.py:627` mirrors the
  scenario with a stalled *ask*. Copies of a guard need copies of the
  test.
- **The app's content-protection path had zero coverage.** No Python
  test referenced `_apply_content_protection` or the `protection:ok` /
  `protection:failed` verdicts it emits — the moat feature's retry and
  stamping, unreferenced by the core suite. Before trusting coverage of
  a feature, grep for its symbols; a name no test mentions is a name no
  test protects.
- **`f²` was accepted back** when `isdecimal()`+`isascii()` was reverted
  to `isdigit()`: the never-raise parser test's candidate list simply had
  no `f<superscript>` input (`tests/test_hotkey.py:119` now has five). An
  adversarial corpus is only as strong as its inventory.
- **Two defense-in-depth guards were masked by synchronous siblings** —
  `_on_frame`'s `stop_requested` check and `_emit`'s `errored` check. No
  loop-driven test can separate them from the sibling that runs in the
  same synchronous block, but a real audio thread delivers frames into
  exactly that window. `tests/test_machine.py:655` and `:673` set the
  machine's state directly and drive the guard. A guard only reachable
  from a foreign thread is *more* in need of a direct test, not less —
  no integration path will ever wander into it.

## Passing for the wrong reason: the delete question

The cheapest audit you can run on any test: *if I delete the line I
believe this tests, does it still pass?* This repo found tests lying in
four distinct ways:

- **Tautology.** The old KeepAlive-after-CloseStream test's fake server
  returned on CloseStream — so nothing could ever be recorded after it
  and the assertion could not fail. The replacement's handler
  deliberately keeps reading (`tests/test_deepgram_client.py:471`).
- **Under-constrained assertion.** Asserting `subprotocol == "token"`
  still passes if the API key is silently dropped from the offer; the
  test at `tests/test_deepgram_client.py:451` asserts the key string
  itself appears in the header.
- **Environmental accident.** The loopback drain above.
- **Wrong rejection reason.** The retry fake above: the code took the
  early exit, just not through the condition under test.

All four are invisible in a green run. Only breaking things exposes them.

## The seam both sides mocked

The worst bug in this repo's history was in no component at all. It was
in the one line joining two correct components, and it was missing for
four commits with every suite green.

`sink.attach(window)` connects the core's event pump to the pywebview
window. Core tests use a fake sink; bridge tests attach a fake window
themselves; frontend tests dispatch `app:event` by hand. Each suite faked
its neighbour faithfully, so the attach call belonged to no suite — and
nothing showed in the real app while the commands, travelling a separate
channel, kept working. A second bug had the same shape: the core and the
frontend disagreed about what a refused stop *means*, and no test owned
that contract either. [07-case-studies.md](07-case-studies.md) tells both
stories in full.

What 08 takes from them is the procedure. `tests/test_app_wiring.py` now
owns the first seam with three tests: the sink is attached, every window
callback is subscribed, and — end to end — a `sink.emit` arrives as a
`window.evaluate_js` call carrying the payload. That third one is the
real fence, because it fakes neither side of the joint.

So: enumerate your seams. For each, name the test that exercises the real
joint with neither side faked. If you cannot name it, that seam is
untested no matter how green both sides are.

## Invariants beat examples for parsers and streams

Three of the strongest tests in the repo assert a *property over all
inputs in a dimension* instead of an expected output for one input:

- **SSE: every cut point** (`tests/test_sse.py:77`). A corpus of eight
  hostile documents (CRLF, comments, `[DONE]`, multi-byte UTF-8), each
  split at *every* byte offset, must parse to exactly the events of the
  batch parse. `test_byte_at_a_time` (`:91`) goes to the pathological
  minimum, including a `€` split mid-encoding.
- **Markdown: streaming equals batch**
  (`frontend/src/__tests__/markdown-render.test.tsx:139`). For every
  prefix length of every corpus document, rendering the prefix and then
  the full text into one reused root must produce DOM identical to a
  single batch render. A sibling test just renders every prefix and
  asserts nothing but "does not throw" (`:153`) — half-typed markdown is
  the *normal* streaming case.
- **Resampler: chunk invariance** (`tests/test_downsample.py`). Output
  must be byte-identical whether the device delivers chunks of 480, 1024,
  6000, or 7777 samples. The old per-chunk resampler failed this
  measurably: up to 2.0 of waveform error on a unit sine, 15,985 samples
  delivered where 16,000 were owed per second, and a 10 kHz tone folded
  into the speech band (now ~59 dB down).

Why these are worth more than example tests: the axis they quantify over
— where the network or device slices the byte stream — is exactly the
axis production varies and no hand-picked example can enumerate. And the
oracle is not a hand-written expectation but the system's own batch
behavior, so the corpus grows without rewriting expected outputs. That
mattered: the original markdown corpus topped out at 78 characters with
no CRLF, so a cut landing between `\r` and `\n` was never exercised
until the corpus was extended — the invariant machinery caught up the
moment the inventory did.

## Adversarial corpora

Where input is untrusted, this repo keeps a hostile inventory and asserts
*structurally*:

- The XSS suite (`markdown-render.test.tsx:81`): eight payloads —
  `<script>`, `<img onerror>`, a fence-escape, attribute-injection
  quotes, `<svg onload>`, `<iframe>`. The assertion is not "this payload
  didn't execute"; it is that zero live elements exist and *no element
  anywhere carries any attribute* beyond `<ol start>`. A global
  structural assertion catches whole classes of payload, including ones
  not in the list yet.
- Hostile chunking at the provider level
  (`tests/test_providers.py:106`): chunk sizes 1, 2, 3, and 7 across
  "café €50 done" — multi-byte characters split at every awkward place.
- Elsewhere: 50,000-deep JSON nesting (RecursionError inside
  `json.loads` must not kill the reader), `"ß".upper() == "SS"` in the
  hotkey parser, `f٢` mapping to a real F-key.

Note the honest scope: the fuzzing here is *exhaustive enumeration over
small domains* — every cut point, every byte boundary, every payload in
a curated list — not random generation. Deterministic, so every failure
reproduces, and each new bug adds its input to the inventory forever.

## Fakes with knobs, not mocks

The core suite never uses `unittest.mock.patch`. It uses two scriptable
fakes (`tests/conftest.py:92` and `:147`) that implement the same
Protocols production wires — `FakeStt` with `connect_gate`,
`finalize_gate`, `connect_error`; `FakeProvider` with `pre_delta_hang`,
`post_delta_hang`, `failures`, `fail_after_first_delta`. The payoff is
that a *scenario* is one line:

- "stop while finalize is stuck" —
  `h.stt.prepare = lambda s: setattr(s, "finalize_gate", finalize_gate)`
- "provider never yields a token" —
  `provider.pre_delta_hang = asyncio.Event()` (never set)
- "connection drops after the first painted delta" —
  `provider.fail_after_first_delta = ProviderFailure("connect")`

Assertions then land on observable behavior — the events `CollectSink`
gathered, in order — not on "was method X called with Y". Mock-patching
couples tests to import paths and call sequences; a knobbed fake couples
them to the Protocol, which is the thing production actually depends on.
And because the fakes sit at the same seams production uses, drift
between fake and real interface is a type error, not a silent divergence.

## Time and concurrency without flakes

Almost nothing in either suite sleeps and hopes. The techniques, each
bought with a real flake or a real impossibility:

- **Injected Timeouts.** The harness runs the machine with a 0.35 s
  first-token timeout and 0.8 s total (`tests/conftest.py:246`), so the
  full timeout-interplay matrix runs in real milliseconds instead of the
  product's 10 s / 60 s values.
- **Injected clock.** The pre-warm throttle test steps a `Clock` object
  to exactly 1.9 and then 2.0 seconds (`tests/test_warm.py:41`). A real
  clock can never assert "1.9 s is throttled, 2.0 s is not" — the
  boundary is only testable because time is a parameter.
- **Wait for signals that mean ready.** A latent frontend flake:
  `renderApp` awaited the status line, which paints *before* settings
  arrive, so settings-dependent assertions raced the load and failed only
  under parallel-suite load. The fix was not a retry — it was a true
  readiness signal: the style chips reflect the PERSISTED style, so
  `waitForSettings` waits for `aria-pressed` on a chip
  (`frontend/src/__tests__/testutils.tsx:82`). Thirteen consecutive full
  runs green after the fix.
- **Distinguish late from wrong.** `asyncUtilTimeout` is 5 s
  (`frontend/vitest.setup.ts:9`) because rAF is a ~16 ms timer in jsdom
  that starves when eleven suites run in parallel on a slow machine; the
  1 s default expired on work that was merely late, not wrong.
- **Gates, not sleeps.** The fakes' knobs are `asyncio.Event`s: the test
  decides exactly when a connect resolves or a finalize unsticks. Where
  real time is unavoidable, `Harness.drain` polls against a deadline and
  returns the moment the condition holds.

## The checklist

Apply to any new test before trusting it:

1. Have you seen it fail? Break the code it guards and confirm the
   failure points at the break.
2. If you delete the line you think you are testing, does it still pass?
   Then it tests something else.
3. Testing one leg of a conjunction? Choose inputs that satisfy every
   *other* leg toward failure, so the leg under test is the only defense.
4. Ordering or race test? Does the test construct the contention, or is
   a fast local environment resolving it for you?
5. Does the assertion state an invariant ("nothing follows CloseStream",
   "no attributes anywhere") rather than one example's expected output?
6. For a parser or stream: is there a cut-point / chunk-invariance test,
   and does the corpus contain the inputs that actually hurt (CRLF,
   multi-byte splits, realistic length)?
7. For every seam between two suites: which test exercises the real
   joint with neither side faked? Name it or write it.
8. Is time injected? Real sleeps only when measuring real time; waits
   should watch a signal that *means* ready, not a proxy that paints
   early.
9. Does anything reference the symbol at all? Zero references means zero
   protection, whatever the coverage number says.
10. Write its docs/TESTING.md bullet: what failure mode does this guard?
    If you cannot say, the test is decoration.

Most examples above are a bullet in `docs/TESTING.md` with the bug they
were purchased by. Read the bullet before the test; guessing the
implementation from the guarantee is itself good practice.
