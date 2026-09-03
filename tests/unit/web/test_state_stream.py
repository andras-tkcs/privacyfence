"""Tests for web/state_stream.py -- the SSE state-push channel (§16.3) and
its call_on_main dispatcher target (§16.2.1). Exercises StateStream.
subscribe() directly (an async generator, not via a real HTTP request) so a
disconnect can be simulated without hanging -- see web/test_server.py's own
comment on why TestClient can't drive an endless SSE response.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from privacyfence.web import state_stream as ss
from privacyfence.web.state_stream import StateStream, call_soon_threadsafe, set_loop


@pytest.fixture(autouse=True)
def _fast_poll(monkeypatch):
    # Real _APPROVALS_POLL_SECONDS (1.0s) is fine for the daemon; shrinking
    # it here keeps the "no push happened, only the poll noticed" tests
    # fast instead of adding a full second of real wall-clock wait each.
    monkeypatch.setattr(ss, "_APPROVALS_POLL_SECONDS", 0.02)


def _parse_sse(chunk: str) -> tuple[str, object]:
    lines = chunk.strip("\n").split("\n")
    event = lines[0].removeprefix("event: ")
    data = json.loads(lines[1].removeprefix("data: "))
    return event, data


class _Disconnector:
    """A stand-in for Request.is_disconnected() that returns True after
    ``n`` calls -- lets a test drain a bounded number of events out of an
    otherwise-infinite subscribe() loop instead of hanging."""

    def __init__(self, after: int) -> None:
        self._after = after
        self._calls = 0

    async def __call__(self) -> bool:
        self._calls += 1
        return self._calls > self._after


class TestSubscribe:
    async def test_first_connect_gets_full_state_immediately(self):
        stream = StateStream(settings_snapshot=lambda: {"a": 1}, list_pending=lambda: [])
        chunks = []
        async for chunk in stream.subscribe(_Disconnector(after=0)):
            chunks.append(chunk)
        events = [_parse_sse(c) for c in chunks]
        assert ("settings", {"a": 1}) in events
        assert ("approvals", []) in events

    async def test_no_settings_event_when_no_controller_is_wired(self):
        stream = StateStream(settings_snapshot=lambda: None, list_pending=lambda: [])
        chunks = []
        async for chunk in stream.subscribe(_Disconnector(after=0)):
            chunks.append(chunk)
        events = [_parse_sse(c)[0] for c in chunks]
        assert "settings" not in events
        assert "approvals" in events

    async def test_a_pushed_settings_update_is_delivered(self):
        stream = StateStream(settings_snapshot=lambda: {"a": 1}, list_pending=lambda: [])

        async def driver():
            gen = stream.subscribe(_Disconnector(after=2))
            out = []
            async for chunk in gen:
                out.append(chunk)
                if len(out) == 1:
                    # Push happens after the initial full-state flush,
                    # while the generator is waiting in its queue.get().
                    stream.push_settings({"a": 2})
            return out

        chunks = await driver()
        events = [_parse_sse(c) for c in chunks]
        assert ("settings", {"a": 2}) in events

    async def test_reconnect_gets_full_state_again_not_a_patch(self):
        stream = StateStream(settings_snapshot=lambda: {"a": 1}, list_pending=lambda: [])
        first = [c async for c in stream.subscribe(_Disconnector(after=0))]
        second = [c async for c in stream.subscribe(_Disconnector(after=0))]
        assert first == second

    async def test_subscriber_is_removed_on_disconnect(self):
        stream = StateStream(settings_snapshot=lambda: None, list_pending=lambda: [])
        async for _ in stream.subscribe(_Disconnector(after=0)):
            pass
        assert stream._subscribers == set()

    async def test_approvals_change_is_delivered_without_an_explicit_push(self):
        pending = []
        stream = StateStream(settings_snapshot=lambda: None, list_pending=lambda: pending)

        async def driver():
            gen = stream.subscribe(_Disconnector(after=3))
            out = []
            async for chunk in gen:
                out.append(chunk)
                if len(out) == 1:
                    pending.append(_FakeApproval("a1"))
            return out

        chunks = await driver()
        events = [_parse_sse(c) for c in chunks]
        ids_seen = [d for e, d in events if e == "approvals"]
        assert [] in ids_seen
        assert any(len(d) == 1 and d[0]["id"] == "a1" for d in ids_seen)


class _FakeApproval:
    def __init__(self, approval_id: str) -> None:
        self.id = approval_id

    def to_summary_dict(self):
        return {"id": self.id}


class TestPushDebouncing:
    def test_a_burst_of_pushes_collapses_to_the_latest(self):
        stream = StateStream(settings_snapshot=lambda: None, list_pending=lambda: [])
        queue: asyncio.Queue = asyncio.Queue(maxsize=1)
        stream._subscribers.add(queue)

        stream.push_settings({"a": 1})
        stream.push_settings({"a": 2})
        stream.push_settings({"a": 3})

        assert queue.qsize() == 1
        event, data = queue.get_nowait()
        assert data == {"a": 3}


class TestCallOnMainDispatcher:
    def test_runs_inline_with_no_loop_captured(self):
        set_loop(None)
        calls = []
        call_soon_threadsafe(lambda x: calls.append(x), "hi")
        assert calls == ["hi"]

    async def test_marshals_onto_the_captured_loop(self):
        loop = asyncio.get_running_loop()
        set_loop(loop)
        try:
            done = asyncio.Event()
            calls = []

            def target(x):
                calls.append(x)
                loop.call_soon_threadsafe(done.set)

            call_soon_threadsafe(target, "hi")
            await asyncio.wait_for(done.wait(), timeout=2)
            assert calls == ["hi"]
        finally:
            set_loop(None)
