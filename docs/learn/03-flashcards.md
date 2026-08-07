# 03 — Flashcards (40 cards)

Cover the answer, say yours OUT LOUD, then compare. Wrong or fuzzy → the
card goes back in the deck. Do 10–15 a day for a few days rather than all
40 once; recall spacing is the point. Cards are grouped by topic so you can
drill your weak areas.

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

## The wire: Deepgram

**Q9. How does the client authenticate the Deepgram WebSocket?**
A: Via the WebSocket subprotocol list: `["token", <api key>]`.

**Q10. Why send KeepAlive every 8 seconds?**
A: Deepgram kills sockets ~10 s after the last audio, and silence during a
call is normal. 8 s stays under the deadline.

**Q11. Why must the keepalive stop the INSTANT a close is requested?**
A: A KeepAlive after CloseStream can error on the CLOSING socket and
fabricate a "lost connection" error during a stop that is succeeding.

**Q12. What does CloseStream buy at finalize time?**
A: The server flushes its held-back tail (including smart_format entity
hold-back) before closing — the last words of the question arrive.

**Q13. How does Deepgram reject a bad API key?**
A: By CLOSING the socket (code 1008, `DATA-xxxx` reason), usually with no
error frame. Close-before-any-Results is classified as a connect failure.

**Q14. Why buffer audio frames before the socket opens, and what are the
buffer's rules?**
A: Capture starts in parallel with connect; without buffering, the first
words are clipped. In-order, capped ~15 s, oldest dropped.

**Q15. `is_final` arrives as `1`. Committed or interim, and why?**
A: Interim. Only literal `True` commits; truthy imposters wrongly promoted
would silently corrupt the committed transcript prefix.

**Q16. Why is finalize idempotent?**
A: Stop-flows can race (user stop vs auto-cap vs teardown); the second
caller must join the first (one CloseStream, one result), not restart the
close dance.

## The wire: LLM providers

**Q17. Recite the retry policy.**
A: Exactly once; only connection-level failure before any delta; never on
HTTP status; never after a delta reached the UI; never after abort;
byte-identical request (same immutable object).

**Q18. Why never retry after the first delta?**
A: The UI appends deltas as they arrive — a second attempt would
concatenate two answers on screen.

**Q19. What distinguishes "connect" from "stream_drop" in wire.py, and why
does it matter?**
A: Whether the HTTP response had started. Connect = server never heard us
(safe to retry); stream_drop = bytes may have painted (never retry).

**Q20. What is `data: [DONE]` and what must you NOT do with it?**
A: OpenAI-style end sentinel. Skip it; do NOT treat it as a terminator —
bytes after it in the same chunk still count.

**Q21. Why parse SSE from bytes with an incremental UTF-8 decoder?**
A: Chunks split anywhere, including mid-character. Decoding chunk-by-chunk
corrupts multi-byte characters (€, 日本語) that straddle a boundary.

**Q22. A stream ends with `data: {"…last words"}` and no newline. What
must happen?**
A: The parser's flush emits that final un-terminated data line — otherwise
the answer's last words are silently lost.

**Q23. Why does Groq's request send `reasoning_effort: "low"` and
`include_reasoning: false`?**
A: gpt-oss is a reasoning model; reasoning tokens are pure delay before
the first spoken word. (And `reasoning_format` is a Qwen-family knob — do
not send it.)

**Q24. Groq returns 404. What's the likely cause and the fix?**
A: Groq retires models on short notice — update the pinned `MODEL`
constant in `app_core/llm/groq.py`. The error message says exactly this.

**Q25. What does the pre-warm actually do, and why read the body?**
A: Fire-and-forget `GET <origin>/v1/models` (3 s timeout, throttled 2 s
per origin) through the SHARED httpx client; reading the body returns the
connection to the pool so the answer request finds a live TLS connection.

## Prompt & caching

**Q26. Why must the prompt be byte-stable across calls?**
A: Anthropic caching is a byte-prefix match. Timestamps or unordered joins
silently zero the hit rate.

**Q27. Where does the cache breakpoint sit and why?**
A: After the resume+JD block, before the style policy — so flipping answer
style never invalidates the cached profile.

**Q28. When is the cache marker a silent no-op, and how do you tell the
truth about it?**
A: Haiku's minimum cacheable prefix is 4096 tokens — typical 1–2K-token
profiles don't qualify; it pays at roughly 16K+ chars of profile.
`usage.cache_read_input_tokens` in the response is the ground truth.

**Q29. Why does the transcript live in the user message, not the system
prompt?**
A: It changes every question. In the system prompt it would sit in the
cached prefix and break byte-stability call over call.

## Security

**Q30. Why does the markdown renderer not parse links AT ALL?**
A: Class elimination: with no `<a href>`, there is nothing to sanitize and
no `javascript:` to smuggle. `[x](url)` renders as literal text.

**Q31. How does model text reach the DOM, and what may it never become?**
A: Only as React text nodes. Never `dangerouslySetInnerHTML`, never an
attribute value.

**Q32. What is the streaming invariant for the renderer?**
A: For every prefix of a document, rendering the prefix then the full text
produces a DOM byte-identical to rendering the full text once — enforced
by the parser being a pure function of the full source, tested at every
cut point.

**Q33. Keys: what does the frontend see, and what do the three patch
states mean?**
A: Only `has<Provider>Key` booleans — never key material. Omitted field =
keep; empty/whitespace = clear; non-empty = replace.

**Q34. A settings file is copied from another machine. What do its `enc:`
secrets decode to?**
A: Unset (DPAPI can't unprotect a foreign blob) — fail closed; never hand
the stored string to a provider.

**Q35. What single Win32 call hides the window from screen sharing?**
A: `SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE)` — re-applied
whenever the window is recreated/reloaded.

## Product behavior & UI

**Q36. When does the latency clock start and what are the three metrics?**
A: At stop-request. `sttFinalizeMs` (stop→final transcript),
`firstTokenMs` (stop→first delta), `totalMs` (stop→answer complete).

**Q37. Two metric honesty rules?**
A: Typed questions report `sttFinalizeMs` exactly 0 (no STT stage); a
no-delta answer reports `firstTokenMs = totalMs`, never 0 — 0 renders as
"instant" and lies.

**Q38. When is a failed/aborted attempt kept in history vs discarded?**
A: Captured a question or partial answer (non-whitespace) → retired into
history (it's user work). Captured nothing → discarded.

**Q39. Why does the frontend buffer events for an unknown session id while
a start/ask call is in flight?**
A: The core may emit for the new session before the command's promise
resolves in JS (two independent channels). Buffer-and-replay on adoption
prevents losing the first partial; anything else stale is dropped.

**Q40. Why does `stop_session` have a "not taken" error return instead of
just doing nothing?**
A: Every other outcome arrives as an event. A silently ignored stop leaves
the UI in "Finalizing…" forever — the return is the only way it learns,
and the UI's recovery path (cancel + idle) hangs off it.
