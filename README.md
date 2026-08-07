# AI Call Assistant v3

A push-to-record **interview copilot** for Windows. Press **Record** while
the other person is asking their question; the app captures **system audio**
(loopback — what's coming out of your speakers, not your microphone),
renders a **live transcript while they're still speaking**, and on **Stop &
Answer** streams an AI-suggested answer grounded in your saved resume and
job description. The product promise is **~1 second from Stop to the first
word of the answer**, and the window is **invisible to screen sharing**.

v3 is a Python-core rebuild: Python 3.12+/asyncio owns the entire pipeline
(WASAPI loopback capture → Deepgram streaming STT → provider-abstracted LLM
streaming → session state machine → settings/DPAPI secrets); the frontend is
a thin React view hosted in a pywebview (WebView2) window.

## Setup

1. **Python core**

   ```
   python -m venv .venv
   .venv\Scripts\python -m pip install pywebview websockets httpx numpy pywin32 PyAudioWPatch pytest pytest-asyncio ruff mypy pyinstaller
   ```

2. **Frontend**

   ```
   cd frontend
   npm install
   npm run build        # produces frontend/dist the app loads from disk
   ```

3. **API keys** (added in the app's Settings, stored DPAPI-encrypted):
   - Deepgram — https://console.deepgram.com
   - Anthropic (default answer provider) — https://platform.claude.com
   - Groq (optional "fastest" preset) — https://console.groq.com

4. **Run**

   ```
   .venv\Scripts\python app.py                    # packaged-style (loads frontend/dist)
   ```

   Dev mode with hot reload:

   ```
   cd frontend && npm run dev                     # Vite dev server
   set AICA_DEV_URL=http://localhost:5173 && .venv\Scripts\python app.py
   ```

5. **Package**

   ```
   powershell -ExecutionPolicy Bypass -File build.ps1    # checks + frontend + PyInstaller
   .venv\Scripts\pyinstaller aica.spec                   # or just the app build
   ```

   `installer.iss` (Inno Setup) wraps the PyInstaller output into an
   installer if you have Inno Setup 6 installed; the PyInstaller folder
   build is a fully working app on its own.

## The latency architecture (condensed)

- **Record pressed** — the Deepgram WebSocket connect and WASAPI loopback
  capture start IN PARALLEL. Frames captured before the socket opens buffer
  in order (~15 s cap, oldest dropped) and flush the instant it opens. The
  LLM origin is pre-warmed (an unauthenticated `GET /v1/models` through the
  shared HTTP client) so a pooled TLS connection is waiting.
- **While recording** — loopback audio → numpy downsample to 16 kHz mono
  i16 → ~128 ms frames → Deepgram; interim transcripts render live;
  KeepAlive every 8 s survives silence.
- **Stop pressed** — **the latency clock starts here.** CloseStream makes
  Deepgram flush its held-back tail (5 s cap). The origin is pre-warmed
  again so the TLS handshake overlaps the finalize. The LLM request fires
  the instant the transcript is final, over the already-warm connection.
- **Answer** — tokens stream into the panel; the header chip reports the
  measured stop-to-first-word.

Timeouts enforced in the core: STT finalize 5 s · LLM first token 10 s ·
LLM total 60 s · recording hard cap 120 s (auto-stop, then answer normally).

## Prompt-caching honesty note

The Anthropic request marks the resume+JD system block with
`cache_control: ephemeral`, with the style policy in a second block AFTER
the breakpoint so flipping styles never invalidates the cache. Be aware:
**Haiku's minimum cacheable prefix is 4096 tokens**, so a typical 1–2K-token
profile makes the marker a silent no-op. It starts paying at roughly 16K+
characters of profile (cache writes cost 1.25×, reads 0.1×, 5-minute TTL).
`usage.cache_read_input_tokens` in the response tells the truth about
whether it engaged.

## Groq model pinning

Groq retires models on short notice. The model is pinned in ONE constant
(`MODEL` in `app_core/llm/groq.py`); if Groq starts returning 404, the model
was probably retired — update that constant. The in-app error message says
exactly this.

## How to add an answer provider

Provider swappability is a product requirement; the session machine, retry
policy, metrics, events, and views are provider-agnostic. To add one:

1. **Write one module** `app_core/llm/<name>.py` implementing the
   `AnswerProvider` Protocol from `app_core/llm/base.py`:
   - `id` / `display_name` / `origin` (origin without a trailing slash — the
     pre-warmer appends `/v1/models`),
   - `build_request(prompt, api_key)` — assemble the FULL wire request once
     (URL, headers, serialized body); it must be deterministic, it is reused
     verbatim on retry,
   - `stream(request, http)` — async-yield answer text deltas; raise
     `ProviderFailure` with the right `kind` ("connect" | "status" |
     "stream_drop" | "empty_body"),
   - `classify_error(failure)` — map to the closed error-code set with an
     actionable message (check `asyncio.CancelledError` FIRST → `aborted`),
   - `is_retryable(failure)` — `True` only for connection-level failures.

   For an OpenAI-compatible API, clone `groq.py` — its SSE delta extractor
   (`extract_openai_delta`), `[DONE]` sentinel handling, and status mapper
   (`classify_openai_failure`) are exported for exactly this purpose, and
   the transport half lives in `wire.py`.

2. **Register it** in `default_registry()` (`app_core/llm/base.py`). That
   alone gives you: the settings `llmProvider` validation, the Settings UI
   `<select>` entry, a key field, the `has<Name>Key` boolean, and the
   first-run nudge.

3. **Clone the tests**: add your provider to the parametrized
   `TestProviderConformance` list in `tests/test_providers.py` (the
   conformance suite any registered provider must pass), then add a
   provider-specific class for your error-mapping matrix, mirroring
   `TestGroqSpecifics`. The retry matrix in `tests/test_retry.py` is
   provider-generic and needs no changes.

## Testing

```
.venv\Scripts\python -m pytest tests -q      # 232 core tests
cd frontend && npm test                       # 86 frontend tests
.venv\Scripts\python -m ruff check app_core app.py tests
.venv\Scripts\python -m mypy                  # strict, on the core
cd frontend && npm run typecheck              # TS strict
```

No test touches the network, a live provider, or an audio device. See
`docs/TESTING.md` for every test documented (what it verifies and why it
exists), and `docs/learn/` for a guided curriculum over the whole codebase.

## Manual QA script

1. First run → status shows "First run: open Settings (gear icon) and add
   your API keys".
2. Open Settings (gear), paste Deepgram + Anthropic keys, Save ("Saved ✓"),
   Back.
3. Play any audio with speech (a video call, a YouTube interview). Press
   **Record** → status "Recording call audio…", level meter moves, live
   transcript appears while the speaker is talking.
4. Press **Stop & Answer** → "Finalizing transcript…" then the answer
   streams in ~1 s; the latency chip shows "X.Xs to first word" (hover for
   the full breakdown).
5. Type a question in the Ask box → same answer pipeline, `sttFinalizeMs`
   shows 0 in the chip tooltip.
6. Press **Regenerate** → a new history entry answers the same question.
7. Flip style chips Brief/Balanced/Detailed and re-ask → no added latency
   (the cached profile prefix is untouched).
8. Answer several questions → history bar appears; ←/→ navigate; Clear
   (idle only) wipes and announces.
9. Focus another app entirely; press Ctrl+Shift+Space → recording toggles
   (the window need not be focused).
10. Register the same hotkey in another app first → the app shows the
    "already taken … shortcut is off" notice instead of a dead key.
11. Share your screen in a call → this window is invisible in the share.
12. Move/resize the window, quit, relaunch → geometry restored.
13. Launch the exe a second time → the first instance is focused instead.
14. Set the window on a second monitor, unplug the monitor, relaunch → the
    window recenters on the remaining display.

## Cost note (approximate)

The LLM side is ~$0.002–0.003 per answer on Claude Haiku pricing (a few
hundred prompt tokens + ≤1024 completion tokens). Deepgram's per-minute
streaming rate (~$0.0059/min for nova-3 pay-as-you-go) dominates in any
real interview: an hour of recording is ~$0.35 of STT versus a few cents of
LLM.

## Package substitutions / deviations (per spec §2)

- **Global hotkey**: implemented directly over Win32 `RegisterHotKey` via
  ctypes (`app_core/bridge/hotkey.py`) instead of the `global_hotkeys`
  package. Reason: the product contract requires honest registration-failure
  reporting (key taken → visible notice), which the wrapper packages don't
  expose reliably. The package remains listed in the install line for
  optional experimentation but is unused.
- **Repo layout**: the spec sketches the core package under `core/`; it
  lives at the repo root as `app_core/` (same subpackage structure:
  `audio/ stt/ llm/ session/ store/ bridge/`) so imports match the package
  name without a mapping shim.
- **Events**: one event was ADDED beyond the spec's list —
  `session:autostopped {sessionId}` — emitted when the 120 s cap fires, so
  the frontend can honestly transition to "Reached the 120s limit —
  answering now" without guessing from its own timer.
- **Event-race hardening**: the frontend buffers events whose session id is
  unknown *while a start/ask command is still in flight* and replays them on
  adoption (pywebview's promise resolution and `evaluate_js` events race;
  strictly dropping them could lose the first partial of an ask).
- **CSP**: `style-src` includes `'unsafe-inline'` — pywebview/WebView2's
  bootstrap requires it; model content still never contributes markup,
  attributes, or styles.
