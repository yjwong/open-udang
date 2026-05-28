"""Scheduled task execution and JobQueue integration for OpenShrimp.

Manages recurring and one-shot Claude prompts that run on a schedule.
Each scheduled task gets its own isolated Claude session with read-only
tools (no approval callbacks, no interactive UI).

Data flow:
  CREATE:
    Claude ──▶ MCP create_schedule ──▶ validate ──▶ DB INSERT ──▶ JobQueue

  EXECUTE:
    APScheduler fires ──▶ acquire semaphore ──▶ check context
      ──▶ get_or_create_session (read-only) ──▶ stream results to chat
      ──▶ timeout guard ──▶ failure notification

  RELOAD (bot startup):
    DB SELECT * ──▶ for each task: register with JobQueue (skip stale)
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import Any

import aiosqlite
from telegram import Bot
from telegram.ext import JobQueue

from open_shrimp.config import Config, ContextConfig, is_sandboxed
from open_shrimp.db import (
    ChatScope,
    ScheduledTask,
    delete_scheduled_task_by_id,
    disable_scheduled_task,
    get_all_scheduled_tasks,
)

logger = logging.getLogger(__name__)

# Maximum concurrent scheduled task executions across all chats.
_MAX_CONCURRENT_TASKS = 3
_semaphore = asyncio.Semaphore(_MAX_CONCURRENT_TASKS)

# Minimum interval for recurring tasks (seconds).
_MIN_INTERVAL_SECONDS = 300  # 5 minutes

# Track currently-executing task IDs to enforce max_instances=1.
_running_task_ids: set[int] = set()


# ---------------------------------------------------------------------------
# Schedule parsing
# ---------------------------------------------------------------------------

# Matches interval strings like "30m", "1h", "2d", "90s".
_INTERVAL_RE = re.compile(r"^(\d+)\s*([smhd])$", re.IGNORECASE)

_INTERVAL_MULTIPLIERS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_interval_seconds(expr: str) -> int:
    """Parse an interval expression like '30m' into seconds.

    Raises ValueError if the expression is invalid.
    """
    m = _INTERVAL_RE.match(expr.strip())
    if not m:
        raise ValueError(
            f"Invalid interval expression: {expr!r}. "
            f"Expected format like '30m', '1h', '2d', '90s'."
        )
    value = int(m.group(1))
    unit = m.group(2).lower()
    seconds = value * _INTERVAL_MULTIPLIERS[unit]
    if seconds <= 0:
        raise ValueError("Interval must be positive.")
    return seconds


def validate_schedule(schedule_type: str, schedule_expr: str) -> None:
    """Validate a schedule type and expression.

    Raises ValueError with a user-friendly message if invalid.
    """
    if schedule_type not in ("cron", "interval", "once"):
        raise ValueError(
            f"Invalid schedule_type: {schedule_expr!r}. "
            f"Must be 'cron', 'interval', or 'once'."
        )

    if schedule_type == "interval":
        seconds = parse_interval_seconds(schedule_expr)
        if seconds < _MIN_INTERVAL_SECONDS:
            raise ValueError(
                f"Minimum interval is {_MIN_INTERVAL_SECONDS // 60} minutes. "
                f"Got {seconds} seconds."
            )

    elif schedule_type == "cron":
        # Validate cron by trying to construct an APScheduler CronTrigger.
        from apscheduler.triggers.cron import CronTrigger

        parts = schedule_expr.strip().split()
        if len(parts) != 5:
            raise ValueError(
                f"Cron expression must have 5 fields "
                f"(minute hour day month day_of_week). Got {len(parts)} fields."
            )
        try:
            CronTrigger(
                minute=parts[0],
                hour=parts[1],
                day=parts[2],
                month=parts[3],
                day_of_week=parts[4],
            )
        except (ValueError, TypeError) as exc:
            raise ValueError(f"Invalid cron expression: {exc}") from exc

        # Check minimum interval for cron: reject "* * * * *" (every minute)
        # by checking if the trigger would fire within _MIN_INTERVAL_SECONDS.
        trigger = CronTrigger(
            minute=parts[0],
            hour=parts[1],
            day=parts[2],
            month=parts[3],
            day_of_week=parts[4],
        )
        now = datetime.now(timezone.utc)
        first = trigger.get_next_fire_time(None, now)
        if first is not None:
            second = trigger.get_next_fire_time(first, first)
            if second is not None:
                gap = (second - first).total_seconds()
                if gap < _MIN_INTERVAL_SECONDS:
                    raise ValueError(
                        f"Cron fires too frequently ({gap:.0f}s between runs). "
                        f"Minimum is {_MIN_INTERVAL_SECONDS // 60} minutes."
                    )

    elif schedule_type == "once":
        try:
            dt = datetime.fromisoformat(schedule_expr)
            if dt.tzinfo is None:
                # Treat naive datetimes as UTC.
                dt = dt.replace(tzinfo=timezone.utc)
            else:
                dt = dt.astimezone(timezone.utc)
        except ValueError as exc:
            raise ValueError(
                f"Invalid datetime for one-shot schedule: {schedule_expr!r}. "
                f"Expected ISO 8601 format (e.g. '2026-03-21T09:00:00'). {exc}"
            ) from exc


def _register_task_with_jobqueue(
    job_queue: JobQueue,
    task: ScheduledTask,
    bot: Bot,
    db: aiosqlite.Connection,
    config: Config,
) -> bool:
    """Register a single task with the APScheduler JobQueue.

    Returns True if successfully registered, False if skipped.
    """
    job_name = f"scheduled_task_{task.id}"

    # Remove existing job with this name (in case of re-registration).
    current_jobs = job_queue.get_jobs_by_name(job_name)
    for job in current_jobs:
        job.schedule_removal()

    async def _job_callback(context: Any) -> None:
        await _execute_task(bot, db, config, task, job_queue)

    try:
        if task.schedule_type == "interval":
            seconds = parse_interval_seconds(task.schedule_expr)
            job_queue.run_repeating(
                _job_callback,
                interval=seconds,
                first=seconds,  # First fire after one interval, not immediately.
                name=job_name,
            )

        elif task.schedule_type == "cron":
            from apscheduler.triggers.cron import CronTrigger

            parts = task.schedule_expr.strip().split()
            trigger = CronTrigger(
                minute=parts[0],
                hour=parts[1],
                day=parts[2],
                month=parts[3],
                day_of_week=parts[4],
            )
            # Use run_custom to register cron jobs through PTB's JobQueue
            # wrapper.  Direct scheduler.add_job() bypasses PTB's args
            # wrapping, which causes get_jobs_by_name() to crash with
            # "tuple index out of range" when it calls from_aps_job().
            job_queue.run_custom(
                _job_callback,
                job_kwargs={"trigger": trigger},
                name=job_name,
            )

        elif task.schedule_type == "once":
            dt = datetime.fromisoformat(task.schedule_expr)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            else:
                dt = dt.astimezone(timezone.utc)

            # Skip one-shot tasks whose time has passed.
            if dt <= datetime.now(timezone.utc):
                logger.info(
                    "Skipping past one-shot task %d (%s): %s",
                    task.id,
                    task.name,
                    task.schedule_expr,
                )
                return False

            job_queue.run_once(
                _job_callback,
                when=dt,
                name=job_name,
            )

        else:
            logger.warning(
                "Unknown schedule_type %r for task %d", task.schedule_type, task.id
            )
            return False

    except Exception:
        logger.exception("Failed to register task %d (%s)", task.id, task.name)
        return False

    logger.info(
        "Registered scheduled task %d: %s (%s %s)",
        task.id,
        task.name,
        task.schedule_type,
        task.schedule_expr,
    )
    return True


# ---------------------------------------------------------------------------
# Task execution
# ---------------------------------------------------------------------------


async def _execute_task(
    bot: Bot,
    db: aiosqlite.Connection,
    config: Config,
    task: ScheduledTask,
    job_queue: JobQueue,
) -> None:
    """Execute a single scheduled task.

    This is called by APScheduler when a task fires. It:
    1. Acquires the global semaphore (skips if full).
    2. Checks the task isn't already running (max_instances=1).
    3. Validates the context still exists in config.
    4. Creates an isolated Claude session with read-only tools.
    5. Streams results to the originating chat/thread.
    6. Handles timeout and failure notifications.
    7. Auto-deletes one-shot tasks after execution.
    """
    scope = task.scope

    # Skip if already running (max_instances=1 equivalent).
    if task.id in _running_task_ids:
        logger.info(
            "Skipping task %d (%s): already running", task.id, task.name
        )
        return

    # Try to acquire semaphore (non-blocking).
    if _semaphore.locked() and _semaphore._value == 0:  # noqa: SLF001
        logger.info(
            "Skipping task %d (%s): max concurrent tasks reached",
            task.id,
            task.name,
        )
        return

    async with _semaphore:
        _running_task_ids.add(task.id)
        try:
            await _execute_task_inner(bot, db, config, task, scope, job_queue)
        finally:
            _running_task_ids.discard(task.id)


async def _execute_task_inner(
    bot: Bot,
    db: aiosqlite.Connection,
    config: Config,
    task: ScheduledTask,
    scope: ChatScope,
    job_queue: JobQueue,
) -> None:
    """Inner execution logic, runs under the semaphore."""
    from open_shrimp.handlers.utils import _thread_kwargs

    # Check context exists.
    ctx_config = config.contexts.get(task.context_name)
    if ctx_config is None:
        logger.warning(
            "Scheduled task %d (%s): context %r not found, skipping",
            task.id,
            task.name,
            task.context_name,
        )
        try:
            await bot.send_message(
                chat_id=scope.chat_id,
                text=(
                    f"⚠️ Scheduled task *{_escape_md(task.name)}* skipped: "
                    f"context `{_escape_md(task.context_name)}` no longer exists\\."
                ),
                parse_mode="MarkdownV2",
                **_thread_kwargs(scope),
            )
        except Exception:
            logger.debug("Failed to send context-missing notification")
        return

    logger.info(
        "Executing scheduled task %d (%s) in context %s",
        task.id,
        task.name,
        task.context_name,
    )

    try:
        await asyncio.wait_for(
            _run_scheduled_prompt(bot, scope, task, ctx_config),
            timeout=task.timeout_seconds,
        )
        logger.info("Scheduled task %d (%s) completed", task.id, task.name)

    except asyncio.TimeoutError:
        logger.warning(
            "Scheduled task %d (%s) timed out after %ds",
            task.id,
            task.name,
            task.timeout_seconds,
        )
        try:
            await bot.send_message(
                chat_id=scope.chat_id,
                text=(
                    f"⏱ Scheduled task *{_escape_md(task.name)}* timed out "
                    f"after {task.timeout_seconds // 60} minutes\\."
                ),
                parse_mode="MarkdownV2",
                **_thread_kwargs(scope),
            )
        except Exception:
            logger.debug("Failed to send timeout notification")

    except Exception as exc:
        if _is_thread_not_found(exc):
            await _handle_thread_not_found(bot, db, task, scope, job_queue)
            return

        logger.exception(
            "Scheduled task %d (%s) failed", task.id, task.name
        )
        try:
            err_text = str(exc)[:200]
            await bot.send_message(
                chat_id=scope.chat_id,
                text=(
                    f"❌ Scheduled task *{_escape_md(task.name)}* failed: "
                    f"{_escape_md(err_text)}"
                ),
                parse_mode="MarkdownV2",
                **_thread_kwargs(scope),
            )
        except Exception:
            logger.debug("Failed to send failure notification")

    # Auto-delete one-shot tasks after execution.
    if task.schedule_type == "once":
        try:
            await delete_scheduled_task_by_id(db, task.id)
            logger.info(
                "Auto-deleted one-shot task %d (%s)", task.id, task.name
            )
        except Exception:
            logger.debug("Failed to auto-delete one-shot task %d", task.id)


def _is_thread_not_found(exc: BaseException) -> bool:
    """Check whether an exception is a Telegram 'message thread not found' error."""
    from telegram.error import BadRequest

    if isinstance(exc, BadRequest):
        return "message thread not found" in str(exc).lower()
    # Check explicit chaining (raise ... from ...) and implicit chaining.
    if exc.__cause__ is not None:
        return _is_thread_not_found(exc.__cause__)
    if exc.__context__ is not None:
        return _is_thread_not_found(exc.__context__)
    return False


async def _handle_thread_not_found(
    bot: Bot,
    db: aiosqlite.Connection,
    task: ScheduledTask,
    scope: ChatScope,
    job_queue: JobQueue,
) -> None:
    """Disable a task whose forum topic has been deleted and notify the user."""
    logger.warning(
        "Scheduled task %d (%s): thread %s not found, disabling",
        task.id,
        task.name,
        scope.thread_id,
    )

    try:
        await disable_scheduled_task(db, task.id)
    except Exception:
        logger.debug("Failed to disable task %d in DB", task.id)

    # Remove from JobQueue so it stops firing.
    job_name = f"scheduled_task_{task.id}"
    for job in job_queue.get_jobs_by_name(job_name):
        job.schedule_removal()

    # Notify in the parent chat (without message_thread_id).
    try:
        await bot.send_message(
            chat_id=scope.chat_id,
            text=(
                f"⚠️ Scheduled task *{_escape_md(task.name)}* has been disabled: "
                f"the thread it was created in no longer exists\\."
            ),
            parse_mode="MarkdownV2",
        )
    except Exception:
        logger.debug(
            "Failed to send thread-not-found notification for task %d",
            task.id,
        )


async def _run_scheduled_prompt(
    bot: Bot,
    scope: ChatScope,
    task: ScheduledTask,
    ctx_config: ContextConfig,
) -> None:
    """Run a Claude prompt for a scheduled task with read-only tools.

    Creates an isolated session (not shared with interactive sessions),
    sends the prompt, and streams results to the chat.
    """
    from open_shrimp.opencode_client import (
        OpenCodeClient,
        OpenCodeOptions,
        split_provider_model,
    )
    from open_shrimp.stream import _DraftState, stream_response

    # Read-only tools by default. Bash is only allowed in containerized
    # contexts where Docker provides the safety boundary.
    allowed_tools = [
        "Read",
        "Glob",
        "Grep",
        "WebSearch",
        "WebFetch",
    ]
    if is_sandboxed(ctx_config):
        allowed_tools.append("Bash")

    def _log_stderr(line: str) -> None:
        logger.info("opencode stderr (scheduled %s): %s", task.name, line.rstrip())

    provider, model = split_provider_model(ctx_config.model)
    options = OpenCodeOptions(
        cwd=ctx_config.directory,
        provider=provider,
        model=model,
        effort=ctx_config.effort,
        allowed_tools=allowed_tools,
        add_dirs=ctx_config.additional_directories,
        setting_sources=["project", "user", "local"],
        include_partial_messages=True,
        stderr=_log_stderr,
        max_buffer_size=10 * 1024 * 1024,  # 10MB
        system_prompt=(
            f"You are running as a scheduled task named '{task.name}'. "
            f"This is an automated execution — there is no human watching. "
            f"Be concise and focused. Report findings clearly."
        ),
    )

    client = OpenCodeClient(options=options)
    try:
        await client.connect()
        await client.query(task.prompt)

        draft_state = _DraftState(
            chat_id=scope.chat_id, thread_id=scope.thread_id
        )

        async def _events():
            async for msg in client.receive_response():
                yield msg

        await stream_response(
            bot=bot,
            chat_id=scope.chat_id,
            events=_events(),
            draft_state=draft_state,
            allowed_tools=allowed_tools,
            cwd=ctx_config.directory,
        )
    finally:
        try:
            await client.disconnect()
        except Exception:
            logger.debug("Error disconnecting scheduled task client")


def _escape_md(text: str) -> str:
    """Escape special characters for Telegram MarkdownV2."""
    special = r"_*[]()~`>#+-=|{}.!"
    return "".join(f"\\{c}" if c in special else c for c in text)


# ---------------------------------------------------------------------------
# Startup reload
# ---------------------------------------------------------------------------


async def reload_tasks(
    db: aiosqlite.Connection,
    bot: Bot,
    config: Config,
    job_queue: JobQueue,
) -> int:
    """Load all scheduled tasks from DB and register with JobQueue.

    Called once on bot startup. Returns the number of tasks registered.

    Stale one-shot tasks (datetime in the past) are deleted from the DB.
    Tasks with missing contexts are skipped (not deleted — the context
    may come back after a config change).
    """
    tasks = await get_all_scheduled_tasks(db)
    registered = 0

    for task in tasks:
        # Check context exists.
        if task.context_name not in config.contexts:
            logger.warning(
                "Skipping task %d (%s): context %r not in config",
                task.id,
                task.name,
                task.context_name,
            )
            continue

        # Delete stale one-shot tasks.
        if task.schedule_type == "once":
            try:
                dt = datetime.fromisoformat(task.schedule_expr)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                else:
                    dt = dt.astimezone(timezone.utc)
                if dt <= datetime.now(timezone.utc):
                    await delete_scheduled_task_by_id(db, task.id)
                    logger.info(
                        "Deleted stale one-shot task %d (%s)", task.id, task.name
                    )
                    continue
            except ValueError:
                logger.warning(
                    "Invalid datetime for task %d, deleting", task.id
                )
                await delete_scheduled_task_by_id(db, task.id)
                continue

        if _register_task_with_jobqueue(job_queue, task, bot, db, config):
            registered += 1

    logger.info("Reloaded %d scheduled tasks from database", registered)
    return registered
