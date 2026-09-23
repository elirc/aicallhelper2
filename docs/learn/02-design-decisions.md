# 02 — Design decisions (ADR-style, with interview translation)

Sixteen decisions in ADR form: **Context → Decision → Alternatives →
Consequences**, plus **"Say it in an interview"** — a 30-second spoken
version, because being able to *explain* a design is a separate skill from
building it. ADR-1 through ADR-10 date from the original build; ADR-11
through ADR-16 were purchased by the hardening, audit, and mutation-testing
passes that followed (the git log's commit messages are the post-mortems).

---

## ADR-1: One asyncio loop on a dedicated thread owns all session state

**Context.** Inputs arrive on at least four foreign threads: PortAudio's
audio callback, pywebview's js_api worker threads, the hotkey message-loop
thread, and timer threads. Session state (which session is live, whether it
stopped) is read and written on every one of those paths.

**Decision.** One `asyncio` event loop runs forever on a dedicated thread.
ALL session state lives there. Foreign threads may only hand work across
via `loop.call_soon_threadsafe` / `asyncio.run_coroutine_threadsafe` —
never mutate shared state directly.

**Alternatives.** (a) Locks around shared state — every new field needs
lock discipline, and the interleavings that matter here (superseding a
session mid-connect) are exactly the ones locks make hard to reason about.
(b) Thread-per-concern with queues — more threads, same serialization,
more ceremony.

**Consequences.** Race analysis reduces to "what order do callbacks run on
the loop", which is testable single-threaded. The cost: any blocking call
on the loop freezes the whole pipeline, so file I/O and DPAPI go through
`asyncio.to_thread`, and the audio callback must stay tiny.

**Say it in an interview.** "I used a single-owner concurrency model:
one event loop owns all mutable session state, and every other thread —
audio callbacks, UI bridge workers, hotkeys — posts messages into it. That
turned data races into ordering questions I could unit-test without
threads, at the cost of being disciplined about never blocking the loop."

---

## ADR-2: The session machine is a plain module with Protocol-injected deps

**Context.** The machine encodes eleven invariants that were each a real
bug in v2. They involve networks, audio devices, and time — all miserable
to test directly.

**Decision.** `SessionManager` depends only on Protocols: `SttStream`,
`AnswerProvider`, `AudioSource`, `EventSink`, `Warmer`, `SettingsReader`,
plus injectable `Timeouts` and a clock. Production wires real adapters;
pytest wires scriptable fakes.

**Alternatives.** Mock-patching real classes (`unittest.mock.patch`) —
couples tests to import paths and internals; fakes with knobs
(`connect_gate`, `finalize_gate`, `post_delta_hang`) express *scenarios*
("stop while finalize is stuck") in one line.

**Consequences.** All 11 invariants + their races run in seconds with zero
network. The seam also forced clean layering: the machine literally cannot
reach into Deepgram-specific or Anthropic-specific code.

**Say it in an interview.** "I hexagonal-architected the core: the state
machine sees only ports — an STT stream, an answer provider, an event sink
— and every wire client is an adapter. The payoff was testing eleven
concurrency invariants, including double-press races, with deterministic
fakes instead of mocks or live services."

---

## ADR-3: Claim the active-session slot BEFORE the first await

**Context.** STT connect is a network round-trip. The user can press
Record again while the first press is still connecting. If the first
session installs itself after its await resolves, it can overwrite the
newer session — the "latest-start-wins" bug class.

**Decision.** `start_session` sets `self._active = session` synchronously,
then spawns the connect as a task. When any await resolves, the code
re-checks `self._active is session`; a loser tears down silently.

**Alternatives.** A lock/queue serializing starts — still needs the
post-await re-check; version counters — equivalent, more machinery.

**Consequences.** The winner is decided at press time, not at
connect-completion time. Every resumption point needs the staleness check
— which is exactly what the tests pin. One nuance the audit exposed: the
key reads precede the slot claim (a missing key must fail the COMMAND, not
a session), so the pre-slot awaits need their own claim — the command
ticket, ADR-13.

**Say it in an interview.** "Classic check-then-act race across an await:
I made the claim synchronous — before any suspension point — and made
every resumption re-validate that it still owns the slot. It's optimistic
concurrency applied to UI sessions."

---

## ADR-4: `stop_session` returns took/not-took; everything else is events

**Context.** Results flow to the UI as events (deltas, done, errors). A
stop can legitimately be refused (already stopped, session gone, still
connecting). If a refused stop is silent, the UI sits in "Finalizing
transcript…" forever — there is no later event to save it.

**Decision.** `stop` returns took/not-took synchronously: an error Result
when not taken, because a refused stop produces no event at all — nothing
else will ever report it. The frontend treats not-taken as "the answer may
still be coming" and arms a bounded fallback rather than cancelling — see
ADR-15 for why cancellation was wrong.

**Consequences.** The UI can never wedge on a refused stop. The cost is an
extra error path the frontend must handle — tested on both sides.

**Say it in an interview.** "I distinguish command acknowledgements from
event streams. Most outcomes arrive asynchronously, but 'your stop did
nothing' can only be learned from the command's return — so that return is
part of the contract, and the UI has a recovery path wired to it."

---

## ADR-5: Retry exactly once, only before the first delta

**Context.** Transient connection failures happen; blind retries are how
you get doubled answers, burned latency budgets, and hammered APIs.

**Decision.** One retry, only when the provider says the failure was
connection-level (server never heard us) AND no delta reached the UI.
Never on HTTP status, never after paint, never after abort. The retried
request is the same immutable object.

**Alternatives.** Exponential backoff with N retries — wrong product: the
user is mid-conversation; after ~2 s they'd rather see an actionable error
than a stale answer. Retry-on-5xx — the server heard us; a 529 retry just
adds load and delay.

**Say it in an interview.** "Retry policy is a product decision disguised
as an infra decision. Here the budget was one second, and the UI paints
tokens as they stream — so the only safe retry is 'the connection never
established and nothing painted', exactly once, with a byte-identical
request. I encoded that in one shared helper so no provider can
accidentally deviate."

---

## ADR-6: Providers behind a Protocol + registry; caching stays inside Anthropic's adapter

**Context.** The user will swap answer providers; LLM vendors churn.
Anthropic has prompt caching with vendor-specific rules; other vendors
don't.

**Decision.** `AnswerProvider` Protocol + a registry that drives settings
validation, the Settings UI, per-provider key storage, and the has-key
booleans. Vendor-specific logic (cache breakpoints, reasoning knobs) lives
entirely inside that vendor's module.

**Consequences.** Adding a provider = one module + one register call +
cloning the conformance tests. The shared pipeline stayed vendor-neutral —
verified by the conformance suite running identically against both
shipped providers.

**Say it in an interview.** "I treated provider swappability as a
requirement, not a refactor for later: a Protocol seam, a registry the UI
renders from, and a conformance test suite any new provider must pass.
Vendor quirks are quarantined in their adapters — Anthropic's cache
breakpoint logic never leaks into the pipeline."

---

## ADR-7: Prompts are byte-stable, split at the cache breakpoint

**Context.** Anthropic's prompt cache matches on byte-identical prefixes.
The resume+JD block is large and stable; the style policy is tiny and
flips often; the transcript changes every question.

**Decision.** Three-part prompt: cached prefix (role+profile) | style
suffix | user message. No timestamps, no unordered joins. Tests assert
byte equality and that style flips never touch the prefix.

**Consequences.** Style flips are latency-free. Caching honesty note: with
Haiku's 4096-token minimum, small profiles make the marker a no-op — the
code comments say so rather than pretending.

**Say it in an interview.** "Prompt layout is cache layout. I ordered the
prompt stable-to-volatile and put the cache breakpoint exactly at the
stable/volatile boundary, then wrote byte-equality tests, because one
drifting byte silently zeroes the hit rate — and I documented when the
cache genuinely doesn't engage rather than cargo-culting it."

---

## ADR-8: The markdown renderer parses a subset and never parses links

**Context.** Model output renders into a desktop webview. Model output is
untrusted input — prompt-injected content could carry HTML/JS.

**Decision.** In-repo parser producing data (never HTML strings); every
string reaches the DOM as a text node; NO link parsing at all — `[x](url)`
stays literal, so no href exists and `javascript:` has nowhere to live.
Streaming safety comes from purity: the parse is a function of the full
source, so every prefix renders consistently, verified at every cut point.

**Alternatives.** `marked` + DOMPurify — two dependencies whose threat
models must be re-audited every upgrade, plus sanitizer-bypass risk.
Sanitizing hrefs — a denylist you have to get right forever; not
generating hrefs is a class-elimination, not a mitigation.

**Say it in an interview.** "I removed the vulnerability class instead of
filtering it: the renderer never produces attributes or links from model
text, everything is a DOM text node, and the XSS suite proves script tags
render as visible text. For streaming, I made the parser pure and tested
that every byte-prefix renders identically to the final render."

---

## ADR-9: Write-only secrets with prefix-tagged, fail-closed storage

**Context.** API keys live in a user-editable JSON file; DPAPI can be
unavailable; files get copied between machines.

**Decision.** Keys encrypt via DPAPI as `enc:<b64>`, with an honestly
MARKED `plain:<b64>` fallback. Decode by stored prefix. Anything
unreadable is "unset". The frontend only ever sees `has<Provider>Key`
booleans; an omitted field means keep, an empty field means clear.

**Say it in an interview.** "Secrets are write-only past the UI boundary
and fail closed everywhere: unknown prefix, foreign-machine blob, bad
base64 all read as 'no key', because the worst outcome is handing a
provider a corrupted string as a bearer token. And when the OS keystore is
unavailable I store a *labeled* plaintext fallback — degraded and honest
beats broken or secretly insecure."

**Superseded (2026-09-22).** The review (R02) showed the fallback was
degraded but NOT honest where it mattered: the prefix was visible only in
the file, while the UI kept saying "encrypted". Encoding now fails closed
(`SecretEncryptionError` -> a visible save error, previous key kept);
legacy `plain:` values still decode and are re-encrypted on the next save.
The lesson: an honest label has to be where the user looks.

---

## ADR-10: Latency is measured, not vibed — and the metrics refuse to lie

**Context.** The product promise is a number. Numbers get gamed by
accident: a typed question has no STT stage; a provider might return a
full answer with no streaming deltas.

**Decision.** The core (not the UI) measures `sttFinalizeMs`,
`firstTokenMs`, `totalMs` from the moment stop was requested. Typed
questions report `sttFinalizeMs: 0` exactly. A no-delta answer reports
`firstTokenMs = totalMs` — never 0, because 0 renders as "instant" and
lies about the one number the app is judged on.

**Say it in an interview.** "I put measurement in the layer that owns the
clock and wrote down the edge-case semantics — what the metric means when
a stage didn't happen. An honest 2.1 s beats a fake 0.0 s; users forgive
slow, they don't forgive lying dashboards."

---

## ADR-11: Event dispatch is batched — and a failed batch retries per event

**Context.** Every event reaches the page through `evaluate_js`, which is
a blocking round trip on a worker thread, and an answer streams dozens of
`llm:delta` events per second — one call per event put those hops on the
latency budget. Batching fixed that but raised the stakes: the first
version dropped a failed batch whole, and serialized events outside the
failure guard, so one unserializable payload could kill the pump task and
silence the app for the rest of the session.

**Decision.** The pump drains whatever is queued into ONE `evaluate_js`
call (capped at 64 events), order preserved — the queue is FIFO and the
page dispatches the array in order. A batch that fails, whether in
serialization or in the webview, is retried event by event so one poison
payload costs itself, not its 63 neighbours. Serialization runs per batch
inside the guard, with `allow_nan=False`, because `JSON.parse` rejects
`NaN`/`Infinity` and a non-finite number would otherwise drop the batch at
the page instead of at the pump. (`app_core/bridge/events.py`)

**Alternatives.** Fire-and-forget dispatch threads — reorders deltas, and
out-of-order `llm:delta` scrambles the answer text; ordering is a
correctness property here, not a nicety. Retrying the failed batch whole —
a poison event loops forever. Dropping the failed batch (the first
implementation) — batching had amplified the blast radius 64×, and one of
those 64 can be the terminal `llm:done`; losing it strands the UI in
"Generating answer…" forever, because no later event will save it.

**Consequences.** A single-event dispatch that still fails is simply lost
— at that point the renderer is dying and the heartbeat watchdog will
reload it. The retry path doubles dispatch work only in the failure case.
Pinned by `tests/test_bridge.py::TestEventBatching` and the batch-failure
isolation tests (a transient failure loses nothing; a poison event cannot
take its neighbours down).

**Say it in an interview.** "I batched a chatty event channel to keep
blocking round trips off the latency budget — and then had to redesign the
failure unit, because batching turned 'one lost event' into 'sixty-four
lost events', one of which can be the terminal event the UI cannot recover
without. Failed batches split and retry per event, and serialization lives
inside the guard so a poison payload can't kill the dispatcher. When you
batch for throughput, re-derive your blast radius."

---

## ADR-12: Content protection is verified by read-back and reported to the user

**Context.** Being invisible to screen sharing is the moat feature.
`SetWindowDisplayAffinity` returns a BOOL that is trivially ignored, and
it CAN fail: an HWND not ready in the first instants after `shown`, a
policy or driver that refuses, Windows builds older than 2004 that lack
`WDA_EXCLUDEFROMCAPTURE` entirely. The mutation-testing pass also found the
shell's apply path had ZERO test coverage — nothing referenced
`_apply_content_protection` or either protection event.

**Decision.** Apply, then VERIFY: success means the affinity READ BACK
equals `WDA_EXCLUDEFROMCAPTURE` — not that the setter returned truthy, and
not `WDA_MONITOR`, which hides from some capture paths but not the ones
screen sharing uses. Retries (5 attempts, growing sleeps) run on their own
thread so no pywebview callback ever blocks. `shown` and `loaded` both
trigger it, so attempts are sequence-stamped: a slow loser finishing after
a newer attempt must not overwrite the fresher verdict. The verdict is
emitted every time as `protection:ok` / `protection:failed` — never only
on transition, because a renderer reload loses the page's previous state —
and a failure renders as a standing `role="alert"` warning in the window.
(`app_core/bridge/protection.py`, `app.py::_apply_content_protection`)

**Alternatives.** Log the failure — a user who believes they are hidden
while being broadcast is this product's worst outcome, and nobody reads
logs mid-interview. Trust the setter's return value — the read-back test
exists precisely because a set that reports success can fail to stick.

**Consequences.** The worst outcome became visible instead of silent. The
cost is a Protocol seam (`DisplayAffinityApi`) so verification is testable
without a real window, and a little threading care (the stamp) for the
overlapping-attempts case — both now pinned by `tests/test_protection.py`
and `TestContentProtectionVerdict`.

**Say it in an interview.** "The feature's failure mode is invisible by
definition — you learn you were broadcast after the interview. So I don't
trust the API's return value: I read the setting back and treat anything
else as failure, and failure is user-facing UI, not a log line. Verify and
report beats set and forget whenever the OS call is best-effort and the
stakes are asymmetric."

---

## ADR-13: A command ticket makes latest-command-wins hold across key reads

**Context.** ADR-3 claims the session slot before the connect — but the
key reads come FIRST, because a missing key must fail the COMMAND (an
error Result the button can show) rather than create or supersede a
session. Those reads hit DPAPI through `asyncio.to_thread` and can stall.
The audit found the gap this opens: an earlier Record press stalled on its
key read could resume late and supersede the Ask the user typed
afterwards, killing it silently.

**Decision.** Every command that awaits before the slot — `start_session`
and `ask` — claims a monotonically increasing ticket synchronously at
entry, before any await (`ask` claims right after its input validation,
which per rule 8 must not disturb a live session). After the key reads
resolve, `_require_ticket` re-checks that no newer command
claimed since; a loser raises `aborted`, which the UI never displays — the
user's newer action is in charge. (`app_core/session/machine.py`,
`_claim_ticket` / `_require_ticket`)

**Alternatives.** Claim the slot before the key reads — turns a missing
key into a session-level error and superseding, killing a live session for
a command that was never viable. Serialize commands through a queue — then
the stalled read *delays* the newer command instead of losing to it;
latest-wins is the product behavior, not FIFO.

**Consequences.** Two claims now exist: the ticket is the claim of
"newest", the slot the claim of "active". A lone command is never
self-superseded (tested). Mutation testing later proved `ask`'s ticket
check was deletable with every test green — the only ticket test stalled
`start_session` — so a mirrored stalled-ask test now makes both copies
load-bearing.

**Say it in an interview.** "Latest-wins was enforced at the session slot,
but commands do work before they reach the slot — key reads that can stall
on the OS keystore — and a stalled older command could resume and kill a
newer one. The fix is a ticket claimed synchronously at command entry and
re-checked after the pre-slot awaits; the loser reports a silent 'aborted'.
Same optimistic-concurrency move as the slot, applied one layer earlier."

---

## ADR-14: The resampler is phase-continuous and anti-aliased — audio correctness is a product decision

**Context.** The device delivers ~125 ms chunks at whatever rate and
slicing it chooses; Deepgram needs 16 kHz mono. The first implementation
resampled each chunk standalone, and measurement showed three silent
failures: the output depended on how the device sliced the stream (up to
2.0 of waveform error on a unit-amplitude 1 kHz sine), samples were
dropped when a chunk did not divide evenly (15 985 delivered where 16 000
were owed, per second), and decimating with no low-pass folded everything
above 8 kHz into the speech band — a 10 kHz tone arrived near full
strength around 6 kHz. None of these crash or error; they degrade exactly
the audio Deepgram transcribes, which degrades the question every answer
is grounded in.

**Decision.** `StreamingResampler` (`app_core/audio/downsample.py`): a
63-tap windowed-sinc FIR (7.2 kHz cutoff) applied overlap-save with the
filter's memory carried across chunks, then interpolation at a carried
fractional read position so consecutive chunks share one continuous time
base. Feeding a signal through it in ANY chunk sizes is byte-identical to
feeding it whole — tested across chunk sizes 480/1024/6000/7777.
Measured: ~59 dB alias suppression, the speech band (300/1000/3000 Hz)
preserved to <1%, and ~1.5% of real time on the callback thread.

**Alternatives.** `scipy.signal.resample_poly` — a new dependency whose
stock API is also whole-signal, so the state-carrying across chunks (the
actual hard part) would still be hand-rolled. Buffer the recording and
resample at Stop — moves the cost into the stop-to-first-word window and
forfeits the live transcript, which needs streaming audio. Keep the
per-chunk version — measured wrong three ways above.

**Consequences.** The resampler is stateful: one instance per capture
stream, threaded through `downsample_chunk`. The same input twice through
one instance deliberately does NOT produce identical output — it continues
phase — and a test pins that so nobody "fixes" it. The cost ceiling is
real: this runs inside the GIL on PortAudio's callback thread, so the
budget is pinned by a test — 125 ms of audio must cost under 12.5 ms — not
left as trivia.

**Say it in an interview.** "The bug class was silent quality loss:
per-chunk resampling dropped samples, aliased, and changed output with
chunk size — no crash, just worse transcripts, and therefore answers
grounded in a mis-heard question. I replaced it with an overlap-save FIR
plus a carried fractional phase, proved chunk-invariance byte-for-byte,
and measured the alias suppression and CPU cost. When the product IS the
audio, DSP correctness is a feature decision, not an optimization."

---

## ADR-15: A refused stop gets bounded recovery, never cancellation

**Context.** Rule 3 says a stop can be refused. The frontend's original
response to refusal was "the session must be gone — cancel and go idle."
Fresh-eyes review found the race that breaks: at 120 s the cap auto-stops
and the session is mid-finalize; a user Stop pressed in that window is
*correctly* refused — and the frontend then cancelled a live, finalizing
session. Two minutes of recording produced no answer, no error, nothing.

**Decision.** A refused stop never cancels. The UI stays in "finalizing",
keeps tracking the session, and arms a bounded fallback
(`STOP_RECOVERY_MS`, 20 s): if a terminal event lands (`llm:done` /
`session:error`), it disarms and the answer is kept; if nothing arrives,
the live entry is retired into history (a captured transcript is user
work) and the UI returns to idle. (`frontend/src/App.tsx`,
`stop-not-taken` / `stop-recover`)

**Alternatives.** Cancel on refusal — destroys the answer in the cap race.
Wait forever — wedges the UI when the session really is gone, the exact
failure the stop contract exists to prevent (ADR-4). Have the core
distinguish "already stopping" from "gone" in the error code — more codes
to keep honest, and the UI still needs a timer for the case where the
promised terminal event never arrives.

**Consequences.** The worst case became "idle 20 seconds late" instead of
"two minutes of the user's question destroyed". The 20 s bound is derived,
not vibed: the 5 s STT flush cap plus the 10 s first-token window, with
slack. Both branches — answer survives, session really gone — are pinned
in `app-coverage.test.tsx` ("a stop that races the 120s cap").

**Say it in an interview.** "A refusal is ambiguous: 'session gone' and
'session already stopping itself' look identical to the caller, and the
first implementation resolved that ambiguity destructively — it lost a
two-minute answer in a race with the recording cap. The fix is bounded
optimism: assume the recoverable case, keep listening, and fall back on a
timeout derived from the pipeline's own caps. Never make the destructive
choice on ambiguous evidence."

---

## ADR-16: Model output is hardened twice — caps in the parser AND a boundary around the renderer

**Context.** Model output is untrusted and unbounded, and React's failure
mode for a render error is total: an uncaught throw unmounts the entire
root, so one pathological answer blanks the whole app mid-call. Hostile
testing found two concrete kill shots: 12 000 consecutive asterisks
overflowed the render stack, and delimiter-dense text made emphasis
resolution quadratic — 18 KB froze the main thread for ~59 seconds.

**Decision.** Both layers, because they cover different failures. The
inline parser enforces caps — nesting depth 24, 1000 emphasis nodes,
emphasis resolution skipped entirely past 20 000 characters — and past a
cap the remaining delimiters render as literal text. Separately,
`MarkdownBoundary` wraps the renderer: if rendering throws anyway, the
answer renders as its raw markdown source in a `<pre>` — still text-only,
still readable — and the boundary re-arms when the source changes so the
next delta gets a fresh attempt. (`frontend/src/markdown/inline.ts`,
`frontend/src/markdown/Markdown.tsx`)

**Alternatives.** Caps only — caps stop the failures we found; the
boundary exists for the one we didn't. Boundary only — a boundary catches
exceptions, not a 59-second synchronous freeze: nothing throws while the
main thread is pegged. Truncate or delete suspicious input — deletion
hides what the model actually said; literal-text degradation keeps it
visible.

**Consequences.** "Cosmetic degradation beats a blank app" means: every
fallback in this chain still shows the full answer as text. Worst case
you see asterisks instead of italics, or unstyled markdown instead of
styled — never an empty window in the middle of an interview. The caps
are pinned from both sides in `markdown-hardening.test.tsx`: hostile
inputs stay bounded AND a normal-emphasis corpus still renders correctly,
so a future tighter cap cannot silently break real answers.

**Say it in an interview.** "Defense in depth against my own dependency —
the model. Deterministic caps neutralize the resource exhaustions I could
demonstrate: a stack overflow from nested emphasis and a quadratic freeze.
An error boundary catches whatever I couldn't foresee, because React's
default is to unmount the entire app on a render error. And every fallback
degrades to visible text, because in this product a cosmetically ugly
answer is fine and a blank window mid-call is catastrophic."

---

## Bonus: smaller calls worth knowing

- **`session:autostopped` event** (spec deviation, documented): the 120 s
  cap fires in the core; the UI needs to switch to "answering now" without
  a user press. Guessing from a UI-side timer would race the core's; an
  explicit event is honest. It is also what makes the ADR-15 race benign:
  the UI learns the cap fired instead of inferring "gone" from a refusal.
- **Pre-warm via `GET /v1/models`**: unauthenticated, throttled, body read
  to completion so the connection returns to the pool. The point is the
  TLS handshake happening BEFORE the stop-to-first-word window.
- **Minimized geometry is rejected** (`plausible_bounds` in `app.py`):
  Windows parks a minimized window at (-32000, -32000) with a
  titlebar-sized rect, and the debounced geometry save wrote exactly that
  — quitting while minimized lost the layout the user had arranged.
- **The hotkey re-registers only when it changed**
  (`app.py::_apply_settings`): re-registering an unchanged accelerator
  briefly unbinds it, and another app can steal it in the gap.
- **A heartbeat watchdog reloads a dead renderer**: the page pings
  liveness through the bridge; if it goes stale for 15 s the shell reloads
  the entry URL, at most once per 10 s so a boot-crash cannot flicker
  forever.
