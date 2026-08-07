# 02 — Design decisions (ADR-style, with interview translation)

Ten decisions, each in ADR form: **Context → Decision → Alternatives →
Consequences**, plus **"Say it in an interview"** — a 30-second spoken
version, because being able to *explain* a design is a separate skill from
building it.

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

**Consequences.** All 11 invariants + their races run in ~1 s with zero
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
— which is exactly what the tests pin.

**Say it in an interview.** "Classic check-then-act race across an await:
I made the claim synchronous — before any suspension point — and made
every resumption re-validate that it still owns the slot. It's optimistic
concurrency applied to UI sessions."

---

## ADR-4: `stop_session` returns took/not-took; everything else is events

**Context.** Results flow to the UI as events (deltas, done, errors). A
stop can legitimately be refused (already stopped, session gone, still
connecting). If a refused stop is silent, the UI sits in "Finalizing…"
forever — there is no later event to save it.

**Decision.** `stop` is the one command with meaningful synchronous
feedback: an error Result when not taken. The frontend treats not-taken as
"recover now" (cancel + go idle).

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

## Bonus: three smaller calls worth knowing

- **`session:autostopped` event** (spec deviation, documented): the 120 s
  cap fires in the core; the UI needs to switch to "answering now" without
  a user press. Guessing from a UI-side timer would race the core's; an
  explicit event is honest.
- **Pre-warm via `GET /v1/models`**: unauthenticated, throttled, body read
  to completion so the connection returns to the pool. The point is the
  TLS handshake happening BEFORE the stop-to-first-word window.
- **One serialized event queue to the webview**: `evaluate_js` per event,
  pumped one at a time. Out-of-order `llm:delta` events would scramble the
  answer text; ordering is a correctness property, so it's structural, not
  incidental.
