# 07 — Case studies: six bugs that shipped

Six real defects from this repo's own history, written up as incident
reports. Every one of them shipped in a build whose test suites were fully
green, and that is the point of the document: the interesting question is
never "what was the bug" but "why did several hundred passing tests not
see it". Each report answers that question specifically — what the suite
was mocking, or asserting, or structurally unable to reach — because that
is the part you can reuse on the next codebase.

Everything below is reconstructed from the git log (the commit messages
are post-mortems), `docs/TESTING.md`, and the code. Every number was
measured in this repo. Where a report says a fence "was verified to
fail", that means the fix was removed (or the mutation re-applied) and
the named test went red — a claim several of these fences could not have
made before the mutation-testing pass, which is case study 4's punchline.

---

## 1. The missing attach: an app that looked alive and showed nothing

**The symptom.** Press Record: the button flips to "Stop & Answer", the
timer counts up. But the level meter never moves, no transcript appears,
and Stop parks on "Finalizing transcript…" forever. No error is shown —
no error CAN be shown. Settings open and save normally. Every control
responds; nothing the app exists to display ever displays. This was the
shipped behavior for four consecutive commits (`b226c15` through
`956be74`), surviving two audit-fix passes that each added dozens of
tests.

**Why every test passed.** Core and shell talk over two independent
channels. Commands (Record, Stop, Ask, settings) travel pywebview's
`js_api` — request/response. Everything the core *emits* — transcript
partials, answer deltas, audio levels, errors — travels one queue pumped
into `window.evaluate_js` by `WebviewEventSink`. Three suites cover that
pipeline, and each mocks a different side of the seam:

- the machine tests drive `SessionManager` with a fake events sink;
- the sink tests construct a `WebviewEventSink` and **attach a fake
  window themselves**;
- the frontend tests dispatch `app:event` CustomEvents by hand.

Each suite proves its own segment. The one line that joins the segments —
telling the production sink about the production window — lived in
`app.py`, which had no tests at all. Nobody owned the seam.

**The root cause.** `app.py` created the window and assigned
`self.window = webview.create_window(...)` but never called
`sink.attach(window)`. The pump is deliberately written to hold events
rather than drop them while no window exists
(`app_core/bridge/events.py:39`, `while self._window is None`), so every
event the core ever emitted queued forever, silently. Hold-don't-drop is
the right design — it is what makes pre-attach events survive startup —
and it is also exactly what made this failure noiseless.

**The fix.** Commit `1536a0c`. Window adoption is now one function,
`App._wire_window` (`app.py:378-393`): it attaches the sink and
subscribes every window callback, and `run()` passes the freshly created
window through it. The docstring calls the attach load-bearing, because
it is.

**The fence.** `tests/test_app_wiring.py::TestWindowWiring`. It builds
the real `App` object — real store, real machine, real sink — and stubs
only the pywebview surface. `test_wiring_attaches_the_event_sink` asserts
the sink holds the wired window;
`test_an_emitted_event_actually_reaches_the_window` goes end to end:
`sink.emit` → pump → `evaluate_js`, asserting the `app:event` name and
payload arrive at the stub within a 3-second deadline. Delete the attach
line and the pump parks exactly as it did in production — the commit
records that the test was verified to fail without the fix.

## 2. The resampler that heard things nobody said

**The symptom.** No crash, no error, no event. Transcripts are just
slightly wrong more often than they should be, so answers are grounded in
a mis-heard question. Three defects, all measured against the old code:

- output depended on how the device happened to slice the stream — up to
  **2.0** of waveform error on a unit-amplitude 1 kHz sine versus
  resampling the same signal whole;
- samples were silently dropped when a chunk length did not divide
  evenly: **15,985** output samples delivered where 16,000 were owed,
  per second;
- decimating 48 kHz to 16 kHz with no low-pass folded everything above
  8 kHz back into the speech band — a 10 kHz tone arrived near full
  strength at ~6 kHz, straight into the frequencies Deepgram reads as
  speech.

**Why every test passed.** The old suite asserted three things (plus a
16 kHz passthrough): a 48k chunk of 4,800 samples resamples to 1,600
**± 1**; a 440 Hz sine keeps its RMS **between 0.6 and 0.8**; empty input
yields empty output. Look at what those tolerances cannot see. Every test
fed exactly one chunk, so a resampler that restarts its time base per
chunk is definitionally invisible — the bug lives *between* two calls,
and no test ever made two.
The ±1-per-chunk length tolerance is precisely the fractional sample the
old code discarded per chunk, so the sample deficit was inside the
allowed error by construction. And 440 Hz sits far below the fold-over
region with an RMS window wide enough to hide substantial distortion; no
test contained any frequency above 8 kHz, so aliasing had nothing to
alias. The tests were true statements that did not constrain the
properties that mattered.

**The root cause.** The old `resample_to_16k` was a bare `np.interp`
over `linspace(0.0, mono.size - 1, num=out_len)` per chunk: both
endpoints of every chunk pinned to input endpoints (phase restarts at
every chunk boundary), `out_len` rounded per chunk (the fractional sample
owed is discarded), and no anti-aliasing filter at all before decimation.

**The fix.** Commit `1536a0c`: `StreamingResampler`
(`app_core/audio/downsample.py:47-99`) — a 63-tap windowed-sinc FIR at
7,200 Hz cutoff, applied overlap-save with the filter's memory carried
across chunks, plus a carried fractional read position so consecutive
chunks share one continuous time base. Measured results: ~59 dB alias
suppression, in-band content (300/1000/3000 Hz) preserved to <1%,
byte-identical output for any chunking, at ~1.5% of real time on
PortAudio's callback thread. One instance per capture stream
(`app_core/audio/capture.py:64`).

**The fence.** `tests/test_downsample.py::TestStreamingResampler` — the
tests are the measurements, and the first three of them fail against the
old code:
`test_output_is_independent_of_how_the_device_chunks_the_audio` demands
byte-identical output across chunk sizes 480/1024/6000/7777;
`test_no_samples_are_lost_over_a_long_stream` counts the total owed;
`test_content_above_the_target_nyquist_is_filtered_not_folded` plays the
10 kHz tone and asserts suppression;
`test_speech_band_content_passes_through_intact` pins the other side so
the filter cannot quietly dull speech;
`test_cost_stays_negligible_on_the_callback_thread` keeps the fix
affordable where it runs.

## 3. The stop that raced the 120-second cap

**The symptom.** Record for the full two minutes and press Stop right as
the cap fires. Result: no answer, no error, back to idle. Two minutes of
question, silently discarded — the worst per-incident data loss in this
app's history, found by a fresh-eyes hunt in the final commit
(`a535ead`), not by any suite.

**Why every test passed.** This is the one case where the suite did not
miss the bug — it *endorsed* it. The old frontend test "a not-taken stop
recovers to idle instead of hanging in Finalizing" asserted
`expect(api.cancel_session).toHaveBeenCalledWith("s1")`: the destructive
recovery was pinned as the specified behavior. It was even right, for the
scenario the test scripted — a session that really is gone. The core
suite, meanwhile, proved its own half correctly:
`test_cap_auto_stops_and_answers_normally` shows the cap finalizing and
answering. Both sides correct in isolation. The gap is that the
frontend's mocked bridge cannot auto-stop on its own — a mock has no
120-second timer — so "the stop was refused because the session is
already stopping *itself*" was unrepresentable in the frontend harness.
The ambiguity the frontend had to handle could not occur in its tests.

**The root cause.** When the cap fires, `_on_record_cap`
(`app_core/session/machine.py:294`) emits `session:autostopped` and
begins the stop; a user Stop press that arrives in that window is
correctly refused by the stop contract (`stop_session`,
`machine.py:176`). The old frontend `doStop` treated every refusal as
"session is gone": it called `bridge.cancelSession(sid)` and dropped to
idle. Cancel is rule-10 silent teardown — it aborts the session
mid-finalize, so no `llm:done` and no error ever arrive. The core did
everything right; the frontend's recovery destroyed the answer the core
was in the middle of producing.

**The fix.** A refused stop never cancels
(`frontend/src/App.tsx:471-485`). The reducer's `stop-not-taken` keeps
the phase at finalizing and arms `stopStranded`; a bounded 20-second
fallback (`STOP_RECOVERY_MS`, `App.tsx:65`) dispatches `stop-recover` —
retire the entry, return to idle — only if no terminal event lands.
Either the cap's answer streams in normally, or the UI recovers on the
timer instead of hanging in "Finalizing…" forever. Both outcomes survive.

**The fence.** `app-coverage.test.tsx`, describe block "a stop that
races the 120s cap". The first test refuses the stop, asserts
`cancel_session` was **not** called, then delivers `llm:delta` /
`llm:done` for the same session and asserts "the hard-won answer"
renders. Under the old code the unconditional cancel fires on the first
assertion — the test cannot pass by accident. The second test covers the
other branch: when the session really is gone, the bounded wait still
recovers to idle.

## 4. CloseStream overtaking the audio queue — and the test that passed by accident

**The symptom.** Under real network congestion, the transcript loses its
last words — precisely the words spoken just before Stop, which in an
interview is the substance of the question. Deepgram discards any audio
that arrives after CloseStream, so the loss is silent: the finalize
succeeds, the answer streams, and it answers a truncated question.

**Why every test passed.** Twice over, which is why this case earns its
place. Originally, audio frames traveled an asyncio queue drained by a
sender task, while `finalize()` wrote CloseStream **directly** on the
socket. Every test runs against a scripted WebSocket server on loopback,
where a send never blocks — the sender drains the whole queue the moment
the loop yields, so the direct write always landed last and the ordering
held by luck of the transport. The audit fix (`a47073d`) routed
CloseStream through the queue and added
`test_closestream_never_overtakes_queued_audio` — and the mutation pass
(`a535ead`) then proved *that regression test also passed for the wrong
reason*: re-introduce the direct `ws.send` and the test stays green,
because its 20 queued frames still drain synchronously before `finalize`
even runs. An ordering test proves nothing unless the losing order is
reachable inside its harness.

**The root cause.** Two writers, one socket, an ordering requirement
between them, and a test transport on which the race window has zero
width. The old `_do_finalize` did `await ws.send(CLOSESTREAM_MSG)` while
up to fifteen seconds of buffered audio could still be sitting in the
queue behind a stalled send.

**The fix.** CloseStream became a queue sentinel (`_CLOSE_SENTINEL`,
`app_core/stt/client.py:67`): `_do_finalize` enqueues it
(`client.py:174`) and the sender writes it only after every frame queued
before it, then exits (`client.py:285-287`). Ordering is now enforced by
the data structure, not by transport timing.

**The fence.**
`tests/test_deepgram_client.py::TestCloseStreamUnderBackpressure::test_closestream_waits_behind_a_stalled_sender`.
It wraps the socket's `send` so the *first* write parks on an event — a
congested socket, manufactured — queues 12 frames, and calls `finalize()`
while the sender is stuck on frame 0. A direct send would escape during
that window; the test asserts the server saw all 12 frames and then the
close, in order. Verified to fail when the sentinel is replaced with a
direct send. The original ordering test stays too, as the cheap fast-path
check.

## 5. The block signature that collided

**The symptom.** A list on screen showing content the model did not
write, and never updating. The trigger is narrow — it needs a specific
control character in the answer text — but the consequence is the
renderer's cardinal sin: DOM that disagrees with the source. Copy would
put the true markdown on the clipboard, so what you paste is not what
you saw.

**Why every test passed.** The renderer's flagship invariant — every
streaming cut point renders identical DOM to a batch render — ran over a
corpus, and no corpus document contained the one character that mattered.
Worse, the suite actively *rewards* the failure mechanism: "completed
blocks keep their DOM nodes across streaming updates" asserts that
`React.memo` bails out, because node identity is what prevents flicker. A
memo comparator is an equality claim. Tests that only ever feed it pairs
where equality is honest cannot catch the pair where it lies.

**The root cause.** `blockSignature` summarized a list block by joining
its item texts with the single character U+001F (the ASCII unit
separator). Item text is untrusted model output, and
U+001F is an ordinary character — `trim()` and `\s` both leave it alone —
so a single item containing `a␟b` produced the same signature as the
two-item list `a`, `b`. (The `a47073d` commit message compresses this to
"`- ab` and `- a\n- b` collided"; under the code as actually shipped that
pair differed — the genuine collision needed the separator character
inside item text.) The collision is not cosmetic because of how the
renderer uses the signature: `MarkdownBlocks` keys blocks by **index**,
and `BlockView` memoizes on `prev.signature === next.signature`
(`frontend/src/markdown/Markdown.tsx:82-94`) — so when the source
changes and the block at index N re-parses to a different list with a
colliding signature, the memo bails and the stale DOM simply stays.

**The fix.** `JSON.stringify(block.items.map((item) => item.text))`
(`frontend/src/markdown/blocks.ts:216-218`) — an injective encoding, not
a cleverer separator. There is no character to smuggle because the
encoding escapes everything.

**The fence.** `markdown-hardening.test.tsx`, describe block "block
signature identity". The load-bearing test is "separator-like control
characters inside item text cannot collide": it builds exactly the pair
that was equal under the old join — `- a` + U+001F + `b` versus
`- a\n- b` — and asserts distinct signatures, so it fails against the old
code outright. Its siblings pin the naive-concatenation collisions and
the user-visible behavior (a rerender across near-colliding lists must
change the `<li>` count).

## 6. The settings writer that erased a just-saved API key

**The symptom.** Paste an API key, click Save, see "Saved ✓", drag the
window to where you want it — within the same half second. The key is
gone, from disk *and* from memory, and nothing reports anything: both
writes returned success. The next Record press says "No Deepgram API key
saved. Open Settings (gear icon) and add it." Depending on which writer
lost, it could be the resume instead.

**Why every test passed.** Every settings test called the store from one
thread. The store's atomicity tests are thorough — tmp file plus
`os.replace`, failed writes leaving memory matching disk — and every one
of those guarantees is about a *single* write. But atomic writes
serialize bytes, not decisions: the lost update happens in the gap
between `data = dict(self._data)` and `self._save(data)`, a window that
no single-threaded test can hold open. The threading reality that makes
the race real lives outside the unit: patches arrive on bridge worker
threads, while bounds saves arrive from a `threading.Timer` debounce and
the close handler (`app.py:324-356`). The unit tests faithfully tested
the store as a single-threaded object, which is a thing it never was in
production.

**The root cause.** `patch()` and `set_window_bounds()` both did an
unsynchronized read-modify-write of the whole settings dict. Interleave
them and the loser's fields are overwritten by the winner's stale
snapshot — committed to disk and installed as the in-memory cache, so
the damage survives. A second, smaller defect compounded it: both
writers shared one tmp path (`settings.json.tmp`), so two concurrent
writes could interleave bytes in the same tmp file before either
`os.replace` landed.

**The fix.** Commit `a47073d`: a `threading.RLock` around both writers
(`app_core/store/settings.py:42`, taken in `patch` and
`set_window_bounds`), and per-writer tmp names —
`settings.json.<pid>.<tid>.tmp` (`settings.py:113`).

**The fence.**
`tests/test_settings.py::TestConcurrentWriters::test_a_bounds_save_racing_a_patch_never_loses_either_writer`
— two real OS threads released by a barrier, each performing 40 contended
writes (one saving keys and resume, one saving bounds), then asserting
both writers' final values survive in memory *and* in a fresh store
reloaded from disk. It can genuinely fail: remove the lock and 40
interleaved read-modify-writes per side make the lost update land in
practice, on the final `dg-39`/`r39` assertions.
`test_no_stray_tmp_files_are_left_behind` pins the visible half of the
tmp change — no tmp file survives a write — but nothing asserts the two
writers get distinct names.

---

## What these have in common

The honest pattern first: five of these six were invisible to a fully
green test suite, and the sixth was worse — the suite looked straight at
it and pinned the wrong behavior as correct (case 3's old test asserted
the destructive cancel). "All tests pass" was a true statement before
and after every one of these bugs. Four mechanisms explain how.

**Seams where each side mocks the other.** Case 1 is the archetype: three
suites, each mocking a different side of one boundary, and the single
line joining the real objects in nobody's jurisdiction. Case 3 is the
same shape in disguise — the frontend's mocked bridge has no 120-second
timer, so the input that mattered was unrepresentable. Case 6 too: the
threads that make the race real live in `app.py`, outside the unit under
test. A mock is a claim that the other side behaves a certain way;
nothing checks the claim unless some test runs the seam with both sides
real. That is why `tests/test_app_wiring.py` now exists as a suite whose
subject is that boundary.

**Tests that pass for the wrong reason.** Case 4's ordering test was
green with the bug re-introduced, because its transport gave the race a
zero-width window. Case 2's tolerances (±1 sample per chunk, RMS between
0.6 and 0.8) were exactly wide enough to contain the defects. A passing
test only carries information if it *can* fail — and that property rots
silently. It is checkable, though: apply the break and watch. The
mutation pass did that systematically — 18 mutations against a green
suite of 288 core tests, **7 survived** — and several fences in this
document say "verified to fail" only because that pass made it a habit.

**Races only reachable from a real thread or a real transport.** Case 6
needs two OS threads in a two-line window; case 4 needs a send that
blocks; case 1's pump parks on a condition tests always satisfied during
setup. Event-loop concurrency is deterministic enough that loop-driven
tests can miss everything that happens off the loop. The shipped fences
manufacture the hostile schedule explicitly — a stalled first send, a
barrier-released pair of writers — because a race a test cannot schedule
is a race the test is not covering.

**Silent loss, not wrong output.** None of the six ever raised. Every
one made the product silently do *less*: events held forever, samples
dropped, an answer cancelled, a tail discarded, DOM not updated, a key
not persisted. Suites are naturally good at "the output is wrong" and
naturally bad at "the output is missing", because the absence of an
event fails no assertion unless a test insists on presence. Most of the
fences above are exactly that insistence: the answer still lands, all 12
frames then close, both writers' last values survive.

This is why the codebase is shaped the way it is: a wiring suite for the
seam nothing owned, resampler tests that are measurements, fences that
manufacture their races, and a `TESTING.md` that records — for every
test — the failure it exists to prevent. The suites were green through
all six of these. The current suites are not proof there is no
seventh; they are the accumulated cost of the first six.
