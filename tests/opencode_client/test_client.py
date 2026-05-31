"""Unit tests for OpenCodeClient end-to-end against a mock server."""

from __future__ import annotations

import asyncio

import pytest

from open_shrimp.opencode_client import client as client_mod
from open_shrimp.opencode_client import (
    AssistantMessage,
    OpenCodeClient,
    OpenCodeEndpoint,
    OpenCodeOptions,
    ProcessError,
    ResultMessage,
    StreamEvent,
    TextBlock,
)

from tests.opencode_client.mock_server import (
    MockOpenCode,
    question_asked,
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


async def test_child_session_helpers(
    mock_server: MockOpenCode, wired_server
) -> None:
    opts = OpenCodeOptions(cwd="/parent", provider="openai", model="gpt-test")
    async with OpenCodeClient(opts) as client:
        parent_id = client.session_id
        assert parent_id is not None
        child_id = await client.create_session(
            directory="/child",
            parent_id=parent_id,
            title="Child task",
            agent="explore",
            model={"providerID": "openai", "modelID": "gpt-child"},
        )
        queue = client.subscribe_session(child_id)
        try:
            mock_server.script(child_id, [text_delta("p1", "child done"), session_idle()])
            await client.prompt_session(
                child_id,
                parts=[{"type": "text", "text": "go"}],
                provider="openai",
                model="gpt-child",
                agent="explore",
            )
            msgs = [m async for m in client.iter_session_response(child_id, queue)]
        finally:
            client.unsubscribe_session(child_id)
        await client.abort_session(child_id)

    child_create = mock_server.created_sessions[-1]
    assert child_create["params"].get("directory") == "/child"
    assert child_create["body"]["parentID"] == parent_id
    assert child_create["body"]["title"] == "Child task"
    assert child_create["body"]["agent"] == "explore"
    assert child_create["body"]["model"] == {"providerID": "openai", "modelID": "gpt-child"}
    assert mock_server.prompts[-1]["session_id"] == child_id
    assert mock_server.prompts[-1]["body"]["agent"] == "explore"
    assert any(isinstance(m, ResultMessage) and m.session_id == child_id for m in msgs)
    assert mock_server.aborted_sessions[-1] == child_id


async def test_fork_session_clones_parent(
    mock_server: MockOpenCode, wired_server
) -> None:
    opts = OpenCodeOptions(cwd="/parent", provider="openai", model="gpt-test")
    async with OpenCodeClient(opts) as client:
        parent_id = client.session_id
        assert parent_id is not None
        fork_id = await client.fork_session(parent_id)

    assert mock_server.forked_sessions[-1]["id"] == fork_id
    assert mock_server.forked_sessions[-1]["parent_id"] == parent_id
    assert mock_server.forked_sessions[-1]["body"] == {}


async def test_fork_session_accepts_message_id(
    mock_server: MockOpenCode, wired_server
) -> None:
    opts = OpenCodeOptions(cwd="/parent", provider="openai", model="gpt-test")
    async with OpenCodeClient(opts) as client:
        parent_id = client.session_id
        assert parent_id is not None
        await client.fork_session(parent_id, message_id="msg_123")

    assert mock_server.forked_sessions[-1]["body"] == {"messageID": "msg_123"}


async def test_supplied_endpoint_skips_host_singleton(mock_setup, monkeypatch) -> None:
    mock_server, base_url = mock_setup

    async def fail_get_or_start(cls):
        raise AssertionError("host singleton should not be used")

    monkeypatch.setattr(
        "open_shrimp.opencode_client.client.OpenCodeServer.get_or_start",
        classmethod(fail_get_or_start),
    )
    opts = OpenCodeOptions(
        cwd="/tmp",
        provider="openai",
        model="gpt-test",
        endpoint=OpenCodeEndpoint(base_url=base_url, auth_header="Basic test"),
    )
    async with OpenCodeClient(opts) as client:
        sid = client.session_id
        assert sid is not None
        mock_server.script(sid, [session_idle()])
        await client.query("hi")
        async for _ in client.receive_response():
            pass

    assert mock_server.created_sessions
    await client_mod._shutdown_buses()


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


async def test_question_asked_replies_with_structured_answers(
    mock_server: MockOpenCode, wired_server
) -> None:
    seen_questions: list[list[dict]] = []

    async def handle_questions(questions):
        seen_questions.append(questions)
        return [["Choice A"], ["Choice B", "Custom text"]]

    opts = OpenCodeOptions(
        cwd="/tmp",
        provider="openai",
        model="gpt-test",
        handle_questions=handle_questions,
    )
    async with OpenCodeClient(opts) as client:
        sid = client.session_id
        assert sid is not None
        mock_server.script(
            sid,
            [
                question_asked(
                    "q_1",
                    [
                        {"question": "First?", "options": [], "multiple": False},
                        {"question": "Second?", "options": [], "multiple": True},
                    ],
                ),
                session_idle(),
            ],
        )
        await client.query("hi")
        await _collect(client)

    assert len(seen_questions) == 1
    assert seen_questions[0][0]["question"] == "First?"
    assert mock_server.question_replies == [
        {
            "request_id": "q_1",
            "body": {"answers": [["Choice A"], ["Choice B", "Custom text"]]},
        }
    ]


async def test_question_asked_rejects_on_handler_failure(
    mock_server: MockOpenCode, wired_server
) -> None:
    async def handle_questions(questions):
        raise RuntimeError("no UI")

    opts = OpenCodeOptions(
        cwd="/tmp",
        provider="openai",
        model="gpt-test",
        handle_questions=handle_questions,
    )
    async with OpenCodeClient(opts) as client:
        sid = client.session_id
        assert sid is not None
        mock_server.script(sid, [question_asked("q_fail", []), session_idle()])
        await client.query("hi")
        await _collect(client)

    assert mock_server.question_replies == []
    assert mock_server.question_rejections == ["q_fail"]


async def test_session_permission_rules_allow_question(
    mock_server: MockOpenCode, wired_server
) -> None:
    opts = OpenCodeOptions(cwd="/tmp", provider="openai", model="gpt-test")
    async with OpenCodeClient(opts):
        pass

    rules = mock_server.created_sessions[0]["body"]["permission"]
    assert {"permission": "question", "pattern": "*", "action": "allow"} in rules
