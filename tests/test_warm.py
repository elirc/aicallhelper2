"""Pre-warm behavior: fire-and-forget, throttled, failure-silent."""

from __future__ import annotations

import asyncio

import httpx

from app_core.llm.warm import PreWarmer


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def make(handler_log: list[str], fail: bool = False) -> tuple[PreWarmer, Clock]:
    def handler(request: httpx.Request) -> httpx.Response:
        handler_log.append(str(request.url))
        if fail:
            raise httpx.ConnectError("no net", request=request)
        return httpx.Response(200, json={"data": []})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    clock = Clock()
    return PreWarmer(http, clock=clock), clock


class TestPreWarm:
    async def test_warms_the_models_endpoint(self) -> None:
        log: list[str] = []
        warmer, _clock = make(log)
        task = warmer.warm("https://api.example.com")
        assert task is not None
        await task
        assert log == ["https://api.example.com/v1/models"]

    async def test_throttled_to_one_per_origin_per_2s(self) -> None:
        log: list[str] = []
        warmer, clock = make(log)
        first = warmer.warm("https://a.example")
        assert first is not None
        assert warmer.warm("https://a.example") is None  # throttled
        clock.now = 1.9
        assert warmer.warm("https://a.example") is None
        clock.now = 2.0
        second = warmer.warm("https://a.example")
        assert second is not None
        await asyncio.gather(first, second)
        assert len(log) == 2

    async def test_throttle_is_per_origin(self) -> None:
        log: list[str] = []
        warmer, _clock = make(log)
        t1 = warmer.warm("https://a.example")
        t2 = warmer.warm("https://b.example")
        assert t1 is not None and t2 is not None
        await asyncio.gather(t1, t2)
        assert len(log) == 2

    async def test_failed_warm_never_raises(self) -> None:
        log: list[str] = []
        warmer, _clock = make(log, fail=True)
        task = warmer.warm("https://down.example")
        assert task is not None
        await task  # swallows the ConnectError
