"""OpenShrimp-owned compatibility implementation of Claude Code's Agent tool."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from telegram import Bot

from open_shrimp import agent_tasks
from open_shrimp.db import ChatScope
from open_shrimp.opencode_client import (
    AssistantMessage,
    OpenCodeClient,
    ProcessError,
    ResultMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    split_provider_model,
)
from open_shrimp.tools import OpenShrimpTool

logger = logging.getLogger(__name__)

_DEFAULT_AGENT = "general"


@dataclass(frozen=True)
class AgentArgs:
    description: str
    prompt: str
    subagent_type: str
    model: str | None = None
    run_in_background: bool = False


@dataclass(frozen=True)
class AgentToolContext:
    client_getter: Callable[[], OpenCodeClient | None]
    cwd: str | None = None
    bot: Bot | None = None
    scope: ChatScope | None = None
    context_name: str | None = None


def create_agent_tool(ctx: AgentToolContext) -> OpenShrimpTool:
    async def handler(raw_args: dict[str, Any]) -> dict[str, Any]:
        try:
            args = validate_agent_args(raw_args)
        except ValueError as exc:
            return _text_result(f"Error: {exc}", is_error=True)
        try:
            if args.run_in_background:
                text = await launch_agent_background(args, ctx)
            else:
                text = await run_agent_foreground(args, ctx)
        except Exception as exc:
            logger.exception("Agent tool failed")
            return _text_result(f"Error running agent: {exc}", is_error=True)
        return _text_result(text)

    return OpenShrimpTool(
        name="agent",
        description=(
            "Launch a specialized subagent in a child OpenCode session and return "
            "its final answer. Use this for independent research or focused work. "
            "If subagent_type is omitted, 'general' is used. Available common "
            "agent types include 'general' and 'explore'."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "description": {
                    "type": "string",
                    "description": "A short (3-5 word) description of the task",
                },
                "prompt": {
                    "type": "string",
                    "description": "The task for the agent to perform",
                },
                "subagent_type": {
                    "type": "string",
                    "description": "The type of specialized agent to use for this task",
                },
                "model": {
                    "type": "string",
                    "description": "Optional provider/model override for this agent",
                },
                "run_in_background": {
                    "type": "boolean",
                    "description": (
                        "Run this agent in the background. You will be "
                        "notified when it completes."
                    ),
                },
            },
            "required": ["description", "prompt"],
        },
        read_only=True,
        handler=handler,
    )


def validate_agent_args(raw_args: dict[str, Any]) -> AgentArgs:
    description = str(raw_args.get("description", "")).strip()
    prompt = str(raw_args.get("prompt", "")).strip()
    subagent_type = str(raw_args.get("subagent_type", "")).strip() or _DEFAULT_AGENT
    model_raw = raw_args.get("model")
    model = str(model_raw).strip() if model_raw is not None else None
    run_in_background = bool(raw_args.get("run_in_background"))
    if not description:
        raise ValueError("description is required")
    if not prompt:
        raise ValueError("prompt is required")
    return AgentArgs(
        description=description,
        prompt=prompt,
        subagent_type=subagent_type,
        model=model or None,
        run_in_background=run_in_background,
    )


async def run_agent_foreground(args: AgentArgs, ctx: AgentToolContext) -> str:
    client = ctx.client_getter()
    if client is None:
        raise ProcessError("parent OpenCode client is not available")
    parent_session_id = client.session_id
    if parent_session_id is None:
        raise ProcessError("parent OpenCode session is not available")

    child_model: dict[str, Any] | None = None
    prompt_provider: str | None = None
    prompt_model: str | None = None
    if args.model:
        prompt_provider, prompt_model = split_provider_model(args.model)
        child_model = {"providerID": prompt_provider, "modelID": prompt_model}

    child_session_id = await client.create_session(
        directory=ctx.cwd,
        permission_rules=client.permission_rules,
        parent_id=parent_session_id,
        title=f"{args.description} (@{args.subagent_type} subagent)",
        agent=args.subagent_type,
        model=child_model,
    )
    queue = client.subscribe_session(child_session_id)
    bridge = client.create_permission_bridge(child_session_id)
    text_parts: list[str] = []
    result: ResultMessage | None = None
    try:
        await client.prompt_session(
            child_session_id,
            parts=[{"type": "text", "text": args.prompt}],
            provider=prompt_provider,
            model=prompt_model,
            agent=args.subagent_type,
        )
        async for message in client.iter_session_response(
            child_session_id, queue, bridge=bridge,
        ):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        text_parts.append(block.text)
                    elif isinstance(block, (ToolUseBlock, ToolResultBlock)):
                        continue
            elif isinstance(message, ResultMessage):
                result = message
    finally:
        if bridge is not None:
            await bridge.stop()
        client.unsubscribe_session(child_session_id)

    final_text = "".join(text_parts).strip()
    if result is not None and result.is_error:
        error = _format_errors(result.errors)
        if final_text:
            return f"{final_text}\n\nAgent completed with errors: {error}"
        return f"Agent completed with errors: {error}"
    return final_text or "Agent completed without a text response."


async def launch_agent_background(args: AgentArgs, ctx: AgentToolContext) -> str:
    client = ctx.client_getter()
    if client is None:
        raise ProcessError("parent OpenCode client is not available")
    parent_session_id = client.session_id
    if parent_session_id is None:
        raise ProcessError("parent OpenCode session is not available")
    if ctx.scope is None:
        raise ProcessError("chat scope is not available")

    child_model: dict[str, Any] | None = None
    prompt_provider: str | None = None
    prompt_model: str | None = None
    if args.model:
        prompt_provider, prompt_model = split_provider_model(args.model)
        child_model = {"providerID": prompt_provider, "modelID": prompt_model}

    child_session_id = await client.create_session(
        directory=ctx.cwd,
        permission_rules=client.permission_rules,
        parent_id=parent_session_id,
        title=f"{args.description} (@{args.subagent_type} subagent)",
        agent=args.subagent_type,
        model=child_model,
    )
    task_id = agent_tasks.new_task_id()
    task = agent_tasks.AgentBackgroundTask(
        task_id=task_id,
        scope=ctx.scope,
        context_name=ctx.context_name,
        parent_session_id=parent_session_id,
        child_session_id=child_session_id,
        tool_use_id=None,
        description=args.description,
        prompt=args.prompt,
        subagent_type=args.subagent_type,
        started_at=asyncio.get_running_loop().time(),
        output_path=agent_tasks.agent_task_output_path(task_id),
        status="running",
        abort=lambda: client.abort_session(child_session_id),
    )
    agent_tasks.register_task(task)
    await agent_tasks.append_transcript(
        task,
        "launched",
        description=args.description,
        subagent_type=args.subagent_type,
        child_session_id=child_session_id,
    )
    bg_task = asyncio.create_task(
        _drive_background_agent(task, args, ctx, client, prompt_provider, prompt_model)
    )
    agent_tasks.set_asyncio_task(task_id, bg_task)
    return (
        "Async agent launched successfully.\n"
        f"agentId: {task_id}\n"
        "The agent is working in the background. You will be notified "
        "automatically when it completes.\n"
        "Do not duplicate this agent's work. Continue only with "
        "non-overlapping work, or stop if there is nothing else useful to do."
    )


async def _drive_background_agent(
    task: agent_tasks.AgentBackgroundTask,
    args: AgentArgs,
    ctx: AgentToolContext,
    client: OpenCodeClient,
    prompt_provider: str | None,
    prompt_model: str | None,
) -> None:
    queue = client.subscribe_session(task.child_session_id)
    bridge = client.create_permission_bridge(task.child_session_id)
    text_parts: list[str] = []
    status: agent_tasks.AgentTaskStatus = "completed"
    try:
        await client.prompt_session(
            task.child_session_id,
            parts=[{"type": "text", "text": args.prompt}],
            provider=prompt_provider,
            model=prompt_model,
            agent=args.subagent_type,
        )
        async for message in client.iter_session_response(
            task.child_session_id, queue, bridge=bridge,
        ):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        text_parts.append(block.text)
                        await agent_tasks.append_transcript(
                            task, "assistant_text", text=block.text,
                        )
                    elif isinstance(block, ToolUseBlock):
                        task.tool_uses += 1
                        task.last_tool_name = block.name
                        agent_tasks.update_projection(task)
                        await agent_tasks.append_transcript(
                            task, "tool_start", tool=block.name, tool_use_id=block.id,
                        )
                    elif isinstance(block, ToolResultBlock):
                        await agent_tasks.append_transcript(
                            task,
                            "tool_result",
                            tool_use_id=block.tool_use_id,
                            is_error=block.is_error,
                        )
            elif isinstance(message, ResultMessage):
                task.total_tokens = _total_tokens(message.usage)
                if message.is_error:
                    status = "failed"
                    task.error = _format_errors(message.errors)
        task.final_text = "".join(text_parts).strip()
        if status == "completed" and not task.final_text:
            task.final_text = "Agent completed without a text response."
    except asyncio.CancelledError:
        status = "killed"
        task.error = "stopped"
        await agent_tasks.append_transcript(task, "stopped")
        raise
    except Exception as exc:
        if task.status == "killed":
            status = "killed"
            task.error = task.error or "stopped"
        else:
            status = "failed"
            task.error = str(exc)
            logger.exception("Background Agent task %s failed", task.task_id)
    finally:
        if bridge is not None:
            await bridge.stop()
        client.unsubscribe_session(task.child_session_id)
        if task.status == "killed":
            status = "killed"
        agent_tasks.complete_task(task, status)
        await agent_tasks.append_transcript(
            task,
            "finished",
            status=status,
            final_text=task.final_text,
            error=task.error,
            total_tokens=task.total_tokens,
            tool_uses=task.tool_uses,
        )
        await _send_task_notification(ctx, task, status)


async def _send_task_notification(
    ctx: AgentToolContext,
    task: agent_tasks.AgentBackgroundTask,
    status: str,
) -> None:
    if ctx.bot is None or ctx.scope is None:
        return
    thread_kwargs: dict[str, Any] = {}
    if ctx.scope.thread_id is not None:
        thread_kwargs["message_thread_id"] = ctx.scope.thread_id
    if status == "completed":
        result = (task.final_text or "").strip()
        if len(result) > 3000:
            result = result[:3000] + "\n... (truncated)"
        text = (
            f"Agent task completed: {task.description}\n"
            f"Task: {task.task_id}\n\n{result}"
        )
    elif status == "killed":
        text = f"Agent task stopped: {task.description}\nTask: {task.task_id}"
    else:
        text = (
            f"Agent task failed: {task.description}\n"
            f"Task: {task.task_id}\n"
            f"Error: {task.error or 'unknown error'}"
        )
    try:
        await ctx.bot.send_message(
            chat_id=ctx.scope.chat_id, text=text, **thread_kwargs,
        )
    except Exception:
        logger.exception("Failed to send Agent task notification for %s", task.task_id)


def _total_tokens(usage: dict[str, Any] | None) -> int:
    if not isinstance(usage, dict):
        return 0
    total = 0
    for key in ("input", "output", "reasoning"):
        value = usage.get(key)
        if isinstance(value, (int, float)):
            total += int(value)
    cache = usage.get("cache")
    if isinstance(cache, dict):
        for value in cache.values():
            if isinstance(value, (int, float)):
                total += int(value)
    return total


def _format_errors(errors: list[dict[str, Any]] | None) -> str:
    if not errors:
        return "unknown error"
    messages = [str(err.get("message", "")).strip() for err in errors]
    messages = [msg for msg in messages if msg]
    return "; ".join(messages) if messages else "unknown error"


def _text_result(text: str, is_error: bool = False) -> dict[str, Any]:
    result: dict[str, Any] = {"content": [{"type": "text", "text": text}]}
    if is_error:
        result["is_error"] = True
    return result
