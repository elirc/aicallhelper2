# 05 — Fire drills: practice the diagnosis, not just the fix

Fifteen incident scenarios, written the way they'd reach you: a vague user
report. Drills 9–15 are real — each one describes a bug that actually
shipped in this repo and was found by an audit, the mutation pass, or
fresh eyes. For each one, BEFORE opening the answer: write down (1) your
first three questions, (2) which file you'd open first, (3) what you'd
expect to find. The skill being trained is forming a hypothesis path — the
fix is usually the easy part.

---

## Drill 1: "It says 'lost connection' every time I stop, but the answer still shows up"

**Think first.** When exactly does the message appear? Is the transcript
complete? Does it happen on cancel too?

<details><summary>Diagnosis path</summary>

"Error on STOP but the pipeline still works" points at the finalize
window: something is touching the socket between CloseStream and the
server's close. Prime suspect list in order: (1) keepalive firing during
the CLOSING state — check `_keepalive` respects `close_requested`
immediately before send; (2) audio frames still being sent after stop —
check rule 4 in `_on_frame`; (3) the reader misclassifying the normal
post-CloseStream close as abnormal. Repro: make keepalive interval tiny in
a test, hold the server's close with a slow handler, stop. The shipped
tests covering this: `test_keepalive_sent_while_open_and_stopped_after_close_request`,
`test_frames_after_stop_requested_are_dropped`.
</details>

## Drill 2: "The first couple words of every question are missing"

**Think first.** Where can audio be lost — device, downsampler, buffer,
socket? What starts in parallel with what?

<details><summary>Diagnosis path</summary>

The Record press starts capture and the WebSocket connect IN PARALLEL;
frames captured before the socket opens must buffer and flush on open. If
the head of the question is missing: (1) is the pre-open buffer present
and flushing in order (`_preopen` deque → queue on connect)? (2) is its
cap big enough (a slow connect + tiny buffer drops the head — ours is
~15 s / 120 frames)? (3) is capture actually started before the connect
await, not after it? Test that pins this:
`test_preopen_frames_buffered_and_flushed_in_order`. If the buffer's fine,
check the accumulation path: capture.py's `_pending` frame accumulator and
the resampler's carried tail (`_tail`/`_pos` in downsample.py) — dropping
a partial frame at chunk boundaries loses up to ~128 ms per chunk, which
presents identically.
</details>

## Drill 3: "Sometimes the answer shows up twice, glued together"

**Think first.** What can run the answer request twice? What's the ONLY
state in which a retry is legal?

<details><summary>Diagnosis path</summary>

Concatenated double answer = a second attempt after deltas painted. Two
candidate sources: (1) the retry helper retrying after a mid-stream drop —
check the `got_delta` guard in `retry.py`; (2) two sessions both painting
into the panel — check the frontend drops deltas whose sessionId isn't
the tracked one, and that supersession sets `aborted` before the new
session emits. The retry matrix test `test_never_after_a_delta` and the
machine test `test_ask_over_streaming_answer_supersedes` are the fences.
(Note the audit refinement: a pre-response READ timeout is its own
non-retryable "timeout" kind now — classifying it "connect" was a third
way to double an answer.)
</details>

## Drill 4: "It's slow now — like 3 seconds before the answer starts"

**Think first.** Which stages make up stop-to-first-word? How do you see
their split? What changed recently?

<details><summary>Diagnosis path</summary>

Hover the latency chip: it splits `sttFinalizeMs` vs first-word.
- **sttFinalizeMs high (~5000)**: the finalize is burning its full cap —
  the server isn't closing after CloseStream (network appliance? Deepgram
  incident?) or the close event isn't being seen.
- **First word high, finalize fine**: the TLS handshake is back inside the
  stop window — is pre-warming firing? Check the warmer throttle, that the
  SHARED httpx client is used (a per-request client has an empty pool!),
  keepalive_expiry ≥ the gap between warm and request, and that
  `origin` matches the request URL's origin byte-for-byte.
- **Also check**: did someone put a timestamp in the prompt (cache misses
  → slower time-to-first-token), or flip the provider to a model with
  reasoning enabled?
</details>

## Drill 5: "After my machine slept, Record does nothing"

**Think first.** What state could a sleep/wake leave the machine in? What
does "does nothing" mean — no UI change, or an error?

<details><summary>Diagnosis path</summary>

"Record does nothing" smells like the active-session slot is occupied by a
ghost: a session that died without `_release`. Audit every exit path for
slot release (rule 11) — the shipped design funnels all of them through
`_release` with a `released` guard, and `_supersede` means even a ghost
gets evicted by the next start… so next suspects: (2) the js_api worker
blocked on a coroutine that never resolves (check the 30 s command
timeout returns an error Result), (3) the audio device list changed across
sleep — `LoopbackCapture.start` raising on a vanished device should
surface as a start error, not silence. Check `crash.log` (asyncio
exception handler writes there) before touching code.
</details>

## Drill 6: "I typed my API key again but it still says the key is wrong"

**Think first.** What's the write path for a key? What could make a saved
key unreadable? What does the UI actually know about keys?

<details><summary>Diagnosis path</summary>

Trace write → store → read: (1) Did the patch actually carry the key? The
UI sends only typed-into fields — placeholder "saved — type to replace"
plus an untouched field means NO change (by design). (2) Was it stored
`enc:` and can it decode? Foreign-machine files and DPAPI hiccups read as
unset — `hasXKey` false in the view tells you decode failed. (3) Is the
401 actually about THE key — check which provider is selected; the Groq
field labeled "only for the Groq preset" being filled doesn't help an
Anthropic 401. The secrets tests
(`test_decodes_by_stored_prefix_not_keystore_availability` etc.) enumerate
the failure modes.
</details>

## Drill 7: "The window came back on a monitor I don't own anymore"

**Think first.** Where does geometry persist? What validates it on the
way back in?

<details><summary>Diagnosis path</summary>

Restore goes through `sanitize_bounds`: position survives only if ≥40 px
of the CLAMPED window overlaps some display's work area on BOTH axes.
If the window restored off-screen: (1) is the sanitizer actually being
called with the CURRENT `EnumDisplayMonitors` output (not cached)? (2) are
the stored bounds corrupt in a way that passes (`test_bounds` has the
gallery: bools, NaN, floats)? (3) is `windowBounds` being written by the
debounced save AND the close flush? The unplug-scenario test is
`test_unplugged_monitor_recenters`.
</details>

## Drill 8: "My answer had weird symbols in the middle — like Ã© instead of é"

**Think first.** Where do bytes become text in the answer path? What's
special about streaming?

<details><summary>Diagnosis path</summary>

Mojibake mid-answer = a multi-byte UTF-8 character split across two
network chunks and decoded separately (é = 0xC3 0xA9 → "Ã©"). The SSE
parser must decode with ONE incremental decoder across chunks — check
`SSEParser.__init__` and that nothing upstream (a proxy layer, a naive
`chunk.decode()`) got introduced. The byte-at-a-time test
(`test_byte_at_a_time`) and the providers' hostile-chunking conformance
test are the regression net. If it only happens with one provider, its
`stream` bypassed `wire.py`.
</details>

## Drill 9: "The app runs, the timer counts, but nothing EVER shows up — no transcript, no answer, not even an error"

**Think first.** Commands clearly work (the timer started). What travels a
different path than commands? How would you prove which side is dead?

<details><summary>Diagnosis path</summary>

This is the two-channel split. Commands travel js_api (button → promise →
core); everything the user SEES travels the event channel (core → sink →
pump → `evaluate_js` → `app:event`). "Commands work, nothing paints, not
even errors" means the event channel is dead end to end — and an error
would have told you otherwise, so its absence is the loudest clue. Check
in order: (1) is the sink attached (`_wire_window` must call
`sink.attach(window)` — the pump parks on `while self._window is None`
without it); (2) is the pump task started (`sink.start` on the loop);
(3) set `AICA_DEBUG=1`, open devtools, and listen for `app:event` by hand.
This exact incident shipped for four commits with three green suites —
every suite mocked one side of the seam. Fence: `tests/test_app_wiring.py`
(`test_an_emitted_event_actually_reaches_the_window`). Debrief question:
what OTHER seam in your system does every suite mock from both sides?
</details>

## Drill 10: "I let it record to the 2-minute limit, it said it was answering, then it just went back to Ready. No answer."

**Think first.** What happens at the cap? What did the user's hands do
near the boundary? Which side refused what?

<details><summary>Diagnosis path</summary>

At the cap the CORE auto-stops: `session:autostopped`, then finalize and
answer as normal. So ask: did the user ALSO press Stop right around then?
A stop pressed after the cap fired is correctly refused (rule 3 — the
session is already finalizing). The old frontend treated ANY refused stop
as "the session is gone" and called `cancel_session` — superseding the
mid-finalize session and dropping its `llm:done`. Two minutes of question,
destroyed by the recovery path. The shipped fix: a refused stop keeps
tracking (`stopStranded`) and falls back to idle only after a bounded
20 s wait, so a live answer always gets to land. Fence: "a stop that
races the 120s cap" in `app-coverage.test.tsx`, both branches. Debrief:
the core and the frontend were each locally correct — the CONTRACT
("refused stop" ≠ "dead session") was the bug.
</details>

## Drill 11: "Transcripts are fine when it's quiet, but they get garbled whenever music plays under the speech"

**Think first.** What does music have that speech doesn't? What happens to
frequencies above 8 kHz when you produce a 16 kHz stream?

<details><summary>Diagnosis path</summary>

Music (and notification chimes) carry real energy above 8 kHz; speech
mostly doesn't. A 16 kHz stream cannot represent that content — it must
be FILTERED OUT before decimation, or it aliases: folds back into the
speech band as inharmonic garbage layered over the words (a 10 kHz tone
lands at ~6 kHz). Check: (1) is capture threading ONE `StreamingResampler`
through `downsample_chunk` (the `resampler=` parameter — omitting it
resamples each chunk standalone with no carried state); (2) run
`test_content_above_the_target_nyquist_is_filtered_not_folded` — the
shipped filter buys ~59 dB of suppression; (3) reproduce deterministically
by playing a pure high tone during a recording. The pre-fix resampler had
NO low-pass — this incident is exactly what it sounded like.
</details>

## Drill 12: "The window never remembers where I put it — it's back in the middle every morning"

**Think first.** When is geometry saved? What does the user do at the end
of the day? What does a minimized window look like to Win32?

<details><summary>Diagnosis path</summary>

Geometry saves on move/resize (debounced 0.5 s) and flushes on close. Ask
the workflow question: does the user MINIMIZE the app and then shut down?
Windows parks minimized windows at (-32000, -32000) with a titlebar-sized
rect — and the debounced save used to persist exactly that, so the close
flush wrote garbage and `sanitize_bounds` dropped the position on the next
launch (correctly!), recentering the window. The fix sits at the SAVE
side: `plausible_bounds` in app.py rejects the sentinel coordinates and
sub-minimum sizes, so the last real position survives a minimize-then-
quit. Fence: `TestMinimizedGeometry`. Debrief: the restore-side sanitizer
was right to reject the garbage — the bug was writing garbage at all.
</details>

## Drill 13: "It says my shortcut is 'already taken by another app', but I closed everything and it still says taken"

**Think first.** How many different ways can a hotkey fail to register?
Are they distinguishable? What did the user actually type?

<details><summary>Diagnosis path</summary>

"Taken by another app" is only ONE of the failure modes, and the old code
collapsed all of them into it. Look at what's stored: if the accelerator
doesn't PARSE (`parse_accelerator` returns None — a typo, an exotic
character, "f25"), no RegisterHotKey call ever happens and no amount of
closing apps will help. The manager now reports four statuses —
`registered` / `disabled` / `invalid` / `unavailable` — and the UI shows
"isn't a shortcut Windows understands" for `invalid`, reserving the
"taken" copy for a real OS refusal. Check `hotkeyStatus` in the settings
view (it rides on every settings Result). Fences:
`TestRegistrationStatus` (including a REAL RegisterHotKey conflict) and
the hotkey-status messaging tests in `app-signals.test.tsx`. Debrief: an
error message that names the wrong cause is worse than no message — it
sends the user on a hunt that cannot succeed.
</details>

## Drill 14: "The answer used to have bullets and headings — now it's one giant paragraph"

**Think first.** The model almost certainly still emits markdown. What
could make every block construct stop matching? What changed — model,
provider, or parser input?

<details><summary>Diagnosis path</summary>

EVERY construct degrading at once is the tell: not a heading bug plus a
list bug, but something upstream of all of them. The block regexes are
`$`-anchored and neither `.` nor `[ \t]` matches `\r` — so if the source
reaches the parser with CRLF line endings, headings, rules, both list
kinds, and opening fences ALL fail to match and everything falls through
to the paragraph path. Check what the provider actually sent: press Copy
(it copies the raw markdown source) and inspect the line endings. The fix
is normalization at parse entry (`source.replace(/\r\n?/g, "\n")` in
`parseBlocks`) — one place, not per-regex patches. Fence: the CRLF
normalization tests in `markdown-hardening.test.tsx`, and the streaming
corpus includes CRLF documents so a cut BETWEEN `\r` and `\n` is
exercised too.
</details>

## Drill 15: "Someone on my call said they could see the assistant when I shared my screen"

**Think first.** This is the moat feature failing. What can make
`SetWindowDisplayAffinity` not stick? Did the app KNOW it wasn't
protected?

<details><summary>Diagnosis path</summary>

Two different incidents hide in this report. (1) The OS refused or
silently dropped the affinity — HWND not ready at `shown`, policy, a
Windows build older than 2004 (no `WDA_EXCLUDEFROMCAPTURE`). The app must
apply, READ BACK, retry (5 attempts, growing backoff), and show the
standing `role="alert"` warning if the OS never confirms — if the user saw
no warning AND was visible, the verdict path is the bug: check the
read-back in `apply_content_protection` and the attempt-stamping (a slow
loser overwriting a fresher verdict was a real bug). (2) The renderer
reloaded (heartbeat watchdog) and the page lost its state — the verdict
is re-emitted after every `loaded`, never only on transition; verify both
`shown` and `loaded` are subscribed in `_wire_window`. Fences:
`tests/test_protection.py`, `TestContentProtectionVerdict`, and the
content-protection warning tests in `app-signals.test.tsx`. Debrief: a
security feature you don't verify is a belief, not a feature.
</details>

---

## Debrief habit

After each drill, say the three-sentence incident summary out loud:
*symptom → root cause → fence that prevents regression*. Naming the FENCE
(the specific test) is the senior-engineer move — a fix without a fence is
a bug on layaway.
