# AI Call Assistant v3

A push-to-record **interview copilot** for Windows. Press **Record** while
the other person is asking their question; the app captures **system
output audio** (WASAPI loopback of the default output device — everything
that device plays, including the caller but also notifications, music or
any other app's sound; it never opens your microphone), renders a **live
transcript while they're still speaking**, and on **Stop & Answer** streams
an AI-suggested answer grounded in the active **profile** (resume, job
description, focus, notes) and shaped for the selected **call type**. The
latency **target** is ~1 second from Stop to the first word of the answer
(the chip reports the measured core-side time, not a guarantee), the answer
is shown **at the camera line** (a prompter strip docked top-centre of the
display), and the window asks Windows to **exclude it from screen capture**
(`WDA_EXCLUDEFROMCAPTURE`), reads the setting back, and warns visibly when
Windows does not confirm it. Exclusion is honoured by capture paths that
respect display affinity; verify it in the call app and sharing mode you
actually use (see [fabledocs/RELEASE-CHECKLIST.md](fabledocs/RELEASE-CHECKLIST.md)).

Each answer is **single-turn**: the prompt contains the active profile, the
call type, the answer style and the current question only. Earlier answers
in the history bar are for display; they are not sent as conversational
context.

v3 is a Python-core rebuild: Python 3.12+/asyncio owns the entire pipeline
(WASAPI loopback capture → Deepgram streaming STT → provider-abstracted LLM
streaming → session state machine → settings/DPAPI secrets); the frontend is
a thin React view hosted in a pywebview (WebView2) window.

## Setup

1. **Python core** (3.12 or 3.13). Dependencies are declared in
   `pyproject.toml` and pinned in `constraints.txt`; CI installs exactly the
   same set with exactly this command:

   ```
   python -m venv .venv
   .venv\Scripts\python -m pip install -c constraints.txt -e ".[dev,build]"
   ```

   (`.[dev]` alone is enough to run and test; `build` adds PyInstaller.)

2. **Frontend** — `npm ci` installs exactly what `package-lock.json` records:

   ```
   npm --prefix frontend ci
   npm --prefix frontend run build     # produces frontend/dist the app loads from disk
   ```

   Or do steps 1 and 2 in one go: `powershell -ExecutionPolicy Bypass -File build.ps1 -Bootstrap`.

3. **API keys** (added in the app's Settings, stored encrypted with Windows
   DPAPI for the current Windows user; if Windows cannot encrypt a key, the
   save fails with an error and nothing is written — a key is never stored
   in plaintext by this version. A key that an older build saved in its
   marked `plain:` fallback still works, is reported as not encrypted, and
   is re-encrypted by the next successful save. Profile text is ordinary
   JSON, not encrypted):
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
   powershell -ExecutionPolicy Bypass -File build.ps1    # metadata + checks + frontend + PyInstaller (+ installer)
   .venv\Scripts\python -m PyInstaller aica.spec --noconfirm   # or just the app build
   ```

   `installer.iss` (Inno Setup 6) wraps the PyInstaller output into a
   per-user installer; `build.ps1` runs it automatically when `ISCC.exe` is
   found. The PyInstaller folder build is a fully working app on its own.
   The version lives in ONE place, `pyproject.toml`: `aica.spec` stamps it
   into the exe's Windows version resource, `build.ps1`/CI pass it to the
   installer, and `tools/release_meta.py check` (run by CI, `build.ps1` and
   `tests/test_release_metadata.py`) fails if `frontend/package.json`, its
   lockfile or `installer.iss` disagree. Every build bundles
   `_internal\build_info.json` recording the version, git revision, whether
   the tree was dirty, the Python version and the pinned dependency set.
   Release steps and the native validation matrix:
   [fabledocs/RELEASE-CHECKLIST.md](fabledocs/RELEASE-CHECKLIST.md).

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

## Prompter mode — answers at eye level

The full window puts the *Suggested answer* panel first, directly under a
slim header, and starts docked at the **top-centre of the primary display**
(shrunk to fit the work area — the old 460x700 default did not fit a
1280x672 laptop work area). Press **⤒ Enter prompter mode** to turn the
window into a 720x260 strip docked under the webcam: large text (14–28 px,
remembered), a 64-character column so the eyes stay near the camera,
**top-anchored** streaming (the opening never scrolls away while you are
saying it), the record button, timer, one-line question, style chips,
history arrows, **⤒ Dock under camera** and **⤢ Exit** (also Esc). The strip
and the full window keep separate geometry (`prompterBounds` /
`windowBounds`). Both layouts are the same HWND, so content protection is
untouched.

Geometry has two unit spaces — pywebview's logical pixels and Win32's
physical pixels once `webview.start()` makes the process DPI-aware — and
`app.py` documents which is which; `_runtime_scale()` bridges them.

## Profiles and call types

Settings hold **profiles** (`profiles[]`, `activeProfileId`), one per
opportunity or kind of call: name, **call type**, **focus** (the stack to
lead with), resume, job description / call context, and notes. The six call
types (`app_core/llm/prompt.py`, `CALL_TYPES`) change the role
instructions, the heading over the JD block, the grounding rule, and the
structure the *Detailed* style follows:

| Call type | Grounding rule | No-question fallback | Longer-answer structure |
| --- | --- | --- | --- |
| Behavioral interview | never invent experience the resume does not support | suggest what to say next | situation → action → result |
| Technical screen | answer on the merits; never claim hands-on experience the resume lacks | suggest a clarifying question | direct answer → how it works → when (not) to use |
| System design | draw on the resume's systems; never claim to have built what it lacks | suggest the next design step | requirements → components/data flow → tradeoff → scale |
| Recruiter screen | never invent experience or credentials | suggest a question about the process | answer → background → enthusiasm |
| Sales or customer call | never invent pricing, features, customers or commitments | suggest a discovery question | acknowledge → answer/benefit → next step |
| General meeting | never invent facts, decisions or commitments | suggest what to say next | point → reason → next step |

Call-type text, focus and notes live in the **cached** prompt prefix (they
are stable per profile); the style suffix stays call-type independent so
flipping styles never invalidates the cache. Switching profiles changes the
cache key, which is correct and unavoidable. Answer style is global. The
main window's *Profile* and *Call type* selects persist to the active
profile; press *Regenerate* to re-answer under the new setting. At most
**20 profiles** can be saved (`MAX_PROFILES` in
`app_core/store/settings.py`); the core rejects a save beyond that. Legacy
files with only top-level `resume`/`jobDescription` are migrated in memory
into one **Default** profile (load never writes); the top-level keys stay as
write-only mirrors of the active profile so an older build keeps working —
note that an older build **keeps only the active profile** if it saves.

## Startup

The window is created before anything heavy loads: `App._build_core`
imports httpx, websockets, numpy and the audio stack on a background thread
while WebView2 boots, then hands the session machine to the bridge. Session
commands wait up to 25 s for it ("The app is still starting up" after
that); settings never need it. `index.html` paints the dark background
before the stylesheet arrives. The renderer watchdog's clock is reset at
`shown`/`loaded`; see `docs/ARCHITECTURE.md` for how an unusually slow
first page load is treated, and `docs/TROUBLESHOOTING.md` for what a
startup failure looks like.

## Prompt-caching honesty note

The Anthropic request marks the profile system block (call-type role +
resume + JD + focus + notes) with
`cache_control: ephemeral`, with the style policy in a second block AFTER
the breakpoint so flipping styles never invalidates the cache. Be aware:
**Haiku's minimum cacheable prefix is 4096 tokens**, so a typical 1–2K-token
profile makes the marker a silent no-op. It starts paying at roughly 16K+
characters of profile (cache writes cost 1.25×, reads 0.1×, 5-minute TTL).
`usage.cache_read_input_tokens` in the response tells the truth about
whether it engaged.

## Groq model pinning

Groq retires models on short notice. The model is pinned in ONE constant
(`MODEL` in `app_core/llm/groq.py`). If Groq answers 404 (or a 400 whose
body says the model is gone), the in-app error names the retired model and
tells the user to switch the answer provider to Claude in Settings or
install the latest version of the app — an installed-app user cannot edit
source. For maintainers, the fix is a new release with that constant
updated and the Groq smoke test in the release checklist re-run.

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
   - `stream(request, http)` — async-yield answer text deltas, then ONE
     `StreamEnd(finish=...)` once the provider's real terminal event
     arrived (`finish` is "complete" | "truncated" | "refused"); raise
     `ProviderFailure` with the right `kind` ("connect" | "timeout" |
     "status" | "stream_drop" | "empty_body" | "provider_error" |
     "incomplete" | "empty_answer") — an in-stream error event is
     `provider_error`, a stream that ends without the terminal event is
     `incomplete`. `retry.py` turns a finished stream with no usable text
     into `empty_answer`,
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
.venv\Scripts\python tools\release_meta.py check   # one version everywhere, deps pinned
.venv\Scripts\python -m pytest tests -q            # core tests
npm --prefix frontend test                         # frontend tests
.venv\Scripts\python -m ruff check app_core app.py tests tools
.venv\Scripts\python -m mypy                       # strict, on the core + tools
npm --prefix frontend run typecheck                # TS strict
```

CI runs the core suite on Python 3.12 (the declared minimum) and 3.13 (the
release runtime), the frontend gates, and a packaging job that builds the
exe and installer and uploads them as an artifact named with the version and
commit. Current test counts are in `docs/TESTING.md`.

No test touches the network, a live provider, or an audio device. See
`docs/TESTING.md` for every test documented (what it verifies and why it
exists).

## Documentation

- **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** — the system map: the
  pipeline and the thread each stage runs on, a file-by-file module map, the
  Protocol seams, and the full command/event catalogue. Read this first.
- **[docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md)** — symptom-driven
  diagnosis, quoting the messages the app actually shows.
- **[docs/TESTING.md](docs/TESTING.md)** — every test, and the failure mode
  it guards.
- **[fabledocs/REPORT.md](fabledocs/REPORT.md)** — the 2026-09-18
  improvement pass: every change, why, and how it was verified.
- **[fabledocs/SETUP-AND-DEPLOY.md](fabledocs/SETUP-AND-DEPLOY.md)** —
  install, run from source, build a release, hand it out.
- **[fabledocs/RELEASE-CHECKLIST.md](fabledocs/RELEASE-CHECKLIST.md)** —
  the production release procedure and the native/manual validation matrix
  (capture exclusion in real call apps, displays/DPI, devices, live
  providers) that the offline tests cannot cover.
- **[fabledocs/PROJECT-REVIEW-2026-09-19.md](fabledocs/PROJECT-REVIEW-2026-09-19.md)**
  — the external review (R01–R13), the project's responses, and follow-up.
- **[fabledocs/USER-GUIDE.md](fabledocs/USER-GUIDE.md)** — how to use the
  app on a call: prompter mode, profiles, call types.
- **[docs/learn/](docs/learn/README.md)** — a thirteen-chapter curriculum
  over this codebase: a guided tour, sixteen design decisions in ADR form,
  flashcards, spot-the-bug, fire drills, exercises, and deep dives on the
  bugs that shipped, testing craft, audio DSP, concurrency, the security
  model, and the latency budget. Start at its README, which lays out reading
  paths for different goals.

## Manual QA script

1. First run → status shows "First run: open Settings (gear icon) and add
   your API keys".
2. Open Settings (gear), paste Deepgram + Anthropic keys, Save ("Saved ✓"),
   Back.
3. Play any audio with speech (a video call, a YouTube interview). Press
   **Record** → status "Recording call audio…", level meter moves, live
   transcript appears while the speaker is talking.
4. Press **Stop & Answer** → "Finalizing transcript…" then the answer
   streams (target ~1 s); the latency chip shows "X.Xs to first word"
   (hover for the full breakdown; it is measured in the core, before the
   text reaches the screen).
5. Type a question in the Ask box → same answer pipeline, `sttFinalizeMs`
   shows 0 in the chip tooltip.
6. Press **Regenerate** → a new history entry answers the same question.
7. Flip style chips Brief/Balanced/Detailed and re-ask → the answer length
   changes; the cacheable profile prefix is unchanged (whether the provider
   cache actually engages depends on profile size — see the caching note).
8. Answer several questions → history bar appears; ←/→ navigate; Clear
   (idle only) wipes and announces.
9. Focus another app entirely; press Ctrl+Shift+Space → recording toggles
   (the window need not be focused).
10. Register the same hotkey in another app first → the app shows the
    "already taken … shortcut is off" notice instead of a dead key.
11. Share your screen in a call → this window must not appear in the
    share, and no protection warning is shown. Repeat for each call app and
    sharing mode (whole screen / window / tab) listed in
    `fabledocs/RELEASE-CHECKLIST.md`.
12. Move/resize the window, quit, relaunch → geometry restored.
13. Launch the exe a second time → the first instance is focused instead.
14. Set the window on a second monitor, unplug the monitor, relaunch → the
    window docks top-centre on the remaining display.
15. Press **⤒ Enter prompter mode** → the window becomes a wide strip at the
    top-centre of the display; record/stop, A−/A+, ← → all work; **Esc**
    returns the full window to its previous place and size; relaunch in
    prompter mode → the strip comes back where it was.
16. Change *Call type* to Technical screen, press **Regenerate** → the
    answer leads with the fact, not a resume story; the history entry is
    tagged with the call type.
17. Settings → Add profile (sales, with notes), Save → the *Profile*
    select appears in the main window; switch it → the header chip names
    the profile and the next answer never invents pricing.
18. Type into a key field, delete the text, Save → the key is still saved
    (Record still works); press **Remove** next to it, Save → first-run
    status returns.

## Cost note (approximate)

The LLM side is ~$0.002–0.003 per answer on Claude Haiku pricing (a few
hundred prompt tokens + ≤1024 completion tokens). Deepgram's per-minute
streaming rate (~$0.0059/min for nova-3 pay-as-you-go) dominates in any
real interview: an hour of recording is ~$0.35 of STT versus a few cents of
LLM.

## Package substitutions / deviations (per spec §2)

- **Global hotkey**: implemented directly over Win32 `RegisterHotKey` via
  ctypes (`app_core/bridge/hotkey.py`) instead of the `global_hotkeys`
  package, which is therefore not a dependency and not in the install line.
  Reason: the product contract requires honest registration-failure
  reporting (key taken → visible notice), which the wrapper packages don't
  expose reliably.
- **Repo layout**: the spec sketches the core package under `core/`; it
  lives at the repo root as `app_core/` (same subpackage structure:
  `audio/ stt/ llm/ session/ store/ bridge/`) so imports match the package
  name without a mapping shim.
- **Events**: three events were ADDED beyond the spec's list.
  `session:autostopped {sessionId}` fires when the 120 s cap trips, so the
  frontend can honestly transition to "Reached the 120s limit — answering
  now" instead of guessing from its own timer. `protection:failed` /
  `protection:ok` report whether Windows actually confirmed
  `WDA_EXCLUDEFROMCAPTURE` (see below).
- **Content protection is verified, and its failure is visible.**
  `SetWindowDisplayAffinity` can fail (an HWND that is not ready yet, policy,
  Windows builds older than 2004). The app applies it, reads the affinity
  back, retries briefly, and — if the OS never confirms — shows a standing
  warning in the window. A user who believes they are hidden while being
  broadcast is this product's worst outcome, so it is not a log line.
- **Silent-capture hint** (addition to §9's main view): when no audible
  frame has arrived for ~5 s of recording, a line appears suggesting the call
  audio may be going somewhere other than the speakers. Loopback capturing
  silence — headset, wrong output device, muted call — is the most common
  real-world failure, and otherwise the user only discovers it at Stop, after
  the question is gone. Because it tracks *when* audio was last heard rather
  than whether it ever was, it also catches a device unplugged mid-question:
  the capture stream stays bound to the dead endpoint and delivers silence
  with no error. It clears the instant audio arrives.
- **Audio is resampled with a phase-continuous, anti-aliased streaming
  resampler** (`app_core/audio/downsample.py`). Resampling each device chunk
  independently made the output depend on how the device sliced the stream,
  dropped samples when a chunk did not divide evenly, and folded everything
  above 8 kHz into the speech band. All three degraded exactly the audio
  Deepgram transcribes.
- **Hotkey failure is reported honestly**: the settings view carries a
  `hotkeyStatus` of `registered` / `disabled` / `invalid` / `unavailable`.
  The spec's "already taken by another app" copy is used only for
  `unavailable`; an unparseable accelerator says so instead of sending the
  user to hunt for a conflict that does not exist.
- **Event dispatch is batched and bounded**: the pump drains whatever is
  queued into one `evaluate_js` call (capped at 64), in order. Each call is
  a blocking round trip on a worker thread with a deadline, and an answer
  streams dozens of deltas per second. A timed-out call is treated as an
  uncertain delivery (events carry `seq`/`pageGen` so the page drops
  duplicates and stale deliveries; terminal and protection events are
  re-sent); see `docs/ARCHITECTURE.md`.
- **Event-race hardening**: the frontend buffers events whose session id is
  unknown *while a start/ask command is still in flight* and replays them on
  adoption (pywebview's promise resolution and `evaluate_js` events race;
  strictly dropping them could lose the first partial of an ask).
- **CSP**: `style-src` includes `'unsafe-inline'` — pywebview/WebView2's
  bootstrap requires it; model content still never contributes markup,
  attributes, or styles.
- **Error kinds**: providers classify failures into eight kinds
  (`connect` · `timeout` · `status` · `stream_drop` · `empty_body` ·
  `provider_error` · `incomplete` · `empty_answer`). Only `connect` is
  retryable — a read timeout while waiting for response headers means the
  server may already be generating the answer, so retrying could produce a
  doubled answer. HTTP 200 is not treated as success on its own: an error
  event inside the stream, a stream that ends before the provider's terminal
  event, and a finished answer with no text are all reported as errors
  (`session:error`), never as a blank finished answer. `llm:done` carries
  `finish` (`complete` | `truncated` | `refused`) so an answer cut off at the
  token limit is distinguishable from a complete one.
- **Markdown limits**: emphasis nesting is capped (24 deep, 1000 pairs, and
  emphasis resolution is skipped past 20 000 characters). Model output is
  untrusted and unbounded nesting overflowed the render stack, which
  unmounts the whole React root. A `MarkdownBoundary` falls back to plain
  text if rendering ever throws anyway.
