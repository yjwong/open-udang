"""Telegram command handlers (/start, /context, /clear, /status, /cancel, /model,
/effort, /resume, /review, /mcp, /tasks, /connect).
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import aiosqlite
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update

from open_shrimp.web_app_button import make_web_app_button
from telegram.ext import ContextTypes

from open_shrimp.client_manager import (
    AgentSession,
    close_session,
    get_session,
)
from open_shrimp.config import Config, ContextConfig, is_sandboxed
from open_shrimp.db import ChatScope, delete_session, get_session_id, set_session_id
from open_shrimp.opencode_client import list_sessions
from open_shrimp.handlers.state import (
    _MCP_STATUS_EMOJI,
    _RESUME_LIST_LIMIT,
    _active_bg_tasks,
    _effort_overrides,
    _injectable_sessions,
    _model_overrides,
    _resume_page_cache,
    _resume_selections,
    _resume_session_cache,
    _running_tasks,
    _setup_queues,
    clear_session_approvals,
)
from open_shrimp.handlers.utils import (
    _cancel_running,
    _escape_mdv2,
    _get_context,
    _get_context_name,
    _get_locked_context,
    _is_authorized,
    _update_pinned_status,
    chat_scope_from_message,
)

logger = logging.getLogger(__name__)


def _is_private_chat(update: Update) -> bool:
    """Return True if this update is from a private (1:1) chat."""
    chat = update.effective_chat
    return chat is not None and chat.type == chat.PRIVATE


# ── /start ──


async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start command: welcome message for first-time users."""
    config: Config = context.bot_data["config"]
    db: aiosqlite.Connection = context.bot_data["db"]
    message = update.effective_message
    if not message or not _is_authorized(update.effective_user and update.effective_user.id, config):
        return

    scope = chat_scope_from_message(message)
    ctx_name, ctx = await _get_context(scope, config, db)

    lines = [
        "👋 *Welcome to OpenShrimp*",
        "",
        "You're connected to OpenShrimp. Just send a message (or voice note) — no command needed.",
        "",
        f"*Working in:* `{ctx_name}` → `{ctx.directory}`",
        "",
        "*Commands worth knowing:*",
        "• /context — switch working directory",
        "• /clear — start a fresh session",
        "• /status — show current state",
    ]
    text = "\n".join(lines)
    for ch in ".-()!>#+={|}~[]":
        text = text.replace(ch, f"\\{ch}")
    await message.reply_text(text, parse_mode="MarkdownV2")


# ── /context ──

_CONTEXT_PAGE_SIZE = 6


def _build_context_page(
    config: Config, current: str, page: int,
) -> tuple[str, InlineKeyboardMarkup]:
    """Build a page of context buttons with optional pagination."""
    names = list(config.contexts.keys())
    total = len(names)
    total_pages = max(1, (total + _CONTEXT_PAGE_SIZE - 1) // _CONTEXT_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    start = page * _CONTEXT_PAGE_SIZE
    page_names = names[start : start + _CONTEXT_PAGE_SIZE]

    buttons: list[list[InlineKeyboardButton]] = []
    for name in page_names:
        ctx = config.contexts[name]
        label = f"{'• ' if name == current else ''}{name} — {ctx.description}"
        buttons.append([InlineKeyboardButton(label, callback_data=f"ctx:{name}")])

    # Pagination row
    if total_pages > 1:
        nav: list[InlineKeyboardButton] = []
        if page > 0:
            nav.append(InlineKeyboardButton("◀ Prev", callback_data=f"ctx_page:{page - 1}"))
        nav.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="ctx_noop"))
        if page < total_pages - 1:
            nav.append(InlineKeyboardButton("Next ▶", callback_data=f"ctx_page:{page + 1}"))
        buttons.append(nav)

    text = "*Select a context:*"
    return text, InlineKeyboardMarkup(buttons)


async def handle_context_callback(
    query: Any, data: str, config: Config, context: ContextTypes.DEFAULT_TYPE,
) -> bool:
    """Handle context selection and pagination callbacks. Returns True if handled."""
    if data == "ctx_noop":
        await query.answer()
        return True

    if data.startswith("ctx_page:"):
        # Pagination
        page = int(data.split(":", 1)[1])
        db: aiosqlite.Connection = context.bot_data["db"]
        if not query.message:
            await query.answer()
            return True
        scope = chat_scope_from_message(query.message)
        current = await _get_context_name(scope, config, db)
        text, markup = _build_context_page(config, current, page)
        try:
            await query.message.edit_text(text, parse_mode="MarkdownV2", reply_markup=markup)
        except Exception:
            pass
        await query.answer()
        return True

    if data.startswith("ctx_clear:"):
        # Clear session for a context (from the "Clear session" button after switch)
        target = data[len("ctx_clear:"):]
        db = context.bot_data["db"]
        if not query.message:
            await query.answer("Cannot determine chat.")
            return True

        scope = chat_scope_from_message(query.message)
        ctx_name = await _get_context_name(scope, config, db)

        if target == ctx_name:
            await _cancel_running(scope)
            _injectable_sessions.pop(scope, None)
            _setup_queues.pop(scope, None)
            await close_session(scope)
            await delete_session(db, scope, ctx_name)
            clear_session_approvals(scope, ctx_name)
            _active_bg_tasks.pop(scope, None)

        ctx = config.contexts.get(target)
        desc = _escape_mdv2(ctx.description) if ctx else ""
        target_escaped = _escape_mdv2(target)
        try:
            await query.message.edit_text(
                f"Switched to context `{target_escaped}` \\- {desc}\n_Started fresh session\\._",
                parse_mode="MarkdownV2",
                reply_markup=None,
            )
        except Exception:
            logger.exception("Failed to update context message")

        await query.answer("Session cleared")
        return True

    if data.startswith("ctx:"):
        # Context selection
        target = data[4:]
        db = context.bot_data["db"]
        if not query.message:
            await query.answer("Cannot determine chat.")
            return True

        scope = chat_scope_from_message(query.message)

        if target not in config.contexts:
            await query.answer("Context no longer exists.")
            return True

        locked = _get_locked_context(scope.chat_id, config)
        if locked:
            await query.answer(f"Chat is locked to context {locked}.")
            return True

        current = await _get_context_name(scope, config, db)
        if target == current:
            await query.answer(f"Already on {target}.")
            return True

        clear_session_approvals(scope, current)
        _model_overrides.pop(scope, None)
        _effort_overrides.pop(scope, None)
        await close_session(scope)

        from open_shrimp.db import set_active_context

        await set_active_context(db, scope, target)
        ctx = config.contexts[target]
        desc = _escape_mdv2(ctx.description)
        target_escaped = _escape_mdv2(target)

        existing_session = await get_session_id(db, scope, target)
        if existing_session:
            text = f"Switched to context `{target_escaped}` \\- {desc}\n_Resuming existing session\\._"
            markup = InlineKeyboardMarkup([[
                InlineKeyboardButton("Clear session", callback_data=f"ctx_clear:{target}"),
            ]])
        else:
            text = f"Switched to context `{target_escaped}` \\- {desc}"
            markup = None

        try:
            await query.message.edit_text(
                text,
                parse_mode="MarkdownV2",
                reply_markup=markup,
            )
        except Exception:
            logger.exception("Failed to update context message")

        await query.answer(f"Switched to {target}")
        await _update_pinned_status(context.bot, scope, target, ctx, db)
        return True

    return False


async def context_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /context command: list or switch contexts."""
    config: Config = context.bot_data["config"]
    db: aiosqlite.Connection = context.bot_data["db"]
    message = update.effective_message
    if not message or not _is_authorized(update.effective_user and update.effective_user.id, config):
        return

    scope = chat_scope_from_message(message)
    args = message.text.split() if message.text else []

    if len(args) < 2:
        # List contexts as inline keyboard
        current = await _get_context_name(scope, config, db)
        locked = _get_locked_context(scope.chat_id, config)
        if locked:
            ctx = config.contexts[locked]
            escaped_name = _escape_mdv2(locked)
            escaped_desc = _escape_mdv2(ctx.description)
            await message.reply_text(
                f"This chat is locked to context `{escaped_name}` \\- {escaped_desc}",
                parse_mode="MarkdownV2",
            )
        else:
            text, markup = _build_context_page(config, current, page=0)
            await message.reply_text(text, parse_mode="MarkdownV2", reply_markup=markup)
        return

    # Switch context
    target = args[1]
    if target not in config.contexts:
        names = ", ".join(f"`{n}`" for n in config.contexts)
        await message.reply_text(
            f"Unknown context: `{target}`\\. Available: {names}",
            parse_mode="MarkdownV2",
        )
        return

    locked = _get_locked_context(scope.chat_id, config)
    if locked:
        await message.reply_text(
            f"This chat is locked to context `{locked}`\\.",
            parse_mode="MarkdownV2",
        )
        return

    old_ctx_name = await _get_context_name(scope, config, db)
    clear_session_approvals(scope, old_ctx_name)
    _model_overrides.pop(scope, None)
    _effort_overrides.pop(scope, None)
    await close_session(scope)

    from open_shrimp.db import set_active_context

    await set_active_context(db, scope, target)
    ctx = config.contexts[target]
    desc = _escape_mdv2(ctx.description)
    target_escaped = _escape_mdv2(target)

    existing_session = await get_session_id(db, scope, target)
    if existing_session:
        text = f"Switched to context `{target_escaped}` \\- {desc}\n_Resuming existing session\\._"
        markup = InlineKeyboardMarkup([[
            InlineKeyboardButton("Clear session", callback_data=f"ctx_clear:{target}"),
        ]])
    else:
        text = f"Switched to context `{target_escaped}` \\- {desc}"
        markup = None

    await message.reply_text(text, parse_mode="MarkdownV2", reply_markup=markup)
    await _update_pinned_status(context.bot, scope, target, ctx, db)


# ── /clear ──


async def clear_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /clear command: start fresh session."""
    config: Config = context.bot_data["config"]
    db: aiosqlite.Connection = context.bot_data["db"]
    message = update.effective_message
    if not message or not _is_authorized(update.effective_user and update.effective_user.id, config):
        return

    scope = chat_scope_from_message(message)
    ctx_name, ctx = await _get_context(scope, config, db)

    await _cancel_running(scope)
    _injectable_sessions.pop(scope, None)
    _setup_queues.pop(scope, None)
    await close_session(scope)
    await delete_session(db, scope, ctx_name)
    clear_session_approvals(scope, ctx_name)
    _model_overrides.pop(scope, None)
    _effort_overrides.pop(scope, None)
    _active_bg_tasks.pop(scope, None)

    if ctx.sandbox is not None:
        sandbox_managers = context.bot_data.get("sandbox_managers") or {}
        manager = sandbox_managers.get(ctx.sandbox.backend)
        if manager is not None:
            active = manager.get_active_sandbox(ctx_name)
            if active is not None and active.supports_port_forwarding():
                try:
                    await asyncio.to_thread(
                        active.cleanup_port_forwards, scope.key,
                    )
                except Exception:
                    logger.exception(
                        "Failed to clean up port forwards on /clear for %s",
                        ctx_name,
                    )

    await message.reply_text(f"Started fresh session in context `{ctx_name}`\\.", parse_mode="MarkdownV2")
    await _update_pinned_status(context.bot, scope, ctx_name, ctx, db)


# ── /status ──


async def status_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /status command: show current state."""
    config: Config = context.bot_data["config"]
    db: aiosqlite.Connection = context.bot_data["db"]
    message = update.effective_message
    if not message or not _is_authorized(update.effective_user and update.effective_user.id, config):
        return

    scope = chat_scope_from_message(message)
    ctx_name, ctx = await _get_context(scope, config, db)
    session_id = await get_session_id(db, scope, ctx_name)
    running = scope in _running_tasks and not _running_tasks[scope].done()
    injectable = scope in _injectable_sessions
    setup_queued = len(_setup_queues.get(scope, []))

    lines = [
        f"*Context:* `{ctx_name}`",
        f"*Directory:* `{ctx.directory}`",
        f"*Model:* `{ctx.model or 'CLI default'}`" + (" (override)" if scope in _model_overrides else ""),
        f"*Effort:* `{ctx.effort or 'default'}`" + (" (override)" if scope in _effort_overrides else ""),
        f"*Session:* {'`' + session_id[:12] + '...' + '`' if session_id else 'None'}",
        f"*Running:* {'Yes' if running else 'No'}",
        f"*Injectable:* {'Yes' if injectable else 'No'}",
        f"*Setup queued:* {setup_queued}",
    ]
    # Background tasks.
    scope_tasks = _active_bg_tasks.get(scope, {})
    if scope_tasks:
        lines.append(f"*Background tasks:* {len(scope_tasks)}")
        now = time.monotonic()
        for task in scope_tasks.values():
            elapsed = int(now - task.started_at)
            minutes, seconds = divmod(elapsed, 60)
            duration = f"{minutes}m{seconds}s" if minutes else f"{seconds}s"
            tid_short = task.task_id[:12]
            ttype = task.task_type or "unknown"
            lines.append(
                f"  • `{tid_short}` {ttype}: "
                f"{task.description or 'N/A'} ({duration})"
            )
    # Escape reserved MarkdownV2 characters (outside * and ` markup)
    text = "\n".join(lines)
    for ch in ".-/()!>#+={|}~[]":
        text = text.replace(ch, f"\\{ch}")
    await message.reply_text(text, parse_mode="MarkdownV2")


# ── /cancel ──


async def cancel_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /cancel command: abort running agent invocation."""
    config: Config = context.bot_data["config"]
    message = update.effective_message
    if not message or not _is_authorized(update.effective_user and update.effective_user.id, config):
        return

    scope = chat_scope_from_message(message)
    had_running = scope in _running_tasks and not _running_tasks[scope].done()
    setup_queued = len(_setup_queues.pop(scope, []))

    if had_running:
        _injectable_sessions.pop(scope, None)
        await _cancel_running(scope)

    if had_running:
        parts = ["Cancelled running task"]
        if setup_queued:
            parts.append(f"cleared {setup_queued} queued message{'s' if setup_queued != 1 else ''}")
        text = "\\. ".join(parts) + "\\."
        await message.reply_text(text, parse_mode="MarkdownV2")
    else:
        await message.reply_text("Nothing running\\.", parse_mode="MarkdownV2")


# ── /model ──


async def model_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /model command: show or override the model for this chat.

    Usage:
        /model              -- show current model (and override if active)
        /model <name>       -- override the model for this chat session
        /model reset        -- clear the override, revert to context default
    """
    config: Config = context.bot_data["config"]
    db: aiosqlite.Connection = context.bot_data["db"]
    message = update.effective_message
    if not message or not _is_authorized(update.effective_user and update.effective_user.id, config):
        return

    if not _is_private_chat(update):
        await message.reply_text("This command can only be used in private chats\\.", parse_mode="MarkdownV2")
        return

    scope = chat_scope_from_message(message)
    ctx_name = await _get_context_name(scope, config, db)
    ctx_default_model = config.contexts[ctx_name].model
    current_override = _model_overrides.get(scope)
    args = message.text.split() if message.text else []

    if len(args) < 2:
        # Show current model
        if current_override:
            text = (
                f"*Model:* `{current_override}` \\(override\\)\n"
                f"*Context default:* `{ctx_default_model or 'CLI default'}`\n\n"
                f"Use `/model reset` to revert\\."
            )
        else:
            text = f"*Model:* `{ctx_default_model or 'CLI default'}` \\(context default\\)"
        for ch in ".-/":
            text = text.replace(ch, f"\\{ch}")
        await message.reply_text(text, parse_mode="MarkdownV2")
        return

    target = args[1]

    if target == "reset":
        if current_override:
            del _model_overrides[scope]
            await close_session(scope)
            model_escaped = _escape_mdv2(ctx_default_model or "CLI default")
            await message.reply_text(
                f"Model override cleared\\. Using context default: `{model_escaped}`",
                parse_mode="MarkdownV2",
            )
        else:
            await message.reply_text(
                "No model override active\\.",
                parse_mode="MarkdownV2",
            )
        return

    # Set override
    _model_overrides[scope] = target
    await close_session(scope)
    model_escaped = _escape_mdv2(target)
    await message.reply_text(
        f"Model overridden to `{model_escaped}`\\. "
        f"Use `/model reset` to revert\\.",
        parse_mode="MarkdownV2",
    )


# ── /effort ──


_VALID_EFFORT_LEVELS: tuple[str, ...] = ("low", "medium", "high", "xhigh", "max")


async def effort_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /effort command: show or override the thinking effort level.

    Usage:
        /effort              -- show current effort level (and override if active)
        /effort <level>      -- override for this chat (low, medium, high, xhigh, max)
        /effort reset        -- clear the override, revert to context default
    """
    config: Config = context.bot_data["config"]
    db: aiosqlite.Connection = context.bot_data["db"]
    message = update.effective_message
    if not message or not _is_authorized(update.effective_user and update.effective_user.id, config):
        return

    if not _is_private_chat(update):
        await message.reply_text("This command can only be used in private chats\\.", parse_mode="MarkdownV2")
        return

    scope = chat_scope_from_message(message)
    ctx_name = await _get_context_name(scope, config, db)
    ctx_default_effort = config.contexts[ctx_name].effort
    current_override = _effort_overrides.get(scope)
    args = message.text.split() if message.text else []

    if len(args) < 2:
        # Show current effort
        if current_override:
            text = (
                f"*Effort:* `{current_override}` \\(override\\)\n"
                f"*Context default:* `{ctx_default_effort or 'default'}`\n\n"
                f"Use `/effort reset` to revert\\."
            )
        else:
            text = f"*Effort:* `{ctx_default_effort or 'default'}` \\(context default\\)"
        text += "\n\nLevels: `low`, `medium`, `high`, `xhigh`, `max`"
        for ch in ".-/":
            text = text.replace(ch, f"\\{ch}")
        await message.reply_text(text, parse_mode="MarkdownV2")
        return

    target = args[1].lower()

    if target == "reset":
        if current_override:
            del _effort_overrides[scope]
            await close_session(scope)
            effort_escaped = _escape_mdv2(ctx_default_effort or "default")
            await message.reply_text(
                f"Effort override cleared\\. Using context default: `{effort_escaped}`",
                parse_mode="MarkdownV2",
            )
        else:
            await message.reply_text(
                "No effort override active\\.",
                parse_mode="MarkdownV2",
            )
        return

    if target not in _VALID_EFFORT_LEVELS:
        levels = ", ".join(f"`{lvl}`" for lvl in _VALID_EFFORT_LEVELS)
        await message.reply_text(
            f"Invalid effort level: `{_escape_mdv2(target)}`\\. Valid: {levels}",
            parse_mode="MarkdownV2",
        )
        return

    # Set override
    _effort_overrides[scope] = target
    await close_session(scope)
    effort_escaped = _escape_mdv2(target)
    await message.reply_text(
        f"Effort overridden to `{effort_escaped}`\\. "
        f"Use `/effort reset` to revert\\.",
        parse_mode="MarkdownV2",
    )


# ── /add_dir ──


async def add_dir_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /add_dir command: add, remove, or list runtime additional directories.

    Usage:
        /add_dir                    -- list current additional directories
        /add_dir <path>             -- add a directory
        /add_dir remove <path>      -- remove a previously added directory
    """
    import os

    from open_shrimp.config import is_sandboxed
    from open_shrimp.db import (
        add_additional_directory,
        get_additional_directories,
        remove_additional_directory,
    )
    from open_shrimp.handlers.state import _additional_dir_cache

    config: Config = context.bot_data["config"]
    db: aiosqlite.Connection = context.bot_data["db"]
    message = update.effective_message
    if not message or not _is_authorized(update.effective_user and update.effective_user.id, config):
        return

    if not _is_private_chat(update):
        await message.reply_text("This command can only be used in private chats\\.", parse_mode="MarkdownV2")
        return

    scope = chat_scope_from_message(message)
    ctx_name, ctx = await _get_context(scope, config, db)
    ctx_dir = config.contexts[ctx_name].directory

    # Parse: strip the /add_dir command, then check for "remove" prefix.
    # Join remaining tokens to support paths with spaces.
    raw = (message.text or "").strip()
    # Remove the /add_dir (or /add_dir@botname) prefix.
    rest = raw.split(None, 1)[1].strip() if " " in raw else ""

    if not rest:
        # List directories
        base_dirs = config.contexts[ctx_name].additional_directories
        runtime_dirs = await get_additional_directories(db, scope, ctx_name)

        lines: list[str] = []
        if base_dirs:
            lines.append("*Config directories:*")
            for d in base_dirs:
                lines.append(f"  `{_escape_mdv2(d)}`")
        if runtime_dirs:
            if lines:
                lines.append("")
            lines.append("*Runtime directories \\(/add\\_dir\\):*")
            for d in runtime_dirs:
                lines.append(f"  `{_escape_mdv2(d)}`")
        if not lines:
            lines.append("No additional directories configured\\.")
        else:
            lines.append("")
            lines.append("Use `/add_dir <path>` to add, `/add_dir remove <path>` to remove\\.")

        await message.reply_text("\n".join(lines), parse_mode="MarkdownV2")
        return

    # Check for "remove" subcommand
    rest_parts = rest.split(None, 1)
    if rest_parts[0] == "remove":
        remove_path = rest_parts[1].strip() if len(rest_parts) > 1 else ""
        if not remove_path:
            await message.reply_text(
                "Usage: `/add_dir remove <path>`",
                parse_mode="MarkdownV2",
            )
            return
        target = os.path.expanduser(remove_path)
        removed = await remove_additional_directory(db, scope, ctx_name, target)
        if not removed:
            await message.reply_text(
                f"Directory not found in runtime list: `{_escape_mdv2(target)}`",
                parse_mode="MarkdownV2",
            )
            return

        # Update cache
        _additional_dir_cache.pop((scope, ctx_name), None)

        # Reconnect session and invalidate sandbox
        await _reconnect_after_dir_change(scope, ctx_name, ctx, context)

        await message.reply_text(
            f"Removed `{_escape_mdv2(target)}`\\. Session will reconnect on next message\\.",
            parse_mode="MarkdownV2",
        )
        return

    # Add directory — resolve relative paths against the context directory.
    target = os.path.expanduser(rest)
    if not os.path.isabs(target):
        target = os.path.join(ctx_dir, target)
    target = os.path.realpath(target)

    if not os.path.isdir(target):
        await message.reply_text(
            f"Directory does not exist: `{_escape_mdv2(target)}`",
            parse_mode="MarkdownV2",
        )
        return

    # Check for duplicates against context dir, config dirs, and runtime dirs.
    # Canonicalize everything so symlinks don't bypass the check.
    canonical_existing = {os.path.realpath(d) for d in ctx.additional_directories}
    canonical_existing.add(os.path.realpath(ctx_dir))
    if target in canonical_existing:
        await message.reply_text(
            f"`{_escape_mdv2(target)}` is already included\\.",
            parse_mode="MarkdownV2",
        )
        return

    # Store pending add and show inline keyboard with short callback keys.
    import uuid

    from open_shrimp.handlers.state import _pending_add_dirs

    key = uuid.uuid4().hex[:12]
    _pending_add_dirs[key] = (scope, ctx_name, target)

    markup = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("This session", callback_data=f"adddir_s:{key}"),
            InlineKeyboardButton("Remember", callback_data=f"adddir_r:{key}"),
        ],
    ])
    await message.reply_text(
        f"Add `{_escape_mdv2(target)}` to *{_escape_mdv2(ctx_name)}*?",
        parse_mode="MarkdownV2",
        reply_markup=markup,
    )


async def handle_add_dir_callback(
    query: Any, data: str, config: Config, context: ContextTypes.DEFAULT_TYPE,
) -> bool:
    """Handle /add_dir inline keyboard callbacks. Returns True if handled."""
    if not data.startswith(("adddir_s:", "adddir_r:")):
        return False

    from pathlib import Path

    from open_shrimp.config import load_config, load_raw_yaml, write_raw_yaml
    from open_shrimp.db import add_additional_directory
    from open_shrimp.handlers.state import _additional_dir_cache, _pending_add_dirs

    db: aiosqlite.Connection = context.bot_data["db"]

    # Parse: "adddir_{s|r}:{key}"
    prefix, key = data.split(":", 1)
    action = "session" if prefix == "adddir_s" else "remember"

    pending = _pending_add_dirs.pop(key, None)
    if pending is None:
        await query.answer("This action has expired.")
        return True

    scope, ctx_name, target = pending
    ctx = config.contexts.get(ctx_name)
    if ctx is None:
        await query.answer("Context no longer exists.")
        return True

    if action == "session":
        # Store in DB only — persists across messages but not bot restarts.
        await add_additional_directory(db, scope, ctx_name, target)
        _additional_dir_cache.pop((scope, ctx_name), None)
        await _reconnect_after_dir_change(scope, ctx_name, ctx, context)

        try:
            await query.message.edit_text(
                f"Added `{_escape_mdv2(target)}` to *{_escape_mdv2(ctx_name)}* "
                f"\\(this session\\)\\.\n"
                f"Session will reconnect on next message\\.",
                parse_mode="MarkdownV2",
                reply_markup=None,
            )
        except Exception:
            pass
        await query.answer()
        return True

    if action == "remember":
        # Write to config.yaml so it persists across restarts.
        config_path_str: str | None = context.bot_data.get("config_path")
        if not config_path_str:
            from open_shrimp.config import DEFAULT_CONFIG_PATH
            config_path_str = str(DEFAULT_CONFIG_PATH)

        config_path = Path(config_path_str)
        try:
            raw = load_raw_yaml(config_path)
            ctx_raw = raw.get("contexts", {}).get(ctx_name, {})
            dirs = ctx_raw.get("additional_directories", [])
            if target not in dirs:
                dirs.append(target)
                ctx_raw["additional_directories"] = dirs
            write_raw_yaml(config_path, raw)
            # Reload config eagerly (hot-reload watcher will also fire).
            new_config = load_config(config_path_str)
            context.bot_data["config"] = new_config
        except Exception:
            logger.exception("Failed to write config for /add_dir remember")
            await query.answer("Failed to update config file.")
            return True

        # No DB entry needed — it's in the config now.
        _additional_dir_cache.pop((scope, ctx_name), None)

        updated_ctx = new_config.contexts.get(ctx_name, ctx)
        await _reconnect_after_dir_change(scope, ctx_name, updated_ctx, context)

        try:
            await query.message.edit_text(
                f"Added `{_escape_mdv2(target)}` to *{_escape_mdv2(ctx_name)}* "
                f"\\(saved to config\\)\\.\n"
                f"Session will reconnect on next message\\.",
                parse_mode="MarkdownV2",
                reply_markup=None,
            )
        except Exception:
            pass
        await query.answer()
        return True

    return False


async def _reconnect_after_dir_change(
    scope: ChatScope,
    ctx_name: str,
    ctx: ContextConfig,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Close session and invalidate sandbox after directory changes."""
    from open_shrimp.config import is_sandboxed
    from open_shrimp.handlers.messages import _select_sandbox_manager

    await close_session(scope)

    if is_sandboxed(ctx):
        manager = _select_sandbox_manager(context.bot_data, ctx)
        if manager is not None:
            manager.invalidate_sandbox(ctx_name)


async def _list_sessions_for_context(
    ctx_name: str,
    ctx: ContextConfig,
    *,
    sandbox_manager: Any | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> "list[Any]":
    """Return ``SessionInfo`` rows for *ctx*."""
    # OpenCode ignores `offset=`; ask for `offset + limit` rows and
    # slice. We pass a request bound so the wire fetch shrinks with
    # smaller pages — otherwise every /resume page fetches 500.
    fetch = (offset + limit) if limit is not None else 500
    if is_sandboxed(ctx):
        if sandbox_manager is None:
            return []
        opencode_home = sandbox_manager.opencode_home_dir(ctx_name)
        if not opencode_home.exists():
            return []

        sandbox = sandbox_manager.create_sandbox(ctx_name, ctx)

        def _ensure_server() -> Any:
            sandbox.ensure_environment()
            sandbox.ensure_running()
            sandbox.provision_workspace()
            return sandbox.ensure_opencode_server()

        server = await asyncio.to_thread(_ensure_server)
        sessions = await list_sessions(
            ctx.directory,
            limit=fetch,
            base_url=server.base_url,
            auth_header=server.auth_header,
        )
    else:
        sessions = await list_sessions(ctx.directory, limit=fetch)
    end = (offset + limit) if limit is not None else None
    return sessions[offset:end]


def _select_sandbox_manager_for_context(
    ctx: ContextConfig,
    context: ContextTypes.DEFAULT_TYPE,
) -> Any | None:
    if ctx.sandbox is None:
        return None
    sandbox_managers = context.bot_data.get("sandbox_managers") or {}
    return sandbox_managers.get(ctx.sandbox.backend)


# ── /resume ──


def _relative_time(epoch_ms: int | None) -> str:
    """Format an epoch-millisecond timestamp as a human-readable relative time."""
    if not epoch_ms:
        return "unknown"
    delta = time.time() - epoch_ms / 1000
    if delta < 60:
        return "just now"
    if delta < 3600:
        m = int(delta / 60)
        return f"{m}m ago"
    if delta < 86400:
        h = int(delta / 3600)
        return f"{h}h ago"
    if delta < 604800:
        d = int(delta / 86400)
        return f"{d}d ago"
    dt = datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc)
    return dt.strftime("%b %d")


async def _build_resume_page(
    ctx_name: str,
    ctx: ContextConfig,
    db: aiosqlite.Connection,
    scope: ChatScope,
    page: int,
    sandbox_manager: Any | None = None,
) -> tuple[str, InlineKeyboardMarkup | None]:
    """Build a single page of the resume session list.

    Returns ``(text, keyboard)`` where *keyboard* is ``None`` when there are
    no sessions at all.
    """
    per_page = _RESUME_LIST_LIMIT
    offset = page * per_page
    # Fetch one extra to detect whether a next page exists.
    sessions = await _list_sessions_for_context(
        ctx_name,
        ctx,
        sandbox_manager=sandbox_manager,
        limit=per_page + 1,
        offset=offset,
    )

    if not sessions:
        if page == 0:
            return (
                f"No sessions found for context `{_escape_mdv2(ctx_name)}`\\.",
                None,
            )
        # Edge case: page beyond last – go back.
        return await _build_resume_page(
            ctx_name, ctx, db, scope, page - 1, sandbox_manager,
        )

    has_next = len(sessions) > per_page
    sessions = sessions[:per_page]

    current_session_id = await get_session_id(db, scope, ctx_name)

    buttons: list[list[InlineKeyboardButton]] = []
    for s in sessions:
        summary = s.summary or "No summary"
        rel = _relative_time(s.last_modified)
        marker = " \u2713" if s.session_id == current_session_id else ""
        # Truncate summary to fit button with timestamp and marker.
        max_summary = 44
        if len(summary) > max_summary:
            summary = summary[:max_summary - 3] + "..."
        label = f"{rel} - {summary}{marker}"
        resume_data = f"resume:{s.session_id}"
        info_data = f"resume_info:{s.session_id}"
        _resume_selections[resume_data] = s.session_id
        _resume_session_cache[s.session_id] = s
        _resume_page_cache[s.session_id] = (ctx_name, page)
        buttons.append([
            InlineKeyboardButton(label, callback_data=resume_data),
            InlineKeyboardButton("\u2139\ufe0f", callback_data=info_data),
        ])

    # Navigation row
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton(
            "\u25c0 Prev", callback_data=f"resume_page:{ctx_name}:{page - 1}",
        ))
    if has_next:
        nav.append(InlineKeyboardButton(
            "Next \u25b6", callback_data=f"resume_page:{ctx_name}:{page + 1}",
        ))
    if nav:
        buttons.append(nav)

    page_label = f" \\(page {page + 1}\\)" if page > 0 or has_next else ""
    text = f"*Recent sessions for* `{_escape_mdv2(ctx_name)}`*:*{page_label}"
    return text, InlineKeyboardMarkup(buttons)


def _build_resume_detail(
    session_id: str,
    ctx_name: str,
    current_session_id: str | None,
) -> tuple[str, InlineKeyboardMarkup]:
    """Build the detail view for a single session."""
    s = _resume_session_cache.get(session_id)
    if not s:
        text = "Session info has expired\\. Use /resume to list again\\."
        keyboard = InlineKeyboardMarkup([])
        return text, keyboard

    lines: list[str] = []
    lines.append(f"*Session details*\n")

    if s.custom_title:
        lines.append(f"*Title:* {_escape_mdv2(s.custom_title)}")
    lines.append(f"*Summary:* {_escape_mdv2(s.summary or 'No summary')}")

    if s.first_prompt:
        prompt = s.first_prompt
        if len(prompt) > 200:
            prompt = prompt[:197] + "..."
        lines.append(f"*First prompt:* {_escape_mdv2(prompt)}")

    if s.git_branch:
        lines.append(f"*Branch:* `{_escape_mdv2(s.git_branch)}`")

    lines.append(f"*Created:* {_escape_mdv2(_relative_time(s.created_at))}")
    lines.append(f"*Last active:* {_escape_mdv2(_relative_time(s.last_modified))}")

    if s.file_size:
        size_kb = s.file_size / 1024
        if size_kb >= 1024:
            size_str = f"{size_kb / 1024:.1f} MB"
        else:
            size_str = f"{size_kb:.0f} KB"
        lines.append(f"*Size:* {_escape_mdv2(size_str)}")

    lines.append(f"*ID:* `{_escape_mdv2(s.session_id)}`")

    if s.session_id == current_session_id:
        lines.append("\n_This is the current session\\._")

    text = "\n".join(lines)

    resume_data = f"resume:{s.session_id}"
    _resume_selections[resume_data] = s.session_id
    ctx_name_cached, page = _resume_page_cache.get(s.session_id, (ctx_name, 0))
    back_data = f"resume_page:{ctx_name_cached}:{page}"

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("\u25b6\ufe0f Resume", callback_data=resume_data)],
        [InlineKeyboardButton("\u25c0 Back to list", callback_data=back_data)],
    ])
    return text, keyboard


async def resume_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /resume command: list recent sessions or resume a specific one.

    Usage:
        /resume          - Show recent sessions for the current context
        /resume <id>     - Resume a session by ID (prefix match supported)
    """
    config: Config = context.bot_data["config"]
    db: aiosqlite.Connection = context.bot_data["db"]
    message = update.effective_message
    if not message or not _is_authorized(update.effective_user and update.effective_user.id, config):
        return

    scope = chat_scope_from_message(message)
    ctx_name, ctx = await _get_context(scope, config, db)

    args = message.text.split() if message.text else []

    if len(args) >= 2:
        # Direct resume by session ID (or prefix)
        target = args[1]
        sessions = await _list_sessions_for_context(
            ctx_name,
            ctx,
            sandbox_manager=_select_sandbox_manager_for_context(ctx, context),
        )
        match = None
        for s in sessions:
            if s.session_id == target or s.session_id.startswith(target):
                match = s
                break

        if not match:
            await message.reply_text(
                f"No session matching `{_escape_mdv2(target)}` found in context `{_escape_mdv2(ctx_name)}`\\.",
                parse_mode="MarkdownV2",
            )
            return

        await close_session(scope)
        await set_session_id(db, scope, ctx_name, match.session_id)
        summary = _escape_mdv2(match.summary or "No summary")
        await message.reply_text(
            f"Resumed session `{_escape_mdv2(match.session_id[:12])}...`\n_{summary}_",
            parse_mode="MarkdownV2",
        )
        await _update_pinned_status(context.bot, scope, ctx_name, ctx, db)
        return

    # List recent sessions for the current context (page 0)
    text, keyboard = await _build_resume_page(
        ctx_name,
        ctx,
        db,
        scope,
        page=0,
        sandbox_manager=_select_sandbox_manager_for_context(ctx, context),
    )

    if keyboard is None:
        await message.reply_text(text, parse_mode="MarkdownV2")
        return

    await message.reply_text(text, parse_mode="MarkdownV2", reply_markup=keyboard)


# ── /resume callback handler ──


async def handle_resume_callback(
    query: Any, data: str, config: Config, context: ContextTypes.DEFAULT_TYPE,
) -> bool:
    """Handle /resume session selection callback. Returns True if handled."""
    prefixes = ("resume:", "resume_page:", "resume_info:")
    if not any(data.startswith(p) for p in prefixes):
        return False

    db: aiosqlite.Connection = context.bot_data["db"]

    # Handle pagination
    if data.startswith("resume_page:"):
        parts = data.split(":", 2)
        if len(parts) != 3:
            await query.answer("Invalid page data.")
            return True
        ctx_name_req, page_str = parts[1], parts[2]
        try:
            page = int(page_str)
        except ValueError:
            await query.answer("Invalid page number.")
            return True
        if not query.message:
            await query.answer("Cannot determine chat.")
            return True
        scope = chat_scope_from_message(query.message)
        _, ctx = await _get_context(scope, config, db)
        # Use the context name from the callback to stay consistent
        ctx = config.contexts.get(ctx_name_req, ctx)
        text, keyboard = await _build_resume_page(
            ctx_name_req,
            ctx,
            db,
            scope,
            page,
            sandbox_manager=_select_sandbox_manager_for_context(ctx, context),
        )
        await query.answer()
        try:
            await query.message.edit_text(
                text=text,
                parse_mode="MarkdownV2",
                reply_markup=keyboard,
            )
        except Exception:
            logger.exception("Failed to update resume page")
        return True

    # Handle session detail view
    if data.startswith("resume_info:"):
        session_id = data[len("resume_info:"):]
        if not query.message:
            await query.answer("Cannot determine chat.")
            return True
        scope = chat_scope_from_message(query.message)
        ctx_name, _ = await _get_context(scope, config, db)
        current_session_id = await get_session_id(db, scope, ctx_name)
        text, keyboard = _build_resume_detail(
            session_id, ctx_name, current_session_id,
        )
        await query.answer()
        try:
            await query.message.edit_text(
                text=text,
                parse_mode="MarkdownV2",
                reply_markup=keyboard,
            )
        except Exception:
            logger.exception("Failed to show session detail")
        return True

    # Handle session resume
    session_id = _resume_selections.pop(data, None)
    if not session_id:
        await query.answer("This selection has expired.")
        return True

    if not query.message:
        await query.answer("Cannot determine chat.")
        return True

    scope = chat_scope_from_message(query.message)

    ctx_name, ctx = await _get_context(scope, config, db)
    await close_session(scope)
    await set_session_id(db, scope, ctx_name, session_id)
    await query.answer(f"Resumed session {session_id[:8]}...")

    try:
        summary_text = f"\u2705 Resumed session `{_escape_mdv2(session_id[:12])}\\.\\.\\.`"
        await query.message.edit_text(
            text=summary_text,
            parse_mode="MarkdownV2",
            reply_markup=None,
        )
    except Exception:
        logger.exception("Failed to update resume message")
        try:
            await query.message.edit_reply_markup(reply_markup=None)
        except Exception:
            logger.exception("Failed to remove resume keyboard")

    await _update_pinned_status(
        context.bot, scope, ctx_name, ctx, db
    )
    # Clean up remaining selections from this listing
    expired = [k for k in _resume_selections if k.startswith("resume:")]
    for k in expired:
        _resume_selections.pop(k, None)
    return True


# ── /review ──


async def review_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /review -- open the review Mini App for the current context."""
    if not update.effective_user or not update.message:
        return

    config: Config = context.bot_data["config"]
    db: aiosqlite.Connection = context.bot_data["db"]
    scope = chat_scope_from_message(update.message)

    if not _is_authorized(update.effective_user.id, config):
        return

    context_name, ctx = await _get_context(scope, config, db)

    # Build the Mini App URL.
    # Use the configured public URL if available, otherwise build from
    # host:port.  For production behind a reverse proxy the user should
    # set review.public_url in config.
    if config.review.public_url:
        base_url = config.review.public_url.rstrip("/")
    else:
        base_url = f"https://{config.review.host}:{config.review.port}"

    chat_type = update.effective_chat.type if update.effective_chat else "private"
    _is_private = chat_type == "private"
    _user_id = update.effective_user.id

    escaped_context = _escape_mdv2(context_name)
    dirs = [ctx.directory] + (ctx.additional_directories or [])
    thread_param = f"&thread_id={scope.thread_id}" if scope.thread_id is not None else ""

    if len(dirs) == 1:
        app_url = f"{base_url}/app/?chat_id={scope.chat_id}{thread_param}"
        keyboard = InlineKeyboardMarkup([
            [make_web_app_button(
                text="\U0001f4dd Open Review",
                url=app_url,
                chat_id=scope.chat_id,
                user_id=_user_id,
                bot_token=config.telegram.token,
                is_private_chat=_is_private,
            )]
        ])
        escaped_dir = _escape_mdv2(ctx.directory)
        text = (
            f"Review changes in *{escaped_context}*\n"
            f"\U0001f4c1 `{escaped_dir}`"
        )
    else:
        # Multiple directories: one button per directory.
        rows = []
        for i, d in enumerate(dirs):
            app_url = f"{base_url}/app/?chat_id={scope.chat_id}&dir={i}{thread_param}"
            basename = d.rstrip("/").rsplit("/", 1)[-1]
            rows.append([make_web_app_button(
                text=f"\U0001f4c1 {basename}",
                url=app_url,
                chat_id=scope.chat_id,
                user_id=_user_id,
                bot_token=config.telegram.token,
                is_private_chat=_is_private,
            )])
        keyboard = InlineKeyboardMarkup(rows)
        text = f"Review changes in *{escaped_context}*"

    await update.message.reply_text(
        text,
        parse_mode="MarkdownV2",
        reply_markup=keyboard,
    )


# ── /vnc ──


async def vnc_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /vnc -- open the VNC Mini App for the current context's desktop."""
    if not update.effective_user or not update.message:
        return

    config: Config = context.bot_data["config"]
    db: aiosqlite.Connection = context.bot_data["db"]
    scope = chat_scope_from_message(update.message)

    if not _is_authorized(update.effective_user.id, config):
        return

    context_name, ctx = await _get_context(scope, config, db)

    # Check that the context has computer_use enabled (any backend).
    _has_cu = (
        (ctx.container is not None and ctx.container.computer_use)
        or (ctx.sandbox is not None and ctx.sandbox.computer_use)
    )
    if not _has_cu:
        await update.message.reply_text(
            f"Context `{_escape_mdv2(context_name)}` does not have computer use enabled\\.",
            parse_mode="MarkdownV2",
        )
        return

    # Build the Mini App URL.
    if config.review.public_url:
        base_url = config.review.public_url.rstrip("/")
    else:
        base_url = f"https://{config.review.host}:{config.review.port}"

    chat_type = update.effective_chat.type if update.effective_chat else "private"
    vnc_url = f"{base_url}/vnc/?context={context_name}"
    keyboard = InlineKeyboardMarkup([
        [make_web_app_button(
            text="View desktop",
            url=vnc_url,
            chat_id=scope.chat_id,
            user_id=update.effective_user.id,
            bot_token=config.telegram.token,
            is_private_chat=chat_type == "private",
        )]
    ])

    escaped_context = _escape_mdv2(context_name)
    await update.message.reply_text(
        f"Desktop for *{escaped_context}*",
        parse_mode="MarkdownV2",
        reply_markup=keyboard,
    )


# ── /connect ──


async def connect_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /connect -- open the provider connection Mini App."""
    if not update.effective_user or not update.message:
        return

    config: Config = context.bot_data["config"]

    if not _is_authorized(update.effective_user.id, config):
        return

    if not _is_private_chat(update):
        await update.message.reply_text("This command can only be used in private chats\\.", parse_mode="MarkdownV2")
        return

    db: aiosqlite.Connection = context.bot_data["db"]
    scope = chat_scope_from_message(update.message)
    context_name, _ctx = await _get_context(scope, config, db)

    args = update.message.text.split(maxsplit=1) if update.message.text else []
    provider = args[1].strip() if len(args) == 2 else ""
    if provider == "list":
        await update.message.reply_text(
            "Open the provider connection Mini App to view and manage providers\.",
            parse_mode="MarkdownV2",
        )
        return
    if provider.startswith("disconnect "):
        await update.message.reply_text(
            "Provider disconnect is not available in Telegram yet\. Use OpenCode's provider UI in `/connect`\.",
            parse_mode="MarkdownV2",
        )
        return

    # Build the Mini App URL.
    if config.review.public_url:
        base_url = config.review.public_url.rstrip("/")
    else:
        base_url = f"https://{config.review.host}:{config.review.port}"

    chat_type = update.effective_chat.type if update.effective_chat else "private"
    connect_url = (
        f"{base_url}/terminal/?mode=connect"
        f"&context={quote(context_name, safe='')}"
    )
    if provider:
        connect_url += f"&provider={quote(provider, safe='')}"
    keyboard = InlineKeyboardMarkup([
        [make_web_app_button(
            text="Connect providers",
            url=connect_url,
            chat_id=scope.chat_id,
            user_id=update.effective_user.id,
            bot_token=config.telegram.token,
            is_private_chat=chat_type == "private",
        )]
    ])

    await update.message.reply_text(
        "Connect model providers",
        parse_mode="MarkdownV2",
        reply_markup=keyboard,
    )


# ── /mcp ──


async def mcp_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /mcp command: list, reconnect, enable, or disable MCP servers.

    Usage:
        /mcp                    -- list all MCP servers and their status
        /mcp reset <name>       -- reconnect a failed/disconnected server
        /mcp enable <name>      -- connect a server
        /mcp disable <name>     -- disconnect a server
    """
    config: Config = context.bot_data["config"]
    db: aiosqlite.Connection = context.bot_data["db"]
    message = update.effective_message
    if not message or not _is_authorized(update.effective_user and update.effective_user.id, config):
        return

    scope = chat_scope_from_message(message)

    session = get_session(scope)
    if session is None:
        await message.reply_text(
            "No active session\\. Send a message first to start a session, "
            "then use /mcp to manage MCP servers\\.",
            parse_mode="MarkdownV2",
        )
        return

    args = message.text.split() if message.text else []
    subcommand = args[1] if len(args) >= 2 else None
    server_name = " ".join(args[2:]) if len(args) >= 3 else None

    if subcommand is None:
        # List all MCP servers
        await _mcp_list(message, session)
    elif subcommand == "reset":
        if not server_name:
            await message.reply_text(
                "Usage: `/mcp reset <server\\-name>`",
                parse_mode="MarkdownV2",
            )
            return
        await _mcp_reconnect(message, session, server_name)
    elif subcommand in ("enable", "disable"):
        if not server_name:
            await message.reply_text(
                f"Usage: `/mcp {subcommand} <server\\-name>`",
                parse_mode="MarkdownV2",
            )
            return
        await _mcp_toggle(message, session, server_name, enabled=(subcommand == "enable"))
    else:
        await message.reply_text(
            "Unknown subcommand\\. Usage:\n"
            "`/mcp` \u2014 list servers\n"
            "`/mcp reset <name>` \u2014 reconnect a server\n"
            "`/mcp enable <name>` \u2014 connect a server\n"
            "`/mcp disable <name>` \u2014 disconnect a server",
            parse_mode="MarkdownV2",
        )


async def _mcp_list(message: Any, session: AgentSession) -> None:
    """Fetch and display MCP server status."""
    try:
        status_resp = await session.client.get_mcp_status()
    except Exception:
        logger.exception("Failed to get MCP status")
        await message.reply_text("Failed to retrieve MCP server status\\.", parse_mode="MarkdownV2")
        return

    servers = status_resp.get("mcpServers", [])
    if not servers:
        await message.reply_text("No MCP servers configured\\.", parse_mode="MarkdownV2")
        return

    lines: list[str] = ["*MCP Servers*\n"]
    for srv in servers:
        name = srv.get("name", "unknown")
        status = srv.get("status", "unknown")
        emoji = _MCP_STATUS_EMOJI.get(status, "\u2753")
        scope = srv.get("scope", "")

        line = f"{emoji} *{_escape_mdv2(name)}*"
        if scope:
            line += f" \\({_escape_mdv2(scope)}\\)"
        line += f" \u2014 {_escape_mdv2(status)}"

        # Show server info (version) when connected
        server_info = srv.get("serverInfo")
        if server_info:
            version = server_info.get("version", "")
            if version:
                line += f" v{_escape_mdv2(version)}"

        # Show error message for failed servers
        error = srv.get("error")
        if error:
            # Truncate long errors
            if len(error) > 120:
                error = error[:117] + "..."
            line += f"\n    \u26a0\ufe0f {_escape_mdv2(error)}"

        # Show tool count when connected
        tools = srv.get("tools", [])
        if tools:
            line += f"\n    \U0001f527 {len(tools)} tool{'s' if len(tools) != 1 else ''}"

        lines.append(line)

    text = "\n".join(lines)
    await message.reply_text(text, parse_mode="MarkdownV2")


async def _mcp_reconnect(message: Any, session: AgentSession, server_name: str) -> None:
    """Reconnect a failed or disconnected MCP server."""
    try:
        await session.client.reconnect_mcp_server(server_name)
    except Exception:
        logger.exception("Failed to reconnect MCP server %s", server_name)
        await message.reply_text(
            f"Failed to reconnect `{_escape_mdv2(server_name)}`\\.",
            parse_mode="MarkdownV2",
        )
        return

    escaped = _escape_mdv2(server_name)
    await message.reply_text(
        f"Reconnect requested for `{escaped}`\\. Use /mcp to check status\\.",
        parse_mode="MarkdownV2",
    )


async def _mcp_toggle(message: Any, session: AgentSession, server_name: str, *, enabled: bool) -> None:
    """Enable or disable an MCP server."""
    action = "enable" if enabled else "disable"
    try:
        await session.client.toggle_mcp_server(server_name, enabled=enabled)
    except Exception:
        logger.exception("Failed to %s MCP server %s", action, server_name)
        await message.reply_text(
            f"Failed to {_escape_mdv2(action)} `{_escape_mdv2(server_name)}`\\.",
            parse_mode="MarkdownV2",
        )
        return

    escaped = _escape_mdv2(server_name)
    if enabled:
        text = f"\U0001f7e2 Connect requested for `{escaped}`\\. Use /mcp to check status\\."
    else:
        text = f"\u26aa Disconnected `{escaped}`\\."
    await message.reply_text(
        text,
        parse_mode="MarkdownV2",
    )


# ── /schedule ──


async def schedule_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /schedule command: list and manage scheduled tasks.

    Usage:
        /schedule           -- list all scheduled tasks for this chat
        /schedule delete <n> -- delete a scheduled task by name
    """
    config: Config = context.bot_data["config"]
    db: aiosqlite.Connection = context.bot_data["db"]
    message = update.effective_message
    if not message or not _is_authorized(update.effective_user and update.effective_user.id, config):
        return

    scope = chat_scope_from_message(message)
    args = message.text.split() if message.text else []

    if len(args) >= 3 and args[1] == "delete":
        # Delete a task by name.
        task_name = " ".join(args[2:])
        from open_shrimp.db import delete_scheduled_task, list_scheduled_tasks

        # Find task ID for JobQueue removal.
        tasks = await list_scheduled_tasks(db, scope)
        task_id = None
        for t in tasks:
            if t.name == task_name:
                task_id = t.id
                break

        deleted = await delete_scheduled_task(db, scope, task_name)
        if deleted:
            # Remove from JobQueue.
            if task_id is not None and context.job_queue:
                job_name = f"scheduled_task_{task_id}"
                for j in context.job_queue.get_jobs_by_name(job_name):
                    j.schedule_removal()

            escaped = _escape_mdv2(task_name)
            await message.reply_text(
                f"Deleted scheduled task `{escaped}`\\.",
                parse_mode="MarkdownV2",
            )
        else:
            escaped = _escape_mdv2(task_name)
            await message.reply_text(
                f"No scheduled task named `{escaped}` found\\.",
                parse_mode="MarkdownV2",
            )
        return

    # List all tasks.
    from open_shrimp.db import list_scheduled_tasks

    tasks = await list_scheduled_tasks(db, scope)
    if not tasks:
        await message.reply_text(
            "No scheduled tasks\\. Ask OpenShrimp to create one\\!",
            parse_mode="MarkdownV2",
        )
        return

    lines = [f"*Scheduled tasks \\({len(tasks)}\\):*\n"]
    for t in tasks:
        type_desc = {
            "interval": f"every {t.schedule_expr}",
            "cron": f"cron: {t.schedule_expr}",
            "once": f"at {t.schedule_expr}",
        }.get(t.schedule_type, t.schedule_expr)

        prompt_preview = t.prompt[:50] + ("..." if len(t.prompt) > 50 else "")
        name_escaped = _escape_mdv2(t.name)
        desc_escaped = _escape_mdv2(type_desc)
        prompt_escaped = _escape_mdv2(prompt_preview)
        ctx_escaped = _escape_mdv2(t.context_name)

        disabled_label = " \\[disabled\\]" if t.disabled else ""
        lines.append(
            f"• *{name_escaped}*{disabled_label}\n"
            f"  📅 {desc_escaped}\n"
            f"  📁 `{ctx_escaped}`\n"
            f"  💬 _{prompt_escaped}_"
        )

    text = "\n".join(lines)
    await message.reply_text(text, parse_mode="MarkdownV2")


# ── /tasks ──


async def tasks_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /tasks command: list active background tasks or stop one.

    Usage:
        /tasks              -- list active background tasks
        /tasks stop <id>    -- stop a background task by ID (prefix match)
    """
    config: Config = context.bot_data["config"]
    message = update.effective_message
    if not message or not _is_authorized(
        update.effective_user and update.effective_user.id, config
    ):
        return

    scope = chat_scope_from_message(message)
    args = message.text.split() if message.text else []

    # ── /tasks stop <id> ──
    if len(args) >= 3 and args[1] == "stop":
        target = args[2]
        scope_tasks = _active_bg_tasks.get(scope, {})

        # Find by exact match or prefix.
        matched_task = None
        for tid, task in scope_tasks.items():
            if tid == target or tid.startswith(target):
                matched_task = task
                break

        if not matched_task:
            await message.reply_text(
                f"No active task matching `{_escape_mdv2(target)}`\\.",
                parse_mode="MarkdownV2",
            )
            return

        from open_shrimp.client_manager import stop_background_task

        success = await stop_background_task(scope, matched_task.task_id)
        if success:
            # Remove from tracking immediately — the TaskNotificationMessage
            # may arrive later when the stream is next consumed, but we
            # don't want the task to linger in /tasks output.
            scope_tasks.pop(matched_task.task_id, None)
            if not scope_tasks:
                _active_bg_tasks.pop(scope, None)
            tid_short = _escape_mdv2(matched_task.task_id[:12])
            await message.reply_text(
                f"Stopped task `{tid_short}`\\.",
                parse_mode="MarkdownV2",
            )
        else:
            await message.reply_text(
                "Failed to stop task \\(no active session\\)\\.",
                parse_mode="MarkdownV2",
            )
        return

    # ── /tasks (list) ──
    scope_tasks = _active_bg_tasks.get(scope, {})
    if not scope_tasks:
        await message.reply_text(
            "No active background tasks\\.", parse_mode="MarkdownV2"
        )
        return

    now = time.monotonic()
    lines = [f"*Active background tasks \\({len(scope_tasks)}\\):*\n"]
    for task in scope_tasks.values():
        elapsed = int(now - task.started_at)
        minutes, seconds = divmod(elapsed, 60)
        duration = f"{minutes}m{seconds}s" if minutes else f"{seconds}s"

        tid_short = _escape_mdv2(task.task_id[:12])
        desc_escaped = _escape_mdv2(task.description or "No description")
        type_escaped = _escape_mdv2(task.task_type or "unknown")

        line = (
            f"• `{tid_short}` \\- {desc_escaped}\n"
            f"  Type: {type_escaped} \\| Duration: {_escape_mdv2(duration)}"
        )
        if task.last_tool_name:
            line += f" \\| Last tool: {_escape_mdv2(task.last_tool_name)}"
        lines.append(line)

    lines.append(f"\nUse `/tasks stop <id>` to stop a task\\.")
    text = "\n".join(lines)
    await message.reply_text(text, parse_mode="MarkdownV2")


# ── /restart ──


async def restart_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /restart command: restart the bot process."""
    config: Config = context.bot_data["config"]
    message = update.effective_message
    if not message or not _is_authorized(update.effective_user and update.effective_user.id, config):
        return

    if not _is_private_chat(update):
        await message.reply_text("This command can only be used in private chats\\.", parse_mode="MarkdownV2")
        return

    import os
    import signal

    from open_shrimp.main import request_restart

    await message.reply_text("Restarting\\.\\.\\.", parse_mode="MarkdownV2")

    # Pass the chat scope via env vars so the new process can send a
    # confirmation message after startup.
    os.environ["OPENSHRIMP_RESTART_CHAT_ID"] = str(message.chat_id)
    thread_id = message.message_thread_id
    if thread_id is not None:
        os.environ["OPENSHRIMP_RESTART_THREAD_ID"] = str(thread_id)
    else:
        os.environ.pop("OPENSHRIMP_RESTART_THREAD_ID", None)

    request_restart()
    os.kill(os.getpid(), signal.SIGTERM)


# ── /config ──


async def config_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /config -- open the config Mini App."""
    if not update.effective_user or not update.message:
        return

    config: Config = context.bot_data["config"]
    message = update.message

    if not _is_authorized(update.effective_user.id, config):
        return

    if not _is_private_chat(update):
        await message.reply_text("This command can only be used in private chats\\.", parse_mode="MarkdownV2")
        return

    # Build the Mini App URL.
    if config.review.public_url:
        base_url = config.review.public_url.rstrip("/")
    else:
        base_url = f"https://{config.review.host}:{config.review.port}"

    chat_type = update.effective_chat.type if update.effective_chat else "private"
    _is_private = chat_type == "private"
    _user_id = update.effective_user.id
    scope = chat_scope_from_message(message)

    app_url = f"{base_url}/config/"
    keyboard = InlineKeyboardMarkup([
        [make_web_app_button(
            text="\u2699\ufe0f Edit Configuration",
            url=app_url,
            chat_id=scope.chat_id,
            user_id=_user_id,
            bot_token=config.telegram.token,
            is_private_chat=_is_private,
        )]
    ])
    await message.reply_text(
        "OpenShrimp configuration",
        reply_markup=keyboard,
    )
