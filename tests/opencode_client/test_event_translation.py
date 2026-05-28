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
    step_ended,
    step_failed,
    step_started,
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


def _final_result(msgs: list) -> ResultMessage:
    results = [m for m in msgs if isinstance(m, ResultMessage)]
    assert len(results) == 1, f"expected one ResultMessage, got {results}"
    return results[0]


def _assistant_with_usage(msgs: list) -> list[AssistantMessage]:
    return [
        m for m in msgs
        if isinstance(m, AssistantMessage) and m.usage is not None
    ]


async def test_step_ended_populates_usage_and_model_usage(
    mock_server: MockOpenCode, wired_server,
) -> None:
    opts = OpenCodeOptions(cwd="/tmp", provider="openai", model="gpt-test")
    async with OpenCodeClient(opts) as client:
        sid = client.session_id
        mock_server.script(
            sid,
            [
                step_started("msg1", "claude-sonnet-4-6"),
                text_delta("p1", "hello"),
                step_ended(
                    "msg1", "claude-sonnet-4-6",
                    input=100, output=20, reasoning=5,
                    cache_read=200, cache_write=50, cost=0.01,
                ),
                session_idle(),
            ],
        )
        await client.query("hi")
        msgs = await _collect(client)

    with_usage = _assistant_with_usage(msgs)
    assert len(with_usage) == 1
    assert with_usage[0].usage == {
        "input": 100,
        "output": 20,
        "reasoning": 5,
        "cache": {"read": 200, "write": 50},
    }

    result = _final_result(msgs)
    assert result.num_steps == 1
    assert result.errors == []
    assert result.is_error is False
    assert result.total_cost_usd == pytest.approx(0.01)
    assert result.model_usage == {
        "claude-sonnet-4-6": {
            "input": 100,
            "output": 20,
            "reasoning": 5,
            "cache": {"read": 200, "write": 50},
            "cost": 0.01,
        },
    }
    assert result.usage == {
        "input": 100,
        "output": 20,
        "reasoning": 5,
        "cache": {"read": 200, "write": 50},
    }


async def test_two_models_accumulate_separately(
    mock_server: MockOpenCode, wired_server,
) -> None:
    opts = OpenCodeOptions(cwd="/tmp", provider="openai", model="gpt-test")
    async with OpenCodeClient(opts) as client:
        sid = client.session_id
        mock_server.script(
            sid,
            [
                step_started("msg1", "model-a"),
                step_ended("msg1", "model-a", input=10, output=1, cost=0.002),
                step_started("msg2", "model-b"),
                step_ended(
                    "msg2", "model-b",
                    input=30, output=4, cache_read=8, cost=0.005,
                ),
                session_idle(),
            ],
        )
        await client.query("hi")
        msgs = await _collect(client)

    result = _final_result(msgs)
    assert result.num_steps == 2
    assert result.model_usage["model-a"]["input"] == 10
    assert result.model_usage["model-a"]["cost"] == pytest.approx(0.002)
    assert result.model_usage["model-b"]["input"] == 30
    assert result.model_usage["model-b"]["cache"] == {"read": 8, "write": 0}
    assert result.model_usage["model-b"]["cost"] == pytest.approx(0.005)
    assert result.total_cost_usd == pytest.approx(0.007)
    assert result.usage["input"] == 40
    assert result.usage["output"] == 5
    assert result.usage["cache"]["read"] == 8


async def test_step_failed_surfaces_error_string(
    mock_server: MockOpenCode, wired_server,
) -> None:
    opts = OpenCodeOptions(cwd="/tmp", provider="openai", model="gpt-test")
    async with OpenCodeClient(opts) as client:
        sid = client.session_id
        mock_server.script(
            sid,
            [
                step_started("msg1", "claude-sonnet-4-6"),
                step_failed(
                    "msg1", "claude-sonnet-4-6",
                    "Prompt is too long", completed=123,
                ),
                session_idle(),
            ],
        )
        await client.query("hi")
        msgs = await _collect(client)

    assistant_msgs = [m for m in msgs if isinstance(m, AssistantMessage)]
    error_msgs = [m for m in assistant_msgs if m.error is not None]
    assert len(error_msgs) == 1
    assert error_msgs[0].error == "Prompt is too long"

    result = _final_result(msgs)
    assert result.is_error is True
    assert result.errors == [
        {"message": "Prompt is too long", "when": 123},
    ]


async def test_mixed_step_ended_and_failed(
    mock_server: MockOpenCode, wired_server,
) -> None:
    opts = OpenCodeOptions(cwd="/tmp", provider="openai", model="gpt-test")
    async with OpenCodeClient(opts) as client:
        sid = client.session_id
        mock_server.script(
            sid,
            [
                step_started("msg1", "model-a"),
                step_ended("msg1", "model-a", input=50, output=10, cost=0.004),
                step_started("msg2", "model-a"),
                step_failed("msg2", "model-a", "rate limit", completed=999),
                session_idle(),
            ],
        )
        await client.query("hi")
        msgs = await _collect(client)

    result = _final_result(msgs)
    assert result.is_error is True
    assert result.num_steps == 2
    assert result.total_cost_usd == pytest.approx(0.004)
    assert result.errors == [{"message": "rate limit", "when": 999}]
    # Usage reflects only the succeeded step (failed step reports no tokens).
    assert result.usage["input"] == 50


@pytest.mark.parametrize("count", [1, 2, 5])
async def test_num_steps_counts_unique_step_ids(
    mock_server: MockOpenCode, wired_server, count: int,
) -> None:
    opts = OpenCodeOptions(cwd="/tmp", provider="openai", model="gpt-test")
    async with OpenCodeClient(opts) as client:
        sid = client.session_id
        script: list[dict] = []
        for i in range(count):
            mid = f"msg{i}"
            script.append(step_started(mid, "model-a"))
            script.append(step_ended(mid, "model-a", input=1, output=1, cost=0.0001))
        script.append(session_idle())
        mock_server.script(sid, script)
        await client.query("hi")
        msgs = await _collect(client)

    result = _final_result(msgs)
    assert result.num_steps == count


async def test_no_step_events_emits_empty_result(
    mock_server: MockOpenCode, wired_server,
) -> None:
    opts = OpenCodeOptions(cwd="/tmp", provider="openai", model="gpt-test")
    async with OpenCodeClient(opts) as client:
        sid = client.session_id
        mock_server.script(sid, [session_idle()])
        await client.query("hi")
        msgs = await _collect(client)

    result = _final_result(msgs)
    assert result.num_steps == 0
    assert result.model_usage == {}
    assert result.errors == []
    assert result.total_cost_usd == 0.0
    assert result.is_error is False


async def test_in_flight_message_update_doesnt_finalise(
    mock_server: MockOpenCode, wired_server,
) -> None:
    """A message.updated with zero tokens and no finish must NOT finalise.

    Mirrors the real OpenCode wire pattern: the first ``message.updated``
    for a new assistant message has all-zero tokens. The wrapper must
    wait for a subsequent update with ``finish`` (or ``error``) before
    folding it in.
    """
    opts = OpenCodeOptions(cwd="/tmp", provider="openai", model="gpt-test")
    async with OpenCodeClient(opts) as client:
        sid = client.session_id
        mock_server.script(
            sid,
            [
                step_started("msg1", "model-a"),
                step_started("msg1", "model-a"),  # repeated in-flight
                step_ended("msg1", "model-a", input=7, output=2, cost=0.001),
                # Duplicate finalised update — must NOT double-count.
                step_ended("msg1", "model-a", input=7, output=2, cost=0.001),
                session_idle(),
            ],
        )
        await client.query("hi")
        msgs = await _collect(client)

    result = _final_result(msgs)
    assert result.num_steps == 1
    assert result.usage["input"] == 7
    assert result.total_cost_usd == pytest.approx(0.001)
    assert len(_assistant_with_usage(msgs)) == 1
