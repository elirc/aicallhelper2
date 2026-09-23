# ARCHITECTURE.md — the system map

AI Call Assistant v3 is a push-to-record interview copilot for Windows.
Press Record while the other person is asking their question; the app
captures system output audio (WASAPI loopback of the default output device
— everything it plays, not only the caller; no microphone is opened),
streams it to Deepgram so a live transcript renders while they are still
speaking, and on Stop & Answer streams an LLM answer grounded in the active
profile. Each answer is single-turn: the prompt holds the profile, call
type, style and the current question, never earlier answers. The design
target everything below serves is **~1 second from the Stop press to the
first word of the answer** — every seam, thread handoff, and pre-warm here
either keeps work out of that window or keeps a failure from silently
eating it. It is a target, not a measured guarantee (see the metrics under
"Commands and events").

This is the canonical reference for the CURRENT implementation. The guided
version of the same material is `docs/learn/`; the dated history of how it
got here is `fabledocs/REPORT.md` and `fabledocs/PROJECT-REVIEW-2026-09-19.md`.
Module docstrings are authoritative for exact constants; this map names
functions rather than line numbers so it does not rot.

## The pipeline

Each stage is labelled with the thread or process that runs it.

```
speaker's voice — whatever any app plays on the default output
      |
      v
WASAPI loopback device (Windows audio engine)
      |  interleaved i16 chunks at the device rate, ~125 ms each
      v
PortAudio callback thread            app_core/audio/capture.py
  i16 -> float mono -> StreamingResampler (16 kHz, anti-aliased FIR)
  -> 2048-sample frames (128 ms) + RMS   app_core/audio/downsample.py
      |  loop.call_soon_threadsafe(on_frame)
      v
core asyncio loop ("core-loop" daemon thread)
  SessionManager._on_frame -> DeepgramStream.send -> sender task
      |  binary PCM over one WebSocket   app_core/stt/client.py
      v
Deepgram (network) -- Results JSON --> reader task
  -> TranscriptAccumulator -> stt:partial   app_core/stt/frames.py

  on Stop (command, arrives via the other channel — see below):
  stop-and-drain (pre-Stop audio delivered, device closed) ->
  CloseStream -> final transcript -> build_prompt ->
  provider.build_request -> stream_answer (retry once) ->
  httpx SSE over the shared, pre-warmed client -> SSEParser ->
  text deltas -> llm:delta ... StreamEnd -> llm:done | session:error
      |          app_core/llm/{prompt,retry,wire,sse,anthropic,groq}.py
      v
WebviewEventSink queue -> pump task (core loop) -> batches of <= 64
  -> evaluate_js on a bounded dispatch thread (one batch at a time)
      |          app_core/bridge/events.py
      v
WebView2 renderer (separate process)     frontend/src/
  CustomEvent "app:event" -> subscribeAppEvents -> reducer (state.ts)
  (llm:delta coalesced to one paint per animation frame)
  -> <Markdown> -> answer text on screen
```

Commands travel the opposite direction on a separate channel:

```
React -> window.pywebview.api.<command>() -> pywebview worker thread
  (JsApi method, app_core/bridge/api.py) -> run_coroutine_threadsafe
  -> core loop -> Result envelope back -> the promise resolves
```

The threads, for orientation:

- **main thread** — `webview.start()` (app.py); the pywebview/WebView2 GUI
  loop. Blocks until exit.
- **core-loop** (daemon) — the asyncio loop. Owns ALL session state, the
  STT and LLM tasks, and the event pump. Nothing else touches that state.
- **audio worker** — ONE executor thread (`machine.py`) on which every
  blocking PortAudio call runs: device open, stop-and-drain, terminate.
  Serializing them there keeps the loop responsive during device
  lifecycle and makes start/stop ordering explicit.
- **PortAudio callback thread** — minimal numpy work only; an exception
  escaping it kills the stream silently, so `capture.py` drops the chunk.
- **pywebview worker threads** — js_api handlers; the ones that reach the
  loop block on `run_coroutine_threadsafe(...).result()` with a 30 s cap.
- **event dispatch threads** — `WebviewEventSink` runs each `evaluate_js`
  on a throwaway daemon thread with a deadline (`DISPATCH_TIMEOUT_S`), and
  caps how many abandoned ones may exist (`MAX_HUNG_DISPATCHES`).
- **`asyncio.to_thread` workers** — DPAPI reads and settings writes:
  blocking work the loop must not do itself.
- **shell helpers** (app.py) — protection retries, heartbeat watchdog,
  focus-signal wait, bounds-save debounce, the background `_build_core`;
  plus the hotkey message loop (bridge/hotkey.py).

## Module map

Each package `__init__.py` holds only the package docstring.

- `app.py` — the shell: single instance, content protection, global
  hotkey, geometry persistence and layout modes, crash log, startup
  readiness, renderer recovery; the load-bearing line is
  `sink.attach(window)` in `_wire_window`.
- `app_core/errors.py` — `AppError` and the closed `ErrorCode` set every
  core->frontend failure must map into.
- `app_core/contracts.py` — the Protocols (`SttStream`, `AudioSource`,
  `EventSink`, `Warmer`, `SettingsReader`), `AnswerConfig` and
  `DEEPGRAM_SECRET_ID`, with NO heavy imports, so the settings store, the
  STT client and the bridge can be imported before httpx/websockets/numpy
  exist. `session/machine.py` re-exports them.
- `app_core/audio/capture.py` — WASAPI loopback capture (PyAudioWPatch,
  lazily imported); device callback -> finished frames on the loop;
  `stop_and_drain` delivers the pre-Stop remainder before closing.
- `app_core/audio/downsample.py` — vectorized audio math: the
  phase-continuous anti-aliased `StreamingResampler`, mono mixdown, RMS.
- `app_core/bridge/api.py` — `JsApi`, every command the frontend can
  call; Result envelopes, never throws across the boundary; the status
  snapshot (`get_status`) and its revision counter.
- `app_core/bridge/events.py` — `WebviewEventSink`: the ordered, batched,
  bounded core->page event pump over `evaluate_js`.
- `app_core/bridge/hotkey.py` — global hotkey via Win32 `RegisterHotKey`
  on a message-loop thread; pure accelerator parser; honest status.
- `app_core/bridge/protection.py` — `SetWindowDisplayAffinity` applied
  AND verified by reading the affinity back.
- `app_core/llm/base.py` — the `AnswerProvider` Protocol,
  `ProviderRequest`, `ProviderFailure` and its `FailureKind`s,
  `StreamEnd`/`AnswerResult` (how an answer ended), the registry, and
  `default_registry()`.
- `app_core/llm/anthropic.py` — the default provider; all
  Anthropic-specific wire, completion (`message_stop`) and prompt-caching
  logic lives here.
- `app_core/llm/groq.py` — the "fastest" preset; doubles as the template
  for any OpenAI-compatible provider (`finish_reason` / `[DONE]`).
- `app_core/llm/prompt.py` — provider-independent prompt construction,
  byte-stable, split so the cache breakpoint lands after the profile.
- `app_core/llm/retry.py` — the retry-once policy (only connection-level
  failures, never after a delta, byte-identical request) and the
  empty-answer post-condition.
- `app_core/llm/sse.py` — incremental SSE parser over raw bytes; survives
  splits mid-line, mid-JSON, and mid UTF-8 character.
- `app_core/llm/warm.py` — `PreWarmer`: throttled fire-and-forget GET
  `<origin>/v1/models` so a pooled TLS connection is waiting after Stop.
- `app_core/llm/wire.py` — the shared httpx SSE transport skeleton;
  classifies transport trouble into `FailureKind`s.
- `app_core/session/machine.py` — the session state machine: one live
  pipeline, the supersession/ticket/timeout rules, the stop-and-drain
  cutoff, all session events.
- `app_core/store/bounds.py` — pure window-geometry math: sanitizing
  saved bounds against ALL connected work areas, docking, per-mode plans.
- `app_core/store/secrets.py` — DPAPI encryption that fails CLOSED
  (`SecretEncryptionError`); legacy `plain:` values still decode;
  undecryptable values read as unset.
- `app_core/store/settings.py` — the settings file: per-field validation,
  atomic writes, backup-before-first-write of an unreadable file, save
  revision precondition, write-only secrets, `view()` for the frontend.
- `app_core/stt/client.py` — the Deepgram WebSocket client: pre-open
  buffering, KeepAlive, CloseStream-behind-audio, close classification.
- `app_core/stt/frames.py` — strict parsing of Deepgram frames (hostile
  input) and the transcript accumulator.
- `tools/release_meta.py` — release metadata: the one version, drift and
  pin checks, `build_info.json` (build time only, not shipped code).
- `frontend/src/main.tsx` — React root, StrictMode.
- `frontend/src/state.ts` — the pure reducer (`State`, `Action`,
  `reduce`), status copy (`statusFor`), silence tracking.
- `frontend/src/App.tsx` — plumbing and layout: event subscription, delta
  coalescing, command coordination, session adoption, view switching.
- `frontend/src/bridge.ts` — the only doorway to the core: typed command
  wrappers, `subscribeAppEvents`, the heartbeat, status parsing.
- `frontend/src/types.ts` — the shared shapes: `Result`, `ErrorCode`,
  `SettingsView`, `Metrics`, `ProtectionVerdict`, event detail.
- `frontend/src/clipboard.ts` — clipboard write with the file:// fallback
  (`navigator.clipboard` needs a secure context the packaged app lacks).
- `frontend/src/format.ts` — mm:ss timer, latency chip and tooltip text.
- `frontend/src/styles.css` — all styling; no CSS-in-JS.
- `frontend/src/components/AnswerPanel.tsx` — answer display, sticky
  scroll, Copy, Regenerate, the latency chip.
- `frontend/src/components/PrompterView.tsx` — the prompter strip.
- `frontend/src/components/ProtectionNotice.tsx` — the tri-state
  capture-protection indicator rendered in every view.
- `frontend/src/components/HistoryBar.tsx` — prev/next/clear over the
  history entries.
- `frontend/src/components/SettingsPanel.tsx` — the settings form; keys
  are write-only (it only ever sees has-key booleans and storage status).
- `frontend/src/markdown/blocks.ts` — block-level markdown subset parser;
  plain data out, links deliberately not parsed.
- `frontend/src/markdown/inline.ts` — inline parser: bold/italic/code
  with flanking rules, nesting caps, backslash escapes.
- `frontend/src/markdown/Markdown.tsx` — the renderer: every string a DOM
  text node, per-block memo, `MarkdownBoundary` plain-text fallback.
- `frontend/src/__tests__/` — the Vitest suites plus `testutils.tsx`
  (mock js_api + event emitter); documented per-test, with current
  counts, in `docs/TESTING.md`.

## The seams

Every dependency of the session machine is injected, the behavioral ones as
Protocols (defined in `app_core/contracts.py`, except `AnswerProvider` in
`llm/base.py`), so the session rules are tested without the real thing — no
test touches the network, a live provider, or an audio device. The five
load-bearing ones:

- **`SttStream`** — `connect / send / finalize / abort`; isolates the
  session rules from Deepgram's wire behavior. `test_machine.py` drives
  the rules with fakes; `test_deepgram_client.py` proves `DeepgramStream`
  honors the contract against a scripted loopback WebSocket server.
- **`AnswerProvider`** (llm/base.py) — everything vendor-specific lives
  behind it; the machine, retry policy, metrics, events, and views are
  provider-agnostic. `stream()` yields text deltas and then one
  `StreamEnd`, or raises `ProviderFailure`. Adding a provider is one module
  plus one `default_registry()` line (README, "How to add an answer
  provider").
- **`AudioSource`** — `start / stop / stop_and_drain`. `LoopbackCapture`
  is the only real implementation; everything else in the repo sees
  frames as bytes.
- **`EventSink`** — `emit`. Tests record the emitted stream and assert on
  names, payloads, and ORDER; `WebviewEventSink` is the production sink.
- **`SettingsReader`** — split by blocking behavior: `answer_config()` is
  a memory-only read, safe on the loop; `get_secret()` may hit DPAPI and
  callers must wrap it in `asyncio.to_thread`. The split encodes which
  reads the loop may do.

Smaller instances of the same pattern: `Warmer` (contracts.py),
`Keystore` (store/secrets.py), `DisplayAffinityApi` (bridge/protection.py).

**The js_api/event boundary is two independent channels.** Commands are
request/response — the page calls, a worker thread blocks, a Result
envelope comes back. Events are fire-and-forget — the core emits, the
pump batches, the page dispatches. They fail independently, and not
hypothetically: for four commits `sink.attach(window)` was missing in
app.py, the pump parked on `while self._window is None`, and every
command still worked — the app looked alive while no transcript, answer,
or error could reach the page. Every suite was green: each side mocked
the other. `tests/test_app_wiring.py` now builds the real `App` against
a fake window and asserts emitted events actually land. Because events can
be missed (page not yet subscribed, reload, timed-out dispatch), the page
also PULLS an authoritative snapshot with `get_status()` — see "Status
snapshot and ordering".

## The three user actions

**Record, then Stop**

1. Record pressed -> `bridge.startSession()`; the UI shows "starting".
2. `start_session()`: claims a command ticket before any await, reads
   both keys off-loop (DPAPI), re-checks the ticket (a stalled read must
   not supersede a NEWER command), supersedes any active session,
   installs the new one, warms the origin, spawns the pipeline, returns
   the id; the frontend adopts it and replays events buffered in flight.
   Stale responses from an older command are recognised on the page and
   never change the newer command's state.
3. Capture (on the audio worker) and the Deepgram connect start in
   parallel; frames captured before the socket opens buffer in the stream
   (~15 s, oldest dropped) and flush the instant it opens. The 120 s cap
   is armed when capture is actually running — not when the socket
   connects — and `session:recording {deadlineMs, capMs}` tells the page
   the deadline so its countdown matches the core.
4. Each device chunk: resample on the PortAudio thread, post 128 ms
   frames to the loop, `send()` to Deepgram, emit one `audio:level`
   (coalesced latest-wins in the event queue).
5. Results frames -> accumulator -> `stt:partial` with the full
   transcript so far -> the live transcript renders.
6. Stop pressed -> `stop_session(sid)`. The stop contract accepts only
   the live, not-yet-stopping session (including one still connecting);
   anything else raises "not taken" (the UI then waits, bounded, rather
   than cancelling — a refused stop can mean the 120 s cap is already
   finalizing).
7. `_begin_stop`: **the latency clock starts at Stop acceptance**, BEFORE
   the drain — the user waits for the drain, so it counts. Phase ->
   finalizing, origin re-warmed, and the audio worker runs
   `stop_and_drain`: device input stops and every pre-Stop sample
   (including the final partial frame) is delivered to the loop, bounded
   at `audio_drain_s` (2 s). Frames are accepted until that drain
   completes (the **capture cutoff**, "Rule 4") and rejected afterwards,
   so nothing can follow CloseStream. Finalize then queues CloseStream
   BEHIND the drained frames, waits for the server flush (5 s cap), and
   takes the transcript from the accumulator. Empty transcript ->
   `no_speech`, never an LLM call. A deferred stop during connect waits
   for the socket, then follows the same path.
8. Answer: prompt built, `ProviderRequest` assembled once, streamed over
   the pre-warmed shared client with retry-once; deltas -> `llm:delta`
   under the 10 s first-token / 60 s total watchdogs. The provider must
   end with its real terminal event: an in-stream error, a stream that
   ends early, or an answer with no text becomes `session:error`, never a
   blank success. Otherwise `llm:done` carries the transcript, answer,
   `finish` and metrics. The entry retires to history; idle.

**Ask**

1. `ask(text)` validates and trims FIRST — garbage input must not kill a
   live session — then ticket, key read, supersede.
2. The new session starts directly in `answering`; its metrics clock is
   ask-accept and `audioDrainMs` / `sttFinalizeMs` are exactly 0.
3. The question is emitted as one final `stt:partial` so it renders
   through the same path as a recorded transcript, then step 8 above.

**Regenerate**

1. Regenerate is not a core concept: the frontend calls `ask` with the
   viewed entry's question (App.tsx).
2. That ask supersedes anything active and lands as a NEW history entry —
   an existing answer is never overwritten.

## State ownership

- **Core session state** (SessionManager, on the loop): the single active
  session slot, its phase and flags, the command ticket, every timer.
  Touched only on the core loop; the ticket is claimed before any await.
- **The core is the source of truth.** The frontend's phase is a
  projection of core events plus the `get_status()` snapshot; it waits
  for `llm:done` or `session:error` and drops anything with a stale
  session id. Its one bounded self-recovery (after a REFUSED stop with no
  further sign of life) is disarmed as soon as the session shows progress
  and is scoped to that session, so it can never retire a live answer or
  a later session.
- **Frontend view state** (App.tsx / state.ts): the history entries (max
  6), viewed index, ask input, recording deadline, RMS / last-heard-audio
  second, protection verdict, settings-open flag, settings draft. History
  is view state ONLY — the core keeps none, it is never sent as prompt
  context, and a renderer reload loses it.
- **Settings on disk** (`%APPDATA%\AICallAssistant\settings.json`, via
  store/settings.py): profiles (max 20) in plain JSON, provider, style,
  hotkey, alwaysOnTop, per-layout bounds, and secrets as DPAPI `enc:`
  blobs (legacy `plain:` values are read and re-encrypted on the next
  successful save). Key material never enters a view — the frontend only
  ever sees has-key booleans and each key's storage status. `crash.log`
  sits next to it.

## Commands and events

Every command returns `{ok: true, value} | {ok: false, error: {code,
message}}` — nothing throws across the boundary, and a command that takes
longer than 30 s resolves to an `internal` error instead of hanging its
worker thread. The `code` set is closed: `errors.py` / `types.ts`.

| command                    | value on ok                                 |
| -------------------------- | ------------------------------------------- |
| `get_settings()`           | SettingsView + hotkey status fields         |
| `set_settings(patch)`      | the fresh SettingsView (a patch built on an older `settingsRevision` is rejected) |
| `get_status()`             | `{revision, coreReady, core, coreError, protection, session: {id, phase}, pageGeneration}` — never waits for the core; `phase` is `unknown` if the loop did not answer in time |
| `start_session()`          | session id (`"s1"`, `"s2"`, …)              |
| `stop_session(sessionId)`  | `null`; error = the stop was not taken      |
| `ask(text)`                | session id                                  |
| `cancel_session(sessionId)`| `null`, always (fire-and-forget, rule 10)   |
| `heartbeat()`              | `null` (renderer liveness for the watchdog) |
| `set_close_guard(active)`  | `null`; the page reports unsaved work: the first native close is cancelled once and `window:close-requested` is emitted; a second close within 10 s, or a dead page, always closes |
| `dock_window()`            | `null`; moves the window top-centre of its display (camera line), synchronously on the worker thread |
| `open_external(url)`       | `null`; https only — frontend-uncalled      |

Events arrive as one DOM `CustomEvent("app:event")` per event, in emit
order while nothing times out (see below). Every payload carries `seq` and
`pageGen`; session-scoped events also carry `sessionId`. Beyond those:

| event                 | payload                                        |
| --------------------- | ---------------------------------------------- |
| `session:recording`   | `{deadlineMs, capMs}` — capture is running; cap deadline (wall clock ms) |
| `stt:partial`         | `{text, isFinal}` — FULL transcript, not delta |
| `audio:level`         | `{rms}` — 0..1; latest-wins while queued; stops after Stop |
| `session:autostopped` | `{}` — the 120 s cap tripped; finalizing now   |
| `llm:delta`           | `{delta}` — answer text, append-only           |
| `llm:done`            | `{transcript, answer, finish, callType, metrics}` |
| `session:error`       | `{error: {code, message}}` — at most one       |

`finish` is `complete`, `truncated` (hit the output-token cap) or
`refused`. `metrics` is `{audioDrainMs, sttFinalizeMs, firstTokenMs,
totalMs}` in integer milliseconds: `firstTokenMs`/`totalMs` run from Stop
acceptance (ask-accept for typed asks) and INCLUDE the drain;
`audioDrainMs` is Stop acceptance to drain complete; `sttFinalizeMs` is
drain complete to final transcript. They are core-side times — bridge
transit and paint are not included.

Window-scoped events (no `sessionId`): `hotkey:toggle {}` (the global
hotkey fired); `protection:ok` / `protection:failed` (whether Windows
confirmed `WDA_EXCLUDEFROMCAPTURE`), carrying the status `revision`;
`core:ready` / `core:failed` (the background core build finished or
failed); `window:close-requested` (see `set_close_guard`).

## Status snapshot and ordering

`get_status()` is the page's authoritative snapshot: core readiness, the
protection verdict (`protected` | `unprotected` | `unknown`), and the live
session's id and phase. `revision` is monotonic and increases whenever any
of those change; protection push events carry the same counter. The page
calls `get_status()` on every load/reload and adopts a snapshot or event
only if its revision is at least the last one seen, so an older snapshot
cannot overwrite a fresher verdict. After a reload it re-adopts a session
that is still live instead of losing track of it.

Event dispatch is serialized through one queue, but a timed-out
`evaluate_js` is an UNCERTAIN delivery: it may still execute later. The
pump therefore (see the `events.py` docstring for the exact rules): stamps
every payload with `seq` (unchanged on retry) and `pageGen` so the page
drops duplicates, out-of-order stale deliveries and events aimed at a
previous page; re-sends only the reserved events (terminal session events,
protection and core status), which the page applies idempotently; bounds
the queue, shedding expendable events oldest-first but never reserved
ones; and stops spawning dispatch threads against a renderer that is
plainly not answering. That limit is counted per page generation, so a
watchdog reload gets deliveries again. A reload advances the generation
exactly once, before `load_url`.

## Startup, readiness and renderer recovery

`App.__init__` builds only what the window needs (settings, event sink,
hotkey manager, `JsApi` with no machine). `run()` computes the initial
geometry (`initial_window_kwargs`, logical units — the process is still
DPI-unaware), creates the window, then starts `_build_core` on a daemon
thread: it imports httpx, the provider stack, websockets, numpy and the
audio stack, constructs the shared `httpx.AsyncClient`, `PreWarmer` and
`SessionManager`, and attaches the core to `JsApi`. httpx is typing-only in
`base.py`, `retry.py`, `warm.py`, `anthropic.py` and `groq.py`, and
`wire.py` imports it inside `stream_sse_data`, so `default_registry()` —
which the settings store needs before the window — loads no network stack.

Readiness is explicit: success emits `core:ready`, failure emits
`core:failed` and is logged to crash.log, and `get_status().coreReady`
reports it. Session commands wait for the core only while it is still
starting (up to `CORE_READY_TIMEOUT_S`); once it has failed they answer at
once with "The app core failed to start…". `get_settings` / `set_settings`
/ `get_status` / `dock_window` never wait.

The renderer watchdog does not judge a page that is still booting: until
the page has loaded or heartbeated, it only acts once a boot grace period
(`BOOT_GRACE_S`) has passed, because probing a booting page blocks inside
pywebview and would misread the boot as a death. After that, a stale
heartbeat triggers a direct `evaluate_js` probe, and only a failed or hung
probe reloads the page (at most once per `RELOAD_COOLDOWN_S`). A reload
re-arms the boot gate; the new page resynchronizes from `get_status()`.

## Geometry and layout modes

`SettingsStore.layout_mode()` is `full` or `prompter`; each mode has its
own bounds key (`windowBounds`, `prompterBounds`). `MIN_SIZE` is
`(380, 160)` — the create-time envelope WinForms enforces for both layouts
— and `FULL_MIN_HEIGHT` (520) is applied when restoring the full layout.
Switching layouts flushes the old mode's bounds under its own key, sets
the new mode BEFORE moving (so move/resize callbacks save under the new
key), then restores that mode's saved position if it is visible on ANY
connected display, or docks it top-centre of the current display at its
saved/default size (`app_core/store/bounds.py`). A saved position is only
reused if enough of the window, including a reachable title-bar strip,
lands on a work area. Runtime work areas come from the monitor APIs
divided by `_runtime_scale()` (`GetDpiForWindow / 96`, the same factor
pywebview applies), so every value handed to `window.move/resize` is
logical. Physical placement across mixed-DPI displays is validated
natively, not by the unit tests (see `fabledocs/RELEASE-CHECKLIST.md`).

## Profiles

`settings.json` holds `profiles[]` (`id`, `name`, `callType`, `focus`,
`resume`, `jobDescription`, `notes`; at most 20, `MAX_PROFILES`) and
`activeProfileId`; `resume`/`jobDescription` at top level are write-only
mirrors of the active profile. `_load` migrates a legacy file into one
`Default` profile in memory (never writes). `view()` and `answer_config()`
snapshot `self._data` once, and `_active_profile()` falls back to the first
profile so nothing raises on the loop. `AnswerConfig` carries
`call_type`/`focus`/`notes`; `build_prompt` places them in the cached
prefix in fixed order (role, resume, JD, focus, notes, grounding).

## Settings durability

Writes are serialized under a lock and atomic (temp file + replace). A
file that is missing is a first run. A file that exists but cannot be read
or parsed loads as defaults and is PRESERVED as a uniquely named `.bak`
before the first write of any kind (including the automatic geometry
save); if it cannot be preserved, the write is refused. Saves may carry a
`baseRevision` precondition so a save built on an older view cannot
overwrite a newer one, whatever order the bridge threads run in. New keys
are encrypted with DPAPI or the save fails visibly with the previous key
left untouched.

## Where to change what

| I want to change…                        | go to                          |
| ---------------------------------------- | ------------------------------ |
| Deepgram model / stream params           | `DEEPGRAM_URL`, stt/client.py  |
| Anthropic model, caching, max tokens     | constants, llm/anthropic.py    |
| Groq model (404 usually = retired)       | `MODEL`, llm/groq.py — then release; users cannot edit it |
| Add an answer provider                   | new llm/<name>.py + registry¹  |
| Prompt wording / grounding rules         | llm/prompt.py                  |
| Pipeline timeouts (drain/5/10/60/120 s)  | `Timeouts`, session/machine.py²|
| Retry policy                             | llm/retry.py                   |
| Frame size, resampler filter             | audio/downsample.py            |
| Session rules (supersession, stop, cap)  | session/machine.py             |
| Add a setting                            | store/settings.py³             |
| Add an event                             | emit in machine.py, handle in state.ts (`reduce`) |
| Add a command                            | bridge/api.py + `RawApi` in frontend/src/bridge.ts |
| Answer rendering / markdown subset       | frontend/src/markdown/         |
| Status copy, phases, history depth       | frontend/src/state.ts          |
| Hotkey, capture protection, shell        | app.py + bridge/hotkey.py, bridge/protection.py |
| Version, dependencies, packaging         | pyproject.toml (+ constraints.txt), aica.spec, installer.iss, tools/release_meta.py |

Core paths are relative to `app_core/`. ¹ `default_registry()` in
llm/base.py — the README has the full recipe. ² Keep wire.py's transport
backstops ABOVE the machine's timers so they never fire first. ³ Touch
defaults + `_load` + `patch` + `view`, then types.ts + SettingsPanel.tsx.
Whatever you change, `docs/TESTING.md` names the tests that judge it.
