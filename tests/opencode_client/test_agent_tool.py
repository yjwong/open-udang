from __future__ import annotations

import asyncio

import pytest

from open_shrimp import agent_tasks
from open_shrimp.agent_tool import AgentToolContext, create_agent_tool, validate_agent_args
from open_shrimp.client_manager import stop_background_task
from open_shrimp.db import ChatScope
from open_shrimp.handlers.state import _active_bg_tasks
from open_shrimp.opencode_client import OpenCodeClient, OpenCodeOptions

from tests.opencode_client.mock_server import MockOpenCode, session_idle, text_delta


class FakeBot:
    def __init__(self) -> None:
        self.messages: list[dict[str, object]] = []

    async def send_message(self, **kwargs):
        self.messages.append(kwargs)


def test_validate_agent_args_defaults_subagent() -> None:
    args = validate_agent_args({"description": "Search repo", "prompt": "Find it"})

    assert args.description == "Search repo"
    assert args.prompt == "Find it"
    assert args.subagent_type == "general"


def test_validate_agent_args_requires_prompt() -> None:
    with pytest.raises(ValueError, match="prompt is required"):
        validate_agent_args({"description": "Search repo"})


@pytest.mark.asyncio
async def test_foreground_agent_tool_runs_child_session(
    mock_server: MockOpenCode, wired_server
) -> None:
    opts = OpenCodeOptions(cwd="/repo", provider="openai", model="gpt-test")
    async with OpenCodeClient(opts) as client:
        tool = create_agent_tool(
            AgentToolContext(client_getter=lambda: client, cwd="/repo")
        )
        child_id = ""

        original_create_session = client.create_session

        async def create_session_spy(**kwargs):
            nonlocal child_id
            child_id = await original_create_session(**kwargs)
            mock_server.script(child_id, [text_delta("p1", "agent answer"), session_idle()])
            return child_id

        client.create_session = create_session_spy  # type: ignore[method-assign]
        result = await tool.handler(
            {
                "description": "Explore code",
                "prompt": "Summarize it",
                "subagent_type": "explore",
            }
        )

    assert result["content"][0]["text"] == "agent answer"
    assert mock_server.created_sessions[-1]["body"]["agent"] == "explore"
    assert mock_server.created_sessions[-1]["body"]["parentID"]
    assert mock_server.prompts[-1]["session_id"] == child_id
    assert mock_server.prompts[-1]["body"]["agent"] == "explore"


@pytest.mark.asyncio
async def test_background_agent_tool_registers_and_notifies(
    mock_server: MockOpenCode, wired_server, tmp_path, monkeypatch
) -> None:
    _active_bg_tasks.clear()
    monkeypatch.setattr(
        agent_tasks,
        "agent_task_output_path",
        lambda task_id: tmp_path / f"{task_id}.jsonl",
    )
    scope = ChatScope(chat_id=123, thread_id=456)
    bot = FakeBot()
    opts = OpenCodeOptions(cwd="/repo", provider="openai", model="gpt-test")

    async with OpenCodeClient(opts) as client:
        tool = create_agent_tool(
            AgentToolContext(
                client_getter=lambda: client,
                cwd="/repo",
                bot=bot,  # type: ignore[arg-type]
                scope=scope,
                context_name="default",
            )
        )
        original_create_session = client.create_session

        async def create_session_spy(**kwargs):
            child_id = await original_create_session(**kwargs)
            mock_server.script(child_id, [text_delta("p1", "done"), session_idle()])
            return child_id

        client.create_session = create_session_spy  # type: ignore[method-assign]
        result = await tool.handler(
            {
                "description": "Explore code",
                "prompt": "Summarize it",
                "subagent_type": "explore",
                "run_in_background": True,
            }
        )
        text = result["content"][0]["text"]
        task_id = text.split("agentId: ", 1)[1].splitlines()[0]

        assert "Async agent launched successfully" in text
        assert task_id in _active_bg_tasks[scope]
        for _ in range(100):
            if bot.messages:
                break
            await asyncio.sleep(0.01)

    assert bot.messages
    assert bot.messages[-1]["chat_id"] == scope.chat_id
    assert bot.messages[-1]["message_thread_id"] == scope.thread_id
    assert "Agent task completed" in str(bot.messages[-1]["text"])
    assert "done" in str(bot.messages[-1]["text"])
    assert task_id not in _active_bg_tasks.get(scope, {})
    assert (tmp_path / f"{task_id}.jsonl").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_background_agent_task_stop_aborts_child_session(
    mock_server: MockOpenCode, wired_server, tmp_path, monkeypatch
) -> None:
    _active_bg_tasks.clear()
    monkeypatch.setattr(
        agent_tasks,
        "agent_task_output_path",
        lambda task_id: tmp_path / f"{task_id}.jsonl",
    )
    scope = ChatScope(chat_id=789, thread_id=None)
    bot = FakeBot()
    opts = OpenCodeOptions(cwd="/repo", provider="openai", model="gpt-test")

    async with OpenCodeClient(opts) as client:
        tool = create_agent_tool(
            AgentToolContext(
                client_getter=lambda: client,
                cwd="/repo",
                bot=bot,  # type: ignore[arg-type]
                scope=scope,
                context_name="default",
            )
        )
        original_create_session = client.create_session
        child_id = ""

        async def create_session_spy(**kwargs):
            nonlocal child_id
            child_id = await original_create_session(**kwargs)
            mock_server.delays[child_id] = 10.0
            mock_server.script(child_id, [text_delta("p1", "late"), session_idle()])
            return child_id

        client.create_session = create_session_spy  # type: ignore[method-assign]
        result = await tool.handler(
            {
                "description": "Long search",
                "prompt": "Keep working",
                "run_in_background": True,
            }
        )
        task_id = (
            result["content"][0]["text"].split("agentId: ", 1)[1].splitlines()[0]
        )
        for _ in range(100):
            if mock_server.prompts and mock_server.prompts[-1]["session_id"] == child_id:
                break
            await asyncio.sleep(0.01)

        assert await stop_background_task(scope, task_id)
        for _ in range(100):
            if bot.messages:
                break
            await asyncio.sleep(0.01)

    assert child_id in mock_server.aborted_sessions
    assert "Agent task stopped" in str(bot.messages[-1]["text"])
