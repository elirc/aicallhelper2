"""DeepgramStream against a scripted loopback WebSocket server.

Wire-level realism where it pays: subprotocol auth, CloseStream flush,
premature-close classification, keepalive suppression after close. Loopback
only — no external network.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import cast

import websockets
from websockets.asyncio.server import Server, ServerConnection
from websockets.typing import Subprotocol

from app_core.errors import AppError
from app_core.stt.client import DeepgramStream

Handler = Callable[[ServerConnection], Awaitable[None]]


class ServerBox:
    def __init__(self, server: Server, url: str) -> None:
        self.server = server
        self.url = url


async def serve(handler: Handler) -> ServerBox:
    server = await websockets.serve(
        handler, "127.0.0.1", 0, subprotocols=[cast(Subprotocol, "token")]
    )
    port = server.sockets[0].getsockname()[1]  # type: ignore[index]
    return ServerBox(server, f"ws://127.0.0.1:{port}")


def results_frame(text: str, is_final: bool) -> str:
    return json.dumps(
        {
            "type": "Results",
            "is_final": is_final,
            "channel": {"alternatives": [{"transcript": text}]},
        }
    )


class Recorder:
    def __init__(self) -> None:
        self.updates: list[tuple[str, bool]] = []
        self.errors: list[AppError] = []

    def on_update(self, text: str, is_final: bool) -> None:
        self.updates.append((text, is_final))

    def on_error(self, err: AppError) -> None:
        self.errors.append(err)


def make_stream(url: str, rec: Recorder, **kwargs: float) -> DeepgramStream:
    return DeepgramStream("test-key", rec.on_update, rec.on_error, url=url, **kwargs)


class TestTranscription:
    async def test_interim_then_final_updates_and_closestream_flush(self) -> None:
        got_audio: list[bytes] = []

        async def handler(ws: ServerConnection) -> None:
            assert ws.subprotocol == "token"
            await ws.send(results_frame("tell me", False))
            async for message in ws:
                if isinstance(message, bytes):
                    got_audio.append(message)
                    continue
                data = json.loads(message)
                if data.get("type") == "CloseStream":
                    # Server flushes its held-back tail, then closes.
                    await ws.send(results_frame("Tell me about yourself.", True))
                    await ws.close()
                    return

        box = await serve(handler)
        try:
            rec = Recorder()
            stream = make_stream(box.url, rec)
            await stream.connect()
            stream.send(b"\x00\x01" * 100)
            await asyncio.sleep(0.1)
            transcript = await stream.finalize()
            assert transcript == "Tell me about yourself."
            assert ("tell me", False) in rec.updates
            assert ("Tell me about yourself.", True) in rec.updates
            assert rec.errors == []
            assert got_audio == [b"\x00\x01" * 100]
        finally:
            box.server.close()

    async def test_preopen_frames_buffered_and_flushed_in_order(self) -> None:
        got_audio: list[bytes] = []
        release = asyncio.Event()

        async def handler(ws: ServerConnection) -> None:
            async for message in ws:
                if isinstance(message, bytes):
                    got_audio.append(message)
                    if len(got_audio) == 3:
                        release.set()
                elif json.loads(message).get("type") == "CloseStream":
                    await ws.close()
                    return

        box = await serve(handler)
        try:
            rec = Recorder()
            stream = make_stream(box.url, rec)
            # Frames sent BEFORE connect: buffered, then flushed on open.
            stream.send(b"one")
            stream.send(b"two")
            await stream.connect()
            stream.send(b"three")
            await asyncio.wait_for(release.wait(), 2.0)
            assert got_audio == [b"one", b"two", b"three"]
            await stream.finalize()
        finally:
            box.server.close()

    async def test_malformed_frames_ignored_never_crash(self) -> None:
        async def handler(ws: ServerConnection) -> None:
            await ws.send("{not json")
            await ws.send(json.dumps({"type": "Metadata"}))
            await ws.send(json.dumps({"type": "Results", "channel": None}))
            await ws.send(results_frame("real text", True))
            async for message in ws:
                if isinstance(message, str) and json.loads(message).get("type") == "CloseStream":
                    await ws.close()
                    return

        box = await serve(handler)
        try:
            rec = Recorder()
            stream = make_stream(box.url, rec)
            await stream.connect()
            await asyncio.sleep(0.1)
            transcript = await stream.finalize()
            assert transcript == "real text"
            assert rec.errors == []
        finally:
            box.server.close()


class TestFailures:
    async def test_close_before_any_results_is_a_connect_failure(self) -> None:
        async def handler(ws: ServerConnection) -> None:
            # Deepgram-style bad-key rejection: close 1008 with DATA reason,
            # no error frame.
            await ws.close(code=1008, reason="DATA-0001")

        box = await serve(handler)
        try:
            rec = Recorder()
            stream = make_stream(box.url, rec)
            await stream.connect()
            await asyncio.sleep(0.2)
            assert len(rec.errors) == 1
            assert rec.errors[0].code == "stt_connect"
            assert "1008" in rec.errors[0].message
            assert "DATA-0001" in rec.errors[0].message
            assert "API key" in rec.errors[0].message
        finally:
            box.server.close()

    async def test_mid_recording_death_after_results_is_stt_error(self) -> None:
        async def handler(ws: ServerConnection) -> None:
            await ws.send(results_frame("some speech", True))
            await asyncio.sleep(0.05)
            await ws.close(code=1011, reason="NET-0001")

        box = await serve(handler)
        try:
            rec = Recorder()
            stream = make_stream(box.url, rec)
            await stream.connect()
            await asyncio.sleep(0.3)
            assert len(rec.errors) == 1
            assert rec.errors[0].code == "stt_error"
            assert "1011" in rec.errors[0].message
        finally:
            box.server.close()

    async def test_error_frame_reported_once_with_detail(self) -> None:
        async def handler(ws: ServerConnection) -> None:
            await ws.send(json.dumps({"type": "Error", "description": "quota exceeded"}))
            await asyncio.sleep(0.2)

        box = await serve(handler)
        try:
            rec = Recorder()
            stream = make_stream(box.url, rec)
            await stream.connect()
            await asyncio.sleep(0.2)
            assert len(rec.errors) == 1
            assert "quota exceeded" in rec.errors[0].message
        finally:
            box.server.close()

    async def test_connect_refused_raises_stt_connect(self) -> None:
        rec = Recorder()
        # Nothing listens on this port.
        stream = make_stream("ws://127.0.0.1:1", rec, connect_timeout=1.0)
        try:
            await stream.connect()
            raise AssertionError("expected AppError")
        except AppError as err:
            assert err.code == "stt_connect"
            assert "API key" in err.message

    async def test_abort_suppresses_the_death_it_causes(self) -> None:
        async def handler(ws: ServerConnection) -> None:
            await ws.send(results_frame("speech", True))
            async for _ in ws:
                pass

        box = await serve(handler)
        try:
            rec = Recorder()
            stream = make_stream(box.url, rec)
            await stream.connect()
            await asyncio.sleep(0.05)
            await stream.abort()
            await asyncio.sleep(0.1)
            assert rec.errors == []  # the abort-caused close is not an error
        finally:
            box.server.close()


class TestFinalize:
    async def test_finalize_is_idempotent_second_call_joins(self) -> None:
        close_streams_seen = 0

        async def handler(ws: ServerConnection) -> None:
            nonlocal close_streams_seen
            async for message in ws:
                if isinstance(message, str) and json.loads(message).get("type") == "CloseStream":
                    close_streams_seen += 1
                    await ws.send(results_frame("final", True))
                    await ws.close()
                    return

        box = await serve(handler)
        try:
            rec = Recorder()
            stream = make_stream(box.url, rec)
            await stream.connect()
            first, second = await asyncio.gather(stream.finalize(), stream.finalize())
            assert first == second == "final"
            assert close_streams_seen == 1
        finally:
            box.server.close()

    async def test_never_opened_stream_finalizes_immediately(self) -> None:
        rec = Recorder()
        stream = make_stream("ws://127.0.0.1:1", rec)
        # connect() never called/failed — finalize must not burn the timeout.
        transcript = await asyncio.wait_for(stream.finalize(), 0.5)
        assert transcript == ""

    async def test_dead_stream_finalizes_immediately_with_what_it_has(self) -> None:
        async def handler(ws: ServerConnection) -> None:
            await ws.send(results_frame("partial speech", True))
            await ws.close(code=1011, reason="NET-0001")

        box = await serve(handler)
        try:
            rec = Recorder()
            stream = make_stream(box.url, rec)
            await stream.connect()
            await asyncio.sleep(0.2)  # stream dies
            transcript = await asyncio.wait_for(stream.finalize(), 0.5)
            assert transcript == "partial speech"
        finally:
            box.server.close()

    async def test_unresponsive_server_finalize_returns_at_cap(self) -> None:
        async def handler(ws: ServerConnection) -> None:
            await ws.send(results_frame("heard this", True))
            async for _ in ws:
                pass  # ignores CloseStream, never closes

        box = await serve(handler)
        try:
            rec = Recorder()
            stream = make_stream(box.url, rec, finalize_timeout=0.2)
            await stream.connect()
            await asyncio.sleep(0.05)
            transcript = await asyncio.wait_for(stream.finalize(), 2.0)
            assert transcript == "heard this"  # cap hit, returns what it has
        finally:
            box.server.close()


class TestKeepalive:
    async def test_keepalive_sent_while_open_and_stopped_after_close_request(self) -> None:
        messages: list[str] = []

        async def handler(ws: ServerConnection) -> None:
            async for message in ws:
                if isinstance(message, bytes):
                    continue
                data = json.loads(message)
                messages.append(data.get("type", "?"))
                if data.get("type") == "CloseStream":
                    await ws.send(results_frame("done", True))
                    await ws.close()
                    return

        box = await serve(handler)
        try:
            rec = Recorder()
            stream = make_stream(box.url, rec, keepalive_interval=0.05)
            await stream.connect()
            await asyncio.sleep(0.18)  # several keepalive periods of silence
            await stream.finalize()
            keepalives_before_close = messages[: messages.index("CloseStream")].count(
                "KeepAlive"
            )
            assert keepalives_before_close >= 2
            # Nothing after CloseStream: a late KeepAlive on the CLOSING
            # socket would fabricate a "lost connection" during a good stop.
            assert messages[messages.index("CloseStream") + 1 :] == []
        finally:
            box.server.close()


class TestFinalizeOrderingAndCleanup:
    async def test_closestream_never_overtakes_queued_audio(self) -> None:
        """CloseStream travels the audio queue, so it is written after every
        frame already queued. Deepgram discards audio arriving after
        CloseStream, so overtaking silently truncates the tail."""
        order: list[str] = []

        async def handler(ws: ServerConnection) -> None:
            async for message in ws:
                if isinstance(message, bytes):
                    order.append(f"audio:{message.decode()}")
                    continue
                if json.loads(message).get("type") == "CloseStream":
                    order.append("close")
                    await ws.send(results_frame("done", True))
                    await ws.close()
                    return

        box = await serve(handler)
        try:
            rec = Recorder()
            stream = make_stream(box.url, rec)
            await stream.connect()
            for i in range(20):
                stream.send(str(i).encode())
            await stream.finalize()  # queued immediately behind the 20 frames
            assert order[-1] == "close"
            assert order[:20] == [f"audio:{i}" for i in range(20)]
        finally:
            box.server.close()

    async def test_finalize_leaves_no_pending_tasks(self) -> None:
        # The sender used to park on the queue forever after finalize, pinning
        # the socket for the life of the process.
        async def handler(ws: ServerConnection) -> None:
            async for message in ws:
                if isinstance(message, str) and json.loads(message).get("type") == "CloseStream":
                    await ws.send(results_frame("done", True))
                    await ws.close()
                    return

        box = await serve(handler)
        try:
            rec = Recorder()
            stream = make_stream(box.url, rec)
            await stream.connect()
            await stream.finalize()
            await asyncio.sleep(0.1)
            pending = [t for t in stream._tasks if not t.done()]
            assert pending == []
        finally:
            box.server.close()

    async def test_unresponsive_server_finalize_still_cleans_up(self) -> None:
        async def handler(ws: ServerConnection) -> None:
            await ws.send(results_frame("heard this", True))
            async for _ in ws:
                pass  # never closes

        box = await serve(handler)
        try:
            rec = Recorder()
            stream = make_stream(box.url, rec, finalize_timeout=0.2)
            await stream.connect()
            await asyncio.sleep(0.05)
            assert await stream.finalize() == "heard this"
            await asyncio.sleep(0.1)
            assert [t for t in stream._tasks if not t.done()] == []
        finally:
            box.server.close()

    async def test_bad_key_close_during_finalize_keeps_connect_classification(self) -> None:
        # User presses Record then Stop immediately; Deepgram rejects the key
        # by closing. "Lost the connection while finalizing" would send them
        # debugging the network instead of the key.
        release = asyncio.Event()

        async def handler(ws: ServerConnection) -> None:
            await release.wait()
            await ws.close(code=1008, reason="DATA-0001")

        box = await serve(handler)
        try:
            rec = Recorder()
            stream = make_stream(box.url, rec, finalize_timeout=1.0)
            await stream.connect()
            finalize = asyncio.get_running_loop().create_task(stream.finalize())
            await asyncio.sleep(0.05)
            release.set()
            await finalize
            await asyncio.sleep(0.1)
            assert len(rec.errors) == 1
            assert rec.errors[0].code == "stt_connect"
            assert "API key" in rec.errors[0].message
        finally:
            box.server.close()


class TestProductionWireConstants:
    def test_url_pins_every_required_query_parameter(self) -> None:
        from urllib.parse import parse_qs, urlsplit

        from app_core.stt.client import DEEPGRAM_URL

        parts = urlsplit(DEEPGRAM_URL)
        assert parts.scheme == "wss" and parts.netloc == "api.deepgram.com"
        assert parts.path == "/v1/listen"
        assert parse_qs(parts.query) == {
            "model": ["nova-3"],
            "encoding": ["linear16"],
            "sample_rate": ["16000"],
            "channels": ["1"],
            "interim_results": ["true"],
            "smart_format": ["true"],
        }

    async def test_the_api_key_is_offered_as_the_second_subprotocol(self) -> None:
        # Asserting only subprotocol == "token" would still pass if the key
        # were dropped from the offer.
        seen: list[str] = []

        async def handler(ws: ServerConnection) -> None:
            header = ws.request.headers.get("Sec-WebSocket-Protocol", "")  # type: ignore[union-attr]
            seen.append(header)
            await ws.close()

        box = await serve(handler)
        try:
            rec = Recorder()
            stream = DeepgramStream("secret-dg-key", rec.on_update, rec.on_error, url=box.url)
            await stream.connect()
            await asyncio.sleep(0.15)
            assert seen and "token" in seen[0] and "secret-dg-key" in seen[0]
        finally:
            box.server.close()

    async def test_no_keepalive_can_follow_closestream(self) -> None:
        # The old assertion was a tautology: the handler returned on
        # CloseStream, so nothing could ever be recorded after it.
        messages: list[str] = []

        async def handler(ws: ServerConnection) -> None:
            async for message in ws:
                if isinstance(message, bytes):
                    continue
                messages.append(json.loads(message).get("type", "?"))
                # Deliberately keeps reading after CloseStream.
            return

        box = await serve(handler)
        try:
            rec = Recorder()
            stream = make_stream(box.url, rec, keepalive_interval=0.05, finalize_timeout=0.3)
            await stream.connect()
            await asyncio.sleep(0.16)
            await stream.finalize()
            await asyncio.sleep(0.25)  # several more keepalive periods
            assert "CloseStream" in messages
            after = messages[messages.index("CloseStream") + 1 :]
            assert after == [], f"messages after CloseStream: {after}"
            assert messages.count("KeepAlive") >= 2
        finally:
            box.server.close()


class TestCloseStreamUnderBackpressure:
    async def test_closestream_waits_behind_a_stalled_sender(self) -> None:
        """The ordering test above passes even with a direct ws.send, because
        on a fast local socket the sender drains the queue synchronously
        before finalize runs. Stall the sender so the race is real: the
        sentinel must still leave last, or Deepgram discards the tail of the
        question at exactly the moment the user pressed Stop."""
        order: list[str] = []

        async def handler(ws: ServerConnection) -> None:
            async for message in ws:
                if isinstance(message, bytes):
                    order.append(f"audio:{message.decode()}")
                elif json.loads(message).get("type") == "CloseStream":
                    order.append("close")
                    await ws.send(results_frame("done", True))
                    await ws.close()
                    return

        box = await serve(handler)
        try:
            rec = Recorder()
            stream = make_stream(box.url, rec, finalize_timeout=3.0)
            await stream.connect()
            released = asyncio.Event()
            real_send = stream._ws.send
            first = {"seen": False}

            async def stalling_send(payload: object) -> None:
                # Hold the very first write so the queue backs up behind it,
                # exactly like a congested socket during a call.
                if not first["seen"]:
                    first["seen"] = True
                    await released.wait()
                await real_send(payload)

            stream._ws.send = stalling_send  # type: ignore[method-assign]
            for i in range(12):
                stream.send(str(i).encode())
            await asyncio.sleep(0.1)  # sender is parked on frame 0
            finalize = asyncio.get_running_loop().create_task(stream.finalize())
            await asyncio.sleep(0.1)  # a direct send would escape here
            released.set()
            await finalize
            assert order, "nothing reached the server"
            assert order[-1] == "close", order[-3:]
            assert order[:12] == [f"audio:{i}" for i in range(12)]
        finally:
            box.server.close()
