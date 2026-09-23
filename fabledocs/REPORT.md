# AI Call Assistant — Project Report

This file has two parts. **Part 1** is the current state of the project
(updated 2026-09-23). **Part 2** is the original 2026-09-18 improvement-pass
report, kept unchanged as a dated record.

---

# Part 1 — Current state (2026-09-23)

## At a glance

| | |
| --- | --- |
| Version | **3.1.0** — one source (`pyproject.toml`), checked by `tools/release_meta.py check` |
| Branch | `master`; last commit `c6191b5`. **Everything below is uncommitted.** |
| Snapshot | The tree before the production-readiness pass is saved at git ref `refs/aica/pre-prod-pass` |
| Release status | Code-complete for release; **native/manual validation not yet run** (see "Before tagging") |

| Gate | 2026-09-18 | Now |
| --- | --- | --- |
| `pytest` (core) | 376 passed | **519 passed** |
| `vitest` (frontend) | 173 passed | **236 passed** (18 files) |
| `ruff` / `mypy --strict` (32 files) / `tsc --noEmit` | clean | clean |
| `vite build` | ok | ok (JS 240.7 kB, 75.3 kB gzip) |
| `release_meta.py check` | n/a | consistent at 3.1.0 |
| `npm audit --omit=dev` / `pip-audit` | not run | 0 vulnerabilities / none known |
| PyInstaller build | not re-run | ok: 58.6 MB folder, 244 files, 9.6 MB exe, ProductVersion 3.1.0 |
| Inno Setup installer | not re-run | **not built locally** (Inno Setup not installed); built by the CI package job |

Since the pre-pass snapshot: 58 tracked files changed (+5,725 / −752),
plus new files: `tests/test_capture.py`,
`tests/test_release_metadata.py`, `tools/release_meta.py`,
`constraints.txt`, `frontend/src/components/ProtectionNotice.tsx`,
`frontend/src/version.d.ts`, five new frontend test files, and
`fabledocs/RELEASE-CHECKLIST.md`.

## What the 2026-09-22 production-readiness pass did

Five parallel agents (frontend; LLM/answer path; audio/STT; store, bridge
and shell; release engineering and docs) closed every finding R01–R13 in
[PROJECT-REVIEW-2026-09-19.md](PROJECT-REVIEW-2026-09-19.md), following the
reviewer's §8 corrections (for example, the Stop timestamp stays *before*
the audio drain so latency is never hidden). Each behavioral fix has a
regression test; the shell agent also mutation-checked six of them.

| ID | Status | What changed |
| --- | --- | --- |
| R01 | Fixed | Tri-state protection verdict (`protected` / `unprotected` / `unknown`) shown in the full view, prompter and Settings. New `get_status()` snapshot with a monotonic `revision`; the page adopts only newer state and re-adopts a live session after a reload. |
| R02 | Fixed | Key encryption fails closed: if DPAPI fails, the save is refused and the old key survives. Legacy `plain:` keys still work and are reported as plaintext in Settings until re-encrypted. |
| R03 | Fixed | Success now requires the provider's own completion signal (Anthropic `message_stop`; Groq `finish_reason`/`[DONE]`). In-stream errors, early EOF and empty answers become errors; truncated/refused answers are labelled via `llm:done.finish`. The SSE last-line flush is kept. |
| R04 | Fixed | Per-command generations in the frontend; a late start/abort only cleans up the session it created. Backend behavior verified and pinned with tests. |
| R05 | Fixed | An unreadable/invalid `settings.json` is backed up (`settings.json.<status>-<stamp>-<id>.bak`) before the first write; if the backup fails, nothing is overwritten. |
| R06 + R10 | Fixed (one change) | Stop drains every pre-Stop sample into Deepgram before CloseStream (explicit capture cutoff). Device calls moved to a single audio worker thread; drain bounded at 2 s. New `audioDrainMs` metric; the recording cap is armed when capture really starts and sent to the UI as `session:recording {deadlineMs, capMs}`. |
| R07 | Fixed | Bounded event queue (2,000); levels, then interim transcripts, then deltas are shed first; terminal/protection/core events are never dropped. Events carry `seq` and `pageGen`; after a timeout only reserved events are re-sent. The old ~975 s retry worst case is gone. |
| R08 | Fixed | Refused-stop recovery is scoped to its session, cleared by progress, asks the core before giving up, and cancels the core session too. |
| R09 | Fixed | Settings saves serialized with a `baseRevision` precondition; explicit Discard; "Saving…" state; typing a key cancels a queued removal. Native close is held once for unsaved work, never permanently. |
| R11 | Fixed | Watchdog never probes a booting page (60 s grace); core start-up failure is surfaced at once (`core:failed`). Clean exit within 3 s, hard exit otherwise. |
| R12 | Fixed | Saved geometry validated against every connected display, title bar must be reachable, usable size even with no display information. |
| R13 | Fixed | Single version source; pinned `constraints.txt`; Windows version resource and `build_info.json` in the exe; CI on Python 3.12 + 3.13, frontend job, and a package job that builds and uploads the exe/installer. Groq retired-model error no longer says to edit source. |
| §4 / §5 | Fixed | "System audio" wording (never "microphone"); hotkey can stop a recording behind Settings; Clear from one history entry; finalizing status in the prompter; 20-profile limit and single-turn context documented. |

Other defects found and fixed along the way: API key/profile removed from
request reprs; provider stream closed promptly on error; pre-warm task held
strongly; bridge listener leak before `pywebviewready`; silent settings-load
failure now alerts and retries; late hotkey registration undone; stricter
`open_external` URL checks; a start that completes after the 30 s bridge
timeout now cancels its own session.

## Independent release review (Fable, 2026-09-22)

A read-only review of the whole pass found **no P0** and confirmed the
audio drain, close guard, fail-closed secrets, settings backup, completion
validation and the frontend/backend contract. Its findings:

| ID | Severity | Finding | Status |
| --- | --- | --- | --- |
| F1 | P1 | Hung-dispatch counter never reset, so after two renderer crashes the pump stopped delivering to the reloaded page forever | **Fixed** — counted per page generation; regression test |
| F2 | P2 | Watchdog reload advanced the page generation twice, dropping events delivered in the load window | **Fixed** — exactly one bump per reload; regression test |
| F3 | P2 | Device-open failure + early Stop reported as "No speech detected" | **Fixed** — reports the device error; regression test |
| F4 | P2 | `audioDrainMs` includes STT connect time when Stop lands during connect | Open (metrics only) |
| F5 | P2 | A session re-adopted after a reload loses its countdown deadline | Open (cosmetic) |
| F6 | P2 | Stop recovery treated a stalled core (`phase: "unknown"`) as dead and cancelled it | **Fixed** — rechecks instead; regression test |
| F7 | P2 | Page JS could set the protection verdict | **Fixed** — setter is private; regression test |
| F8 | P2 | Build record ignored untracked files when reporting `dirty` | **Fixed**; the local installer build is still outstanding |

Two timing-sensitive tests that failed only under full-suite load were also
made deterministic.

## Known open items

- **F4, F5** above (minor).
- **Prompt fencing:** a transcript containing `"""` can close the prompt's
  quoting. Fixing it changes the prompt bytes, so it is a policy decision.
- **Device unplug mid-recording** is not detected actively (the silence
  hint covers it); a mid-recording default-device switch is not followed.
- **Mixed-DPI placement** across monitors needs native verification.
- **vitest advisory** (moderate, dev-only) needs a breaking vitest 5 upgrade.
- **Code signing** needs a certificate; SmartScreen reputation builds over
  time (see SETUP-AND-DEPLOY.md §5).

## Before tagging a release

1. Review and commit the working tree (stage explicitly; do not `git add -A`
   blindly).
2. Build from the committed tree with `build.ps1` (or let the CI package
   job do it) so `build_info.json` records a clean revision; build the
   installer.
3. Run the native matrix in [RELEASE-CHECKLIST.md](RELEASE-CHECKLIST.md):
   capture exclusion on Zoom/Teams/Meet/OBS verified from the remote side,
   multi-monitor/mixed-DPI, audio device unplug/Bluetooth, close guard and
   corrupt-settings recovery, install/upgrade, and a budgeted live-provider
   smoke test.
4. Sign the exe and installer if a certificate is available.

Where to read more: behavior — [docs/ARCHITECTURE.md](../docs/ARCHITECTURE.md);
every test — [docs/TESTING.md](../docs/TESTING.md); setup and CI —
[SETUP-AND-DEPLOY.md](SETUP-AND-DEPLOY.md); users —
[USER-GUIDE.md](USER-GUIDE.md).

---

# Part 2 — Improvement Pass Report (2026-09-18, historical)

> **Errata (added 2026-09-22; the report below is kept unchanged as a dated
> record).** Read it as history, not current truth. Superseded points:
> the "~1 s to ~50 ms on a normal machine" startup sentence is an unmeasured
> estimate; the Defender-exclusion advice is machine-specific and no longer
> recommended as routine setup (see SETUP-AND-DEPLOY.md §3); the
> `git add -A` staging instruction is withdrawn — review the intended diff
> and stage it explicitly; dispatch ordering, the watchdog clock, geometry
> restoration and corrupt-settings handling are qualified by
> PROJECT-REVIEW-2026-09-19.md (R05, R07, R11, R12). Current behavior is
> documented in docs/ARCHITECTURE.md; the release procedure in
> RELEASE-CHECKLIST.md.

**Scope requested:** "improve entire app, refactor, improve loading times,
usability etc. i want answers to display in center middle top of screen at
eye/camera level. explore if it makes sense to have separate user profiles
for different tech stacks depending on the type of call for better more
tailored output. create report in new fabledocs folder, and make any changes
to improve app. and report all changes."

**Result:** version 3.0.0 → **3.1.0**, 39 files changed and 6 added
(~3,550 lines added, ~750 removed), every check green:

| Gate | Before | After |
| --- | --- | --- |
| `pytest tests` (core) | 318 passed, 1 timing-flaky | **376 passed**, flake fixed (3/3 clean reruns of the flaky suite) |
| `vitest run` (frontend) | 148 passed | **173 passed** |
| `ruff` / `mypy --strict` / `tsc --noEmit` | clean | clean |

Nothing is committed: all changes sit in the working tree on `master` (the
prior uncommitted work — renderer probe, settings rename retry, abort-during-
connect — is preserved and built upon). Companion documents:
[SETUP-AND-DEPLOY.md](SETUP-AND-DEPLOY.md) and [USER-GUIDE.md](USER-GUIDE.md).

---

## 1. How this was done

1. **Audit.** A seven-dimension audit ran as parallel agents (startup
   performance, frontend performance, eye-level UX design, profiles design,
   refactoring, correctness, usability), each followed by an adversarial
   verifier. Five audits and two verifications completed and are the basis
   of this plan; the remaining agents stalled on an API rate limit and the
   startup/frontend-performance dimensions were then measured by hand
   (section 4).
2. **Implement.** Changes were made directly, core first (keeping the
   suites green at each step), then frontend, then tests, then docs.
3. **Verify.** Full suites, lint and type checks, a headless smoke test of
   the new deferred startup, and a production frontend build.

---

## 2. Answers at eye / camera level

**What you get.** The app now assumes you are on a video call with the
webcam at the top of the screen.

- **Answer first, docked top-centre.** The full window puts *Suggested
  answer* directly under a slim header and, on first run (or when its
  saved position is off-screen), docks itself at the top-centre of the
  primary display, shrunk to fit the work area. The old default (460×700,
  centred) did not even fit this machine's 1280×672 laptop work area and
  landed under the taskbar.
- **Prompter mode** (⤒ in the header, ⤢ or Esc to exit): the same window
  becomes a 720×260 strip under the camera showing the answer in large text
  (14–28 px, remembered), a 64-character column so the eyes stay centred,
  the record/stop button, timer, one-line question, style chips, history
  arrows, A−/A+, and **⤒ Dock under camera**. Streaming is
  **top-anchored**: a balanced answer arrives faster than you can read it,
  and the old sticky-bottom behaviour would have scrolled the opening away
  while you were still saying it. A "▼ more" hint appears when text
  overflows.
- **Dock under camera** (⊤ in the full header, ⤒ in the strip) moves the
  window to the top-centre of *the display it is on*, so an external webcam
  on a second monitor works too.
- **Separate geometry per layout** (`windowBounds` / `prompterBounds`), so
  switching back restores the full window exactly where it was.

**Design decision.** Three options were evaluated: (A) reorder + dock the
full window, (B) a prompter layout in the same window, (C) a second
frameless window. B with A folded in was chosen. Both layouts are the same
HWND, so the screen-share protection (`WDA_EXCLUDEFROMCAPTURE`, applied
and verified) is untouched — a second window would have been a second
chance to leak into a share and would have doubled every single-window
invariant (sink, watchdog, protection verdict, single-instance focus).

**DPI correctness.** pywebview flips the process to system-DPI-aware inside
`webview.start()`. Before that, Win32 work areas are logical pixels (what
`create_window(x=, y=)` takes); after it they are physical, while
`window.move/resize/x/y` stay logical. `app.py` now documents both spaces
and divides runtime rectangles by `GetDpiForWindow/96` — the same factor
pywebview applies — so docking is exact at 125 % / 150 % scaling.
`webview.screens` was rejected for this because it reports physical pixels
labelled logical once awareness is set.

Files: `app.py` (`initial_window_kwargs`, `_switch_layout`,
`_dock_top_center`, `_dock_current`, `_window_work_area`,
`_runtime_scale`), `app_core/store/bounds.py` (`top_center`,
`primary_area`), `app_core/store/settings.py` (`layoutMode`,
`prompterFontPx`, `answerFontPx`, `prompterBounds`),
`app_core/bridge/api.py` (`dock_window`), `frontend/src/components/PrompterView.tsx`
(new), `App.tsx`, `styles.css`.

---

## 3. Profiles per call type / tech stack — explored and implemented

**Does it make sense? Yes — and the biggest win is the call type, not the
multiple profiles.** The shipped prompt was hard-wired to one situation: a
behavioral interview. Its role text said "live interview", the JD heading
said "THE JOB THEY ARE INTERVIEWING FOR", the grounding rule said "never
invent experience the resume does not support" (wrong for a technical
fact — the resume cannot ground "how does React reconciliation work", so
the model shoehorned resume stories into conceptual answers — and wrong for
a sales call, where what must not be invented is pricing), and the
*Detailed* style hard-coded STAR structure, which is nonsense for a design
prompt or a pricing objection.

Stack tailoring is a **free-text "focus" field**, not an enum: the
transcript usually names the technology; what the model needs is
disambiguation ("how do you handle concurrency?" → asyncio vs goroutines),
which stack to lead with when the resume lists six, and the vocabulary
level.

**What was built.**

- **Six call types** in `app_core/llm/prompt.py` (`CALL_TYPES`), each with
  its own role instructions, JD-block heading, grounding rule, no-question
  fallback and longer-answer structure: Behavioral interview (the original
  prompt, now the default), Technical screen, System design, Recruiter
  screen, Sales or customer call, General meeting. The *Detailed* style now
  defers its structure to "the call guidance above" so the style suffix
  stays call-type independent.
- **Profiles** in the settings store: `profiles[]` of `{id, name, callType,
  focus, resume, jobDescription, notes}` plus `activeProfileId`; up to 20;
  full per-field fallback on load; all-or-nothing validation on save.
- **Zero-loss migration.** A settings file with only the old top-level
  `resume`/`jobDescription` becomes one "Default" profile in memory; load
  never writes; the top-level keys are kept as write-only mirrors of the
  active profile so an older build still works (it keeps only the active
  profile if it saves — documented).
- **Prompt caching preserved.** Call-type text, focus and notes go into the
  cached prefix in a fixed order (role, resume, JD, focus, notes,
  grounding); the style suffix stays after the breakpoint. Switching
  profiles changes the cache key, which is correct. Honest note: Haiku's
  4,096-token minimum means typical profiles never engaged the cache before
  either.
- **UI.** *Profile* (shown once there is more than one) and *Call type*
  selects under the Ask box switch mid-call in one click; the header chip
  shows provider and active profile; each history entry is tagged with the
  call type it was answered under (`llm:done` now carries `callType`).
  Settings gained a Profile section: edit/add/duplicate/delete, name, call
  type, focus, resume (labelled *Background / resume* for sales and
  meetings), job description (*Call context*), notes. Saving makes the
  edited profile the active one.
- **Answer style stays global** — it is a per-question knob flipped from
  the main view; "sales prefers brief" is handled by the sales call-type
  text itself.

**Not measured:** answer quality. The repo has no prompt eval and tests
may not touch a provider. Before relying on a call type for a real call,
run one canned question per type once (the manual QA script in the README
lists them).

Files: `prompt.py`, `settings.py`, `contracts.py` (new: `AnswerConfig`
gains `call_type`/`focus`/`notes`), `machine.py`, `types.ts`,
`SettingsPanel.tsx`, `App.tsx`, `testutils.tsx`.

---

## 4. Loading times

**Finding.** On this development machine `import app` took 12–16 s and
constructing the `App` object another ~19 s before the window could even
be created — but the cause was the machine, not the app: the system
Python's standard library has **no compiled bytecode cache** (the folder is
not user-writable) and every file open costs ~14 ms (real-time antivirus
scanning). With ~1,000 modules imported before the window existed, that
was 35+ s. On a normal machine the same path is well under a second and
WebView2's own boot dominates. A user-writable `PYTHONPYCACHEPREFIX` did
not help (I/O, not compilation, dominates), so the fixes are structural:

- **Window before core.** `App.__init__` now builds only what the window
  needs (settings, event sink, hotkey, the bridge). `App._build_core` runs
  on a background thread started right after `create_window`, importing
  httpx, the provider stack, websockets, numpy and the audio stack and
  assembling the session machine while WebView2 boots and the page loads.
  The bridge waits up to 25 s for the core on session commands and never
  for settings. A new `app_core/contracts.py` holds the Protocols and
  `AnswerConfig` with no heavy imports so the settings store, the STT
  client and the bridge can be imported early; `httpx` imports in `base.py`,
  `retry.py`, `warm.py`, `anthropic.py`, `groq.py` are typing-only, and
  `wire.py` imports it inside the streaming function, so building the
  provider registry at startup no longer loads httpx.

  Measured headless on this machine (same process shape as launch, warm
  disk cache):

  | Stage | Before | After |
  | --- | --- | --- |
  | `import app` (before the window can exist) | 12–16 s | **1.1 s** |
  | `App()` construction (before the window) | ~19 s | **0.01 s** |
  | Heavy modules loaded before the window | httpx, websockets, numpy | **none** |
  | Core build (now in the background, overlapping WebView2 boot) | — | 2.4–12.7 s, never on the critical path |

  On a normal machine the same change turns a ~1 s pre-window stall into
  ~50 ms; WebView2's own boot then dominates.
- **No white flash.** `index.html` paints the dark background inline before
  the stylesheet loads.
- **Watchdog cannot reload a booting page.** The renderer watchdog's clock
  now starts at the window's `shown`/`loaded` events instead of at
  construction, so a slow cold start is never mistaken for a dead renderer
  (it used to be possible for it to reload the page mid-boot).
- **Machine-level fixes for developers** (documented in
  SETUP-AND-DEPLOY.md): compile the stdlib once from an elevated prompt and
  exclude the project and Python folders from Defender. The PyInstaller
  build is unaffected either way (bytecode ships in one archive).

---

## 5. Correctness fixes

| # | Bug | Fix |
| --- | --- | --- |
| 1 | **Stop pressed while Deepgram was still connecting was refused**, leaving the UI stuck in "Finalizing…" for 20 s and losing the recording (the connect window is PortAudio init plus a TLS/WebSocket handshake, up to 5 s). | `stop_session` accepts a `connecting` session; capture stops at once, the latency clock starts, and the finalize is deferred until the socket opens — frames captured meanwhile were already buffered in the stream and flush first. A connect failure after a deferred stop still surfaces. |
| 2 | **Event pump could wedge forever**: `evaluate_js` blocked without a timeout on a semaphore only the page releases, so a renderer death mid-answer froze every later event for every later session. | Dispatch runs on a throwaway daemon thread with a 15 s deadline; a dead page costs one batch. |
| 3 | **Typing into a key field and deleting the text silently erased the saved key** on Save (the core treats `""` as "clear"). | Only non-empty typed keys are sent; an explicit **Remove** button sends the clear. |
| 4 | **Renderer watchdog could reload the page during a slow cold start** (heartbeat clock started at construction, before WebView2 existed). | Clock starts at `shown`/`loaded`. |
| 5 | **Two timing-flaky Deepgram tests** (fixed 0.16–0.18 s sleeps vs. the Windows proactor's ~16 ms timer quantisation and overshoot under load; one failed 7/7 in isolation on this machine). | Condition waits (`wait_until`) replace fixed sleeps. Test bug, not product. |
| 6 | Settings reads of two linked keys (`activeProfileId`, `profiles`) could race a save. | `view()` / `answer_config()` snapshot the dict once; the active-profile lookup falls back to the first profile so nothing raises on the loop. |

---

## 6. Usability improvements

- Answer panel first; provider (and active profile) shown in the header
  chip; tooltips on the record button, hotkey chip, style chips and
  selects.
- **Text size** controls (A−/A+) on the answer panel (12–22 px) and the
  prompter strip (14–28 px), both remembered.
- **Countdown** in the last 30 s before the 120 s auto-stop.
- **Errors are dismissible** (Dismiss button) instead of sitting at the
  bottom forever.
- **Live transcript follows the newest words** while recording.
- **Settings:** grouped into API keys / Profile / App sections; sticky
  Save/Back bar; "Unsaved changes" indicator; Back/Esc with unsaved edits
  asks (Save / Discard / Keep editing) instead of silently discarding;
  **Get a key** links open the provider consoles in the system browser
  (never inside the webview); per-key **Remove**; version and the
  `%APPDATA%` location shown.
- Answer-panel title row wraps instead of overflowing at the 380 px
  minimum width.

---

## 7. Refactoring

- **`frontend/src/state.ts`** — the pure reducer (`State`, `Action`,
  `reduce`, `statusFor`, `silentSoFar`) split out of the 785-line
  `App.tsx`, which is now plumbing plus layout. `Entry`/`Phase` are
  re-exported so existing imports work.
- **`app_core/contracts.py`** — Protocols and value types out of the state
  machine; `settings.py` and `stt/client.py` no longer import
  `session/machine.py` (and through it httpx).
- **`machine.py`** — the duplicated `no_llm_key` / unknown-provider error
  construction is one helper each; `build_prompt` receives the whole
  profile config.
- **`types.ts`** — `ANSWER_STYLES` defined once (chips and settings select
  both use it); `AppEventName` union; profile/call-type types;
  `bridge.dockWindow` / `bridge.openExternal` (the latter existed in the
  core but was never wired to the UI).
- **`settings.py`** — profile validation and loading factored into
  `_load_profiles` / `_validate_profiles`; per-mode bounds keys in one
  table; font/layout validation helpers.
- Version bumped to **3.1.0** in `pyproject.toml`, `package.json`,
  `installer.iss`.

Deliberately **not** done, and why:

- A field-table rewrite of the settings loader (audit item): the explicit
  per-field code is what makes the "one corrupt value costs nothing else"
  contract readable; not worth the churn.
- Moving PortAudio init/terminate off the core loop (audit BUG-04): real,
  but it changes the `AudioSource` protocol to async and touches every
  machine test; the measured cost (100–800 ms at Record and at Stop) is
  worth a dedicated pass with device-level testing.
- A second window for the answer (option C above).
- ESLint setup and the `useLatest` ref pattern: cosmetic relative to the
  rest; the two render-time ref writes are unchanged and behave as before.
- Auto-scroll "at reading pace" in prompter mode: a follow-up once real
  usage shows whether manual scrolling is enough.

---

## 8. Tests added or changed

| Area | Added | Notes |
| --- | --- | --- |
| `tests/test_prompt.py::TestCallTypes` | 10 | fixed section order, prefix/suffix split, fallbacks, headers |
| `tests/test_settings.py` (profiles, layout, fonts, per-mode bounds) | 21 | migration, load fallback, patch validation |
| `tests/test_bounds.py::TestDocking` | 5 | `top_center`, `primary_area` |
| `tests/test_bridge.py::TestDockAndCoreReadiness` | 5 | dock hook, core gating |
| `tests/test_app_wiring.py` (placement, docking, layout switch, deferred core, watchdog clock) | 16 | pywebview stub records logical move/resize calls |
| `tests/test_machine.py::TestStopContract` | +1, 1 rewritten | stop during connect |
| `tests/test_deepgram_client.py` | 2 rewritten | condition waits |
| `frontend/src/__tests__/app-prompter.test.tsx` | 13 | strip, docking, fonts, header |
| `frontend/src/__tests__/app-profiles.test.tsx` | 12 | switcher, settings CRUD, key-erase, unsaved guard |
| `frontend/src/__tests__/app-settings.test.tsx` | 1 changed | new save shape |

Every new test is documented in `docs/TESTING.md` (the repo's contract).
`docs/ARCHITECTURE.md`, `docs/TROUBLESHOOTING.md` and `README.md` were
updated for the new startup order, geometry, profiles, commands and manual
QA steps 15–18.

---

## 9. Full change log by file

**Core**

- `app.py` — deferred core build (`_build_core`, `start_core_thread`);
  layout modes and per-mode geometry; `initial_window_kwargs`,
  `top_center` docking, `_switch_layout`, `_dock_current`,
  `_window_work_area`, `_runtime_scale`; `MIN_SIZE` (380, 160) +
  `FULL_MIN_HEIGHT` 520; watchdog clock reset on shown/loaded;
  `crash_log()` helper; `on_dock` passed to the bridge; docstring on the
  two coordinate spaces.
- `app_core/contracts.py` (new) — Protocols, `AnswerConfig` (+`call_type`,
  `focus`, `notes`), `DEEPGRAM_SECRET_ID`, callback aliases.
- `app_core/session/machine.py` — imports from contracts (re-exported);
  stop-during-connect; `_missing_key_error` / `_unknown_provider_error`;
  passes call type/focus/notes to `build_prompt`; `llm:done` carries
  `callType`; httpx typing-only.
- `app_core/llm/prompt.py` — `CallTypeSpec`, six `CALL_TYPES`,
  `DEFAULT_CALL_TYPE`, `FOCUS_HEADER`, `NOTES_HEADER`,
  `call_type_choices()`, `build_prompt(..., call_type, focus, notes)`;
  `ROLE_INSTRUCTIONS`/`GROUNDING` kept as aliases; *Detailed* suffix
  reworded.
- `app_core/llm/base.py`, `retry.py`, `warm.py`, `anthropic.py`, `groq.py`
  — httpx imported under `TYPE_CHECKING` only; `wire.py` imports it inside
  `stream_sse_data` and keeps the transport timeout as plain numbers
  (`REQUEST_TIMEOUT_S`).
- `app_core/store/settings.py` — profiles + migration + mirrors;
  `layoutMode`, `prompterFontPx`, `answerFontPx`, `prompterBounds`;
  snapshot reads; `layout_mode()`; `window_bounds(mode)` /
  `set_window_bounds(bounds, mode)`.
- `app_core/store/bounds.py` — `WorkArea.width/height`, `top_center`,
  `primary_area`.
- `app_core/bridge/api.py` — `machine` may be `None` + `_attach_core` /
  `_require_core` (25 s wait); `dock_window`; typing-only imports.
- `app_core/bridge/events.py` — bounded `evaluate_js` on a dedicated
  thread (`DISPATCH_TIMEOUT_S` 15 s).
- `app_core/stt/client.py` — imports callback aliases from contracts.

**Frontend**

- `index.html` — inline dark background.
- `src/state.ts` (new) — reducer and derived helpers; `Entry.callType`;
  `error-dismiss` action; `RECORD_CAP_S`.
- `src/App.tsx` — recomposed: answer first, header chip + ⤒ ⊤ ⚙, context
  row (Profile / Call type), countdown, transcript follow, dismissible
  errors, font-step handlers, prompter branch.
- `src/components/PrompterView.tsx` (new).
- `src/components/AnswerPanel.tsx` — font size, A−/A+, call-type tag,
  wrapping title row.
- `src/components/SettingsPanel.tsx` — sections, profiles CRUD, key-erase
  fix, Remove, Get-a-key links, unsaved guard, canonical id adoption.
- `src/types.ts` — profile/call-type/layout types and constants,
  `ANSWER_STYLES`, `AppEventName`, `SettingsPatch` additions.
- `src/bridge.ts` — `dockWindow`, `openExternal`.
- `src/styles.css` — prompter, context row, header chip, error box with
  button, settings sections/sticky actions, wrapping title row, answer
  body sized in `em`.
- `src/__tests__/testutils.tsx` — new view fields, `profile()`, mock
  `dock_window` / `open_external`.

**Build / meta**

- `pyproject.toml`, `frontend/package.json`, `installer.iss` — 3.1.0.
- `.gitignore` — `.coverage` (from the previous session).
- `fabledocs/` (new) — this report, SETUP-AND-DEPLOY.md, USER-GUIDE.md.

---

## 10. Recommended next steps

1. Run the manual QA script (README, steps 1–18) once on a real call with
   a real webcam and both providers; the call-type prompts in particular
   deserve one canned question each.
2. Commit this pass (`git add -A && git commit`) — nothing here is
   committed yet.
3. Rebuild the release with `build.ps1` and re-verify the four release
   checks in SETUP-AND-DEPLOY.md §4.
4. On this development machine, apply the stdlib bytecode compile and the
   Defender exclusions from SETUP-AND-DEPLOY.md §3 — the test suites will
   run several times faster.
5. Follow-ups worth a pass of their own: async audio start/stop off the
   loop (BUG-04), a prompt-quality eval harness for the six call types,
   and reading-pace auto-scroll in prompter mode.
