# 04 — Spot the bug

Ten snippets. Each is a *plausible* version of real code from this repo
with one deliberate flaw — the kind that passes review when everyone's
tired. For each: find the bug, name the user-visible symptom, THEN read
the answer. Write your guess down first; the commitment is what makes it
stick.

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
sending (and again right after the sleep).
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
transcript panel stays empty while the meter moves; stop returns
"not taken". The claim must be synchronous, before any await, and every
resumption must re-check `self._active is session`.
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
emit entirely for `aborted`.
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
keys are GONE. Must write `settings.json.tmp` then `os.replace` (atomic on
NTFS). (2) Memory updated BEFORE the write lands: if the write fails,
memory says saved, disk says old; the next successful save of anything
silently commits the failed patch too. Update the cache only after
`os.replace` returns.
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

---

**Scoring guide.** 8–10 found: you've internalized the invariants — go do
the exercises. 5–7: re-read the guided tour stops for the ones you missed.
Under 5: normal for a first pass; do the flashcards for two days and come
back — these ten map directly onto cards Q11, Q15, Q17–19, Q3, Q26, Q30,
Q8's neighborhood, and Q36.
