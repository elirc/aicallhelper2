# 13 — Review checklist for changes to this codebase

Every question below is here because its "no" already shipped as a bug —
most in this repo, a few in the v2 build these invariants were purchased
from; the git log's commit messages and `docs/TESTING.md` hold the
post-mortems. Use it as a reviewer: find the section for the area the diff
touches, ask each question against the change, and treat the **red flags**
at the bottom as reasons to slow down regardless of area. It is not a
substitute for running the suites; it is the list of things a green suite
has already failed to catch here once.

---

## Session machine (`app_core/session/machine.py`)

- **Does every await added inside a command or pipeline re-check, on
  resumption, that it still owns what it thinks it owns** (`self._active
  is session`, `session.aborted`, or the command ticket)? A start that
  resolved late used to install itself over a newer winner, and a Record
  stalled on its DPAPI key read superseded the Ask typed after it.
- **If you added a command: does it claim a ticket synchronously before
  its first await, and `_require_ticket` after?** The ticket is the only
  thing keeping latest-command-wins true across the pre-slot awaits
  (ADR-13) — and mutation testing proved a missing check stays green.
- **Is new mutable state touched only on the core loop?** Audio callbacks,
  js_api workers, and the hotkey thread must hand off via
  `call_soon_threadsafe`; ad-hoc cross-thread mutation reintroduces the
  races the single-owner loop exists to remove (ADR-1).
- **Does every new failure path go through `_fail`, not a bare emit?**
  `_fail` enforces one error per stream and silence after abort; `_emit`
  refuses non-error events from a failed session because a delta racing a
  timeout used to be stoppable only by a guard mutation testing showed
  was deletable.
- **Can anything emit after `llm:done` or `session:error` for that
  session?** Rule 9: nothing paints after the terminal event.
- **Is the slot released exactly once on your new exit path?** Rule 11 — a
  leaked slot makes the next session supersede a ghost; a double release
  is masked today only by the `released` flag.
- **Does a new timer or callback re-check the session is still live before
  acting?** `_on_record_cap` checks active/aborted/phase/stop_requested;
  a stale timer firing on a superseded session emits for a dead id.
- **Key checks: do they fail the COMMAND (error Result) before any session
  is created or superseded?** A missing key must never kill the live
  recording the user is still in.

## Providers (`app_core/llm/`)

- **Is `build_request` deterministic, and is the retry fed the same
  object?** The retry must be byte-identical, and Anthropic's prompt cache
  is a byte-prefix match — one drifting byte zeroes the hit rate.
- **Does `classify_error` check `asyncio.CancelledError` FIRST?** An abort
  classified as an HTTP failure shows a scary error for the user's own
  click; `aborted` must win before any HTTP inspection.
- **Is `is_retryable` true ONLY for genuine connect failures?** A read
  timeout waiting for response headers was classified `connect` and
  retried while the server may already have been generating — the doubled
  answer bug. Never retryable: status, stream_drop, timeout, empty_body.
- **Did you add the provider (or change) to the parametrized
  `TestProviderConformance` list?** The conformance suite running against
  every provider is what keeps the shared pipeline vendor-neutral.
- **Is every new error message actionable copy inside the closed
  `ErrorCode` set?** The Groq 404 message names the exact constant to
  update because Groq retires models on short notice; "internal error"
  sent users hunting through logs for a missing audio device.
- **Prompt changes: do the byte-equality tests still pass, and does a
  style flip still leave the cached prefix untouched?** The style policy
  sits AFTER the cache breakpoint precisely so flips are latency-free.
- **Does vendor-specific logic stay inside the vendor's module?** Cache
  breakpoints and reasoning knobs must never leak into the shared
  pipeline (ADR-6).

## STT client (`app_core/stt/`)

- **Does anything write to the socket outside the send queue?**
  CloseStream written directly on the socket overtook queued audio under
  backpressure, and Deepgram discards audio after CloseStream — the tail
  of the question was silently lost. Everything travels the one queue.
- **Can anything follow CloseStream?** A late KeepAlive on the CLOSING
  socket fabricates "lost connection" during a stop that is succeeding.
- **Does finalize leave no pending tasks, even against an unresponsive
  server?** The sender task used to park on the queue forever, pinning
  the socket for the process lifetime.
- **Does a hostile or malformed frame drop instead of raising?** One weird
  frame must not kill a recording; `is_final` must be literally `True` —
  a truthy imposter commits interim text into the transcript the answer
  is grounded in.
- **Is a close before any Results still classified as a connect failure —
  including during finalize?** Record-then-immediately-Stop with a bad
  key used to report "lost the connection while finalizing", sending the
  user to debug their network instead of the key.
- **Does an abort still suppress the socket death it causes?** Rule 1:
  the superseded session's teardown must never surface as an error.

## Settings and secrets (`app_core/store/`)

- **Does a new field fall back individually on corruption?** Whole-file
  fallback destroys the user's resume and keys over one bad byte; the
  per-field rule is THE rule of this store.
- **Do concurrent writers still merge?** A bounds save racing a settings
  patch (drag the window, click Save within half a second) dropped one
  writer's fields from disk AND cache before the writers were
  synchronized.
- **Can any path return decoded garbage instead of "unset"?** Fail
  closed: a foreign-machine DPAPI blob handed to a provider as a bearer
  token is worse than no key at all.
- **Does the settings view still expose only `has*Key` booleans?** The
  frontend never receives key material — serialize the whole view in the
  test to prove it.
- **Is new key/field validation rejected at save time with a message
  naming the cause?** A smart quote in a pasted key used to surface as
  "internal error" mid-answer, deep in httpx.
- **Are omitted-vs-empty semantics preserved?** Omitted means keep, empty
  means clear; breaking this clears stored keys on every unrelated save.
- **Does a failed write leave memory matching disk?** Memory ahead of
  disk means the next successful save silently commits the failed patch.

## Audio (`app_core/audio/`)

- **Are you inside the device callback?** Then: numpy-only work, no
  exception may escape (it propagates into PortAudio's C callback and
  kills the stream with no Python-visible error), and the cost budget is
  real — the whole chain is pinned at a small fraction of real time
  (~1.5% measured) because it runs inside the GIL on PortAudio's thread.
- **Does resampling state carry across chunks — one `StreamingResampler`
  per stream, threaded through `downsample_chunk`?** Per-chunk resampling
  measurably dropped samples (15 985 of 16 000 owed per second), aliased
  (a 10 kHz tone landing near 6 kHz), and changed output with chunk size.
- **If you changed anything about chunking or filtering: is output still
  byte-identical across chunk sizes, and is the speech band still passed
  intact?** Chunk-invariance and <1% speech-band loss are tested
  invariants, not aspirations.
- **Does device failure surface actionably and release the slot?** A
  missing loopback device used to say "internal error"; now it names the
  problem and the next attempt works once the device is back.

## Bridge and shell (`app_core/bridge/`, `app.py`)

- **Did you touch window creation or wiring? Is `sink.attach(window)`
  still reached, and does `tests/test_app_wiring.py` still pass?** The
  attach was missing for FOUR commits: commands worked (js_api is a
  separate channel) while the event pump parked forever — no transcript,
  no answer, no errors — and every suite stayed green because each side
  mocked the other.
- **Does the dispatch path preserve order, and does a failure cost at
  most one event?** Out-of-order deltas scramble the answer text; a
  dropped batch can eat `llm:done` and strand the UI in "Generating
  answer…" forever. Failed batches retry event by event.
- **Is every new payload strictly JSON-serializable — no NaN/Infinity, no
  objects json can't dump?** `allow_nan=False` exists because
  `JSON.parse` rejects non-finite numbers at the page; an unserializable
  payload once killed the pump task outright.
- **Does a new window/OS callback return quickly and never raise?**
  Content-protection retries run on their own thread for this reason, and
  the geometry save swallows everything — especially at shutdown.
- **Is an OS call's result verified where it matters?**
  `SetWindowDisplayAffinity` returns a BOOL that lies; success is the
  affinity read back, and `WDA_MONITOR` is not protection.
- **Does user-facing status distinguish failure causes?** "Invalid" and
  "taken by another app" are different problems; a bare `False` from
  hotkey registration sent people hunting for a conflicting app when they
  had a typo.
- **Do commands validate argument types in the worker thread and return
  error envelopes — never throw across the language boundary?** Raw
  exception text is not user copy.

## Frontend (`frontend/src/`)

- **New event consumption: is the stale-session-id guard still the first
  check, and does pre-adoption buffering cover the new path?** Events
  race the command promise (pywebview resolves promises and delivers
  `evaluate_js` events independently); strictly dropping unknown-session
  events would lose the first partial of an ask, and the buffer must also
  be discarded on a failed start/ask so a later session can't inherit it.
- **Does any refused or failed command destroy work?** The refused stop
  used to cancel a finalizing session and destroy a two-minute answer
  (the 120 s cap race). Recovery must be bounded waiting, not
  cancellation; a captured transcript is retired into history, never
  discarded.
- **Is `aborted` still never shown anywhere?** It means the user's own
  newer action won; rendering it blames them for their click.
- **Does an error keep what already painted?** A `session:error` during a
  streaming answer keeps the partial — erasing it looks like data loss.
- **Optimistic UI: does the persisted value win?** Style chips reflect
  the save's RETURN, because the clicked chip can lie about what the next
  answer will actually use.
- **Do new timers and hints reset across phases and recordings?** The
  silence hint clears the instant audio arrives, disappears at Stop, and
  resets between recordings — and it tracks WHEN audio was last heard,
  not whether it ever was, to catch a device dying mid-question.
- **Non-secure-context paths: does the feature work from `file://`?** The
  packaged app has no `navigator.clipboard`; Copy silently failing only
  in the shipped build is the worst kind of bug.

## Markdown renderer (`frontend/src/markdown/`)

- **Does model text ever become anything but a DOM text node?** No
  attributes, no hrefs, no styles derive from model output; `<ol start>`
  (from parsed digits) is the only attribute any element carries. Links
  are deliberately NOT parsed — that is the anti-XSS design, not an
  omission.
- **Is the parse still a pure function of the full source?** Every cut
  point must render identically to the batch render; any
  boundary-dependent behavior is a latent corruption bug while streaming.
- **Does every new block regex tolerate `\r`?** The `$`-anchored regexes
  couldn't match a trailing `\r`, so a CRLF answer degraded EVERY
  construct — headings, rules, lists, fences — into paragraphs.
- **Is every new recursion or resolution loop capped?** 12 000 asterisks
  overflowed the render stack (React unmounts the whole root), and
  delimiter-dense 18 KB froze the main thread ~59 s. Caps degrade to
  literal text; `MarkdownBoundary` stays as the last line.
- **Does the block signature still uniquely identify content?** The old
  list signature joined item texts with U+001F — a character model output
  can itself contain — so a one-item list holding that character produced
  the same signature as the two-item list it separated. `React.memo` then
  bailed out of a real change and left stale DOM on screen. See
  [07-case-studies.md](07-case-studies.md), case 5.
- **Do parser speedup caches survive splices?** The opener-floor cache
  went stale across delimiter characters and real emphasis silently
  vanished from ordinary prose.
- **Did you pin the normal case alongside the hostile one?** The
  emphasis caps are tested from both sides so a tighter cap cannot
  silently break real answers.

## Tests

- **Does the test assert behavior, not a mock's bookkeeping?** The retry
  guard's test used a failure kind `is_retryable` rejected anyway — the
  retry was skipped for the wrong reason and the guard proved nothing.
- **Would deleting the guard you're testing actually fail the test?**
  Apply the mutation and run the suite; that is exactly how seven
  survivors were found in a fully green 288-test run.
- **Is timing driven by injected clocks and gates, not real sleeps or
  fast local I/O?** The CloseStream ordering test passed only because a
  loopback socket drained synchronously — the race it claimed to test
  could never occur in the test.
- **If the change spans the shell↔core seam, is there a test ON the
  seam?** Core tests fake the sink, bridge tests attach a fake window,
  frontend tests dispatch events by hand — each side mocking the other is
  how a missing `attach` stayed green for four commits.
- **Is defense-in-depth driven directly, not through a sibling that masks
  it?** Two machine guards were unreachable through the loop because
  synchronous sibling checks fired first; they are reachable from the
  real audio thread, which is exactly why they need direct tests.
- **Hostile-input tests: is the corpus realistic in size and encoding?**
  The streaming-invariant corpus once topped out at 70 characters; the
  CRLF and separator-character bugs lived outside it.

## Docs

- **Does every number and claim trace to the repo — a measurement, a
  test, the git log, or code you can cite?** These docs are judged as
  part of the product; an unverifiable claim is drift waiting to happen.
- **Did the behavior you changed get its docs updated in the same
  change?** The refused-stop rework made ADR-4's old "cancel + go idle"
  text wrong until it was fixed; `docs/TESTING.md` gains a bullet for
  every new test, with the failure mode it guards.
- **New deviation from the spec: is it in README's deviations section
  with the reason?** Honest deviations are a feature of this repo;
  silent ones are bugs in the docs.

---

## Red flags — stop and look hard

- **An await added before a claim** (slot or ticket): you just reopened
  the latest-wins race across a suspension point.
- **A new event name**: it needs the stale-id guard, pre-adoption
  buffering, its place in batch ordering, and an answer to "what does
  losing this event cost?" — for `llm:done` the answer was "the UI hangs
  forever".
- **A new AppError code outside the closed set**: the UI keys behavior
  off the code; an unknown code has no behavior anywhere.
- **Model text parsed into anything but a text node**: an attribute or
  href reintroduces the vulnerability class the renderer eliminated.
- **A test asserting on a mock instead of behavior**: seven guards
  survived mutation under a fully green suite for exactly this reason.
- **Any change inside the audio callback**: PortAudio's thread, inside
  the GIL — escaping exceptions kill the stream invisibly, and cost is a
  measured budget with a test on it.
- **Dispatch changes that can reorder or drop events**: scrambled deltas
  scramble the answer; a lost terminal event strands the UI.
- **A "harmless" retry**: after paint it concatenates answers; on status
  it adds load and delay. The only retry is connect-level, pre-delta,
  exactly once, byte-identical.
