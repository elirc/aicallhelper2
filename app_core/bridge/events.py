"""Core -> frontend event dispatch.

Events become a DOM CustomEvent named `app:event` with `{ name, payload }`.
Dispatch is serialized through ONE queue on the loop, so events for a
session arrive in the order the core emitted them. `evaluate_js` blocks on
the webview, so each dispatch runs in a worker thread — one at a time — and
is BOUNDED: pywebview's evaluate_js waits on a semaphore that is released
only by the page's reply, so a renderer that dies mid-answer would otherwise
wedge the pump (and every later event, for every later session) for the
life of the process.

A timed-out dispatch is an UNCERTAIN delivery — the abandoned call may still
execute later — so ordering is only guaranteed while nothing times out. The
page gets what it needs to cope: every payload carries `seq` (strictly
increasing per emit, unchanged on a retry) and `pageGen` (the renderer page
generation it was dispatched to), and the recovery path re-sends only the
RESERVED events (terminal session events, protection/core status), which the
page applies idempotently. Streaming events from an uncertain batch are
dropped rather than duplicated: `llm:done` carries the full answer.

The queue is bounded. Past `MAX_PENDING`, expendable events (audio levels,
interim transcripts, then answer deltas) are shed oldest-first; reserved
events are never shed. `audio:level` is coalesced latest-wins per session
while pending, keeping its session id and its original queue position/seq.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import threading
from collections import deque
from typing import Any, Literal

# Cap so one evaluate_js string stays small even if the queue backs up.
MAX_BATCH = 64
# Longer than any healthy page round trip, shorter than the renderer
# watchdog's reload cycle so a dead page is reloaded before the backlog rots.
DISPATCH_TIMEOUT_S = 15.0
# Queue bound; reserved events may exceed it (they are few and must arrive).
MAX_PENDING = 2_000
# Abandoned (timed-out) dispatch threads allowed at once PER PAGE GENERATION.
# Past this the pump stops spawning threads against a renderer that is plainly
# not answering, until a reload starts a new generation. A call against a
# crashed page may never return, so a global count would ratchet shut forever.
MAX_HUNG_DISPATCHES = 4
# Pause between attempts while the renderer is known to be unavailable.
RENDERER_BACKOFF_S = 0.5

# Events whose loss strands the UI or hides a safety verdict. Never shed,
# re-sent after an uncertain delivery (the page treats duplicates as no-ops).
RESERVED_EVENTS = frozenset(
    {
        "llm:done",
        "session:error",
        "session:autostopped",
        "protection:ok",
        "protection:failed",
        "core:ready",
        "core:failed",
    }
)

# "raised": the page refused fast (script not run) - safe to retry per event.
# "timeout": uncertain - the abandoned call may still run later.
DispatchOutcome = Literal["ok", "serialize", "raised", "timeout"]
EvalOutcome = Literal["ok", "raised", "timeout"]


class _Pending:
    """One queued event. Mutable so a coalesced audio level can update its
    payload in place without losing its queue position or seq."""

    __slots__ = ("name", "payload", "sent")

    def __init__(self, name: str, payload: dict[str, object]) -> None:
        self.name = name
        self.payload = payload
        self.sent = False


def _shed_rank(event: _Pending) -> int:
    """Shedding rank: 0 = shed first, higher = later, -1 = never shed."""
    if event.name in RESERVED_EVENTS:
        return -1
    if event.name == "audio:level":
        return 0
    if event.name == "stt:partial" and event.payload.get("isFinal") is not True:
        return 1
    return 2


class WebviewEventSink:
    def __init__(self) -> None:
        self._queue: deque[_Pending] = deque()
        self._wakeup: asyncio.Event | None = None
        self._window: Any = None
        self._task: asyncio.Task[None] | None = None
        self._seq = 0
        # session id -> its still-pending audio:level entry (for coalescing)
        self._pending_levels: dict[object, _Pending] = {}
        self._hung: dict[int, int] = {}  # page generation -> abandoned calls
        self._hung_lock = threading.Lock()
        self.page_generation = 0
        self.shed = 0  # diagnostic: events dropped by the bound

    def attach(self, window: Any) -> None:
        self._window = window

    def new_page(self) -> int:
        """A new renderer page loaded; events dispatched from now on are
        stamped with the new generation."""
        self.page_generation += 1
        return self.page_generation

    def start(self) -> None:
        if self._task is None:
            self._wakeup = asyncio.Event()
            if self._queue:
                self._wakeup.set()
            self._task = asyncio.get_running_loop().create_task(self._pump())

    def emit(self, name: str, payload: dict[str, object]) -> None:
        if name == "audio:level":
            pending = self._pending_levels.get(payload.get("sessionId"))
            if pending is not None and not pending.sent:
                # Latest level wins; seq and position stay the original's.
                pending.payload = {**payload, "seq": pending.payload["seq"]}
                return
        self._seq += 1
        event = _Pending(name, {**payload, "seq": self._seq})
        self._queue.append(event)
        if name == "audio:level":
            self._pending_levels[payload.get("sessionId")] = event
        if len(self._queue) > MAX_PENDING:
            self._shed()
        if self._wakeup is not None:
            self._wakeup.set()

    def pending(self) -> list[tuple[str, dict[str, object]]]:
        """Snapshot of the queue (diagnostics and tests)."""
        return [(e.name, e.payload) for e in self._queue]

    def _shed(self) -> None:
        """Drop expendable events, lowest rank and oldest first, until the
        queue is back within bound (or only reserved events remain)."""
        excess = len(self._queue) - MAX_PENDING
        for rank in (0, 1, 2):
            if excess <= 0:
                break
            kept: deque[_Pending] = deque()
            for event in self._queue:
                if excess > 0 and _shed_rank(event) == rank:
                    excess -= 1
                    self.shed += 1
                    self._forget(event)
                    continue
                kept.append(event)
            self._queue = kept

    def _forget(self, event: _Pending) -> None:
        if event.name == "audio:level":
            key = event.payload.get("sessionId")
            if self._pending_levels.get(key) is event:
                del self._pending_levels[key]

    def _take(self) -> _Pending:
        event = self._queue.popleft()
        event.sent = True
        self._forget(event)
        return event

    async def _pump(self) -> None:
        assert self._wakeup is not None
        while True:
            while not self._queue:
                self._wakeup.clear()
                await self._wakeup.wait()
            while self._window is None:
                await asyncio.sleep(0.05)  # window not created yet — hold, don't drop
            # Drain whatever else is already queued into ONE evaluate_js.
            # Each call is a blocking round trip on a worker thread, and an
            # answer streams dozens of deltas per second — batching keeps
            # those hops off the latency budget. Order is preserved: the
            # queue is FIFO and the page dispatches the array in order.
            batch: list[_Pending] = []
            while self._queue and len(batch) < MAX_BATCH:
                batch.append(self._take())
            outcome = await self._dispatch(batch)
            if outcome == "ok":
                continue
            if outcome in ("serialize", "raised"):
                # Nothing ran. Split so one unserializable or refused event
                # cannot take its neighbours with it — a lost llm:done would
                # strand the UI in "Generating answer…" forever.
                for index, event in enumerate(batch):
                    single = await self._dispatch([event])
                    if single in ("ok", "serialize"):
                        continue  # delivered, or poison that costs only itself
                    # The renderer itself is failing: keep what must arrive.
                    self._requeue_reserved(batch[index:])
                    await asyncio.sleep(RENDERER_BACKOFF_S)
                    break
                continue
            # Timed out (or known hung): delivery is UNCERTAIN. Re-queue only
            # the reserved events, at the FRONT so they keep their order;
            # streaming events are dropped rather than risk being displayed
            # twice. Back off before trying again — never 64 more timeouts.
            self._requeue_reserved(batch)
            await asyncio.sleep(RENDERER_BACKOFF_S)

    def _requeue_reserved(self, batch: list[_Pending]) -> None:
        reserved = [e for e in batch if e.name in RESERVED_EVENTS]
        for event in reversed(reserved):
            event.sent = False
            self._queue.appendleft(event)

    async def _dispatch(self, batch: list[_Pending]) -> DispatchOutcome:
        """Send one batch and classify the result (see DispatchOutcome)."""
        generation = self.page_generation
        try:
            details = json.dumps(
                [
                    {"name": e.name, "payload": {**e.payload, "pageGen": generation}}
                    for e in batch
                ],
                ensure_ascii=False,
                allow_nan=False,  # NaN/Infinity are not JSON; JSON.parse rejects them
            )
        except Exception:
            return "serialize"
        # Double-encode so arbitrary payload text (quotes, backslashes,
        # newlines, U+2028) survives the trip through evaluate_js as a JS
        # string: the outer dump escapes every non-ASCII character.
        code = (
            f"JSON.parse({json.dumps(details)}).forEach("
            "function(d){window.dispatchEvent("
            "new CustomEvent('app:event',{detail:d}))})"
        )
        with self._hung_lock:
            if self._hung.get(generation, 0) >= MAX_HUNG_DISPATCHES:
                return "timeout"  # do not pile more threads onto a dead page

        def on_hung(delta: int) -> None:
            self._on_hung(generation, delta)

        return await _evaluate_bounded(self._window, code, DISPATCH_TIMEOUT_S, on_hung)

    def _on_hung(self, generation: int, delta: int) -> None:
        with self._hung_lock:
            count = self._hung.get(generation, 0) + delta
            if count > 0:
                self._hung[generation] = count
            else:
                self._hung.pop(generation, None)


async def _evaluate_bounded(
    window: Any,
    code: str,
    timeout_s: float,
    on_hung: Any = None,
) -> EvalOutcome:
    """Run window.evaluate_js on a throwaway daemon thread with a deadline.

    A dedicated thread (not the shared to_thread executor) so a call that
    never returns can neither poison the executor nor block interpreter
    exit. A dying webview must not kill the core loop: every failure is
    reported as an outcome, never raised. `on_hung(+1)` fires when the deadline
    abandons a still-running call and `on_hung(-1)` when that call finally
    returns, so the caller can count threads stuck in the renderer.
    """
    loop = asyncio.get_running_loop()
    done: asyncio.Future[EvalOutcome] = loop.create_future()
    state = {"abandoned": False, "finished": False}
    state_lock = threading.Lock()

    def run() -> None:
        result: EvalOutcome
        try:
            window.evaluate_js(code)
            result = "ok"
        except Exception:
            result = "raised"
        with state_lock:
            abandoned = state["abandoned"]
            state["finished"] = True
        if abandoned and on_hung is not None:
            on_hung(-1)
        with contextlib.suppress(RuntimeError):  # loop already closed at shutdown
            loop.call_soon_threadsafe(_settle, done, result)

    threading.Thread(target=run, name="event-dispatch", daemon=True).start()
    try:
        return await asyncio.wait_for(done, timeout_s)
    except TimeoutError:
        with state_lock:
            still_running = not state["finished"]
            state["abandoned"] = still_running
        if still_running and on_hung is not None:
            on_hung(+1)
        return "timeout"


def _settle(future: asyncio.Future[EvalOutcome], result: EvalOutcome) -> None:
    if not future.done():
        future.set_result(result)
