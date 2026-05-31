from __future__ import annotations

import asyncio

import pytest

from open_shrimp import agent_tasks
from open_shrimp.agent_tool import AgentToolContext, create_agent_tool, validate_agent_args
from open_shrimp.client_manager import stop_background_task
from open_shrimp.db import ChatScope
from open_shrimp.handlers.state import _active_bg_tasks
from open_shrimp.handlers.state import _running_tasks
from open_shrimp.opencode_client import OpenCodeClient, OpenCodeOptions

from tests.opencode_client.mock_server import MockOpenCode, session_idle, text_delta


class FakeBot:
    def __init__(self) -> None:
        self.messages: list[dict[str, object]] = []

    async def send_message(self, **kwargs):
        self.messages.append(kwargs)


def test_validate_agent_args_defaults_to_fork() -> None:
    args = validate_agent_args({"description": "Search repo", "prompt": "Find it"})

    assert args.description == "Search repo"
    assert args.prompt == "Find it"
    assert args.subagent_type is None
    assert args.is_fork


def test_validate_agent_args_explicit_general_agent() -> None:
    args = validate_agent_args(
        {
            "description": "Search repo",
            "prompt": "Find it",
            "subagent_type": "general",
        }
    )

    assert args.subagent_type == "general"
    assert not args.is_fork


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
async def test_foreground_agent_tool_forks_when_subagent_omitted(
    mock_server: MockOpenCode, wired_server, monkeypatch
) -> None:
    monkeypatch.setenv("OPENSHRIMP_AGENT_AUTO_BACKGROUND_MS", "0")
    opts = OpenCodeOptions(cwd="/repo", provider="openai", model="gpt-test")
    async with OpenCodeClient(opts) as client:
        parent_id = client.session_id
        assert parent_id is not None
        tool = create_agent_tool(
            AgentToolContext(client_getter=lambda: client, cwd="/repo")
        )
        original_fork_session = client.fork_session
        child_id = ""

        async def fork_session_spy(session_id: str, **kwargs):
            nonlocal child_id
            child_id = await original_fork_session(session_id, **kwargs)
            mock_server.script(child_id, [text_delta("p1", "fork answer"), session_idle()])
            return child_id

        client.fork_session = fork_session_spy  # type: ignore[method-assign]
        result = await tool.handler(
            {
                "description": "Research branch",
                "prompt": "Check status",
            }
        )

    assert result["content"][0]["text"] == "fork answer"
    assert mock_server.forked_sessions[-1]["parent_id"] == parent_id
    assert all(
        created["body"].get("parentID") != parent_id
        for created in mock_server.created_sessions
    )
    assert mock_server.prompts[-1]["session_id"] == child_id
    assert "agent" not in mock_server.prompts[-1]["body"]


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
                terminal_base_url="https://example.test",
                user_id=999,
                bot_token="123:abc",
                is_private_chat=True,
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
        assert bot.messages
        assert bot.messages[-1]["chat_id"] == scope.chat_id
        assert bot.messages[-1]["message_thread_id"] == scope.thread_id
        assert str(bot.messages[-1]["text"]).startswith("⏳ Explore code")
        assert f"`{task_id}`" in str(bot.messages[-1]["text"])
        assert bot.messages[-1]["parse_mode"] == "MarkdownV2"
        assert bot.messages[-1]["disable_notification"] is True
        keyboard = bot.messages[-1]["reply_markup"]
        assert (  # type: ignore[attr-defined]
            keyboard.inline_keyboard[0][0].text == "📺 View output"
        )
        assert keyboard.inline_keyboard[0][0].web_app.url == (  # type: ignore[attr-defined]
            f"https://example.test/terminal/?type=task&id={task_id}"
            "&task_type=opencode_agent"
        )
        for _ in range(100):
            if len(bot.messages) >= 2:
                break
            await asyncio.sleep(0.01)

    assert len(bot.messages) >= 2
    assert bot.messages[-1]["chat_id"] == scope.chat_id
    assert bot.messages[-1]["message_thread_id"] == scope.thread_id
    assert bot.messages[-1]["text"] == (
        f"📋 Agent task completed: Explore code\nTask: `{task_id}`"
    )
    assert f"`{task_id}`" in str(bot.messages[-1]["text"])
    assert bot.messages[-1]["parse_mode"] == "MarkdownV2"
    assert bot.messages[-1].get("reply_markup") is None
    assert task_id not in _active_bg_tasks.get(scope, {})
    assert (tmp_path / f"{task_id}.jsonl").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_foreground_agent_auto_backgrounds_after_timeout(
    mock_server: MockOpenCode, wired_server, tmp_path, monkeypatch
) -> None:
    _active_bg_tasks.clear()
    _running_tasks.clear()
    monkeypatch.setenv("OPENSHRIMP_AGENT_AUTO_BACKGROUND_MS", "20")
    monkeypatch.setattr(
        agent_tasks,
        "agent_task_output_path",
        lambda task_id: tmp_path / f"{task_id}.jsonl",
    )
    scope = ChatScope(chat_id=456, thread_id=None)
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
                terminal_base_url="https://example.test",
                user_id=999,
                bot_token="123:abc",
                is_private_chat=True,
            )
        )
        original_create_session = client.create_session

        async def create_session_spy(**kwargs):
            child_id = await original_create_session(**kwargs)
            mock_server.delays[child_id] = 0.1
            mock_server.script(
                child_id, [text_delta("p1", "late answer"), session_idle()],
            )
            return child_id

        client.create_session = create_session_spy  # type: ignore[method-assign]
        result = await tool.handler(
            {
                "description": "Long explore",
                "prompt": "Keep working",
                "subagent_type": "explore",
            }
        )
        text = result["content"][0]["text"]
        task_id = text.split("agentId: ", 1)[1].splitlines()[0]

        assert "Async agent launched successfully" in text
        assert task_id in _active_bg_tasks[scope]
        assert agent_tasks.get_task(task_id).is_backgrounded  # type: ignore[union-attr]
        assert bot.messages
        assert str(bot.messages[-1]["text"]).startswith("⏳ Long explore")

        for _ in range(100):
            task = agent_tasks.get_task(task_id)
            if task is not None and task.status == "completed" and task.injected:
                break
            await asyncio.sleep(0.01)

    task = agent_tasks.get_task(task_id)
    assert task is not None
    assert task.final_text == "late answer"
    assert task_id not in _active_bg_tasks.get(scope, {})
    assert any("Agent task completed" in str(msg["text"]) for msg in bot.messages)


@pytest.mark.asyncio
async def test_background_agent_injects_parent_notification_once(
    mock_server: MockOpenCode, wired_server, tmp_path, monkeypatch
) -> None:
    _active_bg_tasks.clear()
    _running_tasks.clear()
    monkeypatch.setattr(
        agent_tasks,
        "agent_task_output_path",
        lambda task_id: tmp_path / f"{task_id}.jsonl",
    )
    scope = ChatScope(chat_id=321, thread_id=None)
    bot = FakeBot()
    opts = OpenCodeOptions(cwd="/repo", provider="openai", model="gpt-test")

    async with OpenCodeClient(opts) as client:
        parent_session_id = client.session_id
        assert parent_session_id is not None
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
                "subagent_type": "general",
                "run_in_background": True,
            }
        )
        task_id = result["content"][0]["text"].split("agentId: ", 1)[1].splitlines()[0]
        for _ in range(100):
            task = agent_tasks.get_task(task_id)
            if task is not None and task.injected:
                break
            await asyncio.sleep(0.01)

        parent_prompts = [
            prompt for prompt in mock_server.prompts
            if prompt["session_id"] == parent_session_id
        ]
        assert len(parent_prompts) == 1
        text = parent_prompts[0]["body"]["parts"][0]["text"]
        assert "<task-notification>" in text
        assert f"<task-id>{task_id}</task-id>" in text

        await agent_tasks.drain_parent_notifications(parent_session_id, client)
        parent_prompts = [
            prompt for prompt in mock_server.prompts
            if prompt["session_id"] == parent_session_id
        ]
        assert len(parent_prompts) == 1


@pytest.mark.asyncio
async def test_background_agent_wakes_idle_parent_notification_runner(
    mock_server: MockOpenCode, wired_server, tmp_path, monkeypatch
) -> None:
    _active_bg_tasks.clear()
    _running_tasks.clear()
    monkeypatch.setattr(
        agent_tasks,
        "agent_task_output_path",
        lambda task_id: tmp_path / f"{task_id}.jsonl",
    )
    scope = ChatScope(chat_id=987, thread_id=None)
    bot = FakeBot()
    opts = OpenCodeOptions(cwd="/repo", provider="openai", model="gpt-test")

    async with OpenCodeClient(opts) as client:
        parent_session_id = client.session_id
        assert parent_session_id is not None
        wake_scopes: list[ChatScope] = []

        async def wake_parent(notification_scope: ChatScope) -> None:
            wake_scopes.append(notification_scope)
            await agent_tasks.drain_parent_notifications(parent_session_id, client)

        tool = create_agent_tool(
            AgentToolContext(
                client_getter=lambda: client,
                cwd="/repo",
                bot=bot,  # type: ignore[arg-type]
                scope=scope,
                context_name="default",
                on_parent_notification_ready=wake_parent,
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
                "subagent_type": "general",
                "run_in_background": True,
            }
        )
        task_id = result["content"][0]["text"].split("agentId: ", 1)[1].splitlines()[0]
        for _ in range(100):
            task = agent_tasks.get_task(task_id)
            if task is not None and task.injected:
                break
            await asyncio.sleep(0.01)

        assert wake_scopes == [scope]
        parent_prompts = [
            prompt for prompt in mock_server.prompts
            if prompt["session_id"] == parent_session_id
        ]
        assert len(parent_prompts) == 1
        assert (
            f"<task-id>{task_id}</task-id>"
            in parent_prompts[0]["body"]["parts"][0]["text"]
        )


@pytest.mark.asyncio
async def test_background_agent_injects_when_parent_busy(
    mock_server: MockOpenCode, wired_server, tmp_path, monkeypatch
) -> None:
    _active_bg_tasks.clear()
    _running_tasks.clear()
    monkeypatch.setattr(
        agent_tasks,
        "agent_task_output_path",
        lambda task_id: tmp_path / f"{task_id}.jsonl",
    )
    scope = ChatScope(chat_id=654, thread_id=None)
    bot = FakeBot()
    opts = OpenCodeOptions(cwd="/repo", provider="openai", model="gpt-test")

    async with OpenCodeClient(opts) as client:
        parent_session_id = client.session_id
        assert parent_session_id is not None
        busy_task = asyncio.create_task(asyncio.sleep(10))
        _running_tasks[scope] = busy_task
        try:
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
                    "subagent_type": "general",
                    "run_in_background": True,
                }
            )
            task_id = result["content"][0]["text"].split("agentId: ", 1)[1].splitlines()[0]
            for _ in range(100):
                task = agent_tasks.get_task(task_id)
                if task is not None and task.status == "completed":
                    break
                await asyncio.sleep(0.01)

            parent_prompts = [
                prompt for prompt in mock_server.prompts
                if prompt["session_id"] == parent_session_id
            ]
            assert len(parent_prompts) == 1
            assert (
                "<task-notification>"
                in parent_prompts[0]["body"]["parts"][0]["text"]
            )

            busy_task.cancel()
            _running_tasks.pop(scope, None)
            await agent_tasks.drain_parent_notifications(parent_session_id, client)
        finally:
            busy_task.cancel()
            _running_tasks.pop(scope, None)

        parent_prompts = [
            prompt for prompt in mock_server.prompts
            if prompt["session_id"] == parent_session_id
        ]
        assert len(parent_prompts) == 1
        assert "<task-notification>" in parent_prompts[0]["body"]["parts"][0]["text"]


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
                "subagent_type": "general",
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
