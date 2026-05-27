"""Unit tests for OpenCodeClient end-to-end against a mock server."""

from __future__ import annotations

import asyncio

import pytest

from open_shrimp.opencode_client import (
    AssistantMessage,
    OpenCodeClient,
    OpenCodeOptions,
    ProcessError,
    ResultMessage,
    StreamEvent,
    TextBlock,
)

from tests.opencode_client.mock_server import (
    MockOpenCode,
    session_error,
    session_idle,
    text_delta,
)

pytestmark = pytest.mark.asyncio


async def _collect(client: OpenCodeClient) -> list:
    msgs = []
    async for m in client.receive_response():
        msgs.append(m)
    return msgs


async def test_happy_path_text_streaming(
    mock_server: MockOpenCode, wired_server
) -> None:
    """Single text part: deltas accumulate, idle flushes one AssistantMessage."""
    opts = OpenCodeOptions(cwd="/tmp", provider="openai", model="gpt-test")
    async with OpenCodeClient(opts) as client:
        sid = client.session_id
        assert sid is not None
        mock_server.script(
            sid,
            [
                text_delta("p1", "Hello "),
                text_delta("p1", "world"),
                text_delta("p1", "!"),
                session_idle(),
            ],
        )
        await client.query("hi")
        msgs = await _collect(client)

    stream_events = [m for m in msgs if isinstance(m, StreamEvent)]
    assistants = [m for m in msgs if isinstance(m, AssistantMessage)]
    results = [m for m in msgs if isinstance(m, ResultMessage)]

    assert len(stream_events) == 3
    assert len(assistants) == 1
    assert assistants[0].content == [TextBlock(text="Hello world!")]
    assert len(results) == 1
    assert results[0].session_id == sid
    assert results[0].is_error is False


async def test_session_error_raises_process_error(
    mock_server: MockOpenCode, wired_server
) -> None:
    opts = OpenCodeOptions(cwd="/tmp", provider="openai", model="gpt-test")
    async with OpenCodeClient(opts) as client:
        sid = client.session_id
        assert sid is not None
        mock_server.script(
            sid,
            [
                text_delta("p1", "partial..."),
                session_error("model exploded"),
                session_idle(),
            ],
        )
        await client.query("hi")
        with pytest.raises(ProcessError) as exc:
            async for _ in client.receive_response():
                pass
    assert "model exploded" in str(exc.value)


async def test_query_timeout(
    mock_server: MockOpenCode, wired_server
) -> None:
    """If no session.idle ever arrives, receive_response raises ProcessError."""
    opts = OpenCodeOptions(
        cwd="/tmp", provider="openai", model="gpt-test", query_timeout=0.3
    )
    async with OpenCodeClient(opts) as client:
        sid = client.session_id
        assert sid is not None
        # Empty script -> nothing ever sent.
        mock_server.script(sid, [])
        await client.query("hi")
        with pytest.raises(ProcessError):
            async for _ in client.receive_response():
                pass


async def test_resume_passes_through(
    mock_server: MockOpenCode, wired_server
) -> None:
    """resume= skips POST /session and reuses the supplied session_id."""
    fixed_sid = "deadbeefcafebabe"
    opts = OpenCodeOptions(
        cwd="/tmp", provider="openai", model="gpt-test", resume=fixed_sid
    )
    mock_server.script(fixed_sid, [text_delta("p1", "ok"), session_idle()])
    async with OpenCodeClient(opts) as client:
        assert client.session_id == fixed_sid
        await client.query("hi")
        msgs = await _collect(client)
    assert not mock_server.created_sessions
    assert any(isinstance(m, ResultMessage) for m in msgs)


async def test_two_clients_demuxed(
    mock_server: MockOpenCode, wired_server
) -> None:
    """Two concurrent OpenCodeClients see their own events only."""
    opts_a = OpenCodeOptions(cwd="/tmp", provider="openai", model="gpt-test")
    opts_b = OpenCodeOptions(cwd="/tmp", provider="openai", model="gpt-test")

    client_a = OpenCodeClient(opts_a)
    client_b = OpenCodeClient(opts_b)
    await client_a.connect()
    await client_b.connect()
    try:
        sid_a = client_a.session_id
        sid_b = client_b.session_id
        assert sid_a and sid_b and sid_a != sid_b

        mock_server.script(sid_a, [text_delta("pa", "AAA"), session_idle()])
        mock_server.script(sid_b, [text_delta("pb", "BBB"), session_idle()])

        await client_a.query("hi a")
        await client_b.query("hi b")

        msgs_a, msgs_b = await asyncio.gather(_collect(client_a), _collect(client_b))
    finally:
        await client_a.disconnect()
        await client_b.disconnect()

    text_a = "".join(
        b.text
        for m in msgs_a
        if isinstance(m, AssistantMessage)
        for b in m.content
        if isinstance(b, TextBlock)
    )
    text_b = "".join(
        b.text
        for m in msgs_b
        if isinstance(m, AssistantMessage)
        for b in m.content
        if isinstance(b, TextBlock)
    )
    assert text_a == "AAA"
    assert text_b == "BBB"


async def test_post_session_passes_directory(
    mock_server: MockOpenCode, wired_server
) -> None:
    opts = OpenCodeOptions(cwd="/path/to/ctx", provider="openai", model="gpt-test")
    async with OpenCodeClient(opts) as client:
        sid = client.session_id
        mock_server.script(sid, [session_idle()])
        await client.query("hi")
        async for _ in client.receive_response():
            pass
    assert mock_server.created_sessions
    assert mock_server.created_sessions[0]["params"].get("directory") == "/path/to/ctx"


async def test_invalid_model_surfaces_after_204(
    mock_server: MockOpenCode, wired_server
) -> None:
    """Validated by probe 3: POST returns 204; error arrives on SSE."""
    opts = OpenCodeOptions(cwd="/tmp", provider="openai", model="bogus")
    async with OpenCodeClient(opts) as client:
        sid = client.session_id
        mock_server.script(sid, [session_error("model not found"), session_idle()])
        await client.query("hi")
        with pytest.raises(ProcessError) as exc:
            async for _ in client.receive_response():
                pass
    assert "model not found" in str(exc.value)
