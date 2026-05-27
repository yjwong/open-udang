from __future__ import annotations

import asyncio

import pytest

from open_shrimp.opencode_client.errors import CLIConnectionError
from open_shrimp.opencode_client.sse import EventBus, EventQueue

from tests.opencode_client.conftest import make_fake_server

pytestmark = pytest.mark.asyncio


async def test_start_waits_for_server_connected(mock_setup) -> None:
    _, base = mock_setup
    bus = EventBus(make_fake_server(base))
    try:
        await bus.start()
        assert bus._ready.is_set()
    finally:
        await bus.stop()


async def test_start_times_out_when_no_connected_event(mock_setup) -> None:
    mock, base = mock_setup
    mock.send_initial_connected = False
    bus = EventBus(make_fake_server(base))
    from open_shrimp.opencode_client import sse as sse_mod

    original = sse_mod._CONNECT_TIMEOUT
    sse_mod._CONNECT_TIMEOUT = 0.3
    try:
        with pytest.raises(CLIConnectionError):
            await bus.start()
    finally:
        sse_mod._CONNECT_TIMEOUT = original
        await bus.stop()


async def test_demux_two_sessions(mock_setup) -> None:
    mock, base = mock_setup
    bus = EventBus(make_fake_server(base))
    await bus.start()
    try:
        qa = bus.subscribe("session-a")
        qb = bus.subscribe("session-b")

        mock.broadcast(
            {"type": "message.part.delta", "properties": {"sessionID": "session-a", "field": "text", "delta": "A"}}
        )
        mock.broadcast(
            {"type": "message.part.delta", "properties": {"sessionID": "session-b", "field": "text", "delta": "B"}}
        )

        ea = await asyncio.wait_for(qa.get(), timeout=3.0)
        eb = await asyncio.wait_for(qb.get(), timeout=3.0)

        assert ea["properties"]["delta"] == "A"
        assert eb["properties"]["delta"] == "B"
    finally:
        await bus.stop()


async def test_overflow_drops_oldest() -> None:
    q = EventQueue("sid", maxsize=2)
    q.put_nowait({"i": 1})
    q.put_nowait({"i": 2})
    q.put_nowait({"i": 3})

    first = await q.get()
    second = await q.get()
    assert first == {"i": 2}
    assert second == {"i": 3}


async def test_reconnect_after_disconnect(mock_setup) -> None:
    from open_shrimp.opencode_client import sse as sse_mod

    mock, base = mock_setup
    bus = EventBus(make_fake_server(base))

    original_backoff = sse_mod._BACKOFF_INITIAL
    sse_mod._BACKOFF_INITIAL = 0.05
    try:
        await bus.start()
        q = bus.subscribe("sid-reconnect")

        await mock.disconnect_all()

        await asyncio.sleep(0.4)
        mock.broadcast(
            {"type": "message.part.delta", "properties": {"sessionID": "sid-reconnect", "field": "text", "delta": "after"}}
        )
        evt = await asyncio.wait_for(q.get(), timeout=3.0)
        assert evt["properties"]["delta"] == "after"
    finally:
        sse_mod._BACKOFF_INITIAL = original_backoff
        await bus.stop()
