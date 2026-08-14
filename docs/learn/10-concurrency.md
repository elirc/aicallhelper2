# 10 — Concurrency: one loop, four foreign threads

This is the deep version of ADR-1. The app holds no lock around any piece
of session state, yet its bug class was races — superseded sessions,
stalled commands, events after errors. The resolution is a single-owner
model: one asyncio loop owns everything mutable about a session, and four
families of foreign threads — PortAudio's callback, pywebview's workers,
the hotkey message loop, and the shell's content-protection thread — are
only allowed to post messages into it. This document maps every thread
and every crossing, then walks the rules that keep the model honest, each
tied to the failure it was purchased with.

## The map

Threads alive in a running app:

- **Main thread** — `webview.start()` (app.py:434) blocks it forever
  running the GUI. pywebview fires window callbacks (`shown`, `loaded`,
  `moved`, `resized`, `closing`) and js_api commands on worker threads of
  its own.
- **`core-loop`** — the asyncio loop on a dedicated thread
  (app.py:244–251). All session state lives here and nowhere else.
- **PortAudio's callback thread** — created when the loopback stream
  opens; runs `LoopbackCapture._on_device_chunk` roughly eight times a
  second (`frames_per_buffer=src_rate // 8`, capture.py:82).
- **js_api worker threads** — pywebview invokes every `JsApi` command
  handler off the GUI thread.
- **`hotkey`** — one Win32 `GetMessageW` loop per registered accelerator
  (hotkey.py:163–185). `RegisterHotKey` is thread-bound, so the thread IS
  the registration.
- **Bounds debounce timers** — a fresh `threading.Timer` per move/resize
  burst (app.py:329); each timer is its own short-lived thread.
- **`content-protection`** — spawned per `shown`/`loaded` event
  (app.py:312–316) so the retry sleeps never block a pywebview callback.
- **`heartbeat-watchdog`** and **`focus-signal`** — housekeeping loops
  (app.py:358, 146) that never touch the core.
- **Executor threads** — workers `asyncio.to_thread` borrows for anything
  blocking.

Crossings INTO the loop. Grep the repo for `call_soon_threadsafe` and
`run_coroutine_threadsafe`: production code has exactly five sites, and
they are the only legal doorways.

| Site | From | What crosses |
|---|---|---|
| capture.py:105 | PortAudio callback | one 2048-sample frame + RMS, via `call_soon_threadsafe(on_frame, …)` |
| api.py:99 | js_api worker | every command coroutine, via `run_coroutine_threadsafe(coro).result(timeout=COMMAND_TIMEOUT_S)` — the worker blocks up to 30 s (api.py:25) for the Result envelope |
| api.py:81 | js_api worker | `cancel_session`, via `call_soon_threadsafe` — fire-and-forget, no result to wait for |
| app.py:256 | hotkey thread | `sink.emit("hotkey:toggle", {})` via `call_soon_threadsafe` |
| app.py:310 | content-protection thread | `sink.emit(…)` via `_emit_from_thread` |

The last two exist because `WebviewEventSink.emit` is loop-only: it calls
`put_nowait` on an `asyncio.Queue`, which is not thread-safe. Foreign
threads never call `emit` directly — they schedule it onto the loop.

Crossings OUT of the loop all go through `asyncio.to_thread`: the four
DPAPI key reads in the machine (machine.py:151, 158, 203, 371 — the
`SettingsReader` protocol documents the contract at machine.py:74),
settings view/patch in the bridge (api.py:116, 120), and `evaluate_js` in
the event pump (events.py:81). Anything that can block leaves the loop; a
stalled DPAPI read on the loop would freeze frame forwarding, event
dispatch, and every timer at once.

One hotkey press, end to end, touches four threads: the `hotkey` thread
sees WM_HOTKEY and posts `hotkey:toggle` onto the loop; the pump thread
(`to_thread`) pushes it into the DOM via `evaluate_js`; React calls
`startSession` over the bridge; a js_api worker hands the coroutine to
the loop and blocks; the loop runs it. Every hop is one of the doorways
above.

## Single-owner state

Inputs arrive on at least four foreign threads, and every one of them
needs to consult session state: is there a live session, has it stopped,
has it errored. Guarding that with locks means every new field needs lock
discipline, and the interleavings that matter here — a session superseded
mid-connect — are the ones lock-based reasoning is worst at.

Instead, `SessionManager` is single-threaded by construction. Every
mutation of `_active`, phases, and flags happens on the loop, so a race
is never "two threads interleave writes" — it is "two callbacks run in
some order on one thread". That question is testable without threads:
tests/test_machine.py drives every race deterministically with gated
fakes, holding one command at its await while another runs to completion.

The price is the to_thread discipline above, and an audio callback that
does its numpy work (downsample, RMS, frame accumulation) on PortAudio's
thread so the loop only ever sees finished frames.

## Check-then-act across an await

The natural way to write "start recording" is: check no session is live,
connect to Deepgram, then install the session. That is a check-then-act
race with an await in the middle: press Record twice quickly and the
first press's connect can resolve AFTER the second press installed its
session — the first press then overwrites the winner, and the newer
session the user actually wants is orphaned while its audio feeds a
ghost.

The rule: claim synchronously, before any suspension point, and
re-validate after every one. In `start_session` the block from
`_require_ticket` through `self._active = session` (machine.py:168–171)
contains no await — the winner is decided at press time, atomically on
the loop. The slow part (the STT connect) runs in the spawned
`_run_record` task, and when its awaits resolve it re-checks
`session.aborted or self._active is not session` (machine.py:282) and
tears its stream down silently if it lost. The frame callback makes the
same check on every frame (machine.py:542): frames for a stale or
stopped session are dropped rather than racing the CloseStream flush.

## The command ticket

Claiming the slot before the first await is not available to
`start_session`, and the comment at machine.py:140–144 says why: the key
reads must fail the COMMAND, not a session. If start installed a session
and then discovered there was no API key, installing it would already
have superseded — killed — whatever was live, just to report a
configuration error. So the slot claim sits after the key reads, and
that reopens the stalled-command race in a different shape.

`get_secret` hits DPAPI, and DPAPI can stall. Concretely: the user
presses Record, its key read stalls; the user gives up, types the
question, and presses Ask; Ask reads its key, installs its session, and
starts streaming an answer. Then the Record press's read resumes.
Without further protection it proceeds to `_supersede()` and destroys
the live answer — an EARLIER command superseding a LATER one, the exact
inversion of latest-wins.

The ticket closes the gap: `_command_seq` (machine.py:145) is bumped
synchronously at command entry, before any await; after the awaits,
`_require_ticket` (machine.py:507) raises `AppError("aborted")` if any
newer command claimed since. `aborted` is a code the frontend maps to a
silent reset, never an error banner (App.tsx:458–459) — the user's newer
action is in charge and needs no apology for winning.

Note the asymmetry: `start_session` claims its ticket as the first
statement (machine.py:150), before its key checks; `ask` claims AFTER
validating its input (machine.py:201). Rule 8 demands that garbage input
— an empty question, one over 8000 characters — has zero effect on
anything live, and the ticket claim is itself an effect: it bumps the
sequence, so an empty Ask that claimed first would make a stalled
legitimate command come back, find its ticket stale, and abort itself
over input that never became a command. Validation is synchronous, so
validating first costs nothing; the claim still precedes every await.

This rule earned its own scar tissue: mutation testing showed `ask`'s
`_require_ticket` call was deletable with all tests green
(docs/TESTING.md, mutation-pass section) — the only ticket test stalled `start_session`, so
`ask`'s copy was never load-bearing. The mirror test now stalls an ask.

## Cancellation is control flow

`_supersede` cancels every task of the losing session (machine.py:624).
`asyncio.CancelledError` is not a failure here — it is the mechanism of
abort, and it must reach the task body unswallowed and unremapped.

Two facts make that safe to rely on. First, since Python 3.8
`CancelledError` inherits from `BaseException`, not `Exception` — so
every broad `except Exception` in this codebase (the connect-failure
classification in `_run_record`, `_quiet_abort`'s suppress, retry.py's
handler) lets cancellation fly through untouched. Broad exception
handling and clean cancellation coexist by language design; what would
break it is `except BaseException`, which never appears in production
code. retry.py:47 still re-raises `CancelledError` explicitly before its
`except Exception` — redundant at runtime, load-bearing as documentation:
abort is never retried, never remapped. Second, task reaping respects it:
the `_spawn` done-callback checks `t.cancelled()` before touching
`t.exception()` (machine.py:521–524), so a cancelled pipeline never gets
reported as an internal error.

`_answer` contains the subtlest cancellation site in the app. It runs
the LLM stream as a child task and waits with
`await asyncio.wait({stream_task})` (machine.py:431) instead of
`await stream_task`. The reason: two different parties cancel here, and
they must not look alike. A watchdog cancels `stream_task` when a
timeout fires (machine.py:416); `_supersede` cancels the pipeline task
that is running `_answer` itself. Awaiting the child directly collapses
both into the same `CancelledError` raised at the same line.
`asyncio.wait` never propagates the child's cancellation — it simply
returns when the task settles — so a watchdog cancel is read off
`stream_task.cancelled()` plus `session.timeout_code` and becomes a
structured timeout error, while an abort arrives as cancellation OF
`_answer`, caught at machine.py:432 only long enough to cancel the child
before re-raising. The `finally` disarms both watchdogs on every exit
path, so no timer outlives the answer it was guarding.

## Idempotency: everything terminal happens exactly once

Terminal transitions have multiple callers by design — done, fail,
abort, and lost-the-race paths all converge — so each one is guarded to
run once:

- **Slot release.** `_release` (machine.py:603) checks and sets
  `session.released`; rule 11 in the comment says why: the next session
  must never supersede a ghost. A session that failed AND was superseded
  releases once, not twice.
- **Finalize joins an in-flight finalize.** `DeepgramStream.finalize`
  (app_core/stt/client.py:142–145) creates its flush task once and every
  subsequent caller awaits the same task. The machine's stop contract
  already refuses a second stop (machine.py:187–190), so this is defense
  in depth for teardown paths — a second CloseStream racing the first on
  a closing socket fabricates errors out of a stop that is succeeding.
- **One error per stream.** `_fail` (machine.py:579) refuses a second
  failure, and `_emit` (machine.py:575) enforces it structurally: once
  `errored` is set, the only event a session may still emit is its own
  `session:error`. The STT error callback additionally refuses after
  `stt_done` (machine.py:562) — once the transcript is final, a late
  socket death must not kill an answer that is already streaming.

The stop-vs-cap race shows what exactly-once looks like across the
process boundary. The 120 s cap fires and begins the stop; the user's
own Stop press lands a beat later and is correctly refused — the session
is already finalizing. Before commit a535ead the frontend treated ANY
refused stop as "session is gone" and cancelled, destroying the answer
to a question the interviewer had just spent two minutes asking. A
refused command is ambiguous between "already being handled" and "truly
gone", and resolving that ambiguity by destruction was the bug. Now
`stop-not-taken` keeps the UI in finalizing with `stopStranded` armed
(App.tsx:175–179), and only if no terminal event arrives within 20 s
(App.tsx:65) does the recovery timer retire the session
(App.tsx:432–442).

## Ordering is a correctness property

Events reach the page through one queue and one pump task
(events.py). `evaluate_js` blocks, so each dispatch runs in
`asyncio.to_thread` — but the pump awaits each dispatch before taking
the next, so exactly one is ever in flight. That serialization is not a
performance compromise; it is the guarantee. Rule 9's "nothing paints
after the error" depends on delivery order as much as on suppression at
the source: an `llm:delta` delivered after `llm:done`, or an
`stt:partial` after `session:error`, repaints a terminal screen.

Batching (commit 956be74) had to be added without bending that. An
answer streams dozens of deltas per second and each `evaluate_js` is a
blocking round trip on a worker thread, so the pump drains whatever is
already queued — up to `MAX_BATCH = 64` (events.py:17) — into ONE call.
The queue is FIFO, the drain preserves arrival order, and the page
dispatches the array in order: batching changes hop count, never order.
When a batch fails as a unit, the retry replays it event by event, still
in order, so one unserializable payload cannot take its 63 neighbours
with it — a lost `llm:done` strands the UI in "Generating answer…"
forever.

The pump also parks rather than drops: `while self._window is None`
(events.py:39) holds events emitted before the window exists. That park
is the site of the worst bug this app shipped. `sink.attach(window)` was
missing from app.py for four commits (fixed in 1536a0c): the pump parked
forever, and nothing the core emitted — no transcript, no answer, no
errors — ever reached the page, while every command worked fine because
js_api is a separate channel. Both test suites were green the whole
time, because each side mocked the other. tests/test_app_wiring.py
(docs/TESTING.md, the test_app_wiring.py section) now builds the real `App` and pins the seam.

## The locks that remain

Three locks exist, all shell-side, all protecting state that foreign
threads must touch and that cannot live on the loop:

- **The settings RLock** (settings.py:42). Writers run on three
  different kinds of thread: `patch` on executor threads, the bounds
  debounce timer, and the pywebview close handler. Each does a
  read-modify-write of the whole settings dict; unserialized, one
  writer's fields — a just-saved API key, the resume — vanish from both
  disk and the in-memory cache. Readers take no lock, and the reason is
  the write discipline: writers build a fresh dict and `_save` rebinds
  `self._data` only after `os.replace` lands (settings.py:121). Writers
  never mutate the live dict, and rebinding a reference is atomic under
  the GIL, so a reader sees the old dict or the new one — never a
  half-mutated structure. The residual window is field-level: a reader
  that dereferences `self._data` more than once (`answer_config` reads
  four fields) can mix pre- and post-save values if a save lands
  mid-read. That is accepted — the worst case is one prompt built from a
  resume and a style saved milliseconds apart.
- **The hotkey lock** (hotkey.py:149). Register/unregister is a thread
  handshake — post WM_QUIT to the old message loop, join it, start a new
  thread, wait on its ready event. Two concurrent registers interleaving
  that dance would leak a message loop or record the wrong thread id.
- **The protection lock + sequence** (app.py:288–301). `shown` and
  `loaded` both spawn protection attempts, so attempts overlap; each is
  stamped, and a slow loser that finishes after a newer attempt already
  reported discards its own verdict rather than overwriting the fresher
  one with a stale "failed".

Some shared scalars deliberately go without: `api.last_heartbeat` is
written by js_api workers and read by the watchdog thread (app.py:365)
with no lock. A float attribute assignment is atomic, and the worst
stale read costs one reload attempt inside a 10 s cooldown. The
discipline is not "lock everything" — it is knowing which shared state
has invariants spanning more than one operation.

## How to reason about a change here

Before you touch this code, answer these — in order:

1. **Which thread runs this line?** If you cannot say, trace the doorway
   it arrived through; every entry into the core is one of the five
   crossings in the map.
2. **Adding an await?** Everything can change while you are suspended:
   the active session, its phase, its flags. Re-check ownership after
   the await and assume you lost. If your command can stall there, the
   ticket must cover it — claim before the await, after validation.
3. **Adding a blocking call on the loop?** Don't. `asyncio.to_thread`
   it. One stalled read on the loop stops frame forwarding, event
   dispatch, and every timer simultaneously — the DPAPI stall behind the
   command ticket is what that looks like in practice.
4. **Adding a thread?** If it touches session state, it posts through
   `call_soon_threadsafe` — no exceptions. If it writes settings, it
   takes the RLock. If it emits an event, it schedules `sink.emit` onto
   the loop; `emit` itself is loop-only.
5. **Catching exceptions?** Broad `except Exception` is fine —
   cancellation passes it by design. `except BaseException`, a swallowed
   `CancelledError`, or remapping cancellation into an error breaks
   abort for every path above you.
6. **Adding a terminal transition?** Route it through `_fail` /
   `_release` so the once-only guards hold. Never emit directly after
   setting `errored`; `_emit`'s structural check exists because
   suppression at every call site was not trustworthy.
7. **Refusing a command?** Decide what the caller should do with the
   refusal before you write it. "Refused" meaning both "already being
   handled" and "gone" is the ambiguity that destroyed answers in the
   stop-vs-cap race.
8. **Testing it?** Test the interleaving, not the schedule. The fakes in
   tests/test_machine.py gate their awaits so you can hold one command
   mid-flight, run another to completion, then release — the race
   becomes a deterministic ordering of callbacks, which is exactly what
   the single-owner model promises.
