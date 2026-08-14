# 01 — Guided tour: read the code with me

Read this with the code open. We go in dependency order — pure leaves
first, the state machine in the middle, the shell last — which is also the
order the project was built and tested in. For each stop: what the file
does, the one hard problem in it, and a "look closely" pointer worth
staring at until it clicks.

---

## Stop 0: the product constraint that explains everything

`README.md` → "The latency architecture". The product promise is ~1 second
from Stop-press to the first word of the answer. Hold that number in your
head for the whole tour: almost every odd-looking decision (pre-warming,
parallel connect, buffered frames, byte-stable prompts, retry-only-before-
first-delta) exists to protect it or to make being that fast safe.

## Stop 1: `app_core/errors.py` — the error vocabulary

One dataclass, one `Literal` union of 13 codes. The important idea: the set
is CLOSED. The UI keys behavior off codes ("aborted is never shown", "auth
errors point at Settings"), so a new arbitrary code is an API break, not a
convenience. Notice `AppError` extends `Exception` — raising a user-facing
error and returning one are the same type, so nothing gets lost in
translation at the bridge.

## Stop 2: `app_core/stt/frames.py` — parsing hostile JSON

Deepgram's stream is treated as hostile input. Every field access is
type-checked before use.

**Look closely** at `is_final = data.get("is_final") is True`. Not
`bool(...)`, not `== True` — `is True`. In Python `1 == True` but
`1 is not True`. A truthy imposter (`1`, `"true"`) must parse as *interim*,
because wrongly committing interim text corrupts the transcript prefix the
answer will be grounded in — and that corruption is silent.

The `TranscriptAccumulator` below it maintains `committed + interim`
incrementally. Why not re-join all finals per message? A 2-minute recording
delivers hundreds of messages; O(n) work per message is O(n²) total, on the
hot path, inside the GIL.

## Stop 3: `app_core/llm/sse.py` — the incremental SSE parser

Server-Sent Events look trivial ("lines starting with `data:`") and are a
minefield: chunks split anywhere (including between `\r` and `\n`, and in
the middle of a multi-byte UTF-8 character), three line-ending styles,
comment lines, a leading BOM that would fuse onto the first field name and
silently drop the stream's first event, and — the killer — a final `data:`
line with no trailing newline when the stream is truncated. Losing that
line loses the answer's last words, silently.

**Look closely** at `_pending_cr`. When a chunk ends in `\r`, the parser
has already dispatched the line but must remember to swallow a leading
`\n` in the NEXT chunk — otherwise a CRLF split across chunks fabricates
an empty line, and an empty line means "dispatch the event", so the event
dispatches twice. This is exactly the kind of bug that only appears in
production under real network chunking, which is why
`tests/test_sse.py::TestHostileChunking::test_every_cut_point_matches_batch`
brute-forces every split point.

Also notice: the parser owns an *incremental* UTF-8 decoder
(`codecs.getincrementaldecoder`). Decoding chunk-by-chunk with `.decode()`
would corrupt any 3-byte character that straddles a boundary.

## Stop 4: `app_core/llm/prompt.py` — strings as product behavior

Three parts: `cached_prefix` (role + resume + JD), `style_suffix`, and the
user message. The split is not aesthetic — it maps 1:1 onto Anthropic's
prompt-cache breakpoint. Anthropic caching is a **byte-prefix match**: if
one byte of the prefix drifts (a timestamp, an unordered join), the cache
silently never hits again. So the whole module is deterministic string
concatenation, tested byte-for-byte.

**Look closely** at where the style suffix lives: AFTER the cached prefix.
Flipping Brief↔Detailed changes only the suffix, so a style flip is
latency-free. If styles were inside the cached block, every flip would
re-write the cache (1.25× cost) and lose the read discount.

## Stop 5: `app_core/store/` — settings, secrets, geometry

Three files, three lessons:

- `settings.py`: **per-field fallback**. The settings file is
  user-writable, so it's untrusted input. One corrupt field falls back
  alone; the user's resume survives someone hand-editing the JSON badly.
  Also atomic writes: a per-writer tmp name + `os.replace`, and — subtle —
  the in-memory cache updates only AFTER the write lands. If memory ran
  ahead of disk, a failed write followed by a successful one would
  silently commit the failed change. Notice the `threading.RLock` around
  every write: writers arrive on different threads (a settings patch via
  `asyncio.to_thread`, window bounds from a debounce timer and the close
  handler), and an unlocked read-modify-write of the whole dict would let
  one writer's fields — a just-saved API key, or the resume — vanish from
  both disk and the cache.
- `secrets.py`: keys are DPAPI-encrypted with prefix-tagged storage
  (`enc:` / `plain:`). Decode by STORED prefix, never by current keystore
  availability. Everything unreadable "reads as unset" — fail closed; a
  raw stored string must never reach a provider as an API key.
- `bounds.py`: pure geometry. The 40 px visibility rule (on BOTH axes, at
  the CLAMPED size) is what makes "unplug the monitor, relaunch" recenter
  the window instead of restoring it into the void. Note `isinstance(v,
  bool)` is checked BEFORE `isinstance(v, int)` — `bool` is an `int`
  subclass in Python, and `True` is not a coordinate.

## Stop 6: `app_core/llm/base.py` + `retry.py` + `wire.py` — the provider seam

`AnswerProvider` is a Protocol: `build_request` / `stream` /
`classify_error` / `is_retryable` plus identity fields. The registry feeds
settings validation AND the Settings UI's `<select>`, so "add a provider"
is one module + one `register()` call. This seam is a product requirement,
not tidiness — read README "How to add an answer provider".

`retry.py` is one function of about thirty lines that encodes four hard
rules: retry exactly once; only for connection-level failure; never after
an HTTP status (the server heard us); never after a delta reached the UI
(a second attempt would concatenate two answers). **Look closely** at why
`build_request` returns an immutable request that the retry reuses as-is:
"the retried request is byte-identical" is guaranteed by construction
rather than by discipline.

`wire.py` is the transport skeleton both providers share. Note the flag
`response_started`: an httpx error after the response started is
"stream_drop" (never retryable — bytes may already have been painted).
Before it, only a genuine connection failure (ConnectError,
ConnectTimeout, PoolTimeout) is "connect", the sole retryable kind; a read
timeout while waiting for response headers is "timeout", because the
server may already be generating our answer and a retry would produce a
second one.

## Stop 7: `app_core/llm/anthropic.py` and `groq.py` — two wire formats

Anthropic: system prompt as TWO blocks with `cache_control` on the first
only (see Stop 4). Groq: one joined system string, plus
`reasoning_effort: "low"` and `include_reasoning: false` — gpt-oss is a
reasoning model, and reasoning tokens are pure delay before the first
spoken word. Groq's module deliberately exports its helpers
(`extract_openai_delta`, `is_done_sentinel`, `classify_openai_failure`):
it doubles as the template for any future OpenAI-compatible provider.

**Look closely** at Groq's `[DONE]` handling: it `continue`s — a sentinel
to skip, NOT a stream terminator. Treating it as EOF would drop any bytes
after it in the same chunk.

## Stop 8: `app_core/session/machine.py` — the crown jewels

Read the numbered rule comments (they mirror §5 of the spec, which lives
outside this repo) first, then the file. The core shape: at most ONE live
session; commands (`start_session`, `stop_session`, `ask`,
`cancel_session`) run on the asyncio loop; every session mutation happens
there and only there.

The invariants worth tracing by hand:

1. **Supersession** (`_supersede`): the old session gets `aborted = True`
   FIRST, then its tasks are cancelled and its stream torn down. Every
   event emission checks `aborted` — so events from a dead session,
   *including its `llm:done`*, drop. And the socket death the abort causes
   is suppressed, not reported.
2. **Latest-start-wins**, twice over. `self._active = session` happens
   synchronously BEFORE the connect await: a second Record press
   supersedes the first *while it is still connecting*, and when the
   loser's connect resolves it checks `self._active is not session` and
   tears itself down silently. But `start_session` and `ask` also await
   DPAPI key reads BEFORE any session exists, so a second claim guards
   that window: `_claim_ticket()` runs synchronously at command entry,
   and `_require_ticket()` after the awaits refuses a command that a
   newer one has overtaken. Without the ticket, a Record press whose
   DPAPI read stalled could resume and supersede an Ask the user issued
   LATER — the older command would win. Race bugs like these can't be
   fixed after the await; the claim must precede it.
3. **The stop contract**: `stop_session` returns took/not-took as an error
   Result, because every other outcome is an event. A stop that silently
   did nothing leaves the UI in "Finalizing transcript…" forever — the
   return value is the ONLY way it learns. Note what "not taken" does NOT
   mean: the session may be alive and mid-finalize, because the 120 s cap
   auto-stops on its own (`_on_record_cap` → `session:autostopped` →
   `_begin_stop`), and a user Stop racing that cap is refused —
   `stop_requested` is already set. How the frontend must react to a
   refusal is Stop 13's story; treating it as "session is gone" destroyed
   the answer that was still coming.
4. **Timeout interplay** (`_answer`): two watchdog tasks; the first-token
   watchdog checks a closure over `first_token_ms`; both set
   `session.errored = True` *before* cancelling the stream so that a delta
   racing in after the timeout cannot paint. Note the use of
   `asyncio.wait({stream_task})` instead of awaiting the task directly:
   awaiting a cancelled child raises CancelledError into the parent, which
   would be indistinguishable from the parent itself being cancelled.
   `asyncio.wait` never propagates the child's outcome — it lets the code
   *inspect* `cancelled()` / `exception()` / `result()` explicitly.
5. **Slot release** (`_release`): guarded by `session.released` so it runs
   exactly once, whatever the exit path (done, error, abort, timeout).

**Look closely** at `CancelledError` handling everywhere: it is control
flow (the abort path), never an error. Every `except Exception` in this
file is safe precisely because `CancelledError` derives from
`BaseException` in modern Python.

## Stop 9: `app_core/stt/client.py` — the Deepgram WebSocket client

Map each piece to a wire reality:

- Pre-open buffer (deque, maxlen ≈ 15 s): capture starts in PARALLEL with
  the connect; frames from before the socket opened flush in order.
  Without this, the first words of the question are clipped.
- KeepAlive every 8 s: Deepgram kills sockets ~10 s after the last audio,
  and silence during a call is normal. But the keepalive checks
  `_close_requested` immediately before sending: a KeepAlive after
  CloseStream errors on the CLOSING socket and fabricates a "lost
  connection" during a stop that is *succeeding*.
- Finalize: send CloseStream, wait for the server's flush-and-close (5 s
  cap), return the accumulated transcript. Idempotent — the second caller
  awaits the same task. A stream that never opened or already died returns
  immediately instead of burning the cap.
- Close classification: Deepgram rejects bad keys by CLOSING (1008 with a
  `DATA-xxxx` reason), often with no error frame. Close before any Results
  frame → connect failure ("check the API key"); close after → mid-
  recording death (one `stt_error`, because a silently truncated
  transcript answers the wrong question).

**Look closely** at `_CLOSE_SENTINEL`. CloseStream is not sent directly —
it is queued on the SAME asyncio queue the audio frames travel, so it can
never overtake frames still parked behind a stalled send. Deepgram
discards audio that arrives after CloseStream; letting the close jump the
queue would silently truncate the transcript's tail. A test for this
ordering once passed only because its fake socket drained synchronously —
`TestCloseStreamUnderBackpressure` in `tests/test_deepgram_client.py` now
stalls the sender to prove the sentinel really waits its turn.

## Stop 10: `app_core/audio/` — the only non-async thread work

`capture.py` runs the device callback on PortAudio's thread and does the
minimum — convert, resample, accumulate 2048-sample frames (~128 ms at
16 kHz) — then hands off with `loop.call_soon_threadsafe`. That call is
the ONLY legal doorway from a foreign thread into the loop; grep the repo
for `call_soon_threadsafe` / `run_coroutine_threadsafe` and you'll find
every thread boundary in the app. Note the blanket `except Exception` in
`_on_device_chunk`: an exception escaping into PortAudio's C callback
kills the stream with no Python-visible error — the user would just see a
recording that captures nothing — so a bad chunk is dropped instead.

`downsample.py` is pure numpy (vectorized; per-sample Python loops on the
audio path would burn CPU inside the GIL on PortAudio's thread). Its
centerpiece is `StreamingResampler`, which replaced a resampler that
processed each device chunk standalone — wrong in three MEASURED ways:
the output depended on how the device happened to slice the audio (up to
2.0 of waveform error on a unit-amplitude sine), samples were dropped
whenever a chunk length didn't divide evenly (15,985 delivered where
16,000 were owed, per second), and decimating 48 kHz → 16 kHz with no
low-pass folded everything above 8 kHz back into the speech band (a
10 kHz tone arrived near full strength at ~6 kHz). All three landed on
the audio Deepgram transcribes. `docs/learn/09-audio-and-dsp.md` teaches
the DSP behind each failure from zero.

**Look closely** at the two pieces of state the fix carries across
chunks: `_fir_state`, the overlap-save tail of a 63-tap windowed-sinc
low-pass (~59 dB of alias suppression for ~1.5% of real time on the
callback thread), and `_pos`, the FRACTIONAL read position. Carrying the
fraction — not just the leftover samples — keeps consecutive chunks on
one continuous time base: feeding a signal through in ANY chunk sizes
produces byte-identical output to feeding it whole, and
`tests/test_downsample.py` asserts exactly that. `capture.py` holds one
resampler per stream for the same reason: the state is per-stream, never
per-chunk.

## Stop 11: `app_core/bridge/` — four doorways, each with a contract

- `api.py`: pywebview calls js_api methods on worker threads. Every
  handler that touches the core hands off to the loop — the session
  commands wrap a coroutine with `run_coroutine_threadsafe(...).result()`,
  so the worker thread blocks, the loop does the work, and session state
  is never touched off-loop (`cancel_session` is the deliberate exception:
  fire-and-forget via `call_soon_threadsafe`). Every command returns a
  Result envelope; nothing throws across the language boundary.
- `events.py`: ONE queue, ONE pump task → events reach the DOM in
  emission order. `evaluate_js` blocks, so the pump runs it in
  `asyncio.to_thread` — one *batch* at a time. The pump drains whatever
  is queued into a single `evaluate_js` call (capped at 64): each call is
  a blocking round trip on a worker thread and an answer streams dozens
  of deltas per second, so per-event round trips would eat the latency
  budget. If a batch fails as a unit, it retries event by event — one
  unserializable payload must not take its 63 neighbours with it, because
  a lost `llm:done` strands the UI in "Generating answer…" forever.
- `hotkey.py`: Win32 `RegisterHotKey` on a dedicated message-loop thread
  (the API is thread-bound). It returns an honest status —
  registered / disabled / invalid / unavailable — so the UI can tell
  "another app owns it" apart from "that isn't a shortcut Windows
  understands".
- `protection.py`: content protection, VERIFIED. Invisibility to screen
  sharing is the product's moat feature, and `SetWindowDisplayAffinity`
  returns a BOOL that is easy to ignore — and it CAN fail (an HWND that
  isn't ready yet, policy, Windows builds older than 2004 which lack
  `WDA_EXCLUDEFROMCAPTURE`). So `apply_content_protection` applies, then
  reads the affinity back with `GetWindowDisplayAffinity` and returns
  True ONLY when the OS confirms it. **Look closely** at the docstring's
  framing: a False here is a user-visible fact, not a log line — a user
  who believes they are hidden while being broadcast is this product's
  worst outcome. The real user32 calls live behind a two-method Protocol
  (`DisplayAffinityApi`), which is what lets `tests/test_protection.py`
  exercise every failure path without a window.

## Stop 12: `app.py` — the shell, and the seam that was missing

The shell's duties: crash logging (faulthandler plus all three exception
hooks — `sys.excepthook`, `threading.excepthook`, and the asyncio loop's
exception handler), single instance (named mutex + named event to focus
the first instance), window-geometry persistence (debounced on
move/resize, flushed on close, and `plausible_bounds` refuses the
(-32000, -32000) titlebar-sized rect a minimized window reports), a
heartbeat watchdog that reloads a dead WebView2 renderer at most once per
10 s, and content protection retried across the first instants after
`shown` — with each attempt stamped by a sequence number so a slow loser
that finishes after a newer attempt cannot overwrite the fresher verdict
with a stale one (`_apply_content_protection`).

**Look closely** at `_wire_window`, and memorize it. It is five
subscriptions and one line — `self.sink.attach(window)` — and that line
was missing for FOUR commits. The event pump parks on
`while self._window is None`, so nothing the core emitted reached the
page: no transcript, no answer deltas, no audio level, no errors. And the
app still LOOKED alive, because commands travel a separate channel —
`js_api` is handed to `create_window` directly — so buttons responded
while the window showed nothing at all. Every suite was green the whole
time, because each side mocked the other: core tests used a fake sink,
bridge tests attached a fake window themselves, frontend tests dispatched
events by hand. The one line connecting them was the one line nothing
tested. `tests/test_app_wiring.py` now walks the real seam — it
instantiates the real `App`, wires a stub window, and asserts an emitted
event lands in `evaluate_js`. The lesson generalizes: integration points
where every test mocks the other side are exactly where green suites lie.

## Stop 13: the frontend (`frontend/src/`)

- `bridge.ts`: the typed doorway; every command resolves a Result.
- `App.tsx`: a reducer-based mirror of the core state machine
  (idle/starting/recording/finalizing/answering) plus the 6-entry history.
  **Look closely** at three things: the event buffer that holds events for
  a session id we haven't adopted yet (the promise-resolution race); the
  requestAnimationFrame delta coalescing (one paint per frame while
  streaming); and `retireLive` — an aborted attempt that captured nothing
  is discarded, but one with a question or partial answer is retired into
  history, because the transcript is user work and a vanishing
  half-answer looks like data loss.
- `doStop` and `stopStranded`: the completion of Stop 8's stop contract.
  A refused stop used to be treated as "session is gone", and the UI
  cancelled it — but a Stop press racing the 120 s cap is refused
  precisely BECAUSE the session is alive and finalizing, so the cancel
  destroyed the answer to a question the user had just spent two minutes
  asking. Now a refusal keeps waiting for the terminal event, with a
  20 s bounded recovery (`STOP_RECOVERY_MS`) for the case where the
  session really is gone — neither stuck finalizing forever nor a live
  session killed.
- `markdown/`: the security-critical renderer. Links are NOT parsed — no
  href exists, so there is nothing to sanitize. Every string is a React
  text node; the streaming invariant (every prefix renders identically to
  the batch render) is enforced by the parser being a pure function of the
  full source. Emphasis nesting is capped (24 deep, 1000 pairs, and
  resolution is skipped entirely past 20,000 characters) because model
  output is untrusted and unbounded nesting overflowed the render stack —
  and React unmounts the whole root on an uncaught render error, which
  turns a weird answer into a blank app. `MarkdownBoundary` falls back to
  plain text if rendering ever throws anyway. Read the hardening test
  suite to see what all of this is defending against.

## After the tour

You should now be able to answer, without looking: (1) why claiming
`self._active` must happen before the connect await — and why that alone
wasn't enough once commands awaited DPAPI reads; (2) why a KeepAlive
after CloseStream is a bug; (3) why retry-after-first-delta is forbidden;
(4) why the prompt must be byte-stable; (5) why links aren't parsed;
(6) why a refused stop must NOT cancel the session; (7) why
`sink.attach(window)` is the most important line in `app.py`. If any of
those is fuzzy, that's your re-read list — then go to
[02-design-decisions.md](02-design-decisions.md).
