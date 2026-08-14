# 04 — Spot the bug

Nineteen snippets. Each is a *plausible* version of real code from this
repo with one deliberate flaw — the kind that passes review when everyone's
tired. Snippets 11–19 are drawn from bugs that ACTUALLY SHIPPED here and
were later found by audits, mutation testing, or a fresh-eyes pass. For
each: find the bug, name the user-visible symptom, THEN read the answer.
Write your guess down first; the commitment is what makes it stick.

---

## 1. The eager keepalive

```python
async def _keepalive(self) -> None:
    while not self._closed:
        await asyncio.sleep(self._keepalive_interval)
        try:
            await self._ws.send(KEEPALIVE_MSG)
        except Exception:
            return
```

<details><summary>Answer</summary>

It checks `_closed` but not `_close_requested`. Between CloseStream being
sent and the server's close landing, the socket is CLOSING — a KeepAlive
sent in that window errors, and in v2-style code that error was reported
as a lost connection. Symptom: intermittent "Lost the Deepgram connection"
errors **during stops that were succeeding** — worse the longer the server
takes to flush. The real code checks `_close_requested` immediately before
sending (and again in the loop condition, before the sleep).
</details>

## 2. The committed imposter

```python
is_final = bool(data.get("is_final"))
return TranscriptSegment(text=transcript, is_final=is_final)
```

<details><summary>Answer</summary>

`bool(...)` promotes truthy imposters — `1`, `"true"`, `"false"` (!) — to
final. Interim text gets committed into the transcript prefix and then the
REAL final for the same words arrives and is appended too. Symptom:
stuttered, duplicated transcripts ("tell me tell me about about
yourself"), and answers grounded in that garbage. Must be
`data.get("is_final") is True`.
</details>

## 3. The helpful retry

```python
except Exception as exc:
    if attempt == 1 and provider.is_retryable(exc):
        continue
    raise
```

<details><summary>Answer</summary>

The `got_delta` guard is missing. A connection that drops AFTER streaming
half the answer classifies however the provider says — but even a
"connect"-classified failure mid-stream would re-run the request and
`on_delta` would append a SECOND answer after the first half. Symptom: the
answer panel shows "…half an answer Hello! Great question…" — two answers
concatenated. Retry is only legal when nothing painted:
`attempt == 1 and not got_delta and provider.is_retryable(exc)`.
</details>

## 4. The polite loser

```python
session = self._new_session("record")
self._warmer.warm(provider.origin)
stream = self._stt_factory(...)
await stream.connect()          # network round-trip
self._active = session          # claim after connecting
```

<details><summary>Answer</summary>

The slot is claimed AFTER the await. Press Record twice quickly: press #2
runs `_supersede()` (which sees either nothing or the old session), claims,
connects… then press #1's `connect()` resolves and **installs itself over
the winner**. Audio now routes to a socket the UI isn't tracking. Symptom:
transcript panel stays empty and the level meter never moves (its events
carry the loser's `sessionId`, which the reducer drops) while the timer
counts; stop returns "not taken". The claim must be synchronous, before
any await, and every resumption must re-check `self._active is session`.
</details>

## 5. The tidy cleanup

```python
def _fail(self, session, err):
    session.errored = True
    session.phase = "done"
    self._emit(session, "session:error", {"error": err.to_payload()})
    self._teardown(session)
```

<details><summary>Answer</summary>

No guard. A mid-finalize socket death calls `_fail`; then the finalize
path notices `errored` too late or a second error source fires — and the
user sees TWO error boxes for one failure (or an error after a completed
answer). Rule 6 is "one error per stream, never after abort": the real
code returns early on `released`/`aborted`/already-failed, and skips the
emit entirely for an `aborted` error code.
</details>

## 6. The innocent join

```python
prefix = ROLE_INSTRUCTIONS
sections = {"resume": resume.strip(), "jd": jd.strip()}
for name, text in sections.items():
    if text:
        prefix += f"\n\n--- {name.upper()} ---\n{text}"
prefix += f"\n(generated {datetime.now():%Y-%m-%d %H:%M})"
```

<details><summary>Answer</summary>

Two cache-killers: the timestamp changes every call, and header text
derived from dict iteration invites drift (plus the header strings are
wrong — they're pinned product copy). Anthropic's cache is a byte-prefix
match: ONE differing byte = zero cache hits, silently — you'd only notice
in the bill and the latency. Prompts must be deterministic, verbatim
constants; the tests assert byte equality across calls.
</details>

## 7. The convenient sanitizer

```python
def render_answer(markdown_source: str) -> str:
    html = markdown_to_html(markdown_source)
    html = html.replace("<script", "&lt;script")
    return html  # innerHTML = render_answer(answer)
```

<details><summary>Answer</summary>

Everything. It builds an HTML STRING from untrusted text and patches one
tag: `<img onerror=…>`, `<svg onload=…>`, `<iframe>`, attribute injection
via quotes, `<SCRIPT`, `<scr<scriptipt` all sail through, and innerHTML
executes the result inside a desktop webview. The architecture answer:
never produce HTML strings from model text at all — parse to data, render
as DOM text nodes, don't parse links, and prove it with an adversarial
test suite.
</details>

## 8. The clean write

```python
def _save(self, data) -> None:
    self._data = data                       # keep memory fresh
    with open(self._path, "w") as f:
        json.dump(data, f)
```

<details><summary>Answer</summary>

Two bugs. (1) Truncate-then-write: a crash or full disk mid-write leaves a
half-file; the next load falls back to defaults — the user's resume and
keys are GONE. Must write a tmp file then `os.replace` (atomic on NTFS) —
and the tmp name must be per-writer (pid + thread id), because two
concurrent writers sharing one tmp path interleave bytes before either
replace lands. (2) Memory updated BEFORE the write lands: if the write
fails, memory says saved, disk says old; the next successful save of
anything silently commits the failed patch too. Update the cache only
after `os.replace` returns.
</details>

## 9. The optimistic chip

```tsx
const onStyleSelect = (style: AnswerStyle) => {
  setStyle(style);                       // update UI immediately
  void bridge.setSettings({ answerStyle: style });
};
// …
<button aria-pressed={localStyle === s} …>
```

<details><summary>Answer</summary>

Optimistic UI on a settings write. If the save fails (disk error,
validation), the chip shows Brief while the core will generate Balanced —
the UI is lying about what the next answer will use. Spec: `aria-pressed`
reflects the PERSISTED style **returned by the save**. The real handler
awaits the Result and re-renders from the returned view; there's a test
that feeds a save returning a different style than clicked.
</details>

## 10. The frame that knew too little

```python
def _on_frame(self, session, pcm, rms):
    if self._active is session:
        session.stt.send(pcm)
        self._emit(session, "audio:level", {"rms": rms})
```

<details><summary>Answer</summary>

Missing the `stop_requested` (and phase) check. A frame already in flight
when Stop lands gets sent AFTER CloseStream — racing the server's flush;
depending on timing Deepgram errors or the tail transcript shifts.
Symptom: flaky finalize errors and occasionally-mangled last words, only
under load, never in a debugger. Rule 4: frames only for the live,
not-yet-stopped session, in connecting/recording phases.
</details>

## 11. The wiring that almost worked

```python
def _wire_window(self, window: Any) -> None:
    self.window = window
    window.events.shown += self._on_shown
    window.events.loaded += self._on_loaded
    window.events.moved += lambda *_a: self._schedule_bounds_save()
    window.events.resized += lambda *_a: self._schedule_bounds_save()
    window.events.closing += lambda *_a: self._on_closing()
```

<details><summary>Answer</summary>

`self.sink.attach(window)` is missing. The event pump parks forever on
`while self._window is None`, so NOTHING the core emits — transcript,
answer deltas, audio level, errors — ever reaches the page. Commands still
work because js_api is a separate channel, so the app looks alive while
showing nothing. This exact bug shipped in the first commit and survived
two audit-fix passes, because every suite mocks one side of the seam:
core tests use a fake sink, bridge tests attach a fake window themselves,
frontend tests dispatch events by hand. The fence is
`tests/test_app_wiring.py`, verified to fail without the attach.
</details>

## 12. The stateless resampler

```python
def _on_device_chunk(self, raw, src_rate, channels, on_frame):
    mono = to_mono(interleaved_i16_to_float(raw, channels))
    samples = float_to_i16(resample_to_16k(mono, src_rate))
    self._pending = np.concatenate([self._pending, samples])
    ...
```

<details><summary>Answer</summary>

`resample_to_16k` constructs a FRESH `StreamingResampler` per chunk, so
nothing carries across chunk boundaries: the interpolation restarts at
position 0 in every chunk (output depends on how the device sliced the
stream) and the fractional read position is thrown away (samples are
silently dropped whenever the chunk length doesn't divide evenly — the
shipped per-chunk version delivered 15,985 samples where 16,000 were owed,
per second). The shipped bug was worse still — a bare `np.interp` with no
low-pass, folding everything above 8 kHz into the speech band. Symptom:
transcription accuracy that varies with the device's buffer size and gets
worse when music or notification sounds play. The capture must thread ONE
`self._resampler` through `downsample_chunk` for the stream's lifetime;
`test_output_is_independent_of_how_the_device_chunks_the_audio` is the
fence.
</details>

## 13. The refused stop

```tsx
const doStop = useCallback(async () => {
  const sid = trackedRef.current;
  if (!sid) return;
  dispatch({ type: "stop-accepted" });
  const result = await bridge.stopSession(sid);
  if (!result.ok) {
    // The session must be gone — clean up so we don't hang in Finalizing.
    void bridge.cancelSession(sid);
    trackedRef.current = null;
    dispatch({ type: "stop-recover" });
  }
}, []);
```

<details><summary>Answer</summary>

A refused stop does NOT mean the session is gone. The 120 s cap auto-stops
the session itself; a Stop press racing the cap is correctly refused (rule
3 — the session is already finalizing), and this code then CANCELS a live
session that was about to deliver the answer. Symptom: record to the
2-minute limit, press Stop near the boundary → no answer, no error,
nothing — two minutes of the user's question destroyed. This shipped and
was found by the fresh-eyes pass. The fix: never cancel on refusal — keep
tracking, arm a bounded 20 s recovery timer, and go idle only if no
terminal event arrives. Fence: the "a stop that races the 120s cap" tests
in `app-coverage.test.tsx`, both branches.
</details>

## 14. The tidy signature

```ts
case "list":
  return `l:${block.ordered ? block.start : "-"}:${block.loose ? 1 : 0}:${block.items
    .map((item) => item.text)
    .join("\u001f")}`;
```

<details><summary>Answer</summary>

The separator is a character that can APPEAR IN THE ITEM TEXT. U+001F is
an ordinary character to `trim()` and `\s` — model output can contain it,
and then `["a<U+001F>b"]` and `["a", "b"]` produce the same signature. That
is not cosmetic: `BlockView` memoizes on this signature, so two different
lists sharing one signature means a real change gets memoized away and
STALE DOM stays on screen. Any join-based signature has this hole (the
shipped version collided on `- a<U+001F>b` vs `- a\n- b`). The fix is
`JSON.stringify(block.items.map(i => i.text))` — collision-free by
construction because it escapes its own delimiters.
</details>

## 15. The impatient close

```python
async def _do_finalize(self) -> str:
    self._close_requested = True
    ws = self._ws
    if ws is None or self._closed:
        return self._acc.text
    if self._keepalive_task is not None:
        self._keepalive_task.cancel()
    await ws.send(CLOSESTREAM_MSG)          # tell the server to flush
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(self._closed_evt.wait(), self._finalize_timeout)
    return self._acc.text
```

<details><summary>Answer</summary>

CloseStream is written DIRECTLY on the socket while audio frames travel a
queue. Under send backpressure (slow network, big frames) the direct write
overtakes frames still queued behind a stalled send — and Deepgram
discards audio that arrives after CloseStream, so the tail of the question
is silently lost. The fix: CloseStream travels the SAME queue as audio, as
a sentinel the sender writes last. Bonus lesson: the original ordering
test PASSED with the direct send, because a fast loopback socket drains
the queue synchronously — the fixed test
(`TestCloseStreamUnderBackpressure`) stalls the first write to make the
race observable.
</details>

## 16. The trusting setter

```python
def apply_content_protection(hwnd: int, api: DisplayAffinityApi) -> bool:
    if not hwnd:
        return False
    try:
        return api.set_affinity(hwnd, WDA_EXCLUDEFROMCAPTURE)
    except Exception:
        return False
```

<details><summary>Answer</summary>

It trusts the setter's return value. `SetWindowDisplayAffinity` can report
success while the affinity did not stick — and a user who believes they
are hidden while being broadcast is this product's worst outcome. Success
must mean the affinity READ BACK equals `WDA_EXCLUDEFROMCAPTURE`:
`api.set_affinity(...)` then `api.get_affinity(hwnd) ==
WDA_EXCLUDEFROMCAPTURE`. (`WDA_MONITOR` on read-back is also a failure —
partial protection is not protection.) This shipped fire-and-forget in
the first commit; the hardening pass added the read-back and
`tests/test_protection.py` — and the mutation pass then found the
app-level verdict path (attempt stamping, `protection:ok`/`failed`) STILL
had zero coverage. Verifying is half the job; testing the verification is
the other half.
</details>

## 17. The digit that wasn't

```python
elif part.startswith("f") and part[1:].isdigit() and 1 <= int(part[1:]) <= 24:
    vk = 0x70 + int(part[1:]) - 1
```

<details><summary>Answer</summary>

`str.isdigit()` is true for 128 codepoints that `int()` REJECTS — "f²"
passes the guard and then `int("²")` raises ValueError out of a parser
documented never to raise. Worse, `int()` ACCEPTS some non-ASCII digits:
"f٢" (Arabic-Indic two) would silently map to a real F2 binding. The fix
is `part[1:].isascii() and part[1:].isdecimal()` before `int()`. This
shipped, was fixed, and then the mutation pass caught that reverting it
broke no test — the never-raise test's candidate list had no
`f<superscript>` input until then.
</details>

## 18. The batch that died together

```python
async def _pump(self) -> None:
    while True:
        first = await self._queue.get()
        while self._window is None:
            await asyncio.sleep(0.05)
        batch = [first]
        while len(batch) < MAX_BATCH:
            try:
                batch.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        details = json.dumps(
            [{"name": n, "payload": p} for n, p in batch], ensure_ascii=False
        )
        code = f"JSON.parse({json.dumps(details)}).forEach(...)"
        try:
            await asyncio.to_thread(self._window.evaluate_js, code)
        except Exception:
            pass  # a dying webview must not kill the pump
```

<details><summary>Answer</summary>

Two bugs, both shipped in the first batching commit. (1) `json.dumps` runs
OUTSIDE the try: one unserializable payload raises out of `_pump` and
kills the pump task — the app goes silent for the rest of the session.
(2) A failed `evaluate_js` discards the WHOLE batch: batching amplified
the blast radius 64×, and a lost `llm:done` strands the UI in "Generating
answer…" forever. The fix: serialize per batch inside the guard, and
retry a failed batch event by event so one poison payload costs only
itself. (Also missing: `allow_nan=False` — `JSON.parse` rejects
`NaN`/`Infinity`, which would drop the batch at the page instead of at
the pump.)
</details>

## 19. The eager wake-up

```python
async def _reader(self) -> None:
    ws = self._ws
    error = None
    abnormal_close = False
    try:
        async for message in ws:
            ...  # parse frames, break on an Error frame
    except ConnectionClosedError as exc:
        abnormal_close = True
    finally:
        self._closed = True
        self._closed_evt.set()          # let finalize return promptly
    self._classify_close(error, abnormal_close, "")
```

<details><summary>Answer</summary>

The closed-event is set BEFORE the close is classified. `finalize()` wakes
on that event and cancels the remaining tasks — including this reader,
mid-classification. Symptom: Record then immediately Stop with a bad API
key — the 1008 close lands during finalize, the reader is cancelled before
`_classify_close` runs, and the "check the API key" error never surfaces;
the user gets silence or a generic finalize error and goes off to debug
their network. The real code classifies FIRST, then sets the event in the
`finally`. Fence:
`test_bad_key_close_during_finalize_keeps_connect_classification`.
</details>

---

**Scoring guide.** 15–19 found: you've internalized the invariants — go do
the exercises. 10–14: re-read the guided tour stops for the ones you
missed. Under 10: normal for a first pass; do the flashcards for two days
and come back. The snippets map onto cards: 1→Q15, 2→Q19, 3→Q23–25,
4→Q3, 5→Q8's neighborhood, 6→Q35, 7→Q39, 8→Q81, 9→Q42's neighborhood
(the persisted view is the truth), 10→Q78, 11→Q60–61, 12→Q52–53,
13→Q70/Q80, 14→Q82, 15→Q21, 16→Q47–48, 17→Q83, 18→Q62–64, 19→Q22.
