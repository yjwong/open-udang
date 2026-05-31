"""Telegram bot setup, callback routing, and long polling for OpenShrimp.

This module is the thin orchestration layer that wires up all handler
modules and provides the main ``run_bot`` entry point.  The actual
handler logic lives in ``open_shrimp.handlers.*``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from telegram import BotCommand, BotCommandScopeAllPrivateChats, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import aiosqlite

from open_shrimp.client_manager import (
    close_all_sessions,
    start_idle_sweep,
    stop_idle_sweep,
)
from open_shrimp.config import Config, load_config
from open_shrimp.sandbox import SandboxManager, create_sandbox_managers
from open_shrimp.sandbox.manager import destroy_contexts_background
from open_shrimp.dispatch_registry import (
    register_dispatch,
    register_parent_notification_wakeup,
)
from open_shrimp.handlers.approval import handle_approval_callback
from open_shrimp.handlers.commands import (
    add_dir_handler,
    cancel_handler,
    handle_add_dir_callback,
    clear_handler,
    config_handler,
    context_handler,
    effort_handler,
    handle_context_callback,
    handle_resume_callback,
    login_handler,
    mcp_handler,
    model_handler,
    restart_handler,
    resume_handler,
    review_handler,
    schedule_handler,
    start_handler,
    status_handler,
    tasks_handler,
    usage_handler,
    vnc_handler,
)
from open_shrimp.handlers.messages import message_handler, web_app_data_handler
from open_shrimp.handlers.questions import _handle_question_callback
from open_shrimp.handlers.utils import _is_authorized

logger = logging.getLogger(__name__)


# ── Callback query router ──


async def callback_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle inline keyboard button presses.

    Delegates to the appropriate handler module based on the callback data
    prefix.
    """
    query = update.callback_query
    if not query or not query.data:
        return

    config: Config = context.bot_data["config"]
    data = query.data

    # OpenCode question callbacks (q_opt, q_toggle, q_done, q_other, q_noop)
    if await _handle_question_callback(query, data, config):
        return

    if not _is_authorized(query.from_user and query.from_user.id, config):
        await query.answer("Unauthorized.")
        return

    # /context selection and pagination
    if await handle_context_callback(query, data, config, context):
        return

    # /resume session selection
    if await handle_resume_callback(query, data, config, context):
        return

    # /add_dir persistence choice
    if await handle_add_dir_callback(query, data, config, context):
        return

    # Tool approval, show_prompt, show_bash, accept_all_edits
    if await handle_approval_callback(query, data, config, context):
        return

    # Auto-update confirmation
    if data.startswith(("update_confirm:", "update_skip:")):
        from open_shrimp.updater import handle_update_callback

        await handle_update_callback(query, data, config)
        return

    # Unknown callback — ignore silently
    logger.debug("Unhandled callback data: %s", data)


# ── Config hot-reload ──


async def _watch_config(config_path: str, bot_data: dict) -> None:
    """Watch the config file for changes and hot-reload into bot_data.

    Uses ``watchfiles`` (inotify on Linux, FSEvents on macOS) for
    efficient, near-instant change detection.  Fields like
    ``telegram.token`` and ``review.*`` require a full restart; changes
    to those are logged as warnings but still applied so that the next
    restart picks them up.
    """
    from watchfiles import awatch

    async for _changes in awatch(config_path):
        try:
            new_config = load_config(config_path)
            old_config: Config = bot_data["config"]

            # Warn about fields that need a restart to take full effect.
            if new_config.telegram.token != old_config.telegram.token:
                logger.warning(
                    "Config reload: telegram.token changed — restart required"
                )
            if new_config.review != old_config.review:
                logger.warning(
                    "Config reload: review.* changed — restart required to take effect"
                )

            bot_data["config"] = new_config

            # Log context-level changes.
            old_names = set(old_config.contexts)
            new_names = set(new_config.contexts)
            added = new_names - old_names
            removed = old_names - new_names
            if added:
                logger.info("Config reload: added contexts: %s", added)
            if removed:
                logger.info("Config reload: removed contexts: %s", removed)
                mgrs = bot_data.get("sandbox_managers")
                if mgrs:
                    destroy_contexts_background(removed, mgrs)
            if not added and not removed:
                logger.info("Config reloaded")
        except Exception:
            logger.exception("Config reload failed, keeping current config")


# ── Application setup ──


def build_application(config: Config, db: aiosqlite.Connection) -> Application:
    """Build and configure the Telegram application."""
    app = (
        Application.builder()
        .token(config.telegram.token)
        .build()
    )

    app.bot_data["config"] = config
    app.bot_data["db"] = db
    app.bot_data["config_path"] = None  # set by run_bot if available

    # Command handlers
    app.add_handler(CommandHandler("start", start_handler))
    app.add_handler(CommandHandler("context", context_handler))
    app.add_handler(CommandHandler("clear", clear_handler))
    app.add_handler(CommandHandler("status", status_handler))
    app.add_handler(CommandHandler("cancel", cancel_handler))
    app.add_handler(CommandHandler("resume", resume_handler))
    app.add_handler(CommandHandler("model", model_handler))
    app.add_handler(CommandHandler("effort", effort_handler))
    app.add_handler(CommandHandler("add_dir", add_dir_handler))
    app.add_handler(CommandHandler("review", review_handler))
    app.add_handler(CommandHandler("mcp", mcp_handler))
    app.add_handler(CommandHandler("schedule", schedule_handler))
    app.add_handler(CommandHandler("tasks", tasks_handler))
    app.add_handler(CommandHandler("usage", usage_handler))
    app.add_handler(CommandHandler("vnc", vnc_handler))
    app.add_handler(CommandHandler("login", login_handler))
    app.add_handler(CommandHandler("config", config_handler))
    app.add_handler(CommandHandler("restart", restart_handler))

    # Callback query handler for tool approval buttons
    app.add_handler(CallbackQueryHandler(callback_query_handler))

    # Web App data handler (e.g. commit from review app)
    app.add_handler(MessageHandler(
        filters.StatusUpdate.WEB_APP_DATA, web_app_data_handler
    ))

    # Message handler (text, photos, documents, locations, and voice notes, non-command)
    app.add_handler(MessageHandler(
        (filters.TEXT | filters.PHOTO | filters.Document.ALL | filters.LOCATION | filters.VOICE | filters.VIDEO_NOTE) & ~filters.COMMAND, message_handler
    ))

    return app


async def run_bot(
    config: Config,
    db: aiosqlite.Connection,
    config_path: str | None = None,
    sandbox_managers: "dict[str, SandboxManager] | None" = None,
    mcp_proxy: "Any | None" = None,
) -> None:
    """Start the bot with long polling."""
    app = build_application(config, db)
    app.bot_data["config_path"] = config_path
    app.bot_data["mcp_proxy"] = mcp_proxy
    logger.info("Starting bot with long polling")
    await app.initialize()

    # Register the agent dispatch callback so the review API (and other
    # components) can send prompts to the agent without needing a direct
    # reference to the bot Application.
    from open_shrimp.db import ChatScope
    from open_shrimp.handlers.messages import (
        _dispatch_to_agent,
        _wake_parent_for_agent_notifications,
    )

    async def _dispatch(prompt: str, scope: ChatScope, placeholder: str | None = None) -> None:
        # Build a minimal ContextTypes-compatible object.  _dispatch_to_agent
        # only uses context.bot, context.bot_data, and asyncio.create_task.
        # Read config from bot_data so hot-reloaded config is used.
        await _dispatch_to_agent(
            prompt, [], scope, app.bot_data["config"], db, app,
            placeholder=placeholder,
        )

    async def _wake_parent(
        scope: ChatScope,
        user_id: int = 0,
        is_private_chat: bool = True,
    ) -> None:
        await _wake_parent_for_agent_notifications(
            scope,
            app.bot_data["config"],
            db,
            app,
            user_id=user_id,
            is_private_chat=is_private_chat,
        )

    register_dispatch(_dispatch)
    register_parent_notification_wakeup(_wake_parent)

    common_commands = [
        BotCommand("context", "List or switch contexts"),
        BotCommand("clear", "Start a fresh session"),
        BotCommand("status", "Show current context, session, and state"),
        BotCommand("cancel", "Abort running Claude invocation"),
        BotCommand("resume", "List and resume a previous session"),
        BotCommand("review", "Review and stage git changes"),
        BotCommand("mcp", "List and manage MCP servers"),
        BotCommand("schedule", "List scheduled tasks"),
        BotCommand("tasks", "List or stop background tasks"),
        BotCommand("usage", "Show Claude quota and usage stats"),
        BotCommand("vnc", "View computer-use desktop"),
    ]
    await app.bot.set_my_commands(common_commands)
    # Private-chat-only commands: these expose sensitive info or mutate
    # global state and should not be visible/usable in group chats.
    await app.bot.set_my_commands(
        common_commands + [
            BotCommand("model", "Show or override the model for this chat"),
            BotCommand("effort", "Show or override the thinking effort level"),
            BotCommand("add_dir", "Add a working directory to the context"),
            BotCommand("login", "Re-authenticate Claude Code OAuth"),
            BotCommand("config", "Edit bot configuration"),
            BotCommand("restart", "Restart the bot process"),
        ],
        scope=BotCommandScopeAllPrivateChats(),
    )
    await app.start()
    await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
    logger.info("Bot is running")

    # If we were restarted via /restart or auto-update, send a confirmation.
    import os as _os

    update_version = _os.environ.pop("OPENSHRIMP_UPDATE_VERSION", None)

    restart_chat = _os.environ.pop("OPENSHRIMP_RESTART_CHAT_ID", None)
    if restart_chat is not None:
        restart_thread = _os.environ.pop("OPENSHRIMP_RESTART_THREAD_ID", None)
        try:
            await app.bot.send_message(
                chat_id=int(restart_chat),
                message_thread_id=int(restart_thread) if restart_thread else None,
                text="Back online\\.",
                parse_mode="MarkdownV2",
            )
        except Exception:
            logger.warning("Failed to send restart confirmation", exc_info=True)

    # Notify all allowed users about a successful auto-update.
    if update_version is not None:
        from open_shrimp.updater import _escape_md

        for uid in config.allowed_users:
            try:
                await app.bot.send_message(
                    chat_id=uid,
                    text=f"Updated to `{_escape_md(update_version)}`\\. Back online\\.",
                    parse_mode="MarkdownV2",
                )
            except Exception:
                logger.warning(
                    "Failed to send update confirmation to %d", uid, exc_info=True
                )

    # Instantiate one SandboxManager per backend used in the config.
    _sandbox_managers = sandbox_managers or create_sandbox_managers(config)
    for mgr in _sandbox_managers.values():
        mgr.set_instance_prefix(config.instance_name)
    app.bot_data["sandbox_managers"] = _sandbox_managers

    # Start reapers for all sandbox managers.
    for mgr in _sandbox_managers.values():
        await asyncio.to_thread(mgr.start_reaper)

    active_contexts = set(config.contexts.keys())
    for name, mgr in _sandbox_managers.items():
        async def _run_orphan_cleanup(
            m: SandboxManager = mgr, n: str = name,
        ) -> None:
            try:
                await asyncio.to_thread(m.cleanup_orphans, active_contexts)
            except Exception:
                logger.warning(
                    "%s.cleanup_orphans() failed", n, exc_info=True,
                )
        asyncio.create_task(_run_orphan_cleanup())

    # Reload scheduled tasks from the database.
    from open_shrimp.scheduler import reload_tasks

    if app.job_queue is not None:
        loaded = await reload_tasks(db, app.bot, config, app.job_queue)
        logger.info("Loaded %d scheduled tasks", loaded)
    else:
        logger.warning(
            "JobQueue not available — scheduled tasks disabled. "
            "Install python-telegram-bot[job-queue] to enable."
        )

    # Start idle-session sweep so dangling Claude processes get reaped.
    start_idle_sweep()

    # Register auto-update checker.
    from open_shrimp.updater import register_update_checker

    register_update_checker(app)

    # Start config file watcher for live reloading.
    watcher_task = None
    if config_path:
        watcher_task = asyncio.create_task(_watch_config(config_path, app.bot_data))

    # Keep running until stopped
    stop_event = asyncio.Event()
    try:
        await stop_event.wait()
    except asyncio.CancelledError:
        pass
    finally:
        if watcher_task:
            watcher_task.cancel()
        # Stop PTB first so the bot goes quiet on Telegram immediately.
        # Previously this came after session/sandbox cleanup, which meant
        # getUpdates polls kept firing for tens of seconds after the user
        # triggered /restart — and if any later step hung, polling would
        # continue forever.  Stopping PTB first also frees us to run the
        # rest of the shutdown with less time pressure.
        logger.info("Stopping Telegram polling...")
        try:
            async with asyncio.timeout(10):
                await app.updater.stop()
                await app.stop()
        except (Exception, TimeoutError):
            logger.warning("Error stopping PTB application", exc_info=True)
        # Destroy any live `claude /login` PTY session before we tear
        # down sandboxes — leaving it alive just delays the final SIGTERM
        # fan-out in the systemd cgroup.
        from open_shrimp.terminal.api import shutdown_login_session
        try:
            async with asyncio.timeout(6):
                await shutdown_login_session()
        except (Exception, TimeoutError):
            logger.warning("Error shutting down login session", exc_info=True)
        stop_idle_sweep()
        await close_all_sessions()
        # Stop all sandbox managers.  Each stop_reaper() is wrapped in a
        # timeout because closing a wedged libvirt connection can block
        # indefinitely, and we'd rather lose that reaper cleanup than
        # hang the whole process.
        # The libvirt backend allows up to 180s for ACPI shutdown
        # internally; give it a little headroom on top so that its own
        # timeout wins over this one.
        for name, mgr in _sandbox_managers.items():
            try:
                async with asyncio.timeout(200):
                    await asyncio.to_thread(mgr.stop_all)
            except (Exception, TimeoutError):
                logger.warning(
                    "%s.stop_all() did not finish in time", name, exc_info=True,
                )
            try:
                async with asyncio.timeout(5):
                    await asyncio.to_thread(mgr.stop_reaper)
            except (Exception, TimeoutError):
                logger.warning(
                    "%s.stop_reaper() did not finish in time",
                    name, exc_info=True,
                )
        try:
            async with asyncio.timeout(10):
                await app.shutdown()
        except (Exception, TimeoutError):
            logger.warning("Error during PTB app.shutdown()", exc_info=True)
