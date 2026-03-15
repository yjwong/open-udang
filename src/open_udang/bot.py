"""Telegram bot setup, callback routing, and long polling for OpenUdang.

This module is the thin orchestration layer that wires up all handler
modules and provides the main ``run_bot`` entry point.  The actual
handler logic lives in ``open_udang.handlers.*``.
"""

from __future__ import annotations

import asyncio
import logging

from telegram import BotCommand, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import aiosqlite

from open_udang.client_manager import close_all_sessions
from open_udang.config import Config
from open_udang.dispatch_registry import register_dispatch
from open_udang.handlers.approval import handle_approval_callback
from open_udang.handlers.commands import (
    cancel_handler,
    clear_handler,
    context_handler,
    handle_resume_callback,
    mcp_handler,
    model_handler,
    resume_handler,
    review_handler,
    status_handler,
)
from open_udang.handlers.messages import message_handler, web_app_data_handler
from open_udang.handlers.questions import _handle_question_callback
from open_udang.handlers.utils import _is_authorized

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

    # AskUserQuestion callbacks (q_opt, q_toggle, q_done, q_other, q_noop)
    if await _handle_question_callback(query, data, config):
        return

    if not _is_authorized(query.from_user and query.from_user.id, config):
        await query.answer("Unauthorized.")
        return

    # /resume session selection
    if await handle_resume_callback(query, data, config, context):
        return

    # Tool approval, show_prompt, show_bash, accept_all_edits
    if await handle_approval_callback(query, data, config, context):
        return

    # Unknown callback — ignore silently
    logger.debug("Unhandled callback data: %s", data)


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

    # Command handlers
    app.add_handler(CommandHandler("context", context_handler))
    app.add_handler(CommandHandler("clear", clear_handler))
    app.add_handler(CommandHandler("status", status_handler))
    app.add_handler(CommandHandler("cancel", cancel_handler))
    app.add_handler(CommandHandler("resume", resume_handler))
    app.add_handler(CommandHandler("model", model_handler))
    app.add_handler(CommandHandler("review", review_handler))
    app.add_handler(CommandHandler("mcp", mcp_handler))

    # Callback query handler for tool approval buttons
    app.add_handler(CallbackQueryHandler(callback_query_handler))

    # Web App data handler (e.g. commit from review app)
    app.add_handler(MessageHandler(
        filters.StatusUpdate.WEB_APP_DATA, web_app_data_handler
    ))

    # Message handler (text, photos, documents, and locations, non-command)
    app.add_handler(MessageHandler(
        (filters.TEXT | filters.PHOTO | filters.Document.ALL | filters.LOCATION) & ~filters.COMMAND, message_handler
    ))

    return app


async def run_bot(config: Config, db: aiosqlite.Connection) -> None:
    """Start the bot with long polling."""
    app = build_application(config, db)
    logger.info("Starting bot with long polling")
    await app.initialize()

    # Register the agent dispatch callback so the review API (and other
    # components) can send prompts to the agent without needing a direct
    # reference to the bot Application.
    from open_udang.handlers.messages import _dispatch_to_agent

    async def _dispatch(prompt: str, chat_id: int) -> None:
        # Build a minimal ContextTypes-compatible object.  _dispatch_to_agent
        # only uses context.bot, context.bot_data, and asyncio.create_task.
        await _dispatch_to_agent(prompt, [], chat_id, config, db, app)

    register_dispatch(_dispatch)

    await app.bot.set_my_commands([
        BotCommand("context", "List or switch contexts"),
        BotCommand("clear", "Start a fresh session"),
        BotCommand("status", "Show current context, session, and state"),
        BotCommand("cancel", "Abort running Claude invocation"),
        BotCommand("resume", "List and resume a previous session"),
        BotCommand("model", "Show or override the model for this chat"),
        BotCommand("review", "Review and stage git changes"),
        BotCommand("mcp", "List and manage MCP servers"),
    ])
    await app.start()
    await app.updater.start_polling()
    logger.info("Bot is running")

    # Keep running until stopped
    stop_event = asyncio.Event()
    try:
        await stop_event.wait()
    except asyncio.CancelledError:
        pass
    finally:
        await close_all_sessions()
        await app.updater.stop()
        await app.stop()
        await app.shutdown()
