# ARCHITECTURE.md — the system map

AI Call Assistant v3 is a push-to-record interview copilot for Windows.
Press Record while the other person is asking their question; the app
captures system audio (WASAPI loopback — the speakers, not the mic),
streams it to Deepgram so a live transcript renders while they are still
speaking, and on Stop & Answer streams an LLM answer grounded in the saved
resume and job description. The one hard constraint everything below
serves: **~1 second from the Stop press to the first word of the answer**
— every seam, thread handoff, and pre-warm here either keeps work out of
that window or keeps a failure from silently eating it. This document is
reference; the guided version of the same material is `docs/learn/`.

## The pipeline

Each stage is labelled with the thread or process that runs it.

```
speaker's voice — whatever the call app plays on the default output
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
  CloseStream -> final transcript -> build_prompt ->
  provider.build_request -> stream_answer (retry once) ->
  httpx SSE over the shared, pre-warmed client -> SSEParser ->
  text deltas -> llm:delta ... llm:done
      |          app_core/llm/{prompt,retry,wire,sse,anthropic,groq}.py
      v
WebviewEventSink queue -> pump task (core loop) -> batches of <= 64
  -> evaluate_js via asyncio.to_thread (one batch at a time, in order)
      |          app_core/bridge/events.py
      v
WebView2 renderer (separate process)     frontend/src/
  CustomEvent "app:event" -> subscribeAppEvents -> App reducer
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
- **PortAudio callback thread** — minimal numpy work only; an exception
  escaping it kills the stream silently, so `capture.py` drops the chunk.
- **pywebview worker threads** — js_api handlers; the ones that reach the
  loop block on `run_coroutine_threadsafe(...).result()` with a 30 s cap.
- **asyncio.to_thread workers** — `evaluate_js` dispatch, DPAPI reads,
  settings writes: everything that would otherwise block the loop.
- **shell helpers** (app.py) — protection retries, heartbeat watchdog,
  focus-signal wait, bounds-save debounce; plus the hotkey message loop
  (bridge/hotkey.py).

## Module map

Each package `__init__.py` holds only the package docstring.

- `app.py` — the shell: single instance, content protection, global
  hotkey, geometry persistence, crash log, renderer recovery; the
  load-bearing line is `sink.attach(window)` in `_wire_window`.
- `app_core/errors.py` — `AppError` and the closed `ErrorCode` set every
  core->frontend failure must map into.
- `app_core/audio/capture.py` — WASAPI loopback capture (PyAudioWPatch,
  lazily imported); device callback -> finished frames on the loop.
- `app_core/audio/downsample.py` — vectorized audio math: the
  phase-continuous anti-aliased `StreamingResampler`, mono mixdown, RMS.
- `app_core/bridge/api.py` — `JsApi`, every command the frontend can
  call; Result envelopes, never throws across the boundary.
- `app_core/bridge/events.py` — `WebviewEventSink`: the ordered, batched
  core->page event pump over `evaluate_js`.
- `app_core/bridge/hotkey.py` — global hotkey via Win32 `RegisterHotKey`
  on a message-loop thread; pure accelerator parser; honest status.
- `app_core/bridge/protection.py` — `SetWindowDisplayAffinity` applied
  AND verified by reading the affinity back.
- `app_core/llm/base.py` — the `AnswerProvider` Protocol,
  `ProviderRequest`, `ProviderFailure`, the registry, and
  `default_registry()`.
- `app_core/llm/anthropic.py` — the default provider; all
  Anthropic-specific wire and prompt-caching logic lives here.
- `app_core/llm/groq.py` — the "fastest" preset; doubles as the template
  for any OpenAI-compatible provider.
- `app_core/llm/prompt.py` — provider-independent prompt construction,
  byte-stable, split so the cache breakpoint lands after resume+JD.
- `app_core/llm/retry.py` — the retry-once policy: only connection-level
  failures, never after a delta, byte-identical request.
- `app_core/llm/sse.py` — incremental SSE parser over raw bytes; survives
  splits mid-line, mid-JSON, and mid UTF-8 character.
- `app_core/llm/warm.py` — `PreWarmer`: throttled fire-and-forget GET
  `<origin>/v1/models` so a pooled TLS connection is waiting after Stop.
- `app_core/llm/wire.py` — the shared httpx SSE transport skeleton;
  classifies wire trouble into the five `FailureKind`s.
- `app_core/session/machine.py` — the session state machine: one live
  pipeline, the supersession/ticket/timeout rules, all events. The
  Protocols the machine depends on are defined here.
- `app_core/store/bounds.py` — pure window-geometry sanitizer
  (unplugged-monitor restore rules).
- `app_core/store/secrets.py` — DPAPI encryption with an honestly-marked
  `plain:` fallback; undecryptable values read as unset.
- `app_core/store/settings.py` — the settings file: per-field validation,
  atomic writes, write-only secrets, `view()` for the frontend.
- `app_core/stt/client.py` — the Deepgram WebSocket client: pre-open
  buffering, KeepAlive, CloseStream-behind-audio, close classification.
- `app_core/stt/frames.py` — strict parsing of Deepgram frames (hostile
  input) and the transcript accumulator.
- `frontend/src/main.tsx` — React root, StrictMode.
- `frontend/src/App.tsx` — the whole view state machine: reducer, event
  subscription, delta coalescing, session adoption, history.
- `frontend/src/bridge.ts` — the only doorway to the core: typed command
  wrappers, `subscribeAppEvents`, the heartbeat.
- `frontend/src/types.ts` — the shared shapes: `Result`, `ErrorCode`,
  `SettingsView`, `Metrics`, event detail.
- `frontend/src/clipboard.ts` — clipboard write with the file:// fallback
  (`navigator.clipboard` needs a secure context the packaged app lacks).
- `frontend/src/format.ts` — mm:ss timer, latency chip and tooltip text.
- `frontend/src/styles.css` — all styling; no CSS-in-JS.
- `frontend/src/components/AnswerPanel.tsx` — answer display, sticky
  scroll, Copy, Regenerate, the latency chip.
- `frontend/src/components/HistoryBar.tsx` — prev/next/clear over the
  history entries; hidden below 2 entries.
- `frontend/src/components/SettingsPanel.tsx` — the settings form; keys
  are write-only (it only ever sees `has<Name>Key` booleans).
- `frontend/src/markdown/blocks.ts` — block-level markdown subset parser;
  plain data out, links deliberately not parsed.
- `frontend/src/markdown/inline.ts` — inline parser: bold/italic/code
  with flanking rules, nesting caps, backslash escapes.
- `frontend/src/markdown/Markdown.tsx` — the renderer: every string a DOM
  text node, per-block memo, `MarkdownBoundary` plain-text fallback.
- `frontend/src/__tests__/` — the 148 Vitest tests plus `testutils.tsx`
  (mock js_api + event emitter); documented per-test in `docs/TESTING.md`.

## The seams

Every dependency of the session machine is injected, the behavioral ones as
Protocols, so the session rules are tested without the real thing — no test
touches the network, a live provider, or an audio device. The five
load-bearing ones:

- **`SttStream`** (session/machine.py) — `connect / send / finalize /
  abort`; isolates the session rules from Deepgram's wire behavior.
  `test_machine.py` drives the rules with fakes; `test_deepgram_client.py`
  proves `DeepgramStream` honors the contract against a scripted loopback
  WebSocket server.
- **`AnswerProvider`** (llm/base.py) — everything vendor-specific lives
  behind it; the machine, retry policy, metrics, events, and views are
  provider-agnostic. Adding a provider is one module plus one
  `default_registry()` line (README, "How to add an answer provider").
- **`AudioSource`** (session/machine.py) — `start / stop`.
  `LoopbackCapture` is the only real implementation; everything else in
  the repo sees frames as bytes.
- **`EventSink`** (session/machine.py) — `emit`. Tests record the emitted
  stream and assert on names, payloads, and ORDER; `WebviewEventSink` is
  the production sink.
- **`SettingsReader`** (session/machine.py) — split by blocking behavior:
  `answer_config()` is a memory-only read, safe on the loop;
  `get_secret()` may hit DPAPI and callers must wrap it in
  `asyncio.to_thread`. The split encodes which reads the loop may do.

Smaller instances of the same pattern: `Warmer` (session/machine.py),
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
a fake window and asserts emitted events actually land.

## The three user actions

**Record, then Stop**

1. Record pressed -> `bridge.startSession()`; the UI shows "starting".
2. `start_session()`: claims a command ticket before any await, reads
   both keys off-loop (DPAPI), re-checks the ticket (a stalled read must
   not supersede a NEWER command), supersedes any active session,
   installs the new one, warms the origin, spawns the pipeline, returns
   the id; the frontend adopts it and replays events buffered in flight.
3. Capture and the Deepgram connect start in parallel; frames captured
   before the socket opens buffer in the stream (~15 s, oldest dropped)
   and flush the instant it opens. Phase -> recording; 120 s cap armed.
4. Each device chunk: resample on the PortAudio thread, post 128 ms
   frames to the loop, `send()` to Deepgram, emit one `audio:level`.
5. Results frames -> accumulator -> `stt:partial` with the full
   transcript so far -> the live transcript renders.
6. Stop pressed -> `stop_session(sid)`. The stop contract accepts only
   the live, recording, not-yet-stopping session; anything else raises
   "not taken" (the UI then waits bounded rather than cancelling — a
   refused stop can mean the 120 s cap is already finalizing).
7. `_begin_stop`: **the latency clock starts**, audio stops, phase ->
   finalizing, origin re-warmed. Finalize queues CloseStream BEHIND any
   in-flight frames, waits for the server flush (5 s cap), and takes the
   transcript from the accumulator. Empty transcript -> `no_speech`,
   never an LLM call.
8. Answer: prompt built, `ProviderRequest` assembled once, streamed over
   the pre-warmed shared client with retry-once; deltas -> `llm:delta`
   under the 10 s first-token / 60 s total watchdogs; `llm:done` carries
   transcript, answer, and metrics. The entry retires to history; idle.

**Ask**

1. `ask(text)` validates and trims FIRST — garbage input must not kill a
   live session — then ticket, key read, supersede.
2. The new session starts directly in `answering`; its metrics clock is
   ask-accept and `sttFinalizeMs` is exactly 0 (there was no STT stage).
3. The question is emitted as one final `stt:partial` so it renders
   through the same path as a recorded transcript, then step 8 above.

**Regenerate**

1. Regenerate is not a core concept: the frontend calls `ask` with the
   viewed entry's question (App.tsx, `onRegenerate`).
2. That ask supersedes anything active and lands as a NEW history entry —
   an existing answer is never overwritten.

## State ownership

- **Core session state** (SessionManager, on the loop): the single active
  session slot, its phase and flags, the command ticket, every timer.
  Touched only on the core loop; the ticket is claimed before any await.
- **The core is the source of truth.** The frontend's phase is a
  projection of core events; it never decides a session ended — it waits
  for `llm:done` or `session:error` and drops anything with a stale
  session id. The one bounded exception is deliberate: after a REFUSED
  stop with no terminal event inside 20 s (`STOP_RECOVERY_MS`, App.tsx)
  the UI recovers to idle — the refusal plus the silence is all the
  evidence there is.
- **Frontend view state** (App.tsx): the history entries (max 6), viewed
  index, ask input, recording seconds, RMS / last-heard-audio second,
  protection warning, settings-open flag. History is view state ONLY —
  the core keeps none, and a renderer reload loses it.
- **Settings on disk** (`%APPDATA%\AICallAssistant\settings.json`, via
  store/settings.py): resume and JD verbatim, provider, style, hotkey,
  alwaysOnTop, windowBounds, secrets as DPAPI-wrapped `enc:`/`plain:`
  blobs. Key material never enters a view — the frontend only ever sees
  `has<Name>Key` booleans. `crash.log` sits next to it.

## Commands and events

Every command returns `{ok: true, value} | {ok: false, error: {code,
message}}` — nothing throws across the boundary, and a command that takes
longer than 30 s resolves to an `internal` error instead of hanging its
worker thread. The `code` set is closed: `errors.py` / `types.ts`.

| command                    | value on ok                                 |
| -------------------------- | ------------------------------------------- |
| `get_settings()`           | SettingsView + hotkey status fields         |
| `set_settings(patch)`      | the fresh SettingsView                      |
| `start_session()`          | session id (`"s1"`, `"s2"`, …)              |
| `stop_session(sessionId)`  | `null`; error = the stop was not taken      |
| `ask(text)`                | session id                                  |
| `cancel_session(sessionId)`| `null`, always (fire-and-forget, rule 10)   |
| `heartbeat()`              | `null` (renderer liveness for the watchdog) |
| `open_external(url)`       | `null`; https only — frontend-uncalled      |

Events arrive as one DOM `CustomEvent("app:event")` per event, in emit
order. Session-scoped events all carry `sessionId`; payloads beyond it:

| event                 | payload                                        |
| --------------------- | ---------------------------------------------- |
| `stt:partial`         | `{text, isFinal}` — FULL transcript, not delta |
| `audio:level`         | `{rms}` — 0..1, one per 128 ms frame           |
| `session:autostopped` | `{}` — the 120 s cap tripped; finalizing now   |
| `llm:delta`           | `{delta}` — answer text, append-only           |
| `llm:done`            | `{transcript, answer, metrics}` — see below    |
| `session:error`       | `{error: {code, message}}` — at most one       |

`llm:done`'s `metrics` is `{sttFinalizeMs, firstTokenMs, totalMs}`, all
integer milliseconds since the Stop press (ask-accept for typed asks).
Window-scoped events (no `sessionId`): `hotkey:toggle {}` (the global
hotkey fired), `protection:ok {}` / `protection:failed {}` (whether
Windows confirmed `WDA_EXCLUDEFROMCAPTURE`).

## Where to change what

| I want to change…                        | go to                          |
| ---------------------------------------- | ------------------------------ |
| Deepgram model / stream params           | `DEEPGRAM_URL`, stt/client.py  |
| Anthropic model, caching, max tokens     | constants, llm/anthropic.py    |
| Groq model (404 usually = retired)       | `MODEL`, llm/groq.py           |
| Add an answer provider                   | new llm/<name>.py + registry¹  |
| Prompt wording / grounding rules         | llm/prompt.py                  |
| Pipeline timeouts (5/10/60/120 s)        | `Timeouts`, session/machine.py²|
| Retry policy                             | llm/retry.py                   |
| Frame size, resampler filter             | audio/downsample.py            |
| Session rules (supersession, stop, cap)  | session/machine.py             |
| Add a setting                            | store/settings.py³             |
| Add an event                             | emit in machine.py, handle in App.tsx (`reduceEvent`) |
| Add a command                            | bridge/api.py + `RawApi` in frontend/src/bridge.ts |
| Answer rendering / markdown subset       | frontend/src/markdown/         |
| Status copy, phases, history depth       | frontend/src/App.tsx           |
| Hotkey, capture protection, shell        | app.py + bridge/hotkey.py, bridge/protection.py |

Core paths are relative to `app_core/`. ¹ `default_registry()` in
llm/base.py — the README has the full recipe. ² Keep wire.py's transport
backstops ABOVE the machine's timers so they never fire first. ³ Touch
defaults + `_load` + `patch` + `view`, then types.ts + SettingsPanel.tsx.
Whatever you change, `docs/TESTING.md` names the tests that judge it.
