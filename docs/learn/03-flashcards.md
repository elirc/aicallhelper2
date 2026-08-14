# 03 — Flashcards (84 cards)

Cover the answer, say yours OUT LOUD, then compare. Wrong or fuzzy → the
card goes back in the deck. Do 10–15 a day for a few days rather than all
84 once; recall spacing is the point. Cards are grouped by topic so you can
drill your weak areas. Every answer is checkable against the code — when a
card quotes a number, the number came from the repo.

## Concurrency & asyncio

**Q1. Why does the whole core run on ONE asyncio loop on a dedicated
thread?**
A: So all session state has a single owner. Foreign threads (audio, UI
bridge, hotkey) hand off via `call_soon_threadsafe` /
`run_coroutine_threadsafe`; races become callback-ordering questions that
are testable without threads.

**Q2. What are the only two legal doorways from a foreign thread into the
loop?**
A: `loop.call_soon_threadsafe(fn, ...)` for sync callbacks and
`asyncio.run_coroutine_threadsafe(coro, loop)` for coroutines.

**Q3. Why must `self._active = session` be assigned BEFORE awaiting the
STT connect?**
A: Latest-start-wins. The claim must be synchronous so a second Record
press supersedes the first even while it's still connecting; the loser
discovers `self._active is not session` after its await and tears down
silently.

**Q4. Why is `asyncio.CancelledError` never treated as an error in the
machine?**
A: Cancellation IS the abort path (supersede/cancel/timeout). It derives
from `BaseException`, so `except Exception` blocks can't swallow it by
accident; handlers must let it propagate.

**Q5. Why does `_answer` use `asyncio.wait({stream_task})` instead of
`await stream_task`?**
A: Awaiting a task that gets cancelled raises CancelledError into the
awaiter — indistinguishable from the awaiter itself being cancelled.
`asyncio.wait` doesn't propagate the child's outcome, so the code can
inspect `cancelled()` / `exception()` / `result()` explicitly.

**Q6. Why do file writes and DPAPI calls go through `asyncio.to_thread`?**
A: They block. A blocked loop freezes audio routing, STT reading, and LLM
streaming simultaneously — the entire product stalls.

**Q7. What does the first-token watchdog do BEFORE cancelling the stream
task, and why?**
A: Sets `session.errored = True`. Deltas racing in after the timeout must
be suppressed — nothing may paint after an error.

**Q8. Slot release must happen "exactly once". How is that enforced?**
A: A `released` flag on the session checked in `_release`; every exit path
(done, error, abort, timeout) funnels through it, so a double release
can't make a new session think it superseded a ghost.

## The command ticket & supersession

**Q9. What race does the command ticket close that the active-slot claim
alone did not?**
A: Rule 2 across the WHOLE command, not just the connect. The key read
hits DPAPI (via `asyncio.to_thread`) and can stall; an earlier Record
press resuming late used to supersede an Ask the user typed AFTERWARDS,
killing it silently. The counter is claimed synchronously at command
entry, before any await; `_require_ticket` re-checks it after the awaits.

**Q10. A command loses the ticket race. What does it report, and what
does the user see?**
A: It raises `AppError("aborted", ...)`. The UI treats `aborted` as
always-silent — the user's newer action is in charge, and showing an
error would blame them for their own click.

**Q11. In `ask()`, the ticket is claimed AFTER input validation. Why that
order?**
A: Rule 8 — garbage input (empty, >8000 chars) must fail the command
WITHOUT touching the live session. Validation is synchronous, so it can
run first; the claim still precedes every await.

**Q12. Why does `_supersede` schedule `_quiet_abort(stream)` instead of
letting the socket close report itself?**
A: Aborting CAUSES the socket to die. The stream's abort path sets its
aborted flag first so `_report_error` suppresses that death — otherwise
every supersede would flash a scary "lost connection" over a healthy new
session.

## The wire: Deepgram

**Q13. How does the client authenticate the Deepgram WebSocket?**
A: Via the WebSocket subprotocol list: `["token", <api key>]`.

**Q14. Why send KeepAlive every 8 seconds?**
A: Deepgram kills sockets ~10 s after the last audio, and silence during a
call is normal. 8 s stays under the deadline.

**Q15. Why must the keepalive stop the INSTANT a close is requested?**
A: A KeepAlive after CloseStream can error on the CLOSING socket and
fabricate a "lost connection" error during a stop that is succeeding.

**Q16. What does CloseStream buy at finalize time?**
A: The server flushes its held-back tail (including smart_format entity
hold-back) before closing — the last words of the question arrive.

**Q17. How does Deepgram reject a bad API key?**
A: By CLOSING the socket (code 1008, `DATA-xxxx` reason), usually with no
error frame. Close-before-any-Results is classified as a connect failure.

**Q18. Why buffer audio frames before the socket opens, and what are the
buffer's rules?**
A: Capture starts in parallel with connect; without buffering, the first
words are clipped. In-order, capped ~15 s (120 frames), oldest dropped.

**Q19. `is_final` arrives as `1`. Committed or interim, and why?**
A: Interim. Only literal `True` commits; truthy imposters wrongly promoted
would silently corrupt the committed transcript prefix.

**Q20. Why is finalize idempotent?**
A: Stop-flows can race (user stop vs auto-cap vs teardown); the second
caller must join the first (one CloseStream, one result), not restart the
close dance.

**Q21. Why does CloseStream travel the audio queue as a sentinel instead
of being written directly on the socket?**
A: Under send backpressure a direct write overtakes queued frames, and
Deepgram discards audio that arrives after CloseStream — the tail of the
question silently vanishes. The sentinel drains behind every queued frame;
the sender writes it and exits.

**Q22. Why does the reader set its closed-event only AFTER classifying
the close?**
A: `finalize()` wakes on that event and cancels the remaining tasks. Set
earlier, the cancellation races the reader's own error reporting — a
bad-key close landing during finalize could be cancelled mid-report and
never surface.

## The wire: LLM providers

**Q23. Recite the retry policy.**
A: Exactly once; only connection-level failure before any delta; never on
HTTP status; never after a delta reached the UI; never after abort;
byte-identical request (same immutable object).

**Q24. Why never retry after the first delta?**
A: The UI appends deltas as they arrive — a second attempt would
concatenate two answers on screen.

**Q25. What distinguishes "connect" from "stream_drop" in wire.py, and why
does it matter?**
A: Whether the HTTP response had started. Connect = the request never left
us (safe to retry); stream_drop = bytes may have painted (never retry).

**Q26. What is `data: [DONE]` and what must you NOT do with it?**
A: OpenAI-style end sentinel. Skip it; do NOT treat it as a terminator —
bytes after it in the same chunk still count.

**Q27. Why parse SSE from bytes with an incremental UTF-8 decoder?**
A: Chunks split anywhere, including mid-character. Decoding chunk-by-chunk
corrupts multi-byte characters (€, 日本語) that straddle a boundary.

**Q28. A stream ends with `data: {"…last words"}` and no newline. What
must happen?**
A: The parser's flush emits that final un-terminated data line — otherwise
the answer's last words are silently lost.

**Q29. Why does Groq's request send `reasoning_effort: "low"` and
`include_reasoning: false`?**
A: gpt-oss is a reasoning model; reasoning tokens are pure delay before
the first spoken word. (And `reasoning_format` is a Qwen-family knob — do
not send it.)

**Q30. Groq returns 404. What's the likely cause and the fix?**
A: Groq retires models on short notice — update the pinned `MODEL`
constant in `app_core/llm/groq.py`. The error message says exactly this.

**Q31. What does the pre-warm actually do, and why read the body?**
A: Fire-and-forget `GET <origin>/v1/models` (3 s timeout, throttled 2 s
per origin) through the SHARED httpx client; reading the body returns the
connection to the pool so the answer request finds a live TLS connection.

**Q32. Name the five `ProviderFailure` kinds and the single retryable
one.**
A: `connect` · `timeout` · `status` · `stream_drop` · `empty_body`. Only
`connect` retries — it means the request never left us (httpx
ConnectError / ConnectTimeout / PoolTimeout before the response started).

**Q33. Why is a read timeout waiting for response headers its own kind
("timeout") instead of "connect"?**
A: The body was already written — the server may be generating the answer
right now. The audit found it classified `connect` and therefore retried,
risking a doubled answer. It is non-retryable by design.

**Q34. The machine enforces 10 s first-token / 60 s total. What must the
transport timeouts satisfy?**
A: Sit ABOVE them (read=75 s in wire.py) so they never fire first. The
machine's timers own the user-visible timeout behavior; the transport
values are only a backstop against a truly dead socket.

## Prompt & caching

**Q35. Why must the prompt be byte-stable across calls?**
A: Anthropic caching is a byte-prefix match. Timestamps or unordered joins
silently zero the hit rate.

**Q36. Where does the cache breakpoint sit and why?**
A: After the resume+JD block, before the style policy — so flipping answer
style never invalidates the cached profile.

**Q37. When is the cache marker a silent no-op, and how do you tell the
truth about it?**
A: Haiku's minimum cacheable prefix is 4096 tokens — typical 1–2K-token
profiles don't qualify; it pays at roughly 16K+ chars of profile.
`usage.cache_read_input_tokens` in the response is the ground truth.

**Q38. Why does the transcript live in the user message, not the system
prompt?**
A: It changes every question. In the system prompt it would sit in the
cached prefix and break byte-stability call over call.

## Security & untrusted input

**Q39. Why does the markdown renderer not parse links AT ALL?**
A: Class elimination: with no `<a href>`, there is nothing to sanitize and
no `javascript:` to smuggle. `[x](url)` renders as literal text.

**Q40. How does model text reach the DOM, and what may it never become?**
A: Only as React text nodes. Never `dangerouslySetInnerHTML`, never an
attribute value.

**Q41. What is the streaming invariant for the renderer?**
A: For every prefix of a document, rendering the prefix then the full text
produces a DOM byte-identical to rendering the full text once — enforced
by the parser being a pure function of the full source, tested at every
cut point.

**Q42. Keys: what does the frontend see, and what do the three patch
states mean?**
A: Only `has<Provider>Key` booleans — never key material. Omitted field =
keep; empty/whitespace = clear; non-empty = replace.

**Q43. A settings file is copied from another machine. What do its `enc:`
secrets decode to?**
A: Unset (DPAPI can't unprotect a foreign blob) — fail closed; never hand
the stored string to a provider.

**Q44. Why are non-ASCII API keys refused at save time, with a message
about smart quotes?**
A: A key with a smart quote can't go in an HTTP header — httpx raises
UnicodeEncodeError at send time, mid-answer, surfacing as a useless
"internal error". Refusing at the door names the real cause at the moment
the user can fix it.

**Q45. Recite the markdown emphasis caps and the failure each prevents.**
A: Depth 24, 1000 resolved pairs, resolution skipped past 20,000 chars.
Unbounded nesting overflowed the render stack — and React unmounts the
whole root on an uncaught render error, so one answer blanked the app.
Delimiter-dense text was quadratic: 18 KB of `*a*` froze the main thread
~59 s.

**Q46. After the XSS suite runs, what attribute may a rendered answer
element carry?**
A: Exactly one, anywhere: `<ol start>`. Every payload renders as literal
text with zero live elements, and the suite asserts no element carries any
other attribute.

## Content protection

**Q47. What single Win32 call hides the window from screen sharing, and
why is calling it not enough?**
A: `SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE)`. It returns a
BOOL that can fail (HWND not ready, policy, Windows builds older than
2004) — so the app READS THE AFFINITY BACK and reports success only when
the OS confirms `WDA_EXCLUDEFROMCAPTURE`.

**Q48. Why is `WDA_MONITOR` treated as a failure?**
A: Partial protection is not protection — it hides from some capture
paths but not the ones screen sharing uses. The read-back must equal
`WDA_EXCLUDEFROMCAPTURE` exactly.

**Q49. `shown` and `loaded` both trigger protection attempts. What stops
a slow attempt from lying?**
A: Every attempt takes a sequence stamp under a lock; a loser that
finishes after a newer attempt already reported must not overwrite the
fresher verdict. Each attempt tries up to 5 times with growing backoff,
on a background thread — never blocking a pywebview callback.

**Q50. The OS never confirms protection. What does the user see, and why
is that a product decision?**
A: A standing `role="alert"` warning in the window. A user who believes
they are hidden while being broadcast is this product's worst outcome —
so it is not a log line.

## The audio path & DSP

**Q51. Trace one second of audio from the device to Deepgram.**
A: WASAPI loopback of the default render device → PortAudio callback
(~125 ms i16 chunks, `src_rate // 8` frames) → numpy: interleaved → float
→ mono → `StreamingResampler` → i16 → accumulate 2048-sample (~128 ms at
16 kHz) frames → `call_soon_threadsafe(on_frame)` → the machine's rule-4
checks → `stt.send` → send queue → binary WebSocket frame.

**Q52. Name the three MEASURED ways per-chunk resampling was wrong.**
A: (1) It pinned both endpoints of every chunk, so output depended on how
the device sliced the stream — up to 2.0 of waveform error on a
unit-amplitude sine. (2) It dropped samples when a chunk didn't divide
evenly — 15,985 delivered where 16,000 were owed, per second. (3) No
low-pass, so everything above 8 kHz folded into the speech band — a
10 kHz tone arrived near full strength at ~6 kHz.

**Q53. What state does `StreamingResampler` carry across chunks?**
A: The FIR's memory (the last 62 input samples — overlap-save), the
unread tail of the buffer, and the FRACTIONAL read position `_pos` — the
phase carry that keeps consecutive chunks on one continuous time base.

**Q54. Why does decimating 48→16 kHz without a low-pass fold a 10 kHz
tone to ~6 kHz?**
A: A 16 kHz stream can only represent content up to 8 kHz (Nyquist).
Content above it doesn't vanish when you subsample — it reflects:
10 kHz aliases to 16 − 10 = 6 kHz, indistinguishable from real 6 kHz
once sampled.

**Q55. Recite the anti-aliasing filter's numbers and what each buys.**
A: 63-tap windowed-sinc (Hamming), 7.2 kHz cutoff. ~59 dB alias
suppression; the speech band (300/1000/3000 Hz) preserved to <1%; ~1.5%
of real time on the callback thread. Cost matters because it runs inside
the GIL on PortAudio's thread.

**Q56. Why must NOTHING escape the device callback as an exception?**
A: It would propagate into PortAudio's C callback, which kills the stream
with no Python-visible error — the recording keeps "running" and captures
nothing. The callback swallows, drops the chunk, and resets its pending
buffer.

**Q57. What is THE property test for the resampler?**
A: Chunk-invariance: feeding a signal through in ANY chunk sizes
(480/1024/6000/7777 in the test) yields byte-identical output to feeding
it whole. Any boundary-dependence is a latent bug, because the DEVICE
chooses the slicing, not you.

**Q58. Why does upsampling (or 16 kHz passthrough) skip the FIR?**
A: Aliasing is created by discarding bandwidth. Going up — or staying —
keeps every input frequency representable, so there is nothing to fold.

**Q59. What does the silence hint measure, and what does 0.003 mean?**
A: The recording second audio was LAST heard (rms above 0.003 — over
digital silence and comfort noise, under any real speech). Tracking WHEN,
not whether, catches a device unplugged mid-question: the stream stays
bound to the dead endpoint and delivers silence with no error.

## The shell/core seam & event dispatch

**Q60. Commands worked but nothing ever painted. What are the two
channels, and which one was dead?**
A: Commands travel pywebview's `js_api` (worker thread →
`run_coroutine_threadsafe`). Events travel the sink pump → `evaluate_js`
→ an `app:event` CustomEvent. `sink.attach(window)` was never called in
app.py, so the pump parked forever on `while self._window is None` —
transcript, deltas, levels, and errors all queued into nowhere while
Record/Stop/Ask kept "working".

**Q61. The attach was missing for four commits. Why did every suite
pass?**
A: Each side mocked the other: core tests use a fake sink, bridge tests
attach a fake window themselves, frontend tests dispatch events by hand.
The one line joining them had no owner until `tests/test_app_wiring.py` —
which was verified to fail with the attach removed.

**Q62. Why does the pump batch events, and what is the cap?**
A: Each `evaluate_js` is a blocking round trip on a worker thread, and an
answer streams dozens of deltas per second. The pump drains whatever is
queued into ONE call, capped at 64, order preserved.

**Q63. A batch dispatch fails. What happens, and why not just drop it?**
A: It is retried event by event. Batching had amplified the blast radius
64× — one webview hiccup used to discard up to 64 events, and a lost
`llm:done` strands the UI in "Generating answer…" forever. Serialization
also happens per batch INSIDE the guard, so one poison payload can't kill
the pump task.

**Q64. Why double-JSON-encode the batch, and why `allow_nan=False`?**
A: The outer encode escapes quotes, backslashes, newlines, and
U+2028/U+2029 — JS source line terminators that would break the generated
script. `NaN`/`Infinity` are not JSON; `JSON.parse` would reject the
whole batch at the page instead of at the pump.

**Q65. When do `llm:delta` events reach the reducer?**
A: Coalesced into one dispatch per animation frame while streaming — and
flushed synchronously BEFORE `llm:done` or `session:error` is processed,
or the tail of the answer would land after its own terminal event.

## Product behavior & UI

**Q66. When does the latency clock start and what are the three
metrics?**
A: At stop-request. `sttFinalizeMs` (stop→final transcript),
`firstTokenMs` (stop→first delta), `totalMs` (stop→answer complete).

**Q67. Two metric honesty rules?**
A: Typed questions report `sttFinalizeMs` exactly 0 (no STT stage); a
no-delta answer reports `firstTokenMs = totalMs`, never 0 — 0 renders as
"instant" and lies.

**Q68. When is a failed/aborted attempt kept in history vs discarded?**
A: Captured a question or partial answer (non-whitespace) → retired into
history (it's user work). Captured nothing → discarded.

**Q69. Why does the frontend buffer events for an unknown session id while
a start/ask call is in flight?**
A: The core may emit for the new session before the command's promise
resolves in JS (two independent channels). Buffer-and-replay on adoption
prevents losing the first partial; anything else stale is dropped.

**Q70. Why does `stop_session` have a "not taken" error return, and what
does the frontend do with it?**
A: Every other outcome arrives as an event; the return is the only way
the UI learns its stop went nowhere. The frontend does NOT cancel on a
refusal — the session may be mid-finalize from the 120 s cap with its
answer still coming. It keeps tracking (`stopStranded`) and falls back to
idle only after a bounded 20 s wait. (The old behavior — treat any
refusal as "session is gone" and cancel — destroyed the cap's answer.)

**Q71. Recite the four hotkey statuses and the user problem each names.**
A: `registered` (working) · `disabled` (user chose no shortcut — no
message) · `invalid` (not a shortcut Windows understands — a typo; don't
send them hunting for a conflicting app) · `unavailable` (well-formed but
the OS refused — another app owns it).

**Q72. What geometry does a minimized window report, and why does it
matter?**
A: Windows parks minimized windows at (-32000, -32000) with a
titlebar-sized rect. The debounced bounds save used to persist it, so
quitting while minimized lost the position the user arranged.
`plausible_bounds` rejects the sentinel and sub-minimum sizes.

## Testing craft & mutation testing

**Q73. 18 mutations were applied against a fully green suite. What's the
headline?**
A: 7 SURVIVED — guards that looked tested but that no assertion actually
protected. Each was closed, and every fix was verified by re-applying the
mutation and watching the new test fail.

**Q74. The retry test used a `stream_drop` failure. Why did that prove
nothing about `got_delta`?**
A: `is_retryable` rejects `stream_drop` anyway, so the retry was skipped
for the WRONG reason — deleting `not got_delta` changed nothing. The
fixed test uses a `connect` failure, making the guard the only thing
preventing a doubled answer.

**Q75. Re-applying the direct-`ws.send` mutation left the CloseStream
ordering test green. Why?**
A: On a fast loopback socket the sender drains the queue synchronously
before finalize runs, so overtaking could never be observed — the test
was passing by accident. The fixed test stalls the first write to make
the race real, and was verified to fail against a direct send.

**Q76. What does "verified to fail without the fix" buy that green
doesn't?**
A: Proof the test observes the behavior at all. A regression test you
never saw red may be passing for reasons unrelated to the bug — exactly
what happened in Q74 and Q75.

**Q77. Which shipped code path had ZERO test coverage when the mutation
pass ran?**
A: The content-protection orchestration in app.py — nothing referenced
`_apply_content_protection`, and nothing checked that the app ever EMITS
`protection:ok` / `protection:failed`, so the attempt-stamping added in the
previous commit (36 minutes earlier) shipped untested. (The pure
`apply_content_protection` read-back and the frontend's reaction to those
events were both already covered; the seam between them was not.)

**Q78. `_on_frame`'s stop check and `_emit`'s errored guard needed tests
driven directly against machine state. Why?**
A: They are defense-in-depth masked by synchronous sibling checks — no
loop-driven test can separate them. The window they cover is only
reachable from a real audio thread, which is exactly why it needs a test
rather than a reader's trust.

**Q79. What do the machine-test fakes have that mocks don't?**
A: Knobs — stallable connects and key reads, injectable `Timeouts` and
`clock`. Tests set up a RACE, not an expectation of calls, and assert on
emitted events and state, so refactors don't break them.

## The case studies, compressed

**Q80. A Stop press races the 120 s cap. Walk the failure chain.**
A: Cap fires → auto-stop begins finalize → the user's stop arrives → the
core correctly refuses it (rule 3) → the old frontend read ANY refusal as
"session gone" and called `cancel_session` → supersede aborted the
finalizing session → all its events, including `llm:done`, were dropped.
Two minutes of recording: no answer, no error. Both halves were
individually correct; the contract between them was wrong.

**Q81. What did the settings-writer race lose, and what two changes fixed
it?**
A: A debounced window-bounds save racing a Save patch dropped one
writer's fields from disk AND cache (unsynchronized read-modify-write) —
a just-saved API key or the resume vanished. Fix: an RLock around every
write, plus per-writer tmp names so two writers can't interleave bytes in
one tmp file.

**Q82. Why did the list block signature collide, and what made the
collision visible on screen?**
A: Items were joined with U+001F, a separator that can itself appear in
model text, so a one-item list reading `a<U+001F>b` and the two-item
`- a\n- b` produced one signature. `BlockView` memoizes on the signature,
so a real change was memoized away — stale DOM stayed on screen.
`JSON.stringify` is collision-free by construction.

**Q83. What was wrong with `str.isdigit()` for parsing F-keys?**
A: It accepts 128 codepoints that `int()` rejects ("f²", "f①"), so a
parser documented never to raise raised ValueError — and Arabic-Indic
digits ("f٢") would have mapped to a REAL F-key. The fix is
`isascii()` + `isdecimal()` before `int()`.

**Q84. Three questions to ask before trusting any green test.**
A: Did I see it fail? Is the guard I care about the ONLY thing between
this test and a pass (or is a sibling check masking it)? Does it exercise
the real seam, or did both sides get mocked?
