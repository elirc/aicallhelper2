# 12 — Performance: the one-second budget

The product promise is **~1 second from Stop to the first word of the
answer**, and every number in this document is either enforced in code,
asserted by a test, or was measured on this machine while writing it.
Latency here is not a vibe — the app measures itself on every answer and
shows you the result.

---

## The promise, and where the clock starts

The clock starts at the **stop request** — `_begin_stop` in
`app_core/session/machine.py` stamps `session.stop_time` on the line
commented `THE LATENCY CLOCK STARTS HERE`, before CloseStream is even
queued. A user pressing Stop, the hotkey, and the 120 s cap auto-stop all
funnel through the same function, so the clock always starts at the moment
the user (or the cap) decided the question was over. For a typed Ask, the
clock starts when the command is accepted (`machine.py`, `ask()`).

Every `llm:done` carries four metrics, all measured in the core from
Stop ACCEPTANCE (before the audio drain — the user waits for the drain, so
it counts; moving the origin later would flatter the number without making
anyone wait less):

- **`audioDrainMs`** — Stop → every pre-Stop sample delivered and the
  device closed (bounded at 2 s).
- **`sttFinalizeMs`** — drain complete → final transcript in hand.
- **`firstTokenMs`** — Stop → first answer delta. This is THE
  number; the answer panel's chip renders it as "0.9s to first word"
  (`frontend/src/format.ts`), one decimal, and hovering the chip shows all
  three.
- **`totalMs`** — Stop → last delta received.

All four are core-side: bridge transit, event batching and paint happen
after `firstTokenMs` is stamped, so the first word appears on screen a
little later than the chip says. A true visible-latency benchmark needs a
frontend timestamp as well (see `fabledocs/PROJECT-REVIEW-2026-09-19.md` §5).

Two honesty rules, both purchased with the temptation to lie:

- **A typed Ask reports `audioDrainMs` and `sttFinalizeMs` of exactly 0.** There was no STT
  stage; billing one would be a lie (`machine.py`, `_run_ask`, guarded by
  `test_ask_event_shape_and_metrics`).
- **A provider that never streamed reports `firstTokenMs == totalMs` —
  never 0.** Zero renders as "instant", which lies about the one number
  this app is judged on (`machine.py`, end of `_answer`).

## Walking the budget: Stop press → first word

1. **`_begin_stop` runs synchronously on the loop** — stops capture,
   cancels the cap timer, fires a pre-warm (below), spawns the finalize.
   Cost: microseconds.
2. **STT finalize.** CloseStream travels the same queue as audio (it must
   never overtake queued frames — Deepgram discards audio arriving after
   it), then Deepgram flushes the tail it was holding back for
   `smart_format` and closes. This is the variable chunk of the budget:
   the core caps it at 5 s (`FINALIZE_TIMEOUT_S`), with a +2 s outer guard
   for a finalize that truly hangs.
3. **Key read + prompt build.** The DPAPI secret read runs in
   `asyncio.to_thread` (it can stall, which is why command tickets exist);
   `build_prompt` and `build_request` are pure string work and one
   `json.dumps` — sub-millisecond, and deterministic so a retry can reuse
   the request byte-identically.
4. **The HTTPS request** goes out over the shared `httpx.AsyncClient`'s
   connection pool — and because of pre-warming, a live TLS connection is
   already sitting there. This is the step the architecture exists for.
5. **Provider time-to-first-token.** The irreducible part. Everything
   around it is tuned so this is the only real wait: `max_tokens` is 1024
   ("spoken answers are short; an uncapped completion is pure tail
   latency", `app_core/llm/anthropic.py`), and the Groq provider sends
   `reasoning_effort: "low"` + `include_reasoning: false` because gpt-oss
   is a reasoning model and reasoning is the enemy of time-to-first-word.
6. **Delta → pixel.** Deltas are batched into one `evaluate_js` per queue
   drain and painted once per animation frame (below).

Watchdogs bound the tail: first token 10 s, total 60 s, recording capped
at 120 s. The retry policy is latency-aware too: only a connection-level
failure before any delta is retried, exactly once — an HTTP status is
never retried because an instant identical retry cannot succeed and only
delays the real error (`app_core/llm/retry.py`, `tests/test_retry.py`).

## Pre-warming: pay for TLS before the window opens

`app_core/llm/warm.py` fires an unauthenticated `GET <origin>/v1/models`
through the shared client on **Record press, on Ask, and on Stop press** —
so on the stop path the handshake overlaps the Deepgram finalize instead
of following it. The body is read to completion, which is what returns the
connection to the pool; warms are throttled to one per origin per 2 s, and
a failed warm never raises. The client keeps up to 8 connections alive for
120 s (`app.py`), so the connection warmed at Record press is still live
at Stop press for any recording the 120 s cap permits.

Why this matters, measured on this machine (snippet below): a cold request
to `api.anthropic.com` cost **949 ms** (DNS + TCP + TLS + request); the
same request over the pooled connection cost **101 ms**. The handshake
alone would have spent most of the one-second budget before the model
heard a single word.

## Prompt caching: real rules, honest expectations

Anthropic's prompt cache is a **byte-prefix match**, so the prompt builder
is byte-stable by construction: identical inputs produce byte-identical
prompts — no timestamps, no unordered joins (`tests/test_prompt.py`,
`test_byte_stable_across_calls`). The system prompt is TWO blocks with the
`cache_control: {"type": "ephemeral"}` breakpoint after the resume+JD
block and the style policy AFTER the breakpoint, so flipping
Brief/Balanced/Detailed never invalidates the cached prefix
(`test_style_flip_never_touches_cached_prefix`). The transcript lives in
the user message, outside the prefix — if it leaked in, no two calls would
ever share a cache entry.

The honesty note: **Haiku 4.5's minimum cacheable prefix is 4096 tokens**,
so a typical 1–2K-token profile makes the marker a silent no-op — no
error, just no cache. It starts paying at roughly 16K+ characters of
profile, at which point writes cost 1.25×, reads 0.1×, with a 5-minute
TTL. `usage.cache_read_input_tokens` in the API response tells the truth
about whether it engaged; the marker being present proves nothing.

The thresholds above are the same ones README's "Prompt-caching honesty
note" states; if they change, change them in both places.

## Work that must NOT be on the hot path

- **The audio callback runs inside the GIL on PortAudio's thread.** It is
  allowed minimal numpy work only: the `StreamingResampler` (63-tap FIR +
  interpolation) costs ~1.5% of real time as measured for the audit, and
  1.4–5% run to run on the slow laptop this document was verified on;
  `test_cost_stays_negligible_on_the_callback_thread` asserts a 125 ms
  chunk costs under 12.5 ms. Burning CPU here starves the pipeline.
- **The transcript accumulates incrementally.** `TranscriptAccumulator`
  appends each committed final — O(appended text) per message, never a
  re-join of the whole recording (`app_core/stt/frames.py`). Interims
  replace only the uncommitted tail.
- **Event dispatch is batched.** Each `evaluate_js` is a blocking round
  trip on a worker thread, and an answer streams dozens of deltas per
  second; the pump drains whatever is queued into ONE call (capped at 64,
  order preserved), and a failed batch is retried event by event so a
  webview hiccup cannot cost 64 events (`app_core/bridge/events.py`).
- **Paints are coalesced per frame.** The frontend buffers `llm:delta`
  payloads and flushes them as one reducer action per
  `requestAnimationFrame` (`App.tsx`, `scheduleFlush`), so a burst of
  deltas is one React render, not thirty. Buffered deltas are flushed
  BEFORE `llm:done` / `session:error` so the answer never flickers or
  reorders (`app-gestures.test.tsx`, "delta coalescing and ordering").

## The renderer re-parses everything — and that is fine

`Markdown` re-parses the FULL answer on every flushed delta. That is a
deliberate trade: the parse being a pure function of the whole source is
what makes streamed DOM byte-identical to a batch render at every cut
point (the invariant `markdown-render.test.tsx` proves over a corpus). At
1024 max tokens an answer is a few KB of text, and parsing a few KB per
animation frame is noise — the hardening test renders 24 KB of
delimiter-dense text (`"*a*".repeat(8000)`) in 0.1–0.35 s on this machine,
asserting under 3 s.

The caps exist because model output is untrusted and the un-capped version
was measured failing: 18 KB of `*a*` froze the main thread for ~59 s
(quadratic emphasis resolution), and `*`×12000 nesting overflowed the
render stack — which unmounts the entire React root, blanking the app
mid-call. `frontend/src/markdown/inline.ts` caps emphasis nesting at 24
deep and 1000 resolved pairs, skips emphasis resolution entirely past
20 000 characters, and keeps an opener-floor index so failed closers never
rescan quadratically. Past the caps, delimiters render as literal text.
Per-block memoization (`BlockView`, keyed on a content signature) means
completed blocks keep their DOM nodes; only the growing tail re-renders.

## Measure it yourself

**The chip is the primary instrument.** Every answer's chip shows
`firstTokenMs`; hover it for the full three-metric breakdown. No tooling
needed.

**The resampler's callback-thread cost** (all commands from the repo
root):

```
.venv\Scripts\python -m pytest tests/test_downsample.py -q -k cost
```

or measure it directly:

```python
import time
import numpy as np
from app_core.audio.downsample import StreamingResampler

resampler = StreamingResampler(48_000)
chunk = np.random.default_rng(0).standard_normal(6000).astype(np.float32)
resampler.process(chunk)  # warm-up: the first call pays numpy setup
started = time.perf_counter()
for _ in range(480):      # 480 x 125 ms = 60 s of audio
    resampler.process(chunk)
elapsed = time.perf_counter() - started
print(f"{elapsed:.3f} s for 60 s of audio "
      f"({elapsed / 60 * 100:.2f}% of real time)")
```

(This printed `2.175 s … (3.63% of real time)` here; across runs it ranged
from 1.4% to 5% on this laptop.)

**What pre-warming buys**, with the same pool settings as the app:

```python
import asyncio, time
import httpx

async def main() -> None:
    async with httpx.AsyncClient(
        limits=httpx.Limits(max_keepalive_connections=8,
                            keepalive_expiry=120.0)
    ) as http:
        for label in ("cold (DNS + TCP + TLS + request)",
                      "pooled (request only)"):
            started = time.perf_counter()
            await http.get("https://api.anthropic.com/v1/models",
                           timeout=10.0)
            print(f"{label}: {(time.perf_counter() - started) * 1000:.0f} ms")

asyncio.run(main())
```

(Here: cold 949 ms, pooled 101 ms.)

**The renderer under hostile input:**

```
cd frontend && npx vitest run src/__tests__/markdown-hardening.test.tsx
```

The two "fast enough" tests print their point by passing; adding
`--reporter=verbose` prints per-test times, and this run showed the 24 KB
delimiter-dense case at 87 ms.

**Cache engagement** cannot be verified offline: send two identical
requests with your key and compare `usage.cache_read_input_tokens` in the
responses — nonzero on the second means the prefix cached.

## Cost per answer: where the money goes

**Deepgram dominates.** Streaming nova-3 is ~$0.0059/min pay-as-you-go,
billed while recording: an hour of recorded questions is ~$0.35, and the
120 s cap bounds any single question at ~$0.012. KeepAlive frames during
silence are not audio and cost nothing.

**The LLM is cents-per-session.** Claude Haiku 4.5 is $1/MTok in, $5/MTok
out. A request is one to two thousand prompt tokens (role + profile +
transcript) plus at most 1024 completion tokens — roughly $0.002–0.003 for
a typical answer, ~$0.007 worst case at the full completion cap.

**Pre-warms are free.** The warm `GET /v1/models` is unauthenticated and
carries no tokens. Cache economics (1.25× writes) only enter once your
profile clears the 4096-token minimum — below it the marker changes
nothing in either direction.

## If the app feels slow

| Symptom | Stage responsible | First check |
|---|---|---|
| Transcript lags while they speak | Deepgram network path | Level meter moving? Silence hint showing? |
| "Finalizing transcript…" for seconds | Deepgram tail flush (5 s cap) | `sttFinalizeMs` in the chip hover |
| Slow first word, small drain + finalize | Cold TLS or provider TTFT | `firstTokenMs − audioDrainMs − sttFinalizeMs`; provider status page |
| First word fast, answer drags | Completion length / throughput | `totalMs` vs `firstTokenMs`; try the Brief style |
| UI stutters while streaming | Renderer / dispatch backlog | Run the markdown-hardening tests |
| Record press itself is sluggish | DPAPI key read on start | Not the latency budget — key reads fail the command, never the clock |
| Error at exactly 10 s or 60 s | The LLM watchdogs fired | Provider is up but slow; retry |
| Recording stops by itself at 2:00 | The 120 s cap — by design | The answer still arrives; status says so |

The stages are separable because the metrics are: `audioDrainMs` isolates
the device stop, `sttFinalizeMs` isolates Deepgram,
`firstTokenMs − audioDrainMs − sttFinalizeMs` isolates warm-up plus provider
TTFT, and `totalMs − firstTokenMs` isolates completion throughput. When a
user says "it's slow", the chip hover usually answers which of the three
it was before you open a single file.
