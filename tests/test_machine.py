"""Every §5 invariant of the session state machine, exercised with fakes.

Includes the races that motivated the rules: double-record during connect,
ask-over-recording, cancel-during-stop, stop-during-connect, late STT death
after finalize.
"""

from __future__ import annotations

import asyncio

import pytest

from app_core.errors import AppError
from app_core.llm.base import ProviderFailure
from app_core.session.machine import Timeouts
from tests.conftest import Harness


async def start_and_connect(h: Harness) -> str:
    sid = await h.machine.start_session()
    await h.settle()
    assert h.stt.last.connected
    return sid


class TestHappyPath:
    async def test_record_stop_answer(self, harness: Harness) -> None:
        h = harness
        sid = await start_and_connect(h)
        assert h.audio.starts == 1
        h.audio.feed(b"frame1")
        h.audio.feed(b"frame2")
        assert h.stt.last.frames == [b"frame1", b"frame2"]
        h.stt.last.on_update("tell me about", False)
        await h.machine.stop_session(sid)
        await h.drain()
        done = h.sink.named("llm:done")
        assert len(done) == 1
        assert done[0]["sessionId"] == sid
        assert done[0]["answer"] == "Hello world."
        assert done[0]["transcript"] == "What is your greatest strength?"
        metrics = done[0]["metrics"]
        assert metrics["totalMs"] >= metrics["firstTokenMs"] >= 0
        assert metrics["sttFinalizeMs"] >= 0
        assert [p["delta"] for p in h.sink.named("llm:delta")] == ["Hello ", "world."]
        assert {"sessionId": sid, "text": "tell me about", "isFinal": False} in h.sink.named(
            "stt:partial"
        )
        assert h.audio.stops >= 1

    async def test_audio_level_events_flow_while_recording(self, harness: Harness) -> None:
        h = harness
        await start_and_connect(h)
        h.audio.feed(b"f", rms=0.42)
        levels = h.sink.named("audio:level")
        assert levels and levels[0]["rms"] == 0.42

    async def test_stop_warms_the_provider_origin(self, harness: Harness) -> None:
        h = harness
        sid = await start_and_connect(h)
        warms_before = len(h.warmer.warmed)
        await h.machine.stop_session(sid)
        assert len(h.warmer.warmed) == warms_before + 1
        await h.drain()


class TestKeyChecks:
    async def test_missing_deepgram_key(self, harness: Harness) -> None:
        harness.settings.secrets.pop("deepgram")
        with pytest.raises(AppError) as info:
            await harness.machine.start_session()
        assert info.value.code == "no_stt_key"

    async def test_missing_llm_key_on_start(self, harness: Harness) -> None:
        harness.settings.secrets.pop("fake")
        with pytest.raises(AppError) as info:
            await harness.machine.start_session()
        assert info.value.code == "no_llm_key"

    async def test_missing_llm_key_on_ask(self, harness: Harness) -> None:
        harness.settings.secrets.pop("fake")
        with pytest.raises(AppError) as info:
            await harness.machine.ask("question")
        assert info.value.code == "no_llm_key"


class TestSupersession:
    async def test_new_start_aborts_active_and_drops_its_events(
        self, harness: Harness
    ) -> None:
        h = harness
        sid1 = await start_and_connect(h)
        stt1 = h.stt.last
        sid2 = await h.machine.start_session()
        assert sid2 != sid1
        await h.settle()
        assert stt1.aborted
        # Rule 1: events from the superseded session are dropped by id — and
        # its socket death (caused by the abort) is never reported.
        stt1.on_update("late text", False)
        stt1.on_error(AppError("stt_error", "socket died"))
        await h.settle()
        assert all(p.get("text") != "late text" for p in h.sink.named("stt:partial"))
        assert h.sink.named("session:error") == []

    async def test_ask_over_recording_supersedes_silently(self, harness: Harness) -> None:
        h = harness
        await start_and_connect(h)
        stt1 = h.stt.last
        sid2 = await h.machine.ask("typed question")
        await h.drain()
        assert stt1.aborted
        assert h.sink.named("session:error") == []
        done = h.sink.named("llm:done")
        assert len(done) == 1 and done[0]["sessionId"] == sid2

    async def test_ask_over_streaming_answer_supersedes(self, harness: Harness) -> None:
        h = harness
        h.provider.post_delta_hang = asyncio.Event()  # first answer hangs mid-stream
        await h.machine.ask("first")
        await asyncio.sleep(0.05)
        assert h.sink.named("llm:delta")  # first answer started painting
        h.provider.post_delta_hang = None
        sid2 = await h.machine.ask("second")
        await h.drain()
        done = h.sink.named("llm:done")
        assert [d["sessionId"] for d in done] == [sid2]  # rule 1: old done dropped


class TestLatestStartWins:
    async def test_second_start_during_first_connect_wins(self, harness: Harness) -> None:
        h = harness
        gate = asyncio.Event()
        h.stt.prepare = lambda s: setattr(s, "connect_gate", gate)
        sid1 = await h.machine.start_session()
        await h.settle()
        h.stt.prepare = None  # second stream connects immediately
        sid2 = await h.machine.start_session()
        await h.settle()
        # Rule 2: the loser resolves late, tears down silently, never
        # installs itself over the winner.
        gate.set()
        await h.settle()
        assert h.stt.streams[0].aborted
        assert h.sink.named("session:error") == []
        # Frames route to the winner only.
        h.audio.feed(b"frame")
        assert h.stt.streams[1].frames == [b"frame"]
        assert h.stt.streams[0].frames == []
        await h.machine.stop_session(sid2)
        await h.drain()
        assert [d["sessionId"] for d in h.sink.named("llm:done")] == [sid2]
        assert sid1 != sid2

    async def test_connect_failure_after_losing_is_silent(self, harness: Harness) -> None:
        h = harness
        gate = asyncio.Event()

        def prep(stream: object) -> None:
            stream.connect_gate = gate  # type: ignore[attr-defined]
            stream.connect_error = AppError("stt_connect", "refused")  # type: ignore[attr-defined]

        h.stt.prepare = prep
        await h.machine.start_session()
        await h.settle()
        h.stt.prepare = None
        sid2 = await h.machine.start_session()
        gate.set()
        await h.drain(0.3)
        # The loser's connect failure must not surface: only sid2 is live.
        assert h.sink.named("session:error") == []
        await h.machine.stop_session(sid2)
        await h.drain()


class TestStopContract:
    async def test_unknown_id_not_taken(self, harness: Harness) -> None:
        with pytest.raises(AppError):
            await harness.machine.stop_session("s999")

    async def test_stop_during_connect_not_taken(self, harness: Harness) -> None:
        h = harness
        gate = asyncio.Event()
        h.stt.prepare = lambda s: setattr(s, "connect_gate", gate)
        sid = await h.machine.start_session()
        with pytest.raises(AppError):
            await h.machine.stop_session(sid)
        gate.set()
        await h.settle()

    async def test_second_stop_while_first_runs_not_taken(self, harness: Harness) -> None:
        h = harness
        finalize_gate = asyncio.Event()
        h.stt.prepare = lambda s: setattr(s, "finalize_gate", finalize_gate)
        sid = await start_and_connect(h)
        await h.machine.stop_session(sid)
        with pytest.raises(AppError):
            await h.machine.stop_session(sid)
        finalize_gate.set()
        await h.drain()

    async def test_stop_after_completion_not_taken(self, harness: Harness) -> None:
        h = harness
        sid = await start_and_connect(h)
        await h.machine.stop_session(sid)
        await h.drain()
        with pytest.raises(AppError):
            await h.machine.stop_session(sid)

    async def test_stop_after_error_teardown_not_taken(self, harness: Harness) -> None:
        h = harness
        sid = await start_and_connect(h)
        h.stt.last.on_error(AppError("stt_error", "died"))
        await h.settle()
        with pytest.raises(AppError):
            await h.machine.stop_session(sid)

    async def test_not_taken_emits_no_events(self, harness: Harness) -> None:
        h = harness
        before = len(h.sink.events)
        with pytest.raises(AppError):
            await h.machine.stop_session("s404")
        assert len(h.sink.events) == before


class TestAudioRouting:
    async def test_frames_after_stop_requested_are_dropped(self, harness: Harness) -> None:
        h = harness
        finalize_gate = asyncio.Event()
        h.stt.prepare = lambda s: setattr(s, "finalize_gate", finalize_gate)
        sid = await start_and_connect(h)
        callback = h.audio.on_frame
        assert callback is not None
        h.audio.feed(b"good")
        await h.machine.stop_session(sid)
        # Simulate a frame already in flight when stop landed — it would race
        # the CloseStream flush (rule 4).
        callback(b"late", 0.1)
        assert h.stt.last.frames == [b"good"]
        finalize_gate.set()
        await h.drain()

    async def test_frames_for_stale_session_dropped(self, harness: Harness) -> None:
        h = harness
        await start_and_connect(h)
        old_callback = h.audio.on_frame
        assert old_callback is not None
        await h.machine.start_session()
        await h.settle()
        old_callback(b"stale", 0.1)
        assert h.stt.streams[0].frames == []
        assert h.stt.streams[1].frames == []


class TestSttErrorPolicy:
    async def test_mid_recording_death_one_error_and_teardown(
        self, harness: Harness
    ) -> None:
        h = harness
        sid = await start_and_connect(h)
        h.stt.last.on_error(AppError("stt_error", "Deepgram reported an error: NET-0001"))
        h.stt.last.on_error(AppError("stt_error", "second report"))
        await h.settle()
        errors = h.sink.named("session:error")
        assert len(errors) == 1  # rule 6: one error per stream
        assert errors[0]["error"]["code"] == "stt_error"
        assert errors[0]["sessionId"] == sid
        assert h.audio.stops >= 1
        assert h.sink.named("llm:done") == []

    async def test_late_death_after_finalize_never_kills_streaming_answer(
        self, harness: Harness
    ) -> None:
        h = harness
        h.provider.post_delta_hang = asyncio.Event()
        sid = await start_and_connect(h)
        stt = h.stt.last
        await h.machine.stop_session(sid)
        await asyncio.sleep(0.1)  # transcript finalized; first delta painted
        stt.on_error(AppError("stt_error", "late socket close"))
        h.provider.post_delta_hang.set()
        await h.drain()
        assert h.sink.named("session:error") == []  # rule 5, second half
        assert len(h.sink.named("llm:done")) == 1

    async def test_connect_failure_surfaces_stt_connect(self, harness: Harness) -> None:
        h = harness
        h.stt.prepare = lambda s: setattr(
            s, "connect_error", AppError("stt_connect", "Could not connect to Deepgram.")
        )
        await h.machine.start_session()
        await h.drain(0.3)
        errors = h.sink.named("session:error")
        assert len(errors) == 1 and errors[0]["error"]["code"] == "stt_connect"


class TestEmptyTranscript:
    async def test_whitespace_transcript_is_no_speech_never_an_llm_call(
        self, harness: Harness
    ) -> None:
        h = harness
        h.stt.prepare = lambda s: setattr(s, "transcript", "   \n  ")
        sid = await start_and_connect(h)
        await h.machine.stop_session(sid)
        await h.drain(0.5)
        errors = h.sink.named("session:error")
        assert len(errors) == 1 and errors[0]["error"]["code"] == "no_speech"
        assert "call audio" in errors[0]["error"]["message"]
        assert h.provider.stream_calls == 0  # rule 7


class TestAskPath:
    async def test_garbage_input_never_kills_a_live_session(self, harness: Harness) -> None:
        h = harness
        sid = await start_and_connect(h)
        stt = h.stt.last
        with pytest.raises(AppError):
            await h.machine.ask("   ")
        with pytest.raises(AppError):
            await h.machine.ask("x" * 8001)
        assert not stt.aborted  # recording still live
        await h.machine.stop_session(sid)  # still stoppable
        await h.drain()

    async def test_ask_resolves_id_before_any_event(self, harness: Harness) -> None:
        h = harness
        sid = await h.machine.ask("What is DI?")
        assert sid
        assert h.sink.events == []  # nothing fired before the id resolved
        await h.drain()

    async def test_ask_event_shape_and_metrics(self, harness: Harness) -> None:
        h = harness
        sid = await h.machine.ask("  What is dependency injection?  ")
        await h.drain()
        events = h.sink.for_session(sid)
        assert events[0] == (
            "stt:partial",
            {
                "sessionId": sid,
                "text": "What is dependency injection?",  # trimmed
                "isFinal": True,
            },
        )
        done = h.sink.named("llm:done")[0]
        assert done["metrics"]["sttFinalizeMs"] == 0  # exactly 0 — no STT stage
        assert done["transcript"] == "What is dependency injection?"

    async def test_8000_chars_exactly_is_accepted(self, harness: Harness) -> None:
        h = harness
        await h.machine.ask("x" * 8000)
        await h.drain()
        assert len(h.sink.named("llm:done")) == 1


class TestTimeoutInterplay:
    async def test_first_token_timeout(self, harness: Harness) -> None:
        h = harness
        h.provider.pre_delta_hang = asyncio.Event()  # never yields
        await h.machine.ask("q")
        await h.drain()
        errors = h.sink.named("session:error")
        assert len(errors) == 1
        assert errors[0]["error"]["code"] == "llm_first_token_timeout"
        assert h.sink.named("llm:delta") == []
        assert h.sink.named("llm:done") == []

    async def test_total_timeout_after_deltas(self, harness: Harness) -> None:
        h = harness
        h.provider.post_delta_hang = asyncio.Event()  # hangs after first delta
        await h.machine.ask("q")
        await h.drain()
        errors = h.sink.named("session:error")
        assert len(errors) == 1 and errors[0]["error"]["code"] == "llm_timeout"
        # The first delta was disarmed the first-token timer and painted;
        # nothing painted after the timeout fired.
        assert [d["delta"] for d in h.sink.named("llm:delta")] == ["Hello "]
        assert h.sink.named("llm:done") == []

    async def test_first_delta_disarms_first_token_timer(self, harness: Harness) -> None:
        h = harness
        # Deltas arrive slowly but within total budget; first-token timer
        # must not fire once the first delta landed.
        h.provider.delta_gap_s = 0.15  # 2 deltas * 0.15 = 0.3 < total 0.8
        await h.machine.ask("q")
        await h.drain()
        assert h.sink.named("session:error") == []
        assert len(h.sink.named("llm:done")) == 1


class TestProviderFailures:
    async def test_status_error_surfaces_classified(self, harness: Harness) -> None:
        h = harness
        h.provider.failures = [ProviderFailure("status", status=401)]
        await h.machine.ask("q")
        await h.drain()
        errors = h.sink.named("session:error")
        assert len(errors) == 1 and errors[0]["error"]["code"] == "llm_auth"

    async def test_connect_failure_retried_once_then_succeeds(
        self, harness: Harness
    ) -> None:
        h = harness
        h.provider.failures = [ProviderFailure("connect")]
        await h.machine.ask("q")
        await h.drain()
        assert h.provider.stream_calls == 2
        assert len(h.sink.named("llm:done")) == 1
        assert h.sink.named("session:error") == []


class TestCancel:
    async def test_cancel_is_silent_and_releases_slot(self, harness: Harness) -> None:
        h = harness
        h.provider.post_delta_hang = asyncio.Event()
        sid = await h.machine.ask("q")
        await asyncio.sleep(0.05)
        h.machine.cancel_session(sid)
        await h.settle()
        frozen = len(h.sink.events)
        await asyncio.sleep(0.1)
        assert len(h.sink.events) == frozen  # rule 10: no done, no error, nothing
        # Slot released: a fresh session starts cleanly (rule 11).
        h.provider.post_delta_hang = None
        await h.machine.ask("next")
        await h.drain()
        assert len(h.sink.named("llm:done")) == 1

    async def test_cancel_invalid_id_does_nothing(self, harness: Harness) -> None:
        h = harness
        sid = await start_and_connect(h)
        h.machine.cancel_session("s999")
        await h.settle()
        assert not h.stt.last.aborted
        await h.machine.stop_session(sid)
        await h.drain()

    async def test_cancel_during_stop_is_silent(self, harness: Harness) -> None:
        h = harness
        finalize_gate = asyncio.Event()
        h.stt.prepare = lambda s: setattr(s, "finalize_gate", finalize_gate)
        sid = await start_and_connect(h)
        await h.machine.stop_session(sid)
        h.machine.cancel_session(sid)
        finalize_gate.set()
        await h.settle(80)
        assert h.sink.named("llm:done") == []
        assert h.sink.named("session:error") == []


class TestSlotRelease:
    async def test_after_error_next_session_starts_clean(self, harness: Harness) -> None:
        h = harness
        await start_and_connect(h)
        h.stt.last.on_error(AppError("stt_error", "died"))
        await h.settle()
        sid2 = await start_and_connect(h)
        await h.machine.stop_session(sid2)
        await h.drain()
        assert len(h.sink.named("llm:done")) == 1

    async def test_after_done_next_session_starts_clean(self, harness: Harness) -> None:
        h = harness
        sid1 = await start_and_connect(h)
        await h.machine.stop_session(sid1)
        await h.drain()
        sid2 = await start_and_connect(h)
        await h.machine.stop_session(sid2)
        await h.drain()
        assert len(h.sink.named("llm:done")) == 2


class TestRecordCap:
    async def test_cap_auto_stops_and_answers_normally(self) -> None:
        h = Harness(
            Timeouts(
                stt_finalize_s=0.5,
                llm_first_token_s=0.35,
                llm_total_s=0.8,
                record_cap_s=0.15,
            )
        )
        sid = await start_and_connect(h)
        await asyncio.sleep(0.25)
        await h.drain()
        assert [p["sessionId"] for p in h.sink.named("session:autostopped")] == [sid]
        done = h.sink.named("llm:done")
        assert len(done) == 1 and done[0]["sessionId"] == sid

    async def test_manual_stop_cancels_the_cap(self, harness: Harness) -> None:
        h = harness
        sid = await start_and_connect(h)
        await h.machine.stop_session(sid)
        await h.drain()
        assert h.sink.named("session:autostopped") == []
