"""Python-owned background Agent task registry."""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from open_shrimp.db import ChatScope
from open_shrimp.handlers.state import TrackedTask, _active_bg_tasks

logger = logging.getLogger(__name__)

AgentTaskStatus = Literal["running", "completed", "failed", "killed"]


@dataclass
class AgentBackgroundTask:
    task_id: str
    scope: ChatScope
    context_name: str | None
    parent_session_id: str
    child_session_id: str
    tool_use_id: str | None
    description: str
    prompt: str
    subagent_type: str
    started_at: float
    output_path: Path
    status: AgentTaskStatus
    abort: Callable[[], Awaitable[None]]
    asyncio_task: asyncio.Task[None] | None = None
    last_tool_name: str | None = None
    total_tokens: int = 0
    tool_uses: int = 0
    final_text: str | None = None
    error: str | None = None


_tasks: dict[str, AgentBackgroundTask] = {}


def new_task_id() -> str:
    return "a" + secrets.token_hex(8)


def agent_task_output_path(task_id: str) -> Path:
    from open_shrimp.paths import data_dir

    directory = data_dir() / "agent-tasks"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{task_id}.jsonl"


def register_task(task: AgentBackgroundTask) -> None:
    _tasks[task.task_id] = task
    _active_bg_tasks.setdefault(task.scope, {})[task.task_id] = TrackedTask(
        task_id=task.task_id,
        description=task.description,
        task_type="opencode_agent",
        started_at=task.started_at,
        tool_use_id=task.tool_use_id,
        session_id=task.child_session_id,
        last_tool_name=task.last_tool_name,
    )


def set_asyncio_task(task_id: str, asyncio_task: asyncio.Task[None]) -> None:
    task = _tasks.get(task_id)
    if task is not None:
        task.asyncio_task = asyncio_task


def update_projection(task: AgentBackgroundTask) -> None:
    tracked = _active_bg_tasks.get(task.scope, {}).get(task.task_id)
    if tracked is not None:
        tracked.last_tool_name = task.last_tool_name


def complete_task(task: AgentBackgroundTask, status: AgentTaskStatus) -> None:
    task.status = status
    scope_tasks = _active_bg_tasks.get(task.scope)
    if scope_tasks is not None:
        scope_tasks.pop(task.task_id, None)
        if not scope_tasks:
            _active_bg_tasks.pop(task.scope, None)


def get_task(task_id: str) -> AgentBackgroundTask | None:
    return _tasks.get(task_id)


def find_task(scope: ChatScope, task_id_or_prefix: str) -> AgentBackgroundTask | None:
    for task_id, task in _tasks.items():
        if task.scope == scope and (
            task_id == task_id_or_prefix or task_id.startswith(task_id_or_prefix)
        ):
            return task
    return None


async def stop_task(scope: ChatScope, task_id_or_prefix: str) -> bool:
    task = find_task(scope, task_id_or_prefix)
    if task is None or task.status != "running":
        return False
    task.status = "killed"
    task.error = "stopped"
    try:
        await task.abort()
    except Exception:
        logger.exception("Failed to abort Agent task %s", task.task_id)
    if task.asyncio_task is not None and not task.asyncio_task.done():
        task.asyncio_task.cancel()
    return True


async def append_transcript(
    task: AgentBackgroundTask, event: str, **fields: object,
) -> None:
    payload = {
        "time": time.time(),
        "event": event,
        "task_id": task.task_id,
        **fields,
    }
    line = json.dumps(payload, ensure_ascii=False) + "\n"
    await asyncio.to_thread(_append_line, task.output_path, line)


def _append_line(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(line)
