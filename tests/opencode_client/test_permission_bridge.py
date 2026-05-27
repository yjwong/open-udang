"""Tests for the PermissionBridge.

Covers the bridge wiring from ``permission.asked`` SSE events to a
``can_use_tool`` callback and the matching POST to
``/permission/{id}/reply``.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from open_shrimp.opencode_client import (
    OpenCodeClient,
    OpenCodeOptions,
    PermissionResultAllow,
    PermissionResultDeny,
    ToolPermissionContext,
)

from tests.opencode_client.mock_server import (
    MockOpenCode,
    permission_asked,
    session_idle,
    tool_part_event,
)


pytestmark = pytest.mark.asyncio


async def _wait_for(predicate, *, timeout: float = 2.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition never became true")
        await asyncio.sleep(0.02)


async def _drain(client: OpenCodeClient) -> list:
    msgs = []
    async for m in client.receive_response():
        msgs.append(m)
    return msgs


async def test_allow_sends_reply_once(
    mock_server: MockOpenCode, wired_server,
) -> None:
    calls: list[tuple[str, dict[str, Any], ToolPermissionContext]] = []

    async def can_use_tool(name, tool_input, ctx):
        calls.append((name, tool_input, ctx))
        return PermissionResultAllow()

    opts = OpenCodeOptions(
        cwd="/tmp", provider="openai", model="gpt-test",
        can_use_tool=can_use_tool,
    )
    async with OpenCodeClient(opts) as client:
        sid = client.session_id
        assert sid
        mock_server.script(
            sid,
            [
                tool_part_event(
                    "call_x", "bash", "running",
                    tool_input={"command": "ls"},
                ),
                permission_asked(
                    "req_x", "bash", call_id="call_x",
                    metadata={"command": "ls"},
                ),
                tool_part_event(
                    "call_x", "bash", "completed",
                    tool_input={"command": "ls"},
                    output="ok",
                ),
                session_idle(),
            ],
        )
        await client.query("hi")
        await _drain(client)
        await _wait_for(lambda: bool(mock_server.permission_replies))

    assert len(calls) == 1
    name, tool_input, ctx = calls[0]
    assert name == "Bash"
    assert tool_input == {"command": "ls"}
    assert ctx.tool_use_id == "call_x"

    assert mock_server.permission_replies == [
        {"request_id": "req_x", "body": {"reply": "once"}},
    ]


async def test_deny_sends_reject_with_message(
    mock_server: MockOpenCode, wired_server,
) -> None:
    async def can_use_tool(name, tool_input, ctx):
        return PermissionResultDeny(message="nope")

    opts = OpenCodeOptions(
        cwd="/tmp", provider="openai", model="gpt-test",
        can_use_tool=can_use_tool,
    )
    async with OpenCodeClient(opts) as client:
        sid = client.session_id
        mock_server.script(
            sid,
            [
                tool_part_event(
                    "call_y", "edit", "running",
                    tool_input={"file_path": "/a"},
                ),
                permission_asked(
                    "req_y", "edit", call_id="call_y",
                ),
                session_idle(),
            ],
        )
        await client.query("hi")
        await _drain(client)
        await _wait_for(lambda: bool(mock_server.permission_replies))

    assert mock_server.permission_replies == [
        {
            "request_id": "req_y",
            "body": {"reply": "reject", "message": "nope"},
        },
    ]


async def test_edit_category_disambiguated_by_toolpart(
    mock_server: MockOpenCode, wired_server,
) -> None:
    """`edit` category + ToolPart(tool=write) → hooks name 'Write'."""
    seen: list[str] = []

    async def can_use_tool(name, tool_input, ctx):
        seen.append(name)
        return PermissionResultAllow()

    opts = OpenCodeOptions(
        cwd="/tmp", provider="openai", model="gpt-test",
        can_use_tool=can_use_tool,
    )
    async with OpenCodeClient(opts) as client:
        sid = client.session_id
        mock_server.script(
            sid,
            [
                tool_part_event(
                    "call_z", "write", "running",
                    tool_input={"file_path": "/q", "content": "x"},
                ),
                permission_asked("req_z", "edit", call_id="call_z"),
                session_idle(),
            ],
        )
        await client.query("hi")
        await _drain(client)
        await _wait_for(lambda: bool(mock_server.permission_replies))

    assert seen == ["Write"]


async def test_can_use_tool_exception_yields_reject_reply(
    mock_server: MockOpenCode, wired_server,
) -> None:
    async def can_use_tool(name, tool_input, ctx):
        raise RuntimeError("boom")

    opts = OpenCodeOptions(
        cwd="/tmp", provider="openai", model="gpt-test",
        can_use_tool=can_use_tool,
    )
    async with OpenCodeClient(opts) as client:
        sid = client.session_id
        mock_server.script(
            sid,
            [
                tool_part_event(
                    "call_e", "bash", "running",
                    tool_input={"command": "ls"},
                ),
                permission_asked("req_e", "bash", call_id="call_e"),
                session_idle(),
            ],
        )
        await client.query("hi")
        await _drain(client)
        await _wait_for(lambda: bool(mock_server.permission_replies))

    assert mock_server.permission_replies[0]["body"]["reply"] == "reject"
    assert "boom" in mock_server.permission_replies[0]["body"]["message"]


async def test_duplicate_permission_asked_replies_once(
    mock_server: MockOpenCode, wired_server,
) -> None:
    """Two permission.asked frames for the same id only reply once."""
    async def can_use_tool(name, tool_input, ctx):
        return PermissionResultAllow()

    opts = OpenCodeOptions(
        cwd="/tmp", provider="openai", model="gpt-test",
        can_use_tool=can_use_tool,
    )
    async with OpenCodeClient(opts) as client:
        sid = client.session_id
        mock_server.script(
            sid,
            [
                tool_part_event(
                    "call_d", "bash", "running",
                    tool_input={"command": "ls"},
                ),
                permission_asked("req_d", "bash", call_id="call_d"),
                permission_asked("req_d", "bash", call_id="call_d"),
                session_idle(),
            ],
        )
        await client.query("hi")
        await _drain(client)
        await _wait_for(lambda: bool(mock_server.permission_replies))

    assert len(mock_server.permission_replies) == 1


async def test_tool_input_fetched_from_message_on_cache_miss(
    mock_server: MockOpenCode, wired_server,
) -> None:
    """If no ToolPart has been seen, the bridge fetches GET /session/.../message/."""
    seen_inputs: list[dict[str, Any]] = []

    async def can_use_tool(name, tool_input, ctx):
        seen_inputs.append(tool_input)
        return PermissionResultAllow()

    opts = OpenCodeOptions(
        cwd="/tmp", provider="openai", model="gpt-test",
        can_use_tool=can_use_tool,
    )
    async with OpenCodeClient(opts) as client:
        sid = client.session_id
        # Register a message body the bridge can fetch.
        mock_server.messages[(sid, "msg_fetch")] = {
            "id": "msg_fetch",
            "parts": [
                {
                    "type": "tool",
                    "callID": "call_f",
                    "tool": "read",
                    "state": {
                        "status": "running",
                        "input": {"file_path": "/fetched"},
                    },
                }
            ],
        }
        mock_server.script(
            sid,
            [
                permission_asked(
                    "req_f", "read",
                    call_id="call_f", message_id="msg_fetch",
                ),
                session_idle(),
            ],
        )
        await client.query("hi")
        await _drain(client)
        await _wait_for(lambda: bool(mock_server.permission_replies))

    assert seen_inputs == [{"file_path": "/fetched"}]


async def test_updated_input_dropped_with_warning(
    mock_server: MockOpenCode, wired_server, caplog,
) -> None:
    """PermissionResultAllow(updated_input=…) ignores updated_input, logs WARN."""
    async def can_use_tool(name, tool_input, ctx):
        return PermissionResultAllow(updated_input={"hacked": True})

    opts = OpenCodeOptions(
        cwd="/tmp", provider="openai", model="gpt-test",
        can_use_tool=can_use_tool,
    )
    async with OpenCodeClient(opts) as client:
        sid = client.session_id
        mock_server.script(
            sid,
            [
                tool_part_event(
                    "call_u", "bash", "running",
                    tool_input={"command": "ls"},
                ),
                permission_asked("req_u", "bash", call_id="call_u"),
                session_idle(),
            ],
        )
        await client.query("hi")
        await _drain(client)
        await _wait_for(lambda: bool(mock_server.permission_replies))

    assert mock_server.permission_replies[0]["body"] == {"reply": "once"}
