"""Custom MCP tools exposed to the Claude agent via SDK MCP servers.

Provides in-process tools that Claude can call directly, with access to
the Telegram Bot instance for sending files, images, etc.
"""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import os
import signal
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from open_shrimp.sandbox.base import Sandbox

from claude_agent_sdk import ToolAnnotations, create_sdk_mcp_server, tool
from claude_agent_sdk.types import McpSdkServerConfig
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup

from open_shrimp.web_app_button import make_web_app_button

logger = logging.getLogger(__name__)

# Telegram Bot API limits.
_MAX_DOCUMENT_SIZE = 50 * 1024 * 1024  # 50 MB
_MAX_PHOTO_SIZE = 10 * 1024 * 1024  # 10 MB

# MIME types that Telegram can display as inline photos.
_PHOTO_MIME_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}


def _text_result(text: str, is_error: bool = False) -> dict[str, Any]:
    """Build a standard MCP tool result."""
    result: dict[str, Any] = {
        "content": [{"type": "text", "text": text}],
    }
    if is_error:
        result["is_error"] = True
    return result


def _guess_mime(path: str) -> str | None:
    """Guess the MIME type from the file extension."""
    mime, _ = mimetypes.guess_type(path)
    return mime


def create_openshrimp_mcp_server(
    bot: Bot,
    chat_id: int,
    thread_id: int | None = None,
    db: Any | None = None,
    config: Any | None = None,
    job_queue: Any | None = None,
    sandbox: "Sandbox | None" = None,
    context_name: str | None = None,
    user_id: int = 0,
    is_private_chat: bool = True,
    host_bash_workdir: str | None = None,
) -> McpSdkServerConfig:
    """Create an in-process MCP server with OpenShrimp-specific tools.

    The returned server is bound to a specific *bot*, *chat_id*, and
    optional *thread_id* so tool handlers can send files directly to the
    correct Telegram chat or forum thread.

    When *db*, *config*, and *job_queue* are provided, scheduling tools
    (create_schedule, list_schedules, delete_schedule) are also registered.

    Computer-use tools are registered when *sandbox* has a non-``None``
    :meth:`~Sandbox.get_screenshots_dir`, indicating the backend supports
    GUI interaction.

    Args:
        bot: Telegram Bot instance.
        chat_id: Telegram chat ID to send files to.
        thread_id: Optional message_thread_id for forum topics.
        db: Optional aiosqlite connection for scheduled task persistence.
        config: Optional Config for context validation.
        job_queue: Optional JobQueue for registering scheduled jobs.
        sandbox: Optional sandbox with computer-use support.

    Returns:
        An ``McpSdkServerConfig`` ready for ``ClaudeAgentOptions.mcp_servers``.
    """

    # Build common kwargs for message_thread_id support.
    _thread_kwargs: dict[str, Any] = {}
    if thread_id is not None:
        _thread_kwargs["message_thread_id"] = thread_id

    @tool(
        "send_file",
        "Send a file to the user via Telegram. Use this when the user asks "
        "you to send, share, or deliver a file. The file must exist on the "
        "local filesystem. Maximum size is 50 MB. Images (JPEG, PNG, GIF, "
        "WebP) under 10 MB are automatically sent as inline photos unless "
        "type is set to 'document'.",
        {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Absolute path to the file to send.",
                },
                "caption": {
                    "type": "string",
                    "description": "Optional caption to display with the file.",
                },
                "type": {
                    "type": "string",
                    "enum": ["auto", "photo", "document"],
                    "description": (
                        "How to send the file. 'photo' sends as an inline "
                        "image (max 10 MB, JPEG/PNG/GIF/WebP only). "
                        "'document' sends as a file attachment. 'auto' "
                        "(default) picks photo for eligible images, "
                        "document otherwise."
                    ),
                },
            },
            "required": ["file_path"],
        },
        annotations=ToolAnnotations(readOnlyHint=True),
    )
    async def send_file(args: dict[str, Any]) -> dict[str, Any]:
        file_path = args.get("file_path", "")
        caption = args.get("caption")
        send_type = args.get("type", "auto")

        if not file_path:
            return _text_result("Error: file_path is required.", is_error=True)

        path = os.path.abspath(file_path)

        if not os.path.isfile(path):
            return _text_result(
                f"Error: File not found: {path}", is_error=True,
            )

        size = os.path.getsize(path)
        if size == 0:
            return _text_result("Error: File is empty.", is_error=True)

        if size > _MAX_DOCUMENT_SIZE:
            mb = size / (1024 * 1024)
            return _text_result(
                f"Error: File too large ({mb:.1f} MB). "
                f"Telegram limit is 50 MB.",
                is_error=True,
            )

        # Decide whether to send as photo or document.
        mime = _guess_mime(path)
        use_photo = False
        if send_type == "photo":
            use_photo = True
        elif send_type == "auto" and mime in _PHOTO_MIME_TYPES:
            use_photo = size <= _MAX_PHOTO_SIZE

        if use_photo and size > _MAX_PHOTO_SIZE:
            mb = size / (1024 * 1024)
            return _text_result(
                f"Error: Photo too large ({mb:.1f} MB). "
                f"Telegram limit for photos is 10 MB. "
                f"Use type='document' for larger images.",
                is_error=True,
            )

        filename = os.path.basename(path)

        # Build a "Preview" WebApp button for markdown files.
        reply_markup = None
        if filename.lower().endswith(".md") and config is not None:
            base_url = None
            if config.review.public_url:
                base_url = config.review.public_url.rstrip("/")
            elif config.review.host and config.review.port:
                base_url = f"https://{config.review.host}:{config.review.port}"
            if base_url:
                from urllib.parse import quote

                preview_params = f"path={quote(path, safe='')}&chat_id={chat_id}"
                if thread_id is not None:
                    preview_params += f"&thread_id={thread_id}"
                preview_url = f"{base_url}/preview/?{preview_params}"
                reply_markup = InlineKeyboardMarkup([[
                    make_web_app_button(
                        "📖 Preview",
                        preview_url,
                        chat_id=chat_id,
                        user_id=user_id,
                        bot_token=config.telegram.token,
                        is_private_chat=is_private_chat,
                    ),
                ]])

        try:
            with open(path, "rb") as f:
                if use_photo:
                    await bot.send_photo(
                        chat_id=chat_id,
                        photo=f,
                        caption=caption,
                        **_thread_kwargs,
                    )
                else:
                    await bot.send_document(
                        chat_id=chat_id,
                        document=f,
                        filename=filename,
                        caption=caption,
                        reply_markup=reply_markup,
                        **_thread_kwargs,
                    )
            method = "photo" if use_photo else "document"
            logger.info("Sent %s %s to chat %d", method, path, chat_id)
            return _text_result(f"File sent successfully: {filename}")
        except Exception as exc:
            logger.exception("Failed to send file %s to chat %d", path, chat_id)
            return _text_result(
                f"Error sending file: {exc}", is_error=True,
            )

    # --- edit_topic (forum topics only) ---
    # Only register this tool when the chat is a forum topic, so Claude
    # can set/update the thread title and/or icon.
    tools_list: list[Any] = [send_file]

    if thread_id is not None:
        # Cache for emoji -> custom_emoji_id mapping, populated lazily.
        _emoji_map: dict[str, str] | None = None

        async def _get_emoji_map() -> dict[str, str]:
            nonlocal _emoji_map
            if _emoji_map is None:
                stickers = await bot.get_forum_topic_icon_stickers()
                _emoji_map = {
                    s.emoji: s.custom_emoji_id
                    for s in stickers
                    if s.emoji and s.custom_emoji_id
                }
                logger.info(
                    "Loaded %d forum topic icon stickers", len(_emoji_map),
                )
            return _emoji_map

        @tool(
            "edit_topic",
            "Set or update the title and/or icon of the current Telegram "
            "forum topic. Use this after your first response to set a "
            "concise title (max 128 chars) summarizing the conversation. "
            "If the topic changes significantly later, update the title "
            "again. Optionally set an icon using a standard emoji (e.g. "
            '"📝", "🔥", "💬", "🤖"). Pass an empty string for icon '
            "to remove it.",
            {
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": (
                            "The title for the forum topic. Should be a "
                            "short, descriptive summary (max 128 characters)."
                        ),
                    },
                    "icon": {
                        "type": "string",
                        "description": (
                            "A standard emoji to use as the topic icon "
                            '(e.g. "📝", "🔥", "🤖"). Pass an empty '
                            "string to remove the icon. If omitted, the "
                            "current icon is kept."
                        ),
                    },
                },
            },
            annotations=ToolAnnotations(readOnlyHint=True),
        )
        async def edit_topic(args: dict[str, Any]) -> dict[str, Any]:
            title = args.get("title", "").strip() or None
            icon = args.get("icon")

            if title is not None and len(title) > 128:
                title = title[:128]

            if title is None and icon is None:
                return _text_result(
                    "Error: at least one of title or icon is required.",
                    is_error=True,
                )

            # Resolve emoji to custom_emoji_id.
            icon_custom_emoji_id: str | None = None
            if icon is not None:
                if icon == "":
                    # Empty string = remove icon.
                    icon_custom_emoji_id = ""
                else:
                    emoji_map = await _get_emoji_map()
                    icon_custom_emoji_id = emoji_map.get(icon)
                    if icon_custom_emoji_id is None:
                        # List some available options for the agent.
                        sample = list(emoji_map.keys())[:20]
                        return _text_result(
                            f"Error: emoji {icon!r} is not available as a "
                            f"topic icon. Some available emoji: "
                            f"{' '.join(sample)}",
                            is_error=True,
                        )

            # Build kwargs — only pass what was requested.
            edit_kwargs: dict[str, Any] = {}
            if title is not None:
                edit_kwargs["name"] = title
            if icon_custom_emoji_id is not None:
                edit_kwargs["icon_custom_emoji_id"] = icon_custom_emoji_id

            try:
                await bot.edit_forum_topic(
                    chat_id=chat_id,
                    message_thread_id=thread_id,
                    **edit_kwargs,
                )
            except Exception as exc:
                logger.exception(
                    "Failed to edit topic in chat %d thread %d",
                    chat_id, thread_id,
                )
                return _text_result(
                    f"Error editing topic: {exc}", is_error=True,
                )

            parts = []
            if title is not None:
                parts.append(f"title={title!r}")
            if icon is not None:
                parts.append(
                    f"icon={icon!r}" if icon else "icon removed",
                )
            summary = ", ".join(parts)
            logger.info(
                "Edited topic (%s) in chat %d thread %d",
                summary, chat_id, thread_id,
            )
            return _text_result(f"Topic updated: {summary}")

        tools_list.append(edit_topic)

    # --- Scheduling tools (when db, config, and job_queue are available) ---
    if db is not None and config is not None and job_queue is not None:
        from open_shrimp.db import (
            ChatScope,
            ScheduledTask,
            create_scheduled_task,
            delete_scheduled_task,
            delete_scheduled_task_by_id,
            list_scheduled_tasks,
        )
        from open_shrimp.scheduler import (
            _register_task_with_jobqueue,
            validate_schedule,
        )

        _scope = ChatScope(
            chat_id=chat_id,
            thread_id=thread_id,
        )

        @tool(
            "create_schedule",
            "Create a scheduled task that runs a Claude prompt automatically. "
            "The task will run in the current chat/thread with read-only tools. "
            "Supports three schedule types: 'interval' (e.g. '30m', '1h', '2d'), "
            "'cron' (standard 5-field cron: 'minute hour day month day_of_week'), "
            "and 'once' (ISO 8601 datetime for a one-shot task). "
            "Minimum interval for recurring tasks is 5 minutes. "
            "Maximum 20 scheduled tasks per chat.",
            {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": (
                            "A short, descriptive name for this task "
                            "(e.g. 'CI check', 'daily summary'). Must be "
                            "unique within this chat."
                        ),
                    },
                    "prompt": {
                        "type": "string",
                        "description": (
                            "The prompt to send to Claude when the task fires. "
                            "Be specific about what you want checked or summarized."
                        ),
                    },
                    "schedule_type": {
                        "type": "string",
                        "enum": ["interval", "cron", "once"],
                        "description": (
                            "Type of schedule: 'interval' for recurring with "
                            "a fixed gap (e.g. every 30m), 'cron' for "
                            "time-of-day patterns (e.g. 9am weekdays), "
                            "'once' for a single future execution."
                        ),
                    },
                    "schedule_expr": {
                        "type": "string",
                        "description": (
                            "The schedule expression. For 'interval': '30m', "
                            "'1h', '2d'. For 'cron': '0 9 * * 1-5' (9am "
                            "weekdays). For 'once': ISO datetime like "
                            "'2026-03-21T09:00:00'."
                        ),
                    },
                    "timeout_seconds": {
                        "type": "integer",
                        "description": (
                            "Maximum execution time in seconds. Default 600 "
                            "(10 minutes). The task is cancelled if it exceeds "
                            "this timeout."
                        ),
                    },
                },
                "required": ["name", "prompt", "schedule_type", "schedule_expr"],
            },
            annotations=ToolAnnotations(readOnlyHint=False),
        )
        async def create_schedule(args: dict[str, Any]) -> dict[str, Any]:
            name = args.get("name", "").strip()
            prompt = args.get("prompt", "").strip()
            schedule_type = args.get("schedule_type", "")
            schedule_expr = args.get("schedule_expr", "")
            timeout_seconds = args.get("timeout_seconds", 600)

            if not name:
                return _text_result("Error: name is required.", is_error=True)
            if not prompt:
                return _text_result("Error: prompt is required.", is_error=True)

            # Get the active context for this scope.
            from open_shrimp.db import get_active_context

            context_name = await get_active_context(db, _scope)
            if not context_name:
                context_name = config.default_context

            try:
                validate_schedule(schedule_type, schedule_expr)
            except ValueError as exc:
                return _text_result(f"Error: {exc}", is_error=True)

            try:
                task = await create_scheduled_task(
                    db,
                    _scope,
                    context_name,
                    name,
                    prompt,
                    schedule_type,
                    schedule_expr,
                    timeout_seconds,
                )
            except ValueError as exc:
                return _text_result(f"Error: {exc}", is_error=True)
            except Exception as exc:
                if "UNIQUE constraint" in str(exc):
                    return _text_result(
                        f"Error: A task named '{name}' already exists in "
                        f"this chat. Choose a different name or delete the "
                        f"existing one first.",
                        is_error=True,
                    )
                logger.exception("Failed to create scheduled task")
                return _text_result(f"Error creating task: {exc}", is_error=True)

            # Register with JobQueue. If registration fails, roll back
            # the DB insert to avoid orphaned tasks that never fire.
            registered = _register_task_with_jobqueue(job_queue, task, bot, db, config)
            if not registered:
                await delete_scheduled_task_by_id(db, task.id)
                return _text_result(
                    "Error: failed to register task with scheduler. "
                    "The task was not created.",
                    is_error=True,
                )

            type_desc = {
                "interval": f"every {schedule_expr}",
                "cron": f"cron: {schedule_expr}",
                "once": f"at {schedule_expr}",
            }.get(schedule_type, schedule_expr)

            return _text_result(
                f"Scheduled task '{name}' created successfully.\n"
                f"Schedule: {type_desc}\n"
                f"Context: {context_name}\n"
                f"Timeout: {timeout_seconds}s\n"
                f"Prompt: {prompt[:100]}{'...' if len(prompt) > 100 else ''}"
            )

        @tool(
            "list_schedules",
            "List all scheduled tasks in the current chat/thread. "
            "Shows task name, schedule, context, and prompt.",
            {
                "type": "object",
                "properties": {},
            },
            annotations=ToolAnnotations(readOnlyHint=True),
        )
        async def list_schedules(args: dict[str, Any]) -> dict[str, Any]:
            tasks = await list_scheduled_tasks(db, _scope)

            if not tasks:
                return _text_result("No scheduled tasks in this chat.")

            lines = [f"Scheduled tasks ({len(tasks)}):"]
            for t in tasks:
                type_desc = {
                    "interval": f"every {t.schedule_expr}",
                    "cron": f"cron: {t.schedule_expr}",
                    "once": f"at {t.schedule_expr}",
                }.get(t.schedule_type, t.schedule_expr)

                prompt_preview = t.prompt[:60] + ("..." if len(t.prompt) > 60 else "")
                disabled_label = " [disabled]" if t.disabled else ""
                lines.append(
                    f"\n• {t.name}{disabled_label}\n"
                    f"  Schedule: {type_desc}\n"
                    f"  Context: {t.context_name}\n"
                    f"  Timeout: {t.timeout_seconds}s\n"
                    f"  Prompt: {prompt_preview}\n"
                    f"  Created: {t.created_at}"
                )

            return _text_result("\n".join(lines))

        @tool(
            "delete_schedule",
            "Delete a scheduled task by name. The task will stop firing "
            "immediately.",
            {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "The name of the scheduled task to delete.",
                    },
                },
                "required": ["name"],
            },
            annotations=ToolAnnotations(readOnlyHint=False),
        )
        async def delete_schedule(args: dict[str, Any]) -> dict[str, Any]:
            name = args.get("name", "").strip()
            if not name:
                return _text_result("Error: name is required.", is_error=True)

            # Look up the task ID before deleting so we can remove the
            # correct job from the JobQueue.
            tasks = await list_scheduled_tasks(db, _scope)
            task_id = None
            for t in tasks:
                if t.name == name:
                    task_id = t.id
                    break

            deleted = await delete_scheduled_task(db, _scope, name)
            if not deleted:
                return _text_result(
                    f"No scheduled task named '{name}' found in this chat.",
                    is_error=True,
                )

            # Remove the corresponding job from JobQueue.
            if task_id is not None:
                job_name = f"scheduled_task_{task_id}"
                for j in job_queue.get_jobs_by_name(job_name):
                    j.schedule_removal()

            return _text_result(f"Scheduled task '{name}' deleted successfully.")

        tools_list.extend([create_schedule, list_schedules, delete_schedule])

    # --- Computer use tools (GUI interaction) ---
    # All backends implement the same Sandbox protocol methods
    # (take_screenshot, send_click, send_type, send_key, send_scroll,
    # focus_window), so the tool handlers are backend-agnostic.
    _cu_sandbox = sandbox
    _screenshots_dir: str | None = None
    if _cu_sandbox is not None:
        sd = _cu_sandbox.get_screenshots_dir()
        if sd is not None:
            _screenshots_dir = str(sd)

    if _cu_sandbox is not None and _screenshots_dir is not None:

        @tool(
            "computer_screenshot",
            "Take a screenshot of the current screen. The screen is a "
            "headless 1280x720 Linux desktop with a Wayland compositor. "
            "Returns the file path of the screenshot. Use the Read tool "
            "to view the screenshot image. Always take a screenshot first "
            "to understand the current state before interacting.",
            {
                "type": "object",
                "properties": {},
            },
            annotations=ToolAnnotations(readOnlyHint=True),
        )
        async def computer_screenshot(args: dict[str, Any]) -> dict[str, Any]:
            ts = int(time.time() * 1000)
            host_path = os.path.join(
                _screenshots_dir or "/tmp", f"screenshot-{ts}.png",
            )

            try:
                await asyncio.to_thread(
                    _cu_sandbox.take_screenshot, Path(host_path),
                )
            except Exception as exc:
                return _text_result(
                    f"Error taking screenshot: {exc}", is_error=True,
                )

            # Send screenshot to Telegram for user observability.
            try:
                if os.path.isfile(host_path):
                    # Build "View desktop" button if VNC Mini App is available.
                    reply_markup = None
                    if context_name and config is not None:
                        base_url = None
                        if config.review.public_url:
                            base_url = config.review.public_url.rstrip("/")
                        elif config.review.host and config.review.port:
                            base_url = f"https://{config.review.host}:{config.review.port}"
                        if base_url:
                            vnc_url = f"{base_url}/vnc/?context={context_name}"
                            reply_markup = InlineKeyboardMarkup([[
                                make_web_app_button(
                                    "View desktop",
                                    vnc_url,
                                    chat_id=chat_id,
                                    user_id=user_id,
                                    bot_token=config.telegram.token,
                                    is_private_chat=is_private_chat,
                                ),
                            ]])

                    with open(host_path, "rb") as f:
                        await bot.send_photo(
                            chat_id=chat_id,
                            photo=f,
                            caption="Screenshot",
                            reply_markup=reply_markup,
                            **_thread_kwargs,
                        )
            except Exception:
                logger.debug(
                    "Failed to send screenshot to Telegram", exc_info=True
                )

            return _text_result(
                f"Screenshot saved to {host_path}. "
                f"Use the Read tool to view the image."
            )

        @tool(
            "computer_click",
            "Click at a specific position on the screen. Moves the pointer "
            "to (x, y) and performs a click. Screen size is 1280x720.",
            {
                "type": "object",
                "properties": {
                    "x": {
                        "type": "integer",
                        "description": "X coordinate (0-1279).",
                    },
                    "y": {
                        "type": "integer",
                        "description": "Y coordinate (0-719).",
                    },
                    "button": {
                        "type": "string",
                        "enum": ["left", "right", "middle"],
                        "description": (
                            "Mouse button to click. Default: left."
                        ),
                    },
                },
                "required": ["x", "y"],
            },
        )
        async def computer_click(args: dict[str, Any]) -> dict[str, Any]:
            x = args.get("x", 0)
            y = args.get("y", 0)
            button = args.get("button", "left")

            if not (0 <= x < 1280 and 0 <= y < 720):
                return _text_result(
                    f"Error: coordinates ({x}, {y}) out of range "
                    f"(screen is 1280x720).",
                    is_error=True,
                )

            btn = {"left": "left", "right": "right", "middle": "middle"}.get(
                button, "left",
            )

            try:
                await asyncio.to_thread(
                    _cu_sandbox.send_click, x, y, btn,
                )
            except Exception as exc:
                return _text_result(
                    f"Error clicking: {exc}", is_error=True,
                )

            return _text_result(f"Clicked {btn} at ({x}, {y}).")

        @tool(
            "computer_type",
            "Type text into the currently focused window. The text is sent "
            "as keyboard input character by character.",
            {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "The text to type.",
                    },
                },
                "required": ["text"],
            },
        )
        async def computer_type(args: dict[str, Any]) -> dict[str, Any]:
            text = args.get("text", "")
            if not text:
                return _text_result("Error: text is required.", is_error=True)

            try:
                await asyncio.to_thread(
                    _cu_sandbox.send_type, text,
                )
            except Exception as exc:
                return _text_result(
                    f"Error typing: {exc}", is_error=True,
                )

            preview = text[:50] + ("..." if len(text) > 50 else "")
            return _text_result(f"Typed: {preview!r}")

        @tool(
            "computer_key",
            "Press a key or key combination. Supports special keys like "
            "Return, Tab, Escape, BackSpace, and modifier combos like "
            "ctrl+a, alt+F4, super+d. Modifiers: ctrl, alt, shift, super.",
            {
                "type": "object",
                "properties": {
                    "key": {
                        "type": "string",
                        "description": (
                            "Key or combo to press, e.g. 'Return', 'ctrl+a', "
                            "'alt+F4', 'Tab'."
                        ),
                    },
                },
                "required": ["key"],
            },
        )
        async def computer_key(args: dict[str, Any]) -> dict[str, Any]:
            key = args.get("key", "")
            if not key:
                return _text_result("Error: key is required.", is_error=True)

            try:
                await asyncio.to_thread(
                    _cu_sandbox.send_key, key,
                )
            except Exception as exc:
                return _text_result(
                    f"Error pressing key: {exc}", is_error=True,
                )

            return _text_result(f"Pressed key: {args.get('key', '')}")

        @tool(
            "computer_scroll",
            "Scroll at a specific position on the screen. Moves the pointer "
            "to (x, y) and scrolls in the given direction.",
            {
                "type": "object",
                "properties": {
                    "x": {
                        "type": "integer",
                        "description": "X coordinate (0-1279).",
                    },
                    "y": {
                        "type": "integer",
                        "description": "Y coordinate (0-719).",
                    },
                    "direction": {
                        "type": "string",
                        "enum": ["up", "down", "left", "right"],
                        "description": "Scroll direction.",
                    },
                    "amount": {
                        "type": "integer",
                        "description": (
                            "Number of scroll steps. Default: 3."
                        ),
                    },
                },
                "required": ["x", "y", "direction"],
            },
        )
        async def computer_scroll(args: dict[str, Any]) -> dict[str, Any]:
            x = args.get("x", 0)
            y = args.get("y", 0)
            direction = args.get("direction", "down")
            amount = args.get("amount", 3)

            if not (0 <= x < 1280 and 0 <= y < 720):
                return _text_result(
                    f"Error: coordinates ({x}, {y}) out of range.",
                    is_error=True,
                )

            try:
                await asyncio.to_thread(
                    _cu_sandbox.send_scroll,
                    x, y, direction, amount,
                )
            except Exception as exc:
                return _text_result(
                    f"Error scrolling: {exc}", is_error=True,
                )

            return _text_result(
                f"Scrolled {direction} by {amount} at ({x}, {y})."
            )

        @tool(
            "computer_toplevel",
            "Focus a window by name or part of its title. Use this to "
            "switch between applications (e.g. 'Chromium', 'Google Chrome', 'foot').",
            {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": (
                            "Window title or app name to focus "
                            "(case-insensitive substring match)."
                        ),
                    },
                },
                "required": ["name"],
            },
        )
        async def computer_toplevel(args: dict[str, Any]) -> dict[str, Any]:
            name = args.get("name", "")
            if not name:
                return _text_result("Error: name is required.", is_error=True)

            try:
                await asyncio.to_thread(
                    _cu_sandbox.focus_window, name,
                )
            except NotImplementedError as exc:
                return _text_result(str(exc), is_error=True)
            except Exception as exc:
                return _text_result(
                    f"Error focusing window: {exc}", is_error=True,
                )

            return _text_result(f"Focused window matching: {name!r}")

        tools_list.extend([
            computer_screenshot,
            computer_click,
            computer_type,
            computer_key,
            computer_scroll,
            computer_toplevel,
        ])

    # --- Port forwarding (sandboxed contexts that support it) ---
    if sandbox is not None and sandbox.supports_port_forwarding():
        from open_shrimp.db import ChatScope
        _scope_key = ChatScope(chat_id=chat_id, thread_id=thread_id).key

        @tool(
            "port_forward",
            "Manage TCP port forwards from the sandbox to the host's "
            "loopback interface (127.0.0.1). Use this to expose a service "
            "running inside the sandbox (e.g. a dev server, API) to the "
            "user on their host machine. Three actions: 'create' opens a "
            "new forward and returns the host port (requires user approval); "
            "'list' shows active forwards in this conversation; 'remove' "
            "tears down a forward by id. Forwards are automatically cleaned "
            "up on /clear or when the sandbox stops.",
            {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["create", "list", "remove"],
                        "description": (
                            "What to do: 'create' opens a new forward, "
                            "'list' shows active forwards, 'remove' tears "
                            "down a forward by id."
                        ),
                    },
                    "guest_port": {
                        "type": "integer",
                        "description": (
                            "[create only] Port inside the sandbox to "
                            "expose (1-65535)."
                        ),
                    },
                    "host_port": {
                        "type": "integer",
                        "description": (
                            "[create only] Preferred host port. If "
                            "omitted, the same number as guest_port is "
                            "tried first; if that's taken, the system "
                            "picks any free port. Always bound to "
                            "127.0.0.1 only."
                        ),
                    },
                    "description": {
                        "type": "string",
                        "description": (
                            "[create only] Short human-readable label "
                            "shown to the user in the approval prompt "
                            "(e.g. 'Next.js dev server')."
                        ),
                    },
                    "forward_id": {
                        "type": "string",
                        "description": (
                            "[remove only] The id returned by a previous "
                            "create call (e.g. 'pf-abc12345')."
                        ),
                    },
                },
                "required": ["action"],
            },
            annotations=ToolAnnotations(readOnlyHint=False),
        )
        async def port_forward(args: dict[str, Any]) -> dict[str, Any]:
            action = args.get("action", "")

            if action == "list":
                forwards = await asyncio.to_thread(
                    sandbox.list_port_forwards, _scope_key,
                )
                if not forwards:
                    return _text_result(
                        "No active port forwards in this conversation."
                    )
                lines = [f"Active port forwards ({len(forwards)}):"]
                for f in forwards:
                    desc = f" — {f.description}" if f.description else ""
                    lines.append(
                        f"• {f.id}: guest:{f.guest_port} -> "
                        f"127.0.0.1:{f.host_port}{desc}"
                    )
                return _text_result("\n".join(lines))

            if action == "remove":
                forward_id = (args.get("forward_id") or "").strip()
                if not forward_id:
                    return _text_result(
                        "Error: forward_id is required for action=remove.",
                        is_error=True,
                    )
                removed = await asyncio.to_thread(
                    sandbox.remove_port_forward, forward_id,
                )
                if not removed:
                    return _text_result(
                        f"No port forward with id {forward_id!r} found.",
                        is_error=True,
                    )
                return _text_result(f"Removed port forward {forward_id}.")

            if action == "create":
                guest_port = args.get("guest_port")
                if not isinstance(guest_port, int) or not (
                    0 < guest_port < 65536
                ):
                    return _text_result(
                        "Error: guest_port must be an integer in 1-65535.",
                        is_error=True,
                    )
                host_port = args.get("host_port")
                if host_port is not None and (
                    not isinstance(host_port, int)
                    or not (0 < host_port < 65536)
                ):
                    return _text_result(
                        "Error: host_port must be an integer in 1-65535.",
                        is_error=True,
                    )
                description = args.get("description")
                try:
                    forward = await asyncio.to_thread(
                        sandbox.add_port_forward,
                        guest_port,
                        host_port,
                        _scope_key,
                        description,
                    )
                except Exception as exc:
                    logger.exception("Failed to open port forward")
                    return _text_result(
                        f"Error opening port forward: {exc}", is_error=True,
                    )
                fallback = (
                    f" (requested {host_port} was unavailable)"
                    if host_port is not None and host_port != forward.host_port
                    else ""
                )
                return _text_result(
                    f"Port forward opened: guest:{forward.guest_port} -> "
                    f"http://127.0.0.1:{forward.host_port}{fallback}\n"
                    f"id: {forward.id}"
                )

            return _text_result(
                f"Error: unknown action {action!r}. "
                "Use 'create', 'list', or 'remove'.",
                is_error=True,
            )

        tools_list.append(port_forward)

    # --- host_bash (sudo mode): run a shell command on the host, OUTSIDE
    # the sandbox.  Only registered when the context's sandbox config has
    # ``allow_host_escape: true``.  Every invocation routes through a
    # per-command Telegram approval prompt with a 10-second auto-deny
    # timer; see ``handlers/approval.py``.
    if host_bash_workdir is not None:
        _host_workdir = host_bash_workdir

        @tool(
            "host_bash",
            "Run a shell command on the HOST, outside the sandbox. Use this "
            "only when a task fundamentally requires host access (e.g. "
            "manipulating files outside the mounted project directory, "
            "interacting with host-only services). Every invocation prompts "
            "the user for approval and auto-denies after 10 seconds if they "
            "don't respond. Prefer the sandboxed Bash tool whenever "
            "possible. Captures stdout, stderr, and exit code.",
            {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": (
                            "The shell command to execute on the host. "
                            "Runs via /bin/sh -c."
                        ),
                    },
                    "description": {
                        "type": "string",
                        "description": (
                            "Short human-readable explanation shown to the "
                            "user in the approval prompt (e.g. 'install "
                            "system package'). Optional but strongly "
                            "recommended — the user has 10 seconds to "
                            "decide."
                        ),
                    },
                    "timeout_seconds": {
                        "type": "integer",
                        "description": (
                            "Maximum execution time in seconds. Default "
                            "120. The command is killed if it exceeds this."
                        ),
                    },
                },
                "required": ["command"],
            },
            annotations=ToolAnnotations(readOnlyHint=False),
        )
        async def host_bash(args: dict[str, Any]) -> dict[str, Any]:
            command = args.get("command", "")
            if not command:
                return _text_result(
                    "Error: command is required.", is_error=True,
                )
            timeout_seconds = args.get("timeout_seconds", 120)
            if not isinstance(timeout_seconds, int) or timeout_seconds <= 0:
                timeout_seconds = 120

            logger.warning(
                "host_bash (sudo) running on host: %s", command[:200],
            )
            try:
                proc = await asyncio.create_subprocess_shell(
                    command,
                    cwd=_host_workdir,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    start_new_session=True,
                )
            except Exception as exc:
                return _text_result(
                    f"Error spawning host shell: {exc}", is_error=True,
                )

            try:
                stdout_b, stderr_b = await asyncio.wait_for(
                    proc.communicate(), timeout=float(timeout_seconds),
                )
            except asyncio.TimeoutError:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await proc.wait()
                return _text_result(
                    f"Error: host_bash timed out after {timeout_seconds}s.",
                    is_error=True,
                )

            stdout = stdout_b.decode("utf-8", errors="replace")
            stderr = stderr_b.decode("utf-8", errors="replace")
            exit_code = proc.returncode if proc.returncode is not None else -1

            parts = [f"exit_code: {exit_code}"]
            if stdout:
                parts.append(f"stdout:\n{stdout}")
            if stderr:
                parts.append(f"stderr:\n{stderr}")
            return _text_result(
                "\n\n".join(parts), is_error=(exit_code != 0),
            )

        tools_list.append(host_bash)

    return create_sdk_mcp_server(
        name="openshrimp",
        tools=tools_list,
    )
