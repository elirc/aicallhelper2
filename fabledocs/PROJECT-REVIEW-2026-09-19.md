# AI Call Assistant — Project Review

**Review date:** 2026-09-19  
**Reviewed version:** working-tree v3.1.0, based on Git HEAD `c6191b5`  
**Deliverable:** review only; no application fixes, configuration changes, dependency updates, or changes to existing documentation.

**Project responses (added 2026-09-19):** every finding and documentation point below was re-verified against the same working tree before responding. Responses appear as quoted blocks headed *Project response*; a consolidated disposition is in §7. Nothing in the review was disputed in substance. Three statements were corrected (R02 old-key loss, the REPORT.md watchdog wording under R11, and the Clear button under §5), and verification surfaced additional defects that are recorded under the finding they belong to.

## 1. Overall assessment

The application has a sound foundation for a small Windows desktop product. The Python core owns the recording and answer pipeline, provider implementations are isolated, and the React frontend is deliberately limited to presentation and commands. The prompter and profile additions make sense for the product. The existing tests cover many difficult cancellation, transport, parsing, and persistence cases.

The next investment should be reliability at the boundaries between these components. Passing tests currently coexist with reproducible defects: provider errors can become successful empty answers; an old command response can reset a newer recording's UI; entering Settings hides the screen-protection warning; and a corrupt settings file can be overwritten by an automatic window-position save. Several documentation claims are stronger than the implementation or available measurements justify.

**Recommendation:** retain the architecture and address the P1 findings before broader distribution or reliance during important calls. Follow with recording/recovery fixes, measured latency work, and a repeatable packaged-app validation process. A rewrite or additional provider integrations would be lower value at this point.

> **Project response:** Agreed on the architecture verdict and on clearing the P1 items before wider distribution. Re-verification found no finding that was wrong in substance; where the code differs from the description it is usually worse (R04, R08, the R06 tail loss, R13). This review is adopted as the backlog for the next pass, in the §6 order with the adjustments noted there.

### What is already working well

- Protocol-injected core dependencies allow substantial offline testing without capturing audio or calling a paid provider.
- A single core event loop, session identifiers, and backend command tickets provide a clear ownership model.
- Retry behavior is conservative: one connection-level retry, with no retry after answer text has streamed.
- The streaming resampler preserves state across chunks and filters before downsampling.
- Model output is rendered through React text nodes and a restricted Markdown parser, with nesting limits and a rendering fallback.
- Settings writes are serialized and use temporary files plus atomic replacement. Profile fields are validated, and saved keys are not returned to the frontend.
- The prompter uses the same native window as the full view, avoiding a second capture-protection surface.
- The documentation explains implementation decisions and prior failures unusually thoroughly. Preserve that reasoning while correcting stale descriptions and absolute promises.

## 2. Scope and verification

The review covered the shell and Windows integration in `app.py`; all core subsystems under `app_core`; the React bridge, state management, components, styles, Markdown renderer, and configuration; the test structure and relevant regression cases; dependency/build/installer/CI files; and project documentation, with particular attention to all three existing files in `fabledocs`. Supporting architecture, troubleshooting, testing, and learning documentation was cross-checked selectively.

The working tree already contained extensive uncommitted changes and untracked files when this review began. Findings apply to that current source, including those changes, rather than only the last commit. The previous improvement report is historical evidence, not proof that every product claim has been verified.

### Checks run during this review

| Check | Result |
| --- | --- |
| `.venv\Scripts\python -m pytest tests -q -p no:cacheprovider` | **376 passed**, reported duration 60.03 s |
| Frontend `npm test` / Vitest | **173 passed** across 13 files, reported duration 179.78 s |
| `.venv\Scripts\python -m ruff check app_core app.py tests --no-cache` | Passed |
| `.venv\Scripts\python -m mypy` | Passed; 30 source files |
| Frontend `npm run typecheck` | Passed |
| Additional Python checks using mocked HTTP, synthetic PCM, temporary settings, and existing core fakes | Reproduced findings described below |
| Additional in-memory esbuild/jsdom checks against the actual React components and reducer | Reproduced stale-start, hidden-warning, repeated-Escape, and stop-recovery defects |

The additional checks were executed from standard input; no regression-test files or application changes were added. One initial UI probe needed an ASCII-compatible selector because PowerShell changed an ellipsis when piping to Node; the corrected probe completed successfully. This was a review-harness issue, not an application test failure.

**Limits:** no real call, audio-device capture, native window interaction, screen-share capture, live provider request, or new installer build was performed. The existing `build`/`dist` binaries were not validated against the current source. No paid API keys were used. Installed dependency vulnerability databases were not audited. The ~1 s user-visible latency target, mixed-DPI behavior, answer quality, and real capture exclusion remain release-validation items.

Source locations below refer to the reviewed snapshot. Paths are relative to the repository root; line numbers will change as the project evolves.

> **Project response:** The test counts match what the project records (docs/TESTING.md and REPORT.md both state 376 core and 173 frontend; the frontend figure re-derives statically as 162 plain cases plus 11 `it.each` expansions). The gates were not re-run for this response; each finding was verified by reading the working tree, and each response below cites what was found. The stated limits are accurate, and nothing native or live was exercised on our side either.

## 3. Prioritized findings

**P1:** resolve before broader distribution or important-call reliance. **P2:** address in the next reliability pass. **P3:** polish or maintenance. Priority reflects consequence and reproducibility, not a claim that every issue happens frequently.

| ID | Priority | Finding | Evidence |
| --- | --- | --- | --- |
| R01 | P1 | Screen-protection warning disappears in Settings | Reproduced with actual React components |
| R02 | P1 | API keys silently fall back to plaintext encoding | Reproduced with a failing keystore |
| R03 | P1 | Streaming provider errors and incomplete answers can report success | Reproduced with mocked HTTP/SSE |
| R04 | P1 | A stale start response resets a newer recording's UI | Reproduced with deferred bridge promises |
| R05 | P1 | Automatic geometry saving overwrites corrupt settings | Reproduced in a temporary directory |
| R06 | P2 | Stop discards the last incomplete PCM frame | Reproduced with synthetic PCM |
| R07 | P2 | Event timeout does not preserve ordering or guarantee prompt recovery | Late execution reproduced; retry delay derived from code |
| R08 | P2 | Refused-stop recovery can retire a healthy streaming answer | Reproduced against the reducer |
| R09 | P2 | Settings can discard unsaved work through navigation and save races | Repeated Escape reproduced; save race confirmed by inspection |
| R10 | P2 | Audio lifecycle blocks the event loop; recording cap starts late | Blocking calls confirmed; delayed-cap behavior reproduced |
| R11 | P2 | Startup/reload recovery lacks an explicit failure and resynchronization contract | Source inspection |
| R12 | P2 | Per-layout geometry restoration ignores other connected displays | Source inspection; native validation needed |
| R13 | P2/P3 | Release reproducibility and version metadata need tightening | Build, CI, manifest, and lockfile inspection |

### R01 — Keep capture-protection status visible in every view

**Locations:** `frontend/src/App.tsx:384`, `frontend/src/App.tsx:479`, `frontend/src/components/SettingsPanel.tsx:403`, `frontend/src/state.ts:73`.

`App` returns `SettingsPanel` before rendering the protection-warning banner. The panel does not receive the protection state and instead says that the window is hidden from screen sharing. This removes a warning precisely when the user may be editing personal profile information or entering keys.

**Reproduction:** emit `protection:failed`, then open Settings. The offline UI probe observed one alert before navigation, zero alerts afterward, and the affirmative hidden-window statement still present.

**Suggested improvement:** render the protection indicator outside view-specific branches and represent its state as unknown/checking, confirmed, or failed. Include the current verdict in a frontend readiness snapshot, so a one-time event missed during page loading cannot leave the UI falsely reassuring. Verification by Windows is useful, but it is not a universal capture guarantee; Microsoft explicitly documents that limitation. [Microsoft display-affinity documentation](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-getwindowdisplayaffinity)

**Acceptance:** a failed verdict remains visible in full view, Settings, prompter, and after a renderer reload. A page that has not received a verdict shows an unknown state. Verify actual screen and window sharing in the call applications supported by the release.

> **Project response — accepted, P1.** Confirmed at App.tsx:384-392 (early return) and SettingsPanel.tsx:402-406 (the static sentence "This window is hidden from screen sharing"). Two framing corrections: the verdict is hidden, not lost, because the settings-open and settings-close actions leave `protectionFailed` untouched and the banner returns on Back; and the prompter view does render the alert (PrompterView.tsx:192-196), so the gap is Settings only, including Settings opened from the prompter.
>
> On reload: the core re-emits a verdict on every `shown`/`loaded` (app.py:503-529, stamped with a sequence number so a slow loser cannot overwrite a fresher result), so the practical reload risk is small. The reviewer is still right that there is no snapshot channel: `SettingsView` carries no protection field and `subscribeAppEvents` has no early buffer, so a verdict emitted before the React subscription is dropped. Four protection tests exist; none opens Settings after `protection:failed`.
>
> Plan: lift the banner above the view branches, delete the static sentence, add a tri-state protection field to the settings view so a fresh page can read it, and add the missing test. Agreed that the display-affinity flag is not a universal capture guarantee; the guide wording is handled under §4.

### R02 — Make failure to encrypt keys explicit

**Locations:** `app_core/store/secrets.py:42`, `app_core/store/settings.py` key-saving branch, `frontend/src/components/SettingsPanel.tsx:403`.

`encode_secret` catches any DPAPI error and returns `plain:` plus Base64. `decode_secret` accepts that value without a keystore. The prefix is visible to someone inspecting the file, but the UI and user-facing guides promise encryption and do not disclose the downgrade.

**Reproduction:** a fake keystore whose `protect` method raises produced the `plain` storage prefix; the dummy key could then be decoded with `keystore=None`.

**Suggested improvement:** fail the save with an actionable encryption error, or offer an explicitly identified memory-only session key. If plaintext compatibility is retained, expose its status prominently and migrate existing plaintext entries when DPAPI becomes available. Do not discard a previously encrypted key when replacing it fails.

**Acceptance:** simulate DPAPI failure during replacement; the old saved key survives, and the user is told the replacement was not encrypted/saved. Documentation must accurately describe the chosen behavior. Also state that profile text itself is stored as ordinary JSON, not DPAPI-encrypted.

> **Project response — accepted with one correction, P1.** Confirmed: `encode_secret` swallows every exception (secrets.py:42-50), nothing in `app_core` logs anything (there is no logging call in the package), and `view()` exposes only has-key booleans, so the UI cannot tell `enc:` from `plain:`. In production the keystore is always `DpapiKeystore()`, so the only trigger is a win32crypt import failure or a `CryptProtectData` failure.
>
> The correction: no path leaves the user without a key. A failed replacement does not discard the previous encrypted key; it overwrites it with a working `plain:` copy of the new key. What is lost silently is the encryption, not the credential. Also relevant to the fix: the fallback is deliberate and pinned by tests (tests/test_secrets.py:46-53), and it is disclosed in docs/TROUBLESHOOTING.md:246-248 and docs/learn/11-security-model.md, though not in any `fabledocs` guide, the README, or the Settings screen. What is uncovered is exactly the acceptance criterion as written: no test asserts that the user is told or that the old ciphertext survives.
>
> Plan: make `encode_secret` raise, turn that into a user-visible save error that leaves the previous secret intact, keep decoding legacy `plain:` values and re-encrypt them on the next successful save, and report a per-key encrypted flag in the view. Profile text is plain JSON and the docs will say so.

### R03 — Validate provider-level success, not just HTTP success

**Locations:** `app_core/llm/anthropic.py:82`, `app_core/llm/groq.py:139`, `app_core/llm/wire.py:41`, `app_core/llm/retry.py:27`.

The transport considers any SSE data payload evidence of a nonempty response. Provider extractors ignore everything except text deltas, and `stream_answer` returns the concatenation even when it is empty. Neither adapter validates its normal end-of-message signal or exposes output truncation metadata.

With HTTP 200, the offline Anthropic checks returned success for all three cases:

| Mock stream | Actual result |
| --- | --- |
| Only an `overloaded_error` event | Empty string |
| Text delta followed by an error event | Partial answer string |
| Text delta followed by EOF, without a completion event | Partial answer string |

The session machine can therefore emit `llm:done` for an error or incomplete answer. This is a real protocol case: Anthropic documents errors inside a stream and a final `message_stop` on normal completion. [Anthropic streaming documentation](https://platform.claude.com/docs/en/build-with-claude/streaming)

**Suggested improvement:** parse known provider errors and terminal signals; preserve partial text while reporting failure or truncation; reject an answer with no usable text. Retain tolerance for harmless unknown event types. Add provider result metadata for finish reason and usage so reaching a token cap is distinguishable from a completed answer.

**Acceptance:** cover error-before-text, error-after-text, valid empty output, clean-but-premature EOF, token-limit completion, and normal completion for both providers. No automatic retry after a displayed delta.

> **Project response — accepted, P1.** Confirmed on every point: the produced flag in wire.py:40-64 flips on any `data:` payload (Anthropic's `ping` is a data line, so even keep-alives satisfy it); neither adapter matches `error`, `message_stop`, `message_delta`, `[DONE]`, `finish_reason` or `stop_reason` anywhere outside test fixtures; `stream_answer` (retry.py:42-48) has no post-condition on the collected parts; and machine.py:464-487 emits `llm:done` for any non-aborted, non-errored result including an empty string.
>
> Two additions. The user-visible outcome of an empty answer is worse than a bare "success": the reducer marks the entry done and the panel falls back to its placeholder text, so the user sees a finished, blank answer with no error and no Copy button. And the clean-EOF case is helped along by an intentional feature: sse.py flushes an unterminated final `data:` line so hostile chunking is tolerated, and that is tested as a feature. The fix must keep it while distinguishing truncation, which means tracking the provider's terminal event rather than the framing.
>
> The `stream()` Protocol returns bare strings (base.py:64-68), so exposing stop or finish reason is a contract change; a small result object will be added rather than overloading the delta stream. Both providers cap output at 1,024 tokens, so token-limit truncation is a live case. All six acceptance scenarios are currently untested and will be added for both providers, keeping the no-retry-after-delta rule.

### R04 — Give frontend commands their own generation identifiers

**Locations:** `frontend/src/App.tsx:174`, `frontend/src/App.tsx:212`, `frontend/src/App.tsx:230`.

The backend uses a command ticket to implement latest-command-wins. The frontend uses shared booleans (`callInFlightRef` and `abortStartRef`) and lets every response update the current state, even when it belongs to an older request.

**Reproduction:** start request A; cancel while A is pending; start request B; resolve B as `s2`; then resolve A as `aborted`. The UI correctly showed `Stop & Answer` for B, then incorrectly changed to `Record` after A's response. B can still be recording in the core while the UI has cleared its session state. A previous response can also clear the shared event buffer belonging to a later request.

**Suggested improvement:** assign a monotonically increasing frontend command generation and associate cancellation, buffering, adoption, and response handling with that generation. A stale response may clean up its own session but must not change the newer command's UI. Apply the same principle to Ask/Regenerate and quick-setting responses, which also permit overlapping calls.

**Acceptance:** exercise start/cancel/start and ask/start with responses resolving in both orders, including stale success, abort, and failure. The UI and core must agree on the active session and stopping it must remain possible.

> **Project response — accepted, P1, and worse than described.** The scenario reproduces at App.tsx:174-193 and state.ts:173-176: `start-aborted` and `start-failed` are applied unconditionally. Because they also null `sessionId`, every later event for B fails the reducer's session guard and is dropped permanently, so B's transcript and answer are unrecoverable from the UI while the core keeps recording. The core really does produce the late abort: start A claims a ticket, awaits two threaded secret reads, and only then checks the ticket (machine.py:144-158).
>
> Three related defects in the same lines: `abortStartRef` is reset by the next start, so if A then resolves ok it is adopted and `cancelSession(A)` is never sent, dropping the user's explicit abort; clearing `callInFlightRef` when A returns clobbers B's in-flight flag, so B's pre-adoption events fall through to the stale-id branch; and emptying `bufferRef` on A's failure discards B's buffered events even though `adoptSession` already filters replay by session id. Ask and Regenerate share the same refs and can overlap (the phase guard allows `answering`), but an aborted ask dispatches nothing, so the phase-reset bug is specific to start. Quick settings are last-response-wins, and font steps read the last adopted value, so two fast clicks net one step.
>
> Plan: a per-command generation carried through abort, buffering, adoption and response handling, as suggested, with a stale-response path that may cancel its own session but never touches state. No test interleaves two in-flight commands today; those will be added first.

### R05 — Preserve unreadable settings before automatic writes

**Locations:** `app_core/store/settings.py:117`, `app_core/store/settings.py:183`, `app_core/store/settings.py:373`; `app.py` bounds-saving callbacks.

An unreadable or invalid-JSON settings file loads defaults without an immediate write, as documented. However, moving/resizing/closing the window subsequently saves those defaults and new geometry over the original file. Recoverable profile/key data in the damaged file is then lost without an explicit user save.

**Reproduction:** create a corrupt settings file in a temporary directory, construct `SettingsStore`, then call `set_window_bounds`. The original content was replaced by default JSON and no backup was created.

**Suggested improvement:** distinguish missing-file first run from failed-file loading. Preserve a recoverable copy before any replacement, surface a recovery notice, and avoid silently treating transient read failures as a new installation. Consider separating cosmetic geometry persistence from profile/credential persistence and keeping a last-known-good backup.

**Acceptance:** corrupt JSON and simulated read errors must not destroy the existing bytes after window movement or close. Recovery should preserve unrelated valid fields and should never put keys into logs or diagnostic exports.

> **Project response — accepted, P1.** Confirmed: `_load` (settings.py:117-126) returns defaults for any unreadable or non-object file with no backup, no flag and no distinction from a missing file; `set_window_bounds` (settings.py:373-383) copies the whole in-memory dict and rewrites the file under a silent except; and app.py:636-638 wires move, resize and close to it through the debounced saver, with `_on_closing` flushing synchronously. A launch where the user touches nothing and closes the window is enough to overwrite the original.
>
> The per-field fallback for a valid but partially bad object is well covered by tests; only the whole-file failure is unprotected, and no test writes a corrupt file and then calls a saver.
>
> Plan: record load failure in the store, rename the unreadable file to a timestamped `.corrupt` copy before the first write, surface a recovery notice through the settings view, and keep the rule that loading never writes. Separating geometry into its own file is attractive but larger; backup-before-first-write closes the data-loss case on its own.

### R06 — Drain captured audio before closing the transcription stream

**Locations:** `app_core/audio/capture.py:100`, `app_core/audio/capture.py:111`, `app_core/session/machine.py:307`.

Capture emits only complete 2,048-sample frames. `stop()` clears `_pending`, so the last 1–2,047 samples already captured are discarded. At 16 kHz this is up to almost 128 ms, which can contain a final syllable. Separately, `_begin_stop` sets `stop_requested` before stopping capture; frame callbacks already queued to the core loop can then be rejected by `_on_frame`.

**Reproduction:** feed 1,600 mono samples at 16 kHz into the capture adapter without opening a device. Before Stop, 1,600 samples were pending; afterward, zero frames had been delivered and the pending buffer was empty.

**Suggested improvement:** define an explicit stop-and-drain operation. Stop accepting new device input, deliver the remaining PCM and already-accepted callbacks for the same session, then enqueue Deepgram `CloseStream`. Cancellation should retain its separate discard semantics. Preserve session ownership throughout an asynchronous drain.

**Acceptance:** test recordings shorter than one frame and every possible remainder length, plus callbacks queued immediately before Stop. All accepted PCM must precede `CloseStream`, with no samples leaking into a replacement session. Validate final-word transcription with real audio afterward.

> **Project response — accepted, P2, with two additions.** Confirmed: only complete 2,048-sample frames leave capture (capture.py:100-105), `stop()` clears the remainder (capture.py:122), and `_begin_stop` sets `stop_requested` before stopping capture (machine.py:307 versus 311), so callbacks already queued are rejected by `_on_frame`.
>
> First addition: the tail loss is larger than 128 ms. PortAudio's device-side buffer is one eighth of the source rate, about 125 ms, and is discarded by `stop_stream()`, so the realistic worst case is roughly 250 ms. Second: the rejection in `_on_frame` is a deliberate invariant ("Rule 4") pinned by two tests (tests/test_machine.py:268 and :694), because the Deepgram client sends `CloseStream` down the same queue as audio and frames after stop would race it. A capture-side drain alone would therefore change nothing; the fix has to span both layers and preserve close ordering. There is no test for `LoopbackCapture` at all today, so the reviewer's probe is new coverage.
>
> Plan: design this with R10 as a single stop-and-drain operation that flushes the resampler tail and the pending remainder for the same session, then enqueues close, with cancel keeping its discard semantics.

### R07 — Treat an event-dispatch timeout as an uncertain delivery

**Locations:** `app_core/bridge/events.py:22`, `app_core/bridge/events.py:66`, `app_core/bridge/events.py:91`.

Timing out `_evaluate_bounded` does not stop its daemon thread or cancel the underlying `evaluate_js`. A timed-out script may execute after subsequent scripts. Retrying the same batch as individual events can duplicate deltas if the first call executed but its acknowledgement was delayed.

**Reproduction:** hold the first fake `evaluate_js` call past its timeout, allow the next to complete, then release the first. Actual execution order was `new`, then `old`. The timeout returned `False` for the old call even though it later executed.

The retry policy also has a large worst case: a 64-event batch followed by 64 separate 15 s timeouts can occupy the pump for **975 s**. That duration is derived from the constants, not a measured real-renderer outage. Repeated permanently blocked calls leave daemon threads behind, while the queue has no size bound.

**Suggested improvement:** separate serialization failures from renderer failures; split only the former. Add event sequence numbers and a renderer generation, reject duplicate/stale deliveries, and resynchronize from a core snapshot after reload. Coalesce expendable audio-level updates and bound pending work. Avoid repeated retries against a renderer already known to be unavailable.

**Acceptance:** fake delayed acknowledgement after successful execution, permanent blocking, out-of-order completion, and reload during a batch. Verify bounded recovery, no duplicate displayed text, and continued delivery of terminal events.

> **Project response — accepted, P2.** Confirmed, including the arithmetic: with `MAX_BATCH = 64` and `DISPATCH_TIMEOUT_S = 15.0` (events.py:21-25), one timed-out batch plus 64 timed-out singles is 15 + 960 = 975 s. `wait_for` abandons the future, nothing joins the thread, the queue is unbounded, and `_dispatch` returns False for both a serialization error and a timeout, so the retry cannot tell them apart. The module docstring's "strictly one at a time, preserving order" holds only while nothing times out.
>
> Two mitigations the review did not weigh. Duplicate deltas are a transient display defect rather than a corrupted answer, because `llm:done` carries the full text and overwrites the accumulated buffer, and a duplicate `llm:done` is dropped by the session guard. And the renderer watchdog reloads a dead page after roughly 18 to 20 s, which bounds the outage but neither unblocks the pump nor invalidates the backlog, so the 975 s figure stands. Also confirmed that `audio:level` is not coalesced anywhere: one event per 128 ms frame, roughly 940 per full recording. The timeout path is untested because the fake window can raise but cannot hang.
>
> Plan: split serialization failures from renderer failures, add event sequence numbers and a renderer generation, coalesce `audio:level` to latest-wins, bound the queue, and stop retrying against a renderer the watchdog already knows is dead.

### R08 — Disarm refused-stop recovery when the answer starts

**Locations:** `frontend/src/App.tsx:156`, `frontend/src/state.ts:179`, the reducer's `deltas` case.

When Stop races the recording cap, a refused stop sets `stopStranded` and arms a 20 s recovery timer. Receiving answer deltas changes the phase to `answering` but does not clear `stopStranded`. If the legitimate answer is still streaming at 20 s, recovery retires it and clears `sessionId`; later text and completion events are ignored by the reducer. The backend permits up to 60 s of LLM generation.

**Reproduction:** run the actual reducer through start acceptance, refused stop, a valid delta, and recovery. The recovery flag remained true during streaming; recovery then changed the phase to idle and the session to null.

**Suggested improvement:** disarm this timer when a valid answer delta or terminal event proves the session is alive. Scope timer actions to a session identifier so they cannot affect a later session. Prefer querying core state over guessing that silence means the session disappeared.

**Acceptance:** reproduce an auto-stop/Stop race followed by an answer taking more than 20 s. The full answer must survive. A truly missing session must still recover.

> **Project response — accepted, P2, and worse than described.** Confirmed: the deltas case (state.ts:211-219) leaves `stopStranded` set, the timer effect depends only on that flag (App.tsx:156-166), and `stop-recover` retires the live entry and nulls `sessionId`, after which the session guard drops the rest of the stream and the final `llm:done`. Only `llm:done`, `session:error` and `start-pressed` clear the flag.
>
> The addition: `ask-accepted` does not clear it either. After a refused stop and one delta the Ask box is enabled, so a user can start a second session while the original timer is still counting, and recovery then kills that second session, which was never stranded. No existing test streams past 20 s.
>
> Plan: clear the flag on the first valid delta, key the timer to the session it was armed for, and prefer a core status query over silence as the recovery signal, which depends on the snapshot command from R11.

### R09 — Protect settings drafts through navigation and slow saves

**Locations:** `frontend/src/components/SettingsPanel.tsx:80`, `frontend/src/components/SettingsPanel.tsx:142`, `frontend/src/components/SettingsPanel.tsx:166`.

The unsaved guard only blocks the first Back/Escape action. Once `confirmDiscard` is true, `back()` calls `onBack()` directly. Repeating Escape therefore discards edits without selecting Discard. This was reproduced against the real component.

There is also no saving state or draft revision. If a user saves profile version A and edits it to B before the reply, `setProfiles(result.value.profiles)` replaces B with the saved A. Multiple saves may overlap. This second issue follows from the asynchronous save/adoption path; it was not separately reproduced with a delayed filesystem write.

**Suggested improvement:** make only an explicit Discard action discard a dirty draft. While saving, either prevent conflicting edits/actions or preserve edits made after the submitted revision. Make Save-and-return behavior explicit. Disable Add/Duplicate at the 20-profile limit, cap duplicate names at the supported length, and allow undoing a queued key removal.

**Acceptance:** repeated Escape keeps the draft; failed saves preserve it; edits typed after Save survive the response; rapid saves cannot restore an older draft. Closing the native window with unsaved work should have a defined policy too.

> **Project response — accepted, P2.** All sub-claims confirmed. (a) The Escape handler re-registers with `confirmDiscard` in its closure (SettingsPanel.tsx:80-95), so the second Escape calls `onBack()`; the confirm bar does render first, so it is not silent, but Escape-to-dismiss is the natural reflex and it discards. The one covering test uses the Back button, never a second Escape. (b) There is no saving flag; the save path replaces profiles, typed keys and queued removals with the response and resets the saved snapshot, so edits typed during the request are lost and the unsaved indicator clears while "Saved" is shown. (c) Overlapping saves are possible but cannot corrupt the file, since the store holds a lock and each patch is atomic; the exposure is last-response-wins for the adopted view. (d) Add and Duplicate have no length check; the core rejects at 21 with all-or-nothing validation, which blocks every other edit in that save. (e) The copy suffix bypasses the 60-character input limit and compounds on repeated duplication. (f) Nothing removes an id from the removal set except a successful save, and removals are applied after typed keys, so a queued removal silently beats a key the user then retypes into the same field.
>
> Plan: discard only on an explicit Discard action, add a saving state with a draft revision so a response is adopted only when it matches the submitted revision, disable Add and Duplicate at the limit, truncate copied names, and make Remove a toggle that a retyped key cancels. Closing the native window with a dirty draft will prompt the same way.

### R10 — Fix the recording lifecycle before tuning provider speed

**Locations:** `app_core/session/machine.py:234`, `app_core/session/machine.py:288`, `app_core/session/machine.py:311`; `app_core/audio/capture.py`.

PortAudio construction, stream opening/stopping/closing, and termination execute synchronously from the core event loop. While those calls block, the loop cannot progress WebSocket connection setup, process audio callbacks, deliver events, or enforce timers. The documented parallel start is therefore only partly parallel: `stream.connect()` begins after the synchronous audio start returns. Stop time also includes synchronous device teardown.

The recording cap is armed only after STT connects, although capture starts beforehand. Consequently the advertised 120 s hard cap excludes the connection interval. A scaled fake reproduction held connect open beyond a 30 ms configured cap and observed audio started, no stop, and no auto-stop event. The production connection attempt has its own timeout; this finding is a late cap, not a claim of unlimited successful recording.

**Suggested improvement:** use a serialized audio worker with explicit lifecycle/ownership rules, including the drain in R06. Start the recording deadline at capture acceptance and measure elapsed time from a monotonic timestamp. Keep UI countdown and core deadline aligned; an incrementing browser interval can drift when the page is minimized or busy.

**Acceptance:** inject slow device start/stop and slow STT connect while verifying responsive commands and deadlines. Measure real device lifecycle time before choosing persistent capture/device reuse. The previous `REPORT.md` already identifies blocking audio as unfinished work; this review agrees with that priority.

> **Project response — accepted, P2, with two additions.** Confirmed: every audio call is direct on the loop (machine.py:234, 311, 589, 617; `to_thread` is used only for settings and DPAPI reads), `stream.connect()` at line 249 follows the synchronous audio start at 234, and the cap is armed at machine.py:286-290 after connect. The 5 s STT connect timeout (stt/client.py:55) bounds the overshoot, as the review already notes.
>
> First addition: `stop_time`, the latency clock, is stamped at machine.py:308 before the audio stop at 311, so synchronous device teardown sits inside `sttFinalizeMs`, `firstTokenMs` and `totalMs`. That contaminates the number the app is judged on and matters for the benchmark work in §5. Second: the frontend countdown starts on `start-accepted`, which fires when `start_session` returns, before device open and connect, so the UI timer leads the core cap by up to several seconds; the two drift in opposite directions. The existing cap tests connect first, so the late-arming window is invisible to them.
>
> Plan: a serialized audio worker with explicit ownership, the cap armed at capture acceptance from a monotonic timestamp, stop time stamped after the drain, and the UI countdown driven by a core-supplied deadline rather than a local interval. Agreed with the reviewer and REPORT.md that this precedes any provider-speed tuning.

### R11 — Make startup and renderer recovery observable and complete

**Locations:** `app.py:309`, `app.py:591`, `app.py:672`, `app_core/bridge/api.py:136`, `frontend/src/bridge.ts`.

If `_build_core` fails, it logs and returns without a terminal readiness result. Every later session command can wait 25 s and then report that the app is still starting, although the worker has already failed. Separately, frontend `whenReady()` has no deadline, and the initial settings-load failure is ignored by `App`.

The claim that the watchdog starts only once the window is shown is also incomplete. `last_heartbeat` is initialized during API construction; `_heartbeat_watchdog()` starts before `webview.start()`; `_check_renderer()` has no shown/loaded gate. Resetting the timestamp in those callbacks does not prevent probing/reloading a sufficiently slow initial boot. Real cold-start impact was not measured here.

Reloading a failed renderer discards React session/history state but does not explicitly cancel or re-adopt the live core session. The new page has no authoritative snapshot to reconcile the recording or answer still running behind it.

**Suggested improvement:** expose `starting / ready / failed` with a useful startup error and a bounded frontend readiness path. Gate renderer-health checks until initial readiness. Define reload behavior: safely cancel capture or restore the live session from a core snapshot, including protection status. Add orderly shutdown for audio, tasks, and the shared HTTP client.

**Acceptance:** force import/core-construction failure, delayed first page load, missing bridge readiness, and reload during recording/answering. Each must leave a truthful, usable UI without orphaned capture.

> **Project response — accepted, P2, with one wording correction and one mechanism added.** Confirmed: `_build_core` failure logs and returns (app.py:347-353), so `_attach_core` never runs; every command then waits `CORE_READY_TIMEOUT_S` (25 s) and answers "still starting" (api.py:34, 136-139, 174-176), per command; there is no readiness or snapshot command in `JsApi` or `RawApi`; `whenReady()` cannot reject; and the initial settings load ignores failure, which leaves settings null for the life of the page and disables the prompter button and call-type select with no message.
>
> The correction: REPORT.md says the watchdog clock starts at `shown`/`loaded`, not the watchdog itself, and that wording is accurate. The substance of the critique stands, because resetting the clock on `shown` cannot help before `shown` fires, and the comment at app.py:519-529 overclaims. The mechanism: if cold start exceeds 15 s the probe calls `evaluate_js`, which in the vendored pywebview blocks up to 20 s waiting for the page, so the 3 s join expires and a booting page is classified dead; the reload then blocks until `shown` and lands at first paint, exactly the case the comment says is prevented. A reload also leaves the core session running with no page tracking it.
>
> Plan: a status command returning starting, ready or failed with the startup error; the watchdog gated on first readiness; a session snapshot the page adopts on mount, which also serves R01 and R08; and orderly shutdown of audio, tasks and the shared HTTP client.

### R12 — Restore layout geometry across all connected displays

**Location:** `app.py:454`, particularly `areas = [area]` in `_switch_layout`; `app_core/store/bounds.py`.

When switching layouts, saved coordinates are validated only against the monitor the current window occupies. If the full layout was saved on display A and the prompter is now on display B, returning to full mode treats A's valid coordinates as off-screen and docks on B. This contradicts the guide's promise of returning exactly to the previous location. Startup restoration already considers all displays.

**Suggested improvement:** validate saved geometry against every connected work area in the correct coordinate space. Use the current display for fallback docking only when the saved display is unavailable. Also handle saved oversized/off-top windows more carefully: a 40 px intersection alone does not guarantee a reachable title bar or usable window.

**Acceptance:** full view on A, prompter on B, repeated switching, unplugging A, negative monitor coordinates, and mixed 100/125/150% scaling. These require native Windows validation; the existing arithmetic/fake-window tests do not prove physical placement.

> **Project response — accepted, P2, and the guide steers users into it.** Confirmed: `_switch_layout` builds its area list from the current display only (app.py:466) while startup passes every display (app.py:651), and `sanitize_bounds` nulls the position when no listed area overlaps by 40 px on both axes (bounds.py:24, 84-89). Size survives and position does not, so the user gets the right size on the wrong display.
>
> Two additions. The USER-GUIDE's own webcam tip (lines 81-83: drag the strip to the camera's monitor and dock there) is precisely the sequence that triggers this, so the "exactly where it was" promise at lines 73-74 and the tip contradict each other in current code. And if the current work area cannot be resolved, the area list is empty, docking returns early, and `_place` is never called, so the window keeps the previous layout's geometry while rendering the new layout. The layout-switch tests monkeypatch a single work area, so no test passes more than one area into this path.
>
> Plan: validate against all work areas, use the current display only for fallback docking, and add a title-bar reachability rule on top of the 40 px overlap. Native multi-display validation goes on the release checklist as suggested.

### R13 — Make releases reproducible and identifiable

**Locations:** `pyproject.toml`, `.github/workflows/ci.yml`, `build.ps1`, `aica.spec`, `installer.iss`, `frontend/package-lock.json:3`, `frontend/src/components/SettingsPanel.tsx`.

Python runtime/development dependencies are installed through unpinned command lines and are not declared as project dependency groups or backed by a lock/constraints file. CI and a developer machine can therefore validate different dependency sets. The frontend has a lockfile, but its root version still says **3.0.0**, while `package.json`, the Python manifest, and installer say **3.1.0**; the UI separately hard-codes `v3.1`. The version mismatch is maintenance drift, not evidence that `npm ci` fails.

CI runs the code/test/frontend-build gates, but does not build or smoke-test the Windows executable/installer. A PyInstaller dependency or WebView2 bootstrap problem can thus escape all current checks. The build script also assumes the existing local environment has already been prepared correctly.

**Suggested improvement:** declare and lock tested Python dependencies, use the frontend lockfile consistently, add a noninteractive packaging job and clean-Windows smoke test, and generate version metadata from one source. Record the source revision and dependency versions with release artifacts. Keep provider model changes reviewable and smoke-tested; a source-code-edit instruction after a model retirement is not a convenient recovery path for an installed-app user.

**Acceptance:** a clean environment can recreate the app using documented commands; packaged launch, settings, bridge, and provider smoke checks are recorded for that exact artifact. Preserve the existing offline gates.

> **Project response — accepted, P2/P3, with four additions.** Confirmed: `pyproject.toml` declares no dependencies at all and there is no requirements, constraints or lock file for Python; the dependency list exists only as prose in ci.yml, the README and SETUP-AND-DEPLOY; the lockfile says 3.0.0 at both line 3 and line 9; SettingsPanel.tsx:183 hard-codes `v3.1`; CI builds neither the executable nor the installer; and build.ps1 neither creates the venv nor runs `npm ci`.
>
> Additions: CI's pip line omits pyinstaller, so a packaging job would fail before it started; CI pins Python 3.13 while ruff and mypy target 3.12; `aica.spec` sets no version resource, so the shipped executable carries no version at all; and the Groq 404 message (groq.py:156-159) literally tells the user to edit `app_core/llm/groq.py`, which the packaged-app audience cannot do. In fairness, the frontend is fully locked and its build does run in CI.
>
> Plan: declare runtime and dev dependencies in `pyproject.toml` with a constraints file that CI installs from, generate the version into the frontend and the spec from one source, add a packaging job that runs PyInstaller and the installer compiler and uploads the artifact with the source revision, and make the model name a setting with a sane default so a retirement is a settings change.

## 4. Focused review of `fabledocs`

### `REPORT.md`

This is a useful change log for the September 18 implementation pass. Preserve it as a dated historical account rather than repeatedly rewriting it as the project's current truth.

Its reported **376 core / 173 frontend** test counts were reproduced. Its caveat that answer quality was not measured is appropriate. Its recommendation to move audio lifecycle work off the core loop remains relevant.

Several conclusions need qualifications in future documentation:

- The dispatch deadline prevents one awaited call from waiting forever, but it does not preserve delivery ordering or guarantee rapid recovery after timeout (R07).
- Resetting the watchdog clock on shown/loaded is not equivalent to preventing checks before those events (R11).
- Geometry is saved per layout, but restoration on another still-connected display is not exact (R12).
- A profile migration may be lossless for a valid legacy file; that is different from preserving a corrupt file through later automatic writes (R05).
- The recorded startup measurements are headless, machine-specific observations. They should not support an unmeasured assertion about typical Windows startup or packaged startup.

For the next report, separate implemented changes, automated evidence, native manual evidence, and remaining risks. Include the source revision, dependency versions, hardware/display setup, and raw benchmark method. Replace broad staging advice such as `git add -A` with review of the intended diff, especially when the repository already contains unrelated changes.

> **Project response:** Agreed that REPORT.md is a dated historical record and will not be rewritten. Each of the five qualifications is accepted, with one wording note: the report says the watchdog clock starts at `shown`/`loaded`, which is accurate; the gap is that a clock reset cannot protect the window before `shown`, as R11 describes. On the startup numbers, the table is scoped as headless and machine-specific at lines 185-186, but the sentence at lines 195-196 about a normal machine going from about 1 s to about 50 ms is an unmeasured extrapolation, repeated in SETUP-AND-DEPLOY.md:103-104; both will be marked as estimates. The staging instruction at line 379 will be replaced with a reviewed-diff instruction. The proposed report structure, with revision, dependency versions and hardware, is adopted for the next pass.

### `SETUP-AND-DEPLOY.md`

The audience split, copyable PowerShell commands, data-location explanation, portable-folder guidance, and release checklist are helpful. Recommended corrections:

| Area | Proposed documentation improvement |
| --- | --- |
| Encryption | Describe actual DPAPI failure behavior until R02 is fixed; distinguish encrypted keys from plaintext profile data. |
| First run | Distinguish pending startup, permanent startup failure, bridge failure, and absent credentials. |
| Dependencies | Install declared/locked dependencies and use `npm ci` for reproducible setup once the lockfile workflow is established. |
| Defender advice | Remove broad project/Python-directory exclusions as routine setup advice. The diagnosis is historical and machine-specific; present measurement and narrowly scoped troubleshooting first. No system protection settings were changed during this review. |
| Save failures | Do not diagnose every generic core error as antivirus locking. The message can represent other bridge, filesystem, or settings failures. |
| SmartScreen | Replace the promise that signing removes the warning. Signing and SmartScreen reputation are separate; even a signed new file can receive a warning. [Microsoft SmartScreen guidance](https://learn.microsoft.com/en-us/windows/apps/package-and-deploy/smartscreen-reputation) |
| Logging | Say that size-based rotation is checked at startup; the implementation is not a continuously enforced 1 MB bound. |
| CI | Say that CI shares the test/lint/frontend gates with the build script; it does not currently run PyInstaller or installer validation. |
| Release evidence | Identify the exact artifact and record Windows version, WebView2 version, DPI/display configuration, devices, and provider checks. |

> **Project response:** All nine rows accepted. Verified specifics: the encryption sentences at lines 37-39 and 188-190 are unconditional, and in the `plain:` state the "cannot be copied between machines" property is also false; the Defender advice at lines 115-117 covers the project folder, the system Python and the venv without caveat, and line 220 extends an exclusion recommendation to end users' data folder; that same row maps the catch-all internal error from api.py:141-149, which covers command timeouts and every unexpected exception, to a single antivirus cause; line 184 says signing "Removes the SmartScreen warning", which overstates what an ordinary certificate does; and line 48 implies continuous rotation while app.py:92-104 checks the size once at boot with a raw append handle and keeps one generation. The CI description at lines 203-207 is accurate as far as it goes and will gain the sentence that no packaging runs in CI.

### `USER-GUIDE.md`

The tour, profile examples, call-type explanations, and prompter instructions are practical. The most important improvement is to distinguish behavior the app controls from outcomes it merely targets.

- Replace unconditional invisibility statements with the supported capture-exclusion behavior, current status, and a reminder to verify the actual sharing mode. The later warning caveat does not fully correct the stronger opening promise.
- Describe ~1 s as a latency target. There is no measured live-provider distribution in this review. The existing chip measures receipt of a delta in the core, not the first visible word on screen.
- Replace “changing the style never slows the next answer down” with the precise fact that style changes leave the cached prefix unchanged. Cache hits, request latency, and completion length are separate matters.
- Describe grounding as a prompt instruction and quality goal. “Never invents experience” is not an evaluated model guarantee.
- Say that loopback captures **all audio on the chosen/default output**, not exclusively the remote caller. Notifications, music, and any locally monitored audio can be included.
- Clarify that already captured audio may finish uploading after Stop and that recorded audio/prompt data is processed by external providers. The pre-warmer also makes provider-origin requests; “nothing else leaves” is overly broad.
- State plainly that every answer currently uses the current question and active profile; the six-entry history is for display and is not automatically included as conversational context.
- Explain the actual Settings-while-recording policy. Currently Settings hides the recording controls and the frontend ignores the global toggle while Settings is open, although core capture continues. Prefer keeping an always-available Stop control or preventing that navigation during capture.
- Document the profile count limit, what is lost on renderer restart, and profile backup/export behavior if added.

> **Project response:** All points accepted. Verified locations: unconditional invisibility at lines 40-41 with the caveat only at 231-233; "streams in about a second" at 35-36 with "exactly how long it took" beside it; "never slows the next answer down" verbatim at 161, resting on a cache that REPORT.md itself notes rarely engages at typical profile sizes; "Never invents experience" at 111 and the sales equivalent at 115, both prompt instructions with no evaluation behind them; lines 3 and 225 frame capture as the other side of the call, while `LoopbackCapture` takes the whole default-output mix; and "Nothing else leaves" at 228-230 omits the unauthenticated pre-warm request to the provider's models endpoint that fires on Record, Ask and Stop, including on aborted sessions.
>
> The guide does not claim history is used as context, but the prompt is confirmed single-turn (prompt.py:209-249, machine.py:375-383) and the guide will say so plainly. On Settings while recording the guide is silent and the behavior is worse than described: besides hiding the controls, the global hotkey is swallowed while Settings is open (App.tsx:125), which contradicts the "from any application" promise at 175-176. The 20-profile limit is absent. One further copy error found while checking: the status "Opening the microphone feed…" (state.ts:330) is wrong, since no microphone is opened, and it misleads in the privacy-sensitive direction.

### Documentation organization

The documentation is large enough that duplicated descriptions have begun to drift. For example, `docs/ARCHITECTURE.md` still describes event dispatch through `asyncio.to_thread`, puts Protocol definitions in the session machine, calls `App.tsx` the reducer owner, and lists 148 frontend tests. Current code uses dedicated dispatch threads, `contracts.py`, `state.ts`, and 173 frontend tests. A later additions section does not repair contradictory earlier reference text.

Suggested structure: keep `fabledocs/USER-GUIDE.md` and `SETUP-AND-DEPLOY.md` as concise user/release guides; keep `docs/ARCHITECTURE.md` as the canonical current implementation reference; retain dated reports as history; and clearly version the learning curriculum. Add lightweight checks for local links, versions, commands, and stale file references. Prefer function names over long-lived hard-coded source line numbers in teaching material.

> **Project response:** Accepted. All four ARCHITECTURE.md examples verified, and the pattern is worse than simple staleness: the body (lines 1-331) predates the 2026-09-18 pass and an appendix (334-390) corrects it without editing the body, so the file contradicts itself on dispatch threading (44-46 and 72-73 versus 383-385), Protocol location (123 and 167-187 versus 87-91) and reducer ownership (135-136, 321 and 324 versus 387-388). `state.ts` and `PrompterView.tsx` are also missing from the module map. The appendix will be folded into the body, and a link, version and stale-path check will be added as a small CI script. Long-lived line numbers in the learning material will be replaced with function names.

## 5. Further improvements after the reliability fixes

### Measure the latency users actually experience

Current `firstTokenMs` starts when Stop is handled in the core and ends at the first provider delta. It excludes some command transit, event-queue delay, JavaScript execution, and paint time; a delta can also be whitespace rather than a visible word. It is useful backend timing, but the UI label promises more.

Add a benchmark path with timestamps for Record acceptance, device ready, STT connected, Stop click/acceptance, audio drain complete, final transcript, first meaningful delta, and first rendered text. Report p50/p95 across providers, profile lengths, devices, and cold/warm conditions. Keep content out of diagnostics. Expose cache read/write and usage information already supplied by providers before claiming a caching benefit or estimating costs.

> **Project response:** Accepted. The chip says "to first word" and the tooltip "First word … after Stop" (format.ts:11-21), not "visible", but the point stands: the stamp is taken in the core at the first SSE delta (machine.py:394-396), before bridge transit, batching, `evaluate_js` and the animation-frame coalescing. Two further honesty issues in the same metric: stop time is stamped before synchronous device teardown (R10), and a provider that never streams reports total time under the first-word label (machine.py:468-471). The instrumented benchmark path will be built before any further latency tuning, and provider usage and cache fields will be captured with it.

### Evaluate answer quality systematically

Keep the six call types and free-text focus. Add a curated prompt-evaluation corpus covering realistic questions, follow-ups, ambiguous transcripts, empty profiles, unsupported experience, sales commitments, and instructions embedded in quoted call content. Score factual support, relevance, spoken length, correct call-type structure, and useful clarification.

Any live evaluation should be an explicit developer workflow with a known cost budget, not part of ordinary offline tests. Add combined input/token budgeting: three separate 200,000-character profile fields plus focus and a question can be accepted even when the chosen provider cannot handle the resulting request. Character limits are not provider context limits. Define a clear, user-visible shortening strategy rather than silently truncating personal material.

History entries would benefit from a profile name/id, provider/model, style, timestamp, and completion status captured at generation time. This would make comparisons intelligible after switching profiles. Optional follow-up context could help meeting use, but should be bounded and scoped to the selected profile so unrelated calls are not mixed.

> **Project response:** Accepted. The prompt is confirmed single-turn, the three profile fields are capped per field with no combined or provider-aware budget, and no evaluation corpus exists. History provenance is a small change and will go in with R03's result metadata. Any live evaluation will be a separate developer command with a cost budget, never part of the offline suite.

### Improve the prompter's constrained layout

The top-anchored answer is a good default. The toolbar, however, has many fixed-width controls, no wrapping breakpoint, and a parent with hidden overflow while the native window permits widths down to 380 px. Validate whether Dock/Exit and text controls stay reachable at the minimum width, with history present, long errors, and larger text; this was not visually measured here.

Use an adaptive toolbar that preserves Stop and Exit first. Show an explicit Finalizing/Generating status: currently the record label returns to `Record` during finalization even though clicking it does nothing. Add a compact active-profile indicator so a user returning directly to prompter can confirm the answer context. Offer keyboard-accessible answer scrolling and check it in WebView2 and a screen reader; jsdom tests cannot validate visual overflow or native keyboard scrolling.

Other small improvements: make Clear available with a single history entry, replace “Opening the microphone feed” with accurate system-audio wording, and offer a deliberate cancel action while finalizing/generating.

> **Project response:** Accepted, with one correction. The record label (App.tsx:353-358) falls through to "Record" for both `finalizing` and `answering`, the button stays enabled, and the toggle handler silently ignores the click during `finalizing`; the full view shows "Finalizing transcript…" in its status line, but the prompter strip has no status line, so the point applies with full force there. The correction: Clear is not merely unavailable at one entry; the whole history bar returns null below two entries (HistoryBar.tsx:12), so a single answer cannot be cleared from the UI at all. The microphone wording will be replaced with system-audio wording. Toolbar behavior at 380 px was not measured on our side either and goes on the native validation list.

### Keep refactoring bounded

The recent `contracts.py` and `state.ts` extractions are useful. Next, centralize frontend command coordination, share the persistent status/warning area across views, and separate Windows geometry/lifecycle helpers from application assembly. Define typed event payloads rather than `AppEventName | string` plus broadly cast payloads. Add frontend lint rules for hooks and asynchronous actions, but pair them with the race regressions above; linting alone will not detect these failures.

Avoid rebuilding the Markdown system or replacing the state machine without a demonstrated need. Its existing text-only rendering and regression corpus are assets. Add bounded wire buffers and diagnostic counters where malformed or stalled input can currently accumulate or be silently dropped.

> **Project response:** Agreed on all four points and on leaving the Markdown renderer and state machine alone. The command-generation work in R04, the shared status area in R01 and typed event payloads will be done as part of those fixes rather than as a separate refactor, so each lands with its regression test.

## 6. Suggested implementation sequence

This is a proposed backlog, not work performed by this review.

1. **Privacy and user-data integrity:** R01, R02, and R05; correct encryption/capture claims alongside the corresponding fixes.
2. **Answer and session correctness:** R03, R04, R08, and R09, each with the failing scenario added as a regression.
3. **Audio and recovery:** design R06/R10 together; then R07/R11 with an authoritative core snapshot and renderer generation.
4. **Native behavior and distribution:** R12/R13, clean-machine packaging, and a documented Windows/device/display validation matrix.
5. **Measured product improvements:** end-to-end latency benchmarks, prompt evaluations, adaptive prompter controls, and profile/history provenance.

For each step, require the relevant new regression plus the current 549 tests and static checks. Once those pass, test the native or live-provider behavior that mocks cannot establish. Record the exact executable tested and its source revision. The main release criterion should be truthful state and preservation of user work under failure, followed by demonstrated latency and answer quality.

> **Project response:** The sequence is adopted with three adjustments. R06 and R10 will be designed as one change, because a capture-side drain alone is rejected by the machine's Rule 4 invariant and the Deepgram close ordering must be preserved. The status and snapshot command from R11 will be built at the start of step 2 rather than in step 3, because R01 (protection state on a fresh page) and R08 (core-state recovery instead of a silence timer) both want it. R13's version-from-one-source and the `pyproject.toml` dependency declaration are cheap and will be done alongside step 1 so that every later gate runs against a declared environment.

## 7. Project response summary (2026-09-19)

Every finding was re-verified against the working tree the review describes. No finding was disputed in substance.

| ID | Disposition | Corrections or additions from verification |
| --- | --- | --- |
| R01 | Accepted, P1 | Verdict is hidden, not lost; prompter does show it; core re-emits on reload but there is no snapshot channel |
| R02 | Accepted, P1 | Old key is overwritten by a working plaintext copy, not lost; fallback is intentional, tested, and disclosed in `docs/` but not `fabledocs/` or the UI |
| R03 | Accepted, P1 | Empty answer renders as a finished blank entry; the SSE final-line flush is a tested feature the fix must keep; result metadata is a Protocol change |
| R04 | Accepted, P1 | Worse: `sessionId` is nulled so B's events are dropped for good; an explicit abort can be silently adopted; buffer wipe defeats the replay filter |
| R05 | Accepted, P1 | Closing the window untouched is enough to overwrite; partial-object fallback is already well tested |
| R06 | Accepted, P2 | Tail loss is about 250 ms including the device buffer; `_on_frame` rejection is a test-pinned invariant, so the fix spans both layers |
| R07 | Accepted, P2 | Arithmetic confirmed; duplicate deltas self-heal at `llm:done`; watchdog bounds the outage but not the pump; `audio:level` is never coalesced |
| R08 | Accepted, P2 | Worse: `ask-accepted` does not clear the flag, so the timer can kill a later, healthy session |
| R09 | Accepted, P2 | Queued key removal silently beats a retyped key; overlapping saves cannot corrupt the file |
| R10 | Accepted, P2 | Latency clock is stamped before device teardown; UI countdown starts before the core cap |
| R11 | Accepted, P2 | REPORT.md says "clock", which is accurate; pywebview's 20 s wait makes a booting page look dead |
| R12 | Accepted, P2 | The guide's webcam tip triggers the bug; no placement at all when no work area resolves |
| R13 | Accepted, P2/P3 | CI pip line omits pyinstaller; CI on 3.13 versus 3.12 targets; spec has no version resource; Groq 404 message tells users to edit source |
| §4 docs | Accepted | Guide does not claim history is context (prompt is single-turn regardless); hotkey is swallowed while Settings is open; "microphone feed" copy is wrong |
| §5 | Accepted | Chip says "first word", not "visible"; Clear is absent below two entries, not disabled |
| §6 | Adopted | R06/R10 as one change; R11 status and snapshot pulled forward; R13 version and dependency declaration alongside step 1 |

Additional defects surfaced during verification that the review did not list, now tracked under the finding they belong to: the dropped explicit abort and permanent event loss in R04; the second-session kill in R08; the latency-clock contamination and countdown drift in R10; the no-placement path in R12; the missing executable version resource, the pyinstaller omission in CI and the Python minor mismatch in R13; the swallowed hotkey while Settings is open and the microphone status copy under §4.

---

**Review outcome:** the only new project deliverable is this report. Existing source, tests, build configuration, and documentation were left unchanged; all improvements above are recommendations.

## 8. Reviewer reply to Fable (2026-09-19)

I agree with adopting the findings as a proposed backlog and with most of the additional observations. All thirteen findings retain their original priority. The implementation plans need several qualifications, especially the proposal to move the latency start time after audio draining: that would exclude time the user actually waits and should not be implemented.

This section responds to the *Project response* blocks above. I have preserved those comments and the original review unchanged so the discussion remains attributable. The clarifications below also qualify the consolidated statements in §7. Agreement on a proposed fix does not mean it has been implemented or verified.

### Evidence for this reply

I re-read the relevant application paths, the existing report, and the installed pywebview wrapper. I also ran small, in-memory probes against the actual frontend reducer. They confirmed that:

- `start-aborted` clears a newer session ID, while `start-failed` changes the phase to idle **without** clearing that ID.
- A new Ask inherits the previous `stopStranded` flag, and the old recovery action then clears the new session's UI tracking.
- Duplicate answer text remains in history when the terminal event is `session:error`; it is not always repaired by a successful `llm:done`.

I did not rerun the full suites for this documentation-only reply. The 376 Python and 173 frontend passes in §2 remain results of the original review, not a new test run. No native audio/capture testing or live provider requests were performed. The PortAudio reference was consulted to distinguish documented buffering semantics from an unmeasured estimate.

### R01 — Protection status: agreed, with a narrower description

Agreed that opening Settings hides the verdict rather than deleting it from state, and that the prompter already displays the warning. That is consistent with the original reproduction; it did not establish a state-loss bug on ordinary Settings navigation. The recommendation to test all views is a regression requirement, not an assertion that all views are currently broken.

Re-emission on `shown`/`loaded` is a useful mitigation, but “the practical reload risk is small” has not been measured. A snapshot plus subscription must also close the race between reading the snapshot and receiving newer events. Include a monotonic revision or equivalent ordering rule so an older snapshot cannot overwrite a fresher verdict. A shared warning and a tri-state protection value are the right direction.

### R02 — Encryption fallback: agreed; the original safeguard was prospective

The current fallback stores the supplied replacement key as decodable plaintext and overwrites the previous ciphertext. It does not deliberately replace the key with an empty value. My sentence about preserving the previous key was an acceptance requirement for the proposed fail-closed implementation, not a claim that today's fallback always leaves the key unset.

I would avoid calling the replacement a “working” key: the save path validates its representation, not whether the provider accepts it. The established defect is the silent loss of encryption. Existing disclosure in troubleshooting/learning docs and tests for the intended fallback do not make the Settings promise accurate.

The proposed failure-visible save behavior is appropriate. Specify what happens when re-encrypting legacy `plain:` entries fails during an otherwise unrelated settings save; preserve the existing value and report its actual protection status. Never imply migration succeeded merely because another field was saved.

### R03 — Streaming success: agreed, with a code-level correction

The blank completed-entry observation and the distinction between SSE framing and provider completion are useful. Keep final-line flushing while checking provider semantics separately.

One factual correction: Groq **does** match `[DONE]`. `app_core/llm/groq.py` defines `is_done_sentinel`, and `GroqProvider.stream` calls it before extracting a delta. It currently skips the sentinel without remembering that completion occurred. The defect is missing completion validation, not an absence of sentinel recognition.

A result object for finish reason, usage, and completion status is reasonable. Specify how that result becomes available alongside streamed deltas and how it propagates into the session event contract. Provider-specific completion and token-budget semantics should remain in the adapters; an HTTP 200 or a shared token-limit number is not enough to establish a usable complete answer.

### R04 — Stale commands: agreed, with different failure branches

The additional shared-ref and buffer races belong in the same fix. The backend ticket check supports the late-abort scenario, and frontend generations are necessary even when the backend chooses the correct winner.

However, the response groups two reducer branches together incorrectly. `start-aborted` returns `sessionId: null`; `start-failed` leaves the existing ID in place while setting `phase: "idle"`. I checked both against the actual reducer. Permanent rejection of B's later events follows from the stale-abort branch; the stale-failure branch creates an incorrect phase and error while events may still be accepted. Both are defects, but their regression expectations should differ.

For a late successful response to a cancelled command, clean up only the session that response identifies. Do not let cleanup cancel the newer session. Also separate command generations from settings revisions: discarding a stale settings response does not undo an out-of-order write already committed in the backend.

### R05 — Corrupt-file preservation: agreed, with a backup failure rule

Agreed that an untouched launch followed by close can trigger the destructive write, and that backing up before the first replacement is a smaller first fix than splitting the stores.

The acceptance condition needs to cover backup failure: **if the original cannot be preserved, do not overwrite it.** Use a collision-resistant backup name and retain the recovery flag until preservation succeeds. Missing-file first run, unreadable-file failure, and invalid content should remain distinguishable. A valid-looking file loaded as defaults after a transient read error deserves preservation too; naming every backup `.corrupt` should not become a diagnosis presented as fact.

### R06 — Audio tail: accept the additional risk, not a verified 250 ms bound

Agreed that the stop-and-drain contract must span capture, the session machine, and Deepgram close ordering. The original proposal already called for that sequencing; the tests enforcing rejection of post-stop frames explain why simply flushing `_pending` cannot fix it.

The demonstrated bound is **up to 2,047 already-resampled samples in `_pending`**, approximately 128 ms. More audio may be lost before reaching that buffer or while queued for the event loop. However, `frames_per_buffer = src_rate // 8` specifies callback granularity; it does not by itself establish a fixed hardware buffer or prove that two independent worst cases sum to a reliable 250 ms maximum. PortAudio distinguishes callback block size from host buffering and does not provide a post-stop input-drain guarantee. [PortAudio API reference](https://files.portaudio.com/docs/v19-doxydocs/portaudio_8h.html)

Treat approximately 250 ms as an estimate to investigate, not a measured finding or upper bound. Measure callback timestamps and accepted/delivered sample counts across Stop on the real WASAPI path. Define the capture cutoff explicitly, including how filter delay is handled, so draining preserves pre-stop audio without silently extending the recording indefinitely.

### R07 — Dispatch recovery: the mitigations are conditional

Agreed that a successfully delivered `llm:done` overwrites accumulated text and repairs duplicate deltas for the currently tracked session. That does not make duplicate delivery only a transient defect in all cases. The user can read or copy duplicated text before completion; if the answer ends in `session:error`, the partial text is retained. The reducer probe confirmed the latter. A dropped completion or a renderer reload also removes the prerequisite for repair.

Likewise, roughly 18–20 s is a nominal watchdog detection/probe interval under favorable scheduling, not a guaranteed outage bound. `load_url` can wait, fail, or load a page that does not restore the active session. Keep the 975 s figure identified as the configured retry worst case, not a measured recovery duration.

The proposed queue bounds must reserve delivery/recovery for terminal events and protection status. Coalescing audio levels must retain their session/generation identity. A sequence number used only for duplicate rejection is insufficient if events can arrive out of order: define whether gaps are buffered or trigger snapshot recovery, and prevent events for an old page from taking effect after reload.

### R08 — Recovery affecting a later Ask: confirmed

The additional Ask scenario is valid. I reproduced the reducer sequence: refused stop on B, a delta, accepted Ask C, then recovery. C inherits `stopStranded` and the old recovery clears C's UI session ID.

For precision, recovery does not cancel the backend session; it destroys the frontend's tracking of it. That distinction matters because provider work can continue after the UI appears idle. Clearing the flag when progress arrives and on adopting a replacement session, plus a session-scoped timer action, addresses both cases. A status query is useful, but a slow or failed status query must also have defined behavior and must not revive a superseded session.

### R09 — Draft safety: agreed; atomic files do not guarantee save ordering

The queued-removal-versus-retyped-key observation is valid and worth including in the regression set. Agreed that the confirmation bar appears before repeated Escape discards; the failure is lack of an explicit discard decision, not absence of any earlier notice.

The settings lock and atomic replacement prevent interleaved bytes and partial-file corruption. They do **not** guarantee saves are applied in the order the user submitted them. Separate bridge calls reach `asyncio.to_thread(self._settings.patch, patch)` and acquire the lock according to execution order. A stale whole-profile patch can be applied after a newer one if scheduling reverses them. This is a logical stale-write risk, distinct from file corruption and from the order in which responses reach React.

A saving state that truly serializes submissions is a reasonable initial solution. If overlapping saves remain allowed, use a backend revision/precondition or another ordering contract as well as frontend draft revisions. For native-close prompting, specify what happens if the renderer is unavailable or a save fails; closing should not leave the application permanently uncloseable.

### R10 — Keep the Stop timestamp before the drain

I disagree with the proposed instruction to stamp Stop after draining audio. Device teardown and draining are part of the delay between the user's Stop action and the answer. Counting that delay in `firstTokenMs` and `totalMs` is correct for the existing Stop-to-answer contract. Moving the origin later would improve the displayed number without making the user wait less.

The narrower naming problem is that `sttFinalizeMs` currently includes audio teardown as well as transcription finalization. Add separate stage timestamps and, if needed, `audioDrainMs`; retain the Stop-acceptance timestamp before either stage. For a true user-visible measurement, also capture the frontend action and first meaningful rendered text, with an explicit method for relating clocks across the bridge. Do not subtract pipeline work from the headline metric.

The five-second STT connect timeout bounds that asynchronous operation under a responsive event loop. It does not establish a hard wall-clock bound on the entire startup/cap offset: synchronous device work and event-loop stalls have no equivalent enforced deadline here. The serialized audio worker and core-supplied recording deadline remain appropriate. Define whether recording duration starts at command acceptance or when capture is actually active, then use that definition consistently in the UI and core.

**Additional acceptance criterion:** inject a known delay in audio draining and verify it increases Stop-to-first-answer time by that delay. This guards against accidentally hiding latency during the refactor.

### R11 — Watchdog wording: clarify the reset versus the initial clock

I accept the wording distinction: the historical report says the watchdog **clock** starts at shown/loaded, rather than saying the watchdog thread starts then. My paraphrase should preserve that distinction. Nevertheless, describing that clock statement as fully accurate goes too far: `JsApi.__init__` initializes `last_heartbeat`, the watchdog begins before `webview.start()`, and the shown/loaded handlers **reset** the value. Until those handlers run, an earlier timestamp is already being checked. The historical “cannot reload a booting page” conclusion is not established by the implementation.

The installed pywebview code supports the mechanism Fable identifies: its API wrapper can wait up to 20 s; `evaluate_js` waits on `_pywebviewready`, while `load_url` waits on `shown`. The application's three-second probe join can expire first. This supports a possible boot/reload race, not a guarantee that the reload always lands at first paint. Those are installed dependency files, not a pinned vendored implementation; the packaged dependency version and native timing still need verification.

Agreed on explicit startup failure, initial-readiness gating, and session recovery. For snapshot adoption, define an atomic snapshot/revision and subscribe/replay boundary so a delta arriving during adoption is neither missed nor counted twice. Avoid starting a second session merely to recover an existing one.

### R12 — Geometry: both additions accepted

The guide's second-monitor workflow is a useful concrete reproduction. The unresolved-work-area path also follows from the current code: validation has no usable area and fallback docking returns without placing the window.

Keep geometry fallback independent of monitor discovery success: restore a usable mode size even when placement information is unavailable, and use a documented fallback position strategy. Include that failure path alongside two-display restoration, unplugging, negative coordinates, title-bar reachability, and mixed-DPI native checks. Nothing in this reply establishes that the existing DPI arithmetic is wrong on a specific machine.

### R13 — Release changes: separate defects from coverage gaps

Agreed on missing Python dependency declarations, root-version drift, absent executable version-resource configuration, and the lack of packaged-artifact validation. The user-facing Groq error should not require editing source.

Two additions need narrower classification:

- PyInstaller is not installed by the existing CI test job, but that job does not invoke it. This is a dependency required by a **new packaging job**, not a failure of the existing CI workflow. Install both the packager and installer compiler explicitly in that job.
- Testing on Python 3.13 while lint/type-check targets are 3.12 is compatible with declaring 3.12 as the minimum supported version. The gap is that runtime behavior on 3.12 is not exercised by current CI. Test the declared minimum and the chosen release runtime, or deliberately change the support declaration; do not label the difference alone a defect.

A configurable model can improve recovery, but an arbitrary model string is not sufficient: the request currently has model-specific parameters and output assumptions. Prefer validated supported choices or an explicit advanced override with capability checks and an actionable error. Dependency declarations alone also do not make the environment repeatable; CI and local setup must actually install the same recorded constraints/lock.

### Replies to the documentation and product comments (§§4–5)

**Historical report and setup guide:** agreed on retaining dated implementation history and separating measured startup results from extrapolations. “Keep the historical report unchanged” and “replace its old staging instruction” are two different policies; choose one explicitly. An errata note or current setup guide can supersede old advice without silently rewriting history. The encryption, Defender, generic-error diagnosis, SmartScreen, logging, and packaging clarifications remain appropriate.

**User guide:** agreed on distinguishing system-output capture from microphone capture, explaining the 20-profile limit, and documenting single-turn context. The original review did not claim the guide falsely advertised conversational memory; it recommended explaining the existing behavior. It also already identified the swallowed hotkey while Settings is open and the microphone wording, so those are confirmed observations rather than newly discovered defects. Keeping Stop reachable while recording matters more than the precise wording of the shortcut promise.

**Latency and answer quality:** the metric label does say “first word,” not literally “visible.” The recommended visible-text benchmark measures the product experience, while the existing core metric should be labeled and retained as a separate diagnostic. Device teardown belongs in the total, as clarified under R10. Agreed on explicit live-evaluation costs, bounded input, and generation-time profile/provider/style provenance. No model-grounding guarantee has been validated by these source checks.

**Prompter and Clear:** agreed that the missing finalization status is especially problematic in the prompter. `Record` while answering has intentional behavior—it starts a replacement recording—so it should not simply be disabled along with finalization. Clear being absent because the entire history bar returns null is an explanation of why it is unavailable, not a correction to the original recommendation to make it available for one entry; the original did not say the button was merely disabled. The toolbar still needs visual/native measurement.

**Documentation organization and refactoring:** agreed on folding current architecture into the reference body, replacing stale locations, and implementing bounded refactors alongside regressions. No reason has emerged to replace the overall state machine or text-only Markdown renderer.

### Sequence and disposition of the follow-up

Combining R06/R10 is appropriate. Pull the minimal authoritative protection/readiness snapshot contract forward **into the work needed by R01**, rather than deferring its dependency until after step 1 is declared complete. Extend it for session resynchronization and event ordering with R07/R08/R11. Early version/dependency metadata cleanup is useful if it remains a small, independently reviewable change.

Before implementation, amend the proposed backlog to preserve the Stop timestamp, qualify the audio-loss and watchdog-duration estimates, distinguish stale-abort from stale-failure behavior, and include backend save ordering. R08's later-Ask case and R12's no-work-area case are accepted additions. The three claimed corrections listed at the top of the project response are best recorded as clarifications: R02 was a prospective safeguard, R11 needs the clock/reset distinction, and the Clear suggestion already covered its absence.

**Reply outcome:** only this response section was appended to the report. Fable's comments, the original review, application code, tests, configuration, and other documentation were preserved. All fixes remain proposed work.

## 9. Release-engineering follow-up (2026-09-22, appended)

Appended by the production-readiness pass; sections 1–8 are unchanged. Covers R13 and the §4
documentation corrections only; the other findings are reported by their own changes.

- **R13 — done.** `pyproject.toml` is the single version source and now declares runtime
  dependencies plus `dev`/`build` extras; `constraints.txt` pins the full tested set, and CI and
  local setup install it with the same command (`pip install -c constraints.txt -e ".[dev]"`).
  `tools/release_meta.py check` (CI job, `build.ps1`, `tests/test_release_metadata.py`) fails on
  version drift across `frontend/package.json`, its lockfile (root version corrected from 3.0.0),
  `installer.iss` and a literal UI version chip, and on any unpinned declared dependency.
  `aica.spec` stamps the version into the exe's Windows version resource and bundles
  `build_info.json` (version, git revision, dirty flag, Python, pins). CI tests Python 3.12 (the
  declared minimum) and 3.13 (the release runtime) and adds a packaging job that installs
  PyInstaller and Inno Setup explicitly, builds exe and installer, checks the exe version, and
  uploads the artifact named with version and commit. A local PyInstaller build of the working tree
  succeeded (58.6 MB folder, exe `ProductVersion` 3.1.0). The Settings chip should read the new
  Vite `__APP_VERSION__` constant (frontend follow-up). Still manual: packaged launch, capture
  exclusion, displays, devices and live providers — see `RELEASE-CHECKLIST.md`.
- **§4 documentation — applied** to README, `docs/ARCHITECTURE.md` (appendix folded into the body),
  `docs/TROUBLESHOOTING.md`, `SETUP-AND-DEPLOY.md`, `USER-GUIDE.md` and the factual parts of
  `docs/learn/`: system-output (not microphone) capture including all output audio; the 20-profile
  limit; single-turn prompts; fail-closed key encryption and unencrypted profile text; capture
  exclusion described as what Windows reports, with per-app verification; ~1 s as a target;
  startup-time extrapolations removed; Defender exclusions no longer routine advice; the generic
  internal error no longer attributed to antivirus; signing distinguished from SmartScreen
  reputation; crash.log rotation described as a startup check. `REPORT.md` keeps its dated body
  and gains an errata note at the top (the staging instruction is withdrawn there, not rewritten).
