"""Python-owned background Agent task registry."""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from html import escape
from pathlib import Path
from typing import Literal, Protocol

from open_shrimp.db import ChatScope
from open_shrimp.handlers.state import TrackedTask, _active_bg_tasks, _running_tasks

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
    notified: bool = False
    injected: bool = False
    is_backgrounded: bool = True


class ParentPromptClient(Protocol):
    async def prompt_session(
        self,
        session_id: str,
        *,
        parts: list[dict[str, object]],
    ) -> None: ...


_tasks: dict[str, AgentBackgroundTask] = {}
_pending_notifications: dict[str, list[tuple[str, str]]] = {}
_injection_locks: dict[str, asyncio.Lock] = {}


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


def get_task_output_path(task_id: str) -> Path | None:
    task = _tasks.get(task_id)
    if task is None:
        return None
    return task.output_path


def is_task_running(task_id: str) -> bool:
    task = _tasks.get(task_id)
    return task is not None and task.status == "running"


def parent_session_busy(scope: ChatScope) -> bool:
    running = _running_tasks.get(scope)
    return running is not None and not running.done()


def build_task_notification_payload(task: AgentBackgroundTask) -> str:
    status = task.status
    summary_status = {
        "completed": "completed",
        "failed": "failed",
        "killed": "stopped",
    }.get(status, status)
    if status == "completed":
        result = task.final_text or "Agent completed without a text response."
    elif status == "killed":
        result = task.final_text or "Agent task was stopped."
    else:
        result = task.error or "unknown error"
    return (
        "<task-notification>\n"
        f"<task-id>{escape(task.task_id)}</task-id>\n"
        f"<status>{escape(status)}</status>\n"
        f"<summary>Agent \"{escape(task.description)}\" {summary_status}</summary>\n"
        f"<result>{escape(result)}</result>\n"
        "</task-notification>"
    )


def enqueue_parent_notification(task: AgentBackgroundTask, payload: str) -> None:
    if task.injected:
        return
    queue = _pending_notifications.setdefault(task.parent_session_id, [])
    if not any(task_id == task.task_id for task_id, _payload in queue):
        queue.append((task.task_id, payload))


def has_parent_notifications(parent_session_id: str) -> bool:
    queue = _pending_notifications.get(parent_session_id)
    return bool(queue)


async def drain_parent_notifications(
    parent_session_id: str,
    client: ParentPromptClient,
) -> int:
    lock = _injection_locks.setdefault(parent_session_id, asyncio.Lock())
    async with lock:
        injected = 0
        while True:
            queue = _pending_notifications.get(parent_session_id)
            if not queue:
                _pending_notifications.pop(parent_session_id, None)
                return injected
            task_id, payload = queue.pop(0)
            task = _tasks.get(task_id)
            if task is None or task.injected:
                continue
            try:
                await client.prompt_session(
                    parent_session_id,
                    parts=[{"type": "text", "text": payload}],
                )
            except Exception:
                queue.insert(0, (task_id, payload))
                logger.exception(
                    "Failed to inject Agent notification %s into parent session %s",
                    task_id,
                    parent_session_id,
                )
                return injected
            task.injected = True
            injected += 1


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
