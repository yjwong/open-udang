"""Transport-neutral OpenShrimp tool definitions."""

from __future__ import annotations

import logging
import mimetypes
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from telegram import Bot, InlineKeyboardMarkup

from open_shrimp.web_app_button import make_web_app_button

logger = logging.getLogger(__name__)

_MAX_DOCUMENT_SIZE = 50 * 1024 * 1024
_MAX_PHOTO_SIZE = 10 * 1024 * 1024
_PHOTO_MIME_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}


@dataclass(frozen=True)
class OpenShrimpTool:
    name: str
    description: str
    input_schema: dict[str, Any]
    read_only: bool
    handler: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


def _text_result(text: str, is_error: bool = False) -> dict[str, Any]:
    result: dict[str, Any] = {"content": [{"type": "text", "text": text}]}
    if is_error:
        result["is_error"] = True
    return result


def _guess_mime(path: str) -> str | None:
    mime, _ = mimetypes.guess_type(path)
    return mime


def create_openshrimp_tools(
    bot: Bot,
    chat_id: int,
    thread_id: int | None = None,
    db: Any | None = None,
    config: Any | None = None,
    job_queue: Any | None = None,
    context_name: str | None = None,
    user_id: int = 0,
    is_private_chat: bool = True,
    include_sandbox_tools: bool = False,
) -> list[OpenShrimpTool]:
    """Create OpenShrimp's non-sandbox tool definitions for a chat scope."""
    if include_sandbox_tools:
        logger.warning("Sandbox OpenShrimp MCP tools are not implemented yet")

    thread_kwargs: dict[str, Any] = {}
    if thread_id is not None:
        thread_kwargs["message_thread_id"] = thread_id

    async def send_file(args: dict[str, Any]) -> dict[str, Any]:
        file_path = args.get("file_path", "")
        caption = args.get("caption")
        send_type = args.get("type", "auto")
        if not file_path:
            return _text_result("Error: file_path is required.", is_error=True)
        path = os.path.abspath(file_path)
        if not os.path.isfile(path):
            return _text_result(f"Error: File not found: {path}", is_error=True)
        size = os.path.getsize(path)
        if size == 0:
            return _text_result("Error: File is empty.", is_error=True)
        if size > _MAX_DOCUMENT_SIZE:
            mb = size / (1024 * 1024)
            return _text_result(
                f"Error: File too large ({mb:.1f} MB). Telegram limit is 50 MB.",
                is_error=True,
            )

        mime = _guess_mime(path)
        use_photo = send_type == "photo" or (
            send_type == "auto" and mime in _PHOTO_MIME_TYPES and size <= _MAX_PHOTO_SIZE
        )
        if use_photo and size > _MAX_PHOTO_SIZE:
            mb = size / (1024 * 1024)
            return _text_result(
                f"Error: Photo too large ({mb:.1f} MB). Telegram limit for photos is 10 MB. "
                "Use type='document' for larger images.",
                is_error=True,
            )

        filename = os.path.basename(path)
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
                reply_markup = InlineKeyboardMarkup([[
                    make_web_app_button(
                        "📖 Preview",
                        f"{base_url}/preview/?{preview_params}",
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
                        chat_id=chat_id, photo=f, caption=caption, **thread_kwargs
                    )
                else:
                    await bot.send_document(
                        chat_id=chat_id,
                        document=f,
                        filename=filename,
                        caption=caption,
                        reply_markup=reply_markup,
                        **thread_kwargs,
                    )
            logger.info("Sent %s to chat %d", path, chat_id)
            return _text_result(f"File sent successfully: {filename}")
        except Exception as exc:
            logger.exception("Failed to send file %s to chat %d", path, chat_id)
            return _text_result(f"Error sending file: {exc}", is_error=True)

    tools: list[OpenShrimpTool] = [
        OpenShrimpTool(
            name="send_file",
            description=(
                "Send a file to the user via Telegram. The file must exist on the "
                "local filesystem. Images under 10 MB are sent as inline photos "
                "unless type is set to 'document'."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "Absolute path to the file to send."},
                    "caption": {"type": "string", "description": "Optional caption."},
                    "type": {"type": "string", "enum": ["auto", "photo", "document"]},
                },
                "required": ["file_path"],
            },
            read_only=True,
            handler=send_file,
        )
    ]

    if thread_id is not None:
        emoji_map: dict[str, str] | None = None

        async def get_emoji_map() -> dict[str, str]:
            nonlocal emoji_map
            if emoji_map is None:
                stickers = await bot.get_forum_topic_icon_stickers()
                emoji_map = {s.emoji: s.custom_emoji_id for s in stickers if s.emoji and s.custom_emoji_id}
            return emoji_map

        async def edit_topic(args: dict[str, Any]) -> dict[str, Any]:
            title = args.get("title", "").strip() or None
            icon = args.get("icon")
            if title is not None and len(title) > 128:
                title = title[:128]
            if title is None and icon is None:
                return _text_result("Error: at least one of title or icon is required.", is_error=True)
            icon_custom_emoji_id: str | None = None
            if icon is not None:
                if icon == "":
                    icon_custom_emoji_id = ""
                else:
                    mapping = await get_emoji_map()
                    icon_custom_emoji_id = mapping.get(icon)
                    if icon_custom_emoji_id is None:
                        sample = list(mapping.keys())[:20]
                        return _text_result(
                            f"Error: emoji {icon!r} is not available as a topic icon. "
                            f"Some available emoji: {' '.join(sample)}",
                            is_error=True,
                        )
            edit_kwargs: dict[str, Any] = {}
            if title is not None:
                edit_kwargs["name"] = title
            if icon_custom_emoji_id is not None:
                edit_kwargs["icon_custom_emoji_id"] = icon_custom_emoji_id
            try:
                await bot.edit_forum_topic(chat_id=chat_id, message_thread_id=thread_id, **edit_kwargs)
            except Exception as exc:
                logger.exception("Failed to edit topic in chat %d thread %d", chat_id, thread_id)
                return _text_result(f"Error editing topic: {exc}", is_error=True)
            parts = []
            if title is not None:
                parts.append(f"title={title!r}")
            if icon is not None:
                parts.append(f"icon={icon!r}" if icon else "icon removed")
            return _text_result(f"Topic updated: {', '.join(parts)}")

        tools.append(
            OpenShrimpTool(
                name="edit_topic",
                description="Set or update the title and/or icon of the current Telegram forum topic.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "description": "Short topic title, max 128 characters."},
                        "icon": {"type": "string", "description": "Standard emoji icon, or empty string to remove."},
                    },
                },
                read_only=True,
                handler=edit_topic,
            )
        )

    if db is not None and config is not None and job_queue is not None:
        from open_shrimp.db import ChatScope, create_scheduled_task, delete_scheduled_task, delete_scheduled_task_by_id, get_active_context, list_scheduled_tasks
        from open_shrimp.scheduler import _register_task_with_jobqueue, validate_schedule

        scope = ChatScope(chat_id=chat_id, thread_id=thread_id)

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
            active_context = context_name or await get_active_context(db, scope) or config.default_context
            try:
                validate_schedule(schedule_type, schedule_expr)
                task = await create_scheduled_task(
                    db, scope, active_context, name, prompt, schedule_type, schedule_expr, timeout_seconds
                )
            except ValueError as exc:
                return _text_result(f"Error: {exc}", is_error=True)
            except Exception as exc:
                if "UNIQUE constraint" in str(exc):
                    return _text_result(f"Error: A task named '{name}' already exists in this chat.", is_error=True)
                logger.exception("Failed to create scheduled task")
                return _text_result(f"Error creating task: {exc}", is_error=True)
            if not _register_task_with_jobqueue(job_queue, task, bot, db, config):
                await delete_scheduled_task_by_id(db, task.id)
                return _text_result("Error: failed to register task with scheduler. The task was not created.", is_error=True)
            return _text_result(
                f"Scheduled task '{name}' created successfully.\n"
                f"Schedule: {schedule_expr}\nContext: {active_context}\nTimeout: {timeout_seconds}s"
            )

        async def list_schedules(args: dict[str, Any]) -> dict[str, Any]:
            tasks = await list_scheduled_tasks(db, scope)
            if not tasks:
                return _text_result("No scheduled tasks in this chat.")
            lines = [f"Scheduled tasks ({len(tasks)}):"]
            for t in tasks:
                prompt_preview = t.prompt[:60] + ("..." if len(t.prompt) > 60 else "")
                disabled_label = " [disabled]" if t.disabled else ""
                lines.append(
                    f"\n• {t.name}{disabled_label}\n  Schedule: {t.schedule_type}: {t.schedule_expr}\n"
                    f"  Context: {t.context_name}\n  Timeout: {t.timeout_seconds}s\n  Prompt: {prompt_preview}"
                )
            return _text_result("\n".join(lines))

        async def delete_schedule(args: dict[str, Any]) -> dict[str, Any]:
            name = args.get("name", "").strip()
            if not name:
                return _text_result("Error: name is required.", is_error=True)
            tasks = await list_scheduled_tasks(db, scope)
            task_id = next((t.id for t in tasks if t.name == name), None)
            if not await delete_scheduled_task(db, scope, name):
                return _text_result(f"No scheduled task named '{name}' found in this chat.", is_error=True)
            if task_id is not None:
                for job in job_queue.get_jobs_by_name(f"scheduled_task_{task_id}"):
                    job.schedule_removal()
            return _text_result(f"Scheduled task '{name}' deleted successfully.")

        tools.extend([
            OpenShrimpTool(
                "create_schedule",
                "Create a scheduled task that runs a Claude prompt automatically in the current chat/thread.",
                {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "prompt": {"type": "string"},
                        "schedule_type": {"type": "string", "enum": ["interval", "cron", "once"]},
                        "schedule_expr": {"type": "string"},
                        "timeout_seconds": {"type": "integer"},
                    },
                    "required": ["name", "prompt", "schedule_type", "schedule_expr"],
                },
                False,
                create_schedule,
            ),
            OpenShrimpTool("list_schedules", "List scheduled tasks in the current chat/thread.", {"type": "object", "properties": {}}, True, list_schedules),
            OpenShrimpTool(
                "delete_schedule",
                "Delete a scheduled task by name in the current chat/thread.",
                {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
                False,
                delete_schedule,
            ),
        ])

    return tools
