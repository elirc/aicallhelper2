# 05 — Fire drills: practice the diagnosis, not just the fix

Eight incident scenarios, written the way they'd reach you: a vague user
report. For each one, BEFORE opening the answer: write down (1) your first
three questions, (2) which file you'd open first, (3) what you'd expect to
find. The skill being trained is forming a hypothesis path — the fix is
usually the easy part.

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
check the downsampler's accumulation — dropping the partial frame at
chunk boundaries loses ~100 ms per chunk, which presents identically.
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

---

## Debrief habit

After each drill, say the three-sentence incident summary out loud:
*symptom → root cause → fence that prevents regression*. Naming the FENCE
(the specific test) is the senior-engineer move — a fix without a fence is
a bug on layaway.
