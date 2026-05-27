"""Tests for ToolPart → ToolUseBlock/ToolResultBlock synthesis."""

from __future__ import annotations

import pytest

from open_shrimp.opencode_client import (
    AssistantMessage,
    OpenCodeClient,
    OpenCodeOptions,
    ResultMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from tests.opencode_client.mock_server import (
    MockOpenCode,
    permission_asked,
    session_idle,
    text_delta,
    tool_part_event,
)


pytestmark = pytest.mark.asyncio


async def _collect(client: OpenCodeClient) -> list:
    msgs = []
    async for m in client.receive_response():
        msgs.append(m)
    return msgs


async def test_pending_then_running_emits_tool_use_once(
    mock_server: MockOpenCode, wired_server,
) -> None:
    opts = OpenCodeOptions(cwd="/tmp", provider="openai", model="gpt-test")
    async with OpenCodeClient(opts) as client:
        sid = client.session_id
        assert sid
        mock_server.script(
            sid,
            [
                tool_part_event(
                    "call_a", "read", "pending", tool_input={},
                ),
                tool_part_event(
                    "call_a", "read", "running",
                    tool_input={"file_path": "/tmp/x"},
                ),
                tool_part_event(
                    "call_a", "read", "running",
                    tool_input={"file_path": "/tmp/x"},
                ),  # duplicate; dropped
                tool_part_event(
                    "call_a", "read", "completed",
                    tool_input={"file_path": "/tmp/x"},
                    output="contents",
                ),
                session_idle(),
            ],
        )
        await client.query("hi")
        msgs = await _collect(client)

    tool_uses = [
        b for m in msgs
        if isinstance(m, AssistantMessage)
        for b in m.content
        if isinstance(b, ToolUseBlock)
    ]
    tool_results = [
        b for m in msgs
        if isinstance(m, UserMessage)
        for b in m.content
        if isinstance(b, ToolResultBlock)
    ]

    assert len(tool_uses) == 1
    assert tool_uses[0].id == "call_a"
    assert tool_uses[0].name == "Read"
    assert tool_uses[0].input == {"file_path": "/tmp/x"}

    assert len(tool_results) == 1
    assert tool_results[0].tool_use_id == "call_a"
    assert tool_results[0].content == "contents"
    assert tool_results[0].is_error is False

    assert any(isinstance(m, ResultMessage) for m in msgs)


async def test_error_status_marks_tool_result_error(
    mock_server: MockOpenCode, wired_server,
) -> None:
    opts = OpenCodeOptions(cwd="/tmp", provider="openai", model="gpt-test")
    async with OpenCodeClient(opts) as client:
        sid = client.session_id
        mock_server.script(
            sid,
            [
                tool_part_event(
                    "call_b", "bash", "running",
                    tool_input={"command": "ls"},
                ),
                tool_part_event(
                    "call_b", "bash", "error",
                    tool_input={"command": "ls"},
                    error="permission denied",
                ),
                session_idle(),
            ],
        )
        await client.query("hi")
        msgs = await _collect(client)

    results = [
        b for m in msgs if isinstance(m, UserMessage)
        for b in m.content if isinstance(b, ToolResultBlock)
    ]
    assert len(results) == 1
    assert results[0].is_error is True
    assert results[0].content == "permission denied"


async def test_pending_with_empty_input_defers_until_running(
    mock_server: MockOpenCode, wired_server,
) -> None:
    """A pending ToolPart with no input must not emit a ToolUseBlock yet."""
    opts = OpenCodeOptions(cwd="/tmp", provider="openai", model="gpt-test")
    async with OpenCodeClient(opts) as client:
        sid = client.session_id
        mock_server.script(
            sid,
            [
                tool_part_event(
                    "call_c", "read", "pending", tool_input={},
                ),
                # No running emit yet → no ToolUseBlock until then.
                tool_part_event(
                    "call_c", "read", "running",
                    tool_input={"file_path": "/y"},
                ),
                session_idle(),
            ],
        )
        await client.query("hi")
        msgs = await _collect(client)

    tool_uses = [
        b for m in msgs if isinstance(m, AssistantMessage)
        for b in m.content if isinstance(b, ToolUseBlock)
    ]
    assert len(tool_uses) == 1
    assert tool_uses[0].input == {"file_path": "/y"}


async def test_pretool_text_flushes_before_tool_use(
    mock_server: MockOpenCode, wired_server,
) -> None:
    """Pre-tool text should arrive before the ToolUseBlock — ordering check."""
    opts = OpenCodeOptions(cwd="/tmp", provider="openai", model="gpt-test")
    async with OpenCodeClient(opts) as client:
        sid = client.session_id
        mock_server.script(
            sid,
            [
                text_delta("p1", "I'll read the file. "),
                tool_part_event(
                    "call_d", "read", "running",
                    tool_input={"file_path": "/z"},
                ),
                tool_part_event(
                    "call_d", "read", "completed",
                    tool_input={"file_path": "/z"},
                    output="ok",
                ),
                text_delta("p2", "Done."),
                session_idle(),
            ],
        )
        await client.query("hi")
        msgs = await _collect(client)

    # Find ordering of meaningful events.
    seq: list[str] = []
    for m in msgs:
        if isinstance(m, AssistantMessage):
            if any(isinstance(b, ToolUseBlock) for b in m.content):
                seq.append("tool_use")
            elif any(isinstance(b, TextBlock) for b in m.content):
                # Capture which text part was flushed
                texts = [
                    b.text for b in m.content if isinstance(b, TextBlock)
                ]
                if any("I'll read" in t for t in texts):
                    seq.append("pre_text")
                else:
                    seq.append("post_text")
        elif isinstance(m, UserMessage) and any(
            isinstance(b, ToolResultBlock) for b in m.content
        ):
            seq.append("tool_result")
        elif isinstance(m, ResultMessage):
            seq.append("result")

    # Expect pre_text → tool_use → tool_result → post_text → result.
    assert seq[: seq.index("result") + 1] == [
        "pre_text", "tool_use", "tool_result", "post_text", "result",
    ]


async def test_unknown_tool_passes_through_name(
    mock_server: MockOpenCode, wired_server,
) -> None:
    opts = OpenCodeOptions(cwd="/tmp", provider="openai", model="gpt-test")
    async with OpenCodeClient(opts) as client:
        sid = client.session_id
        mock_server.script(
            sid,
            [
                tool_part_event(
                    "call_e", "openshrimp_send_file", "running",
                    tool_input={"path": "/tmp/x"},
                ),
                tool_part_event(
                    "call_e", "openshrimp_send_file", "completed",
                    tool_input={"path": "/tmp/x"},
                    output="sent",
                ),
                session_idle(),
            ],
        )
        await client.query("hi")
        msgs = await _collect(client)

    tool_uses = [
        b for m in msgs if isinstance(m, AssistantMessage)
        for b in m.content if isinstance(b, ToolUseBlock)
    ]
    assert tool_uses[0].name == "openshrimp_send_file"


async def test_permission_asked_does_not_yield_into_response_iter(
    mock_server: MockOpenCode, wired_server,
) -> None:
    """permission.asked is bridge-only; receive_response must not see it."""
    opts = OpenCodeOptions(cwd="/tmp", provider="openai", model="gpt-test")
    async with OpenCodeClient(opts) as client:
        sid = client.session_id
        mock_server.script(
            sid,
            [
                permission_asked("req_1", "bash", call_id="call_p"),
                session_idle(),
            ],
        )
        await client.query("hi")
        msgs = await _collect(client)

    # Should yield only the final ResultMessage, no StreamEvent for the
    # permission frame.
    assert all(
        m.__class__.__name__ != "StreamEvent" for m in msgs
    )
