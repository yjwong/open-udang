"""Telegram bot setup, handlers, and long polling for OpenUdang.

Handles commands, message routing, group chat logic, ACL enforcement,
inline keyboard tool approval, and integration with the agent/stream modules.
"""

from __future__ import annotations

import asyncio
import difflib
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

import aiosqlite
from telegram import (
    Bot,
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from open_udang.agent import ImageAttachment, run_agent
from open_udang.config import Config, ContextConfig
from open_udang.db import (
    delete_session,
    get_active_context,
    get_pinned_message_id,
    get_session_id,
    set_active_context,
    set_pinned_message_id,
    set_session_id,
)
from open_udang.stream import (
    StreamResult,
    _DraftState,
    add_tool_notification,
    finalize_and_reset,
    stream_response,
)

logger = logging.getLogger(__name__)

# Per-chat running asyncio task (for cancellation)
_running_tasks: dict[int, asyncio.Task[Any]] = {}

# Pending tool approval futures: callback_data -> asyncio.Future[bool]
_approval_futures: dict[str, asyncio.Future[bool]] = {}

# Pending Agent tool inputs for "Show prompt" expansion: tool_use_id -> tool_input
_pending_agent_inputs: dict[str, dict[str, Any]] = {}

# Media group batching: media_group_id -> list of messages received so far.
# When Telegram sends an album (multiple photos), each photo arrives as a
# separate Update sharing the same media_group_id.  We collect them here and
# use a short delay to wait for the full batch before processing.
_media_group_messages: dict[str, list[Any]] = {}
_media_group_tasks: dict[str, asyncio.Task[Any]] = {}

# How long to wait for additional media group messages (seconds).
_MEDIA_GROUP_WAIT: float = 0.5


@dataclass
class _QuestionState:
    """State for an active AskUserQuestion inline keyboard."""

    question_id: str
    chat_id: int
    options: list[dict[str, Any]]
    multi_select: bool
    future: asyncio.Future[str]
    selected: set[int] = field(default_factory=set)
    other_texts: list[str] = field(default_factory=list)
    message_id: int | None = None


# Pending question states: question_id -> _QuestionState
_question_states: dict[str, _QuestionState] = {}

# Pending text input futures for "Other" answers: chat_id -> asyncio.Future[str]
_pending_text_inputs: dict[int, asyncio.Future[str]] = {}


def _escape_mdv2(text: str) -> str:
    """Escape MarkdownV2 special characters in plain text."""
    for ch in r"_*[]()~`>#+-=|{}.!":
        text = text.replace(ch, f"\\{ch}")
    return text


def _get_locked_context(chat_id: int, config: Config) -> str | None:
    """Return the context name this chat is locked to, or None."""
    for name, ctx in config.contexts.items():
        if chat_id in ctx.locked_for_chats:
            return name
    return None


async def _get_context_name(chat_id: int, config: Config, db: aiosqlite.Connection) -> str:
    """Get the active context name for a chat (persisted in DB)."""
    # If locked, always use that context regardless of what's saved
    locked = _get_locked_context(chat_id, config)
    if locked:
        await set_active_context(db, chat_id, locked)
        return locked

    saved = await get_active_context(db, chat_id)
    if saved and saved in config.contexts:
        return saved

    # Check if this chat has a default context configured
    for name, ctx in config.contexts.items():
        if chat_id in ctx.default_for_chats:
            await set_active_context(db, chat_id, name)
            return name

    await set_active_context(db, chat_id, config.default_context)
    return config.default_context


async def _get_context(
    chat_id: int, config: Config, db: aiosqlite.Connection
) -> tuple[str, ContextConfig]:
    """Get context name and config for a chat."""
    name = await _get_context_name(chat_id, config, db)
    return name, config.contexts[name]


def _is_authorized(user_id: int | None, config: Config) -> bool:
    """Check if a user is in the allowlist."""
    return user_id is not None and user_id in config.allowed_users


def _is_bot_addressed(update: Update, bot_username: str) -> bool:
    """Check if the bot is @mentioned or replied to in a group chat.

    In private chats, always returns True.
    """
    message = update.effective_message
    if message is None:
        return False

    chat = update.effective_chat
    if chat is None or chat.type == "private":
        return True

    # Check if replying to the bot
    if message.reply_to_message and message.reply_to_message.from_user:
        if message.reply_to_message.from_user.username == bot_username:
            return True

    # Check for @mention in entities (text messages) or caption_entities (photos)
    entities = message.entities or message.caption_entities or []
    text = message.text or message.caption or ""
    for entity in entities:
        if entity.type == "mention":
            mention = text[entity.offset : entity.offset + entity.length]
            if mention.lower() == f"@{bot_username.lower()}":
                return True

    return False


def _strip_mention(text: str, bot_username: str) -> str:
    """Remove @bot_username from message text."""
    mention = f"@{bot_username}"
    # Case-insensitive removal
    idx = text.lower().find(mention.lower())
    if idx != -1:
        text = text[:idx] + text[idx + len(mention) :]
    return text.strip()


async def _cancel_running(chat_id: int) -> None:
    """Cancel any running agent task for a chat."""
    task = _running_tasks.pop(chat_id, None)
    if task and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        logger.info("Cancelled running task for chat %d", chat_id)


def _format_token_count(count: int) -> str:
    """Format a token count as a human-readable string (e.g. 12.3k, 1.2M)."""
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M"
    if count >= 1_000:
        return f"{count / 1_000:.1f}k"
    return str(count)


def _build_status_text(
    ctx_name: str,
    ctx: ContextConfig,
    usage: dict[str, int] | None = None,
    total_cost_usd: float | None = None,
) -> str:
    """Build the pinned status message text in MarkdownV2."""
    escaped_name = _escape_mdv2(ctx_name)
    escaped_desc = _escape_mdv2(ctx.description)
    escaped_dir = _escape_mdv2(ctx.directory)
    escaped_model = _escape_mdv2(ctx.model)
    lines = [
        f"📌 *Active context:* `{escaped_name}`",
        f"{escaped_desc}",
        "",
        f"📁 `{escaped_dir}`",
        f"🤖 `{escaped_model}`",
    ]

    if usage:
        input_tokens = usage.get("input_tokens", 0)
        output_tokens = usage.get("output_tokens", 0)
        cache_read = usage.get("cache_read_input_tokens", 0)
        cache_creation = usage.get("cache_creation_input_tokens", 0)

        input_str = _escape_mdv2(_format_token_count(input_tokens))
        output_str = _escape_mdv2(_format_token_count(output_tokens))

        lines.append("")
        lines.append(f"📊 *Context window:* {input_str} in / {output_str} out")

        if cache_read or cache_creation:
            cache_parts = []
            if cache_read:
                cache_parts.append(f"{_escape_mdv2(_format_token_count(cache_read))} read")
            if cache_creation:
                cache_parts.append(f"{_escape_mdv2(_format_token_count(cache_creation))} created")
            separator = " \\| "
            lines.append(f"💾 *Cache:* {separator.join(cache_parts)}")

        if total_cost_usd is not None:
            cost_str = _escape_mdv2(f"${total_cost_usd:.4f}")
            lines.append(f"💰 *Cost:* {cost_str}")

    return "\n".join(lines)


async def _update_pinned_status(
    bot: Bot,
    chat_id: int,
    ctx_name: str,
    ctx: ContextConfig,
    db: aiosqlite.Connection,
    usage: dict[str, int] | None = None,
    total_cost_usd: float | None = None,
) -> None:
    """Send or update the pinned status message for a chat."""
    text = _build_status_text(ctx_name, ctx, usage=usage, total_cost_usd=total_cost_usd)
    existing_msg_id = await get_pinned_message_id(db, chat_id)

    # Try to edit the existing pinned message
    if existing_msg_id:
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=existing_msg_id,
                text=text,
                parse_mode="MarkdownV2",
            )
            return
        except Exception:
            logger.debug(
                "Could not edit pinned message %d in chat %d, will send new one",
                existing_msg_id,
                chat_id,
            )

    # Send a new message and pin it
    try:
        msg = await bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode="MarkdownV2",
        )
        await set_pinned_message_id(db, chat_id, msg.message_id)
        await bot.pin_chat_message(
            chat_id=chat_id,
            message_id=msg.message_id,
            disable_notification=True,
        )
    except Exception:
        logger.exception("Failed to send/pin status message in chat %d", chat_id)


# ── Command handlers ──


async def context_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /context command: list or switch contexts."""
    config: Config = context.bot_data["config"]
    db: aiosqlite.Connection = context.bot_data["db"]
    message = update.effective_message
    if not message or not _is_authorized(update.effective_user and update.effective_user.id, config):
        return

    chat_id = message.chat_id
    args = message.text.split() if message.text else []

    if len(args) < 2:
        # List contexts
        current = await _get_context_name(chat_id, config, db)
        locked = _get_locked_context(chat_id, config)
        if locked:
            ctx = config.contexts[locked]
            escaped_name = locked.replace("_", "\\_")
            escaped_desc = ctx.description.replace("_", "\\_").replace(".", "\\.")
            await message.reply_text(
                f"This chat is locked to context `{escaped_name}` \\- {escaped_desc}",
                parse_mode="MarkdownV2",
            )
        else:
            lines = ["*Available contexts:*\n"]
            for name, ctx in config.contexts.items():
                marker = " \\(active\\)" if name == current else ""
                escaped_name = name.replace("_", "\\_")
                escaped_desc = ctx.description.replace("_", "\\_").replace(".", "\\.")
                lines.append(f"• `{escaped_name}` \\- {escaped_desc}{marker}")
            await message.reply_text("\n".join(lines), parse_mode="MarkdownV2")
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

    locked = _get_locked_context(chat_id, config)
    if locked:
        await message.reply_text(
            f"This chat is locked to context `{locked}`\\.",
            parse_mode="MarkdownV2",
        )
        return

    await set_active_context(db, chat_id, target)
    ctx = config.contexts[target]
    desc = ctx.description.replace(".", "\\.").replace("-", "\\-")
    await message.reply_text(
        f"Switched to context `{target}` \\- {desc}",
        parse_mode="MarkdownV2",
    )
    await _update_pinned_status(context.bot, chat_id, target, ctx, db)


async def clear_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /clear command: start fresh session."""
    config: Config = context.bot_data["config"]
    db: aiosqlite.Connection = context.bot_data["db"]
    message = update.effective_message
    if not message or not _is_authorized(update.effective_user and update.effective_user.id, config):
        return

    chat_id = message.chat_id
    ctx_name, ctx = await _get_context(chat_id, config, db)

    await _cancel_running(chat_id)
    await delete_session(db, chat_id, ctx_name)
    await message.reply_text(f"Started fresh session in context `{ctx_name}`\\.", parse_mode="MarkdownV2")
    await _update_pinned_status(context.bot, chat_id, ctx_name, ctx, db)


async def status_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /status command: show current state."""
    config: Config = context.bot_data["config"]
    db: aiosqlite.Connection = context.bot_data["db"]
    message = update.effective_message
    if not message or not _is_authorized(update.effective_user and update.effective_user.id, config):
        return

    chat_id = message.chat_id
    ctx_name, ctx = await _get_context(chat_id, config, db)
    session_id = await get_session_id(db, chat_id, ctx_name)
    running = chat_id in _running_tasks and not _running_tasks[chat_id].done()

    lines = [
        f"*Context:* `{ctx_name}`",
        f"*Directory:* `{ctx.directory}`",
        f"*Model:* `{ctx.model}`",
        f"*Session:* {'`' + session_id[:12] + '...' + '`' if session_id else 'None'}",
        f"*Running:* {'Yes' if running else 'No'}",
    ]
    # Escape dots and dashes for MarkdownV2
    text = "\n".join(lines)
    for ch in ".-/":
        text = text.replace(ch, f"\\{ch}")
    await message.reply_text(text, parse_mode="MarkdownV2")


async def cancel_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /cancel command: abort running Claude invocation."""
    config: Config = context.bot_data["config"]
    message = update.effective_message
    if not message or not _is_authorized(update.effective_user and update.effective_user.id, config):
        return

    chat_id = message.chat_id
    if chat_id in _running_tasks and not _running_tasks[chat_id].done():
        await _cancel_running(chat_id)
        await message.reply_text("Cancelled\\.", parse_mode="MarkdownV2")
    else:
        await message.reply_text("Nothing running\\.", parse_mode="MarkdownV2")


# ── Message handler ──


async def _download_telegram_photos(
    messages: list[Any], bot: Bot
) -> list[ImageAttachment]:
    """Download photos from one or more Telegram messages and return as ImageAttachments.

    Each message may contain one photo (represented as a list of PhotoSize
    objects at different resolutions).  We take the largest resolution from
    each message.
    """
    attachments: list[ImageAttachment] = []
    for message in messages:
        if not message.photo:
            continue
        # message.photo is a list of PhotoSize objects sorted by size.
        # Take the largest one (last element) for best quality.
        photo = message.photo[-1]
        file = await bot.get_file(photo.file_id)
        photo_bytes = bytes(await file.download_as_bytearray())
        attachments.append(ImageAttachment(data=photo_bytes, mime_type="image/jpeg"))
    return attachments


async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle incoming text and photo messages: route to Claude agent.

    For media groups (albums with multiple photos), messages are batched
    using a short delay so all photos are collected before processing.
    """
    config: Config = context.bot_data["config"]
    db: aiosqlite.Connection = context.bot_data["db"]
    message = update.effective_message
    if not message:
        return

    # Must have text, caption, photo, or location
    has_text = bool(message.text)
    has_photo = bool(message.photo)
    has_caption = bool(message.caption)
    has_location = bool(message.location)

    logger.info(
        "message_handler: chat=%s has_text=%s has_photo=%s has_caption=%s has_location=%s media_group_id=%s",
        message.chat_id, has_text, has_photo, has_caption, has_location, message.media_group_id,
    )

    if not has_text and not has_photo and not has_location:
        logger.info("message_handler: no text, photo, or location, ignoring")
        return

    if not _is_authorized(update.effective_user and update.effective_user.id, config):
        logger.info("message_handler: unauthorized user %s", update.effective_user)
        return

    # Check if this is a text response to an "Other" question prompt.
    # If there's a pending text input future for this chat, resolve it
    # and don't dispatch to the agent.
    chat_id_for_input = message.chat_id
    if has_text and chat_id_for_input in _pending_text_inputs:
        text_future = _pending_text_inputs.get(chat_id_for_input)
        if text_future and not text_future.done():
            text_future.set_result(message.text or "")
            logger.info("Resolved pending text input for chat %d", chat_id_for_input)
            return

    bot_username = (await context.bot.get_me()).username or ""
    if not _is_bot_addressed(update, bot_username):
        logger.info("message_handler: bot not addressed, ignoring")
        return

    # If this message is part of a media group (album), batch it.
    if message.media_group_id and has_photo:
        await _handle_media_group_message(update, context, message)
        return

    chat_id = message.chat_id

    # Extract text from either message.text or message.caption (for photos)
    raw_text = message.text or message.caption or ""
    prompt = _strip_mention(raw_text, bot_username)

    # Build location context string if a location was shared
    if has_location:
        loc = message.location
        location_text = f"User shared location: {loc.latitude}, {loc.longitude}"
        if loc.horizontal_accuracy:
            location_text += f" (accuracy: {loc.horizontal_accuracy}m)"
        if loc.heading is not None:
            location_text += f" (heading: {loc.heading}°)"
        # Prepend location to any existing prompt text
        prompt = f"{location_text}\n\n{prompt}" if prompt else location_text
        logger.info("Location shared in chat %d: %s, %s", chat_id, loc.latitude, loc.longitude)

    # For photos without a caption, use a default prompt
    if not prompt and has_photo:
        prompt = "What's in this image?"
    elif not prompt:
        return

    # Download photo attachments if present
    images: list[ImageAttachment] = []
    if has_photo:
        images = await _download_telegram_photos([message], context.bot)
        logger.info(
            "Downloaded %d photo(s) for chat %d (%d bytes)",
            len(images), chat_id, sum(len(img.data) for img in images),
        )

    await _dispatch_to_agent(prompt, images, chat_id, config, db, context)


async def _handle_media_group_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE, message: Any
) -> None:
    """Collect media group messages and dispatch once the batch is complete.

    Telegram sends each photo in an album as a separate Update with the same
    media_group_id.  We accumulate them and use a short timer to detect when
    the batch is complete (no new messages for _MEDIA_GROUP_WAIT seconds).
    """
    group_id = message.media_group_id
    if group_id not in _media_group_messages:
        _media_group_messages[group_id] = []
    _media_group_messages[group_id].append(message)

    logger.info(
        "Media group %s: collected %d message(s) so far",
        group_id, len(_media_group_messages[group_id]),
    )

    # Cancel the previous timer for this group (we got another message).
    existing_task = _media_group_tasks.pop(group_id, None)
    if existing_task and not existing_task.done():
        existing_task.cancel()

    # Start a new timer.  When it fires, the batch is considered complete.
    async def _process_group() -> None:
        await asyncio.sleep(_MEDIA_GROUP_WAIT)
        messages = _media_group_messages.pop(group_id, [])
        _media_group_tasks.pop(group_id, None)
        if not messages:
            return

        config: Config = context.bot_data["config"]
        db: aiosqlite.Connection = context.bot_data["db"]
        chat_id = messages[0].chat_id
        bot_username = (await context.bot.get_me()).username or ""

        # Extract caption from the first message that has one.
        raw_text = ""
        for msg in messages:
            if msg.caption:
                raw_text = msg.caption
                break

        prompt = _strip_mention(raw_text, bot_username) if raw_text else ""
        if not prompt:
            count = len(messages)
            prompt = f"What's in {'this image' if count == 1 else 'these images'}?"

        images = await _download_telegram_photos(messages, context.bot)
        logger.info(
            "Media group %s complete: %d photo(s) for chat %d (%d bytes)",
            group_id, len(images), chat_id, sum(len(img.data) for img in images),
        )

        await _dispatch_to_agent(prompt, images, chat_id, config, db, context)

    _media_group_tasks[group_id] = asyncio.create_task(_process_group())


async def _dispatch_to_agent(
    prompt: str,
    images: list[ImageAttachment],
    chat_id: int,
    config: Config,
    db: aiosqlite.Connection,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Cancel any running task, then launch the agent for the given prompt."""
    # Cancel any running task for this chat
    await _cancel_running(chat_id)

    ctx_name, ctx_config = await _get_context(chat_id, config, db)
    session_id = await get_session_id(db, chat_id, ctx_name)

    # Ensure pinned status message exists (e.g. after a restart)
    if not await get_pinned_message_id(db, chat_id):
        await _update_pinned_status(context.bot, chat_id, ctx_name, ctx_config, db)

    async def _run() -> None:
        # Create draft state early so it can be shared between the
        # stream and the tool approval callback. This ensures the
        # approval callback can finalize the in-progress draft
        # before sending the keyboard, preserving message ordering.
        # It also carries session_id for early capture so we can
        # persist the session even if the task is cancelled.
        draft_state = _DraftState(chat_id=chat_id)

        try:
            async def request_approval(
                tool_name: str, tool_input: dict[str, Any], tool_use_id: str
            ) -> bool:
                # Finalize any in-progress draft so the approval keyboard
                # appears after the accumulated text, not out of order.
                await finalize_and_reset(context.bot, draft_state)
                return await _send_approval_keyboard(
                    context.bot, chat_id, tool_name, tool_input, tool_use_id
                )

            async def handle_questions(
                questions: list[dict[str, Any]],
            ) -> dict[str, str]:
                return await _handle_ask_user_questions(
                    context.bot, chat_id, questions, draft_state
                )

            events = run_agent(
                prompt=prompt,
                context=ctx_config,
                request_approval=request_approval,
                session_id=session_id,
                images=images if images else None,
                handle_user_questions=handle_questions,
            )

            result = await stream_response(
                bot=context.bot,
                chat_id=chat_id,
                events=events,
                draft_state=draft_state,
                allowed_tools=ctx_config.allowed_tools,
            )

            if result.session_id:
                await set_session_id(db, chat_id, ctx_name, result.session_id)

            # Update pinned message with context window usage
            if result.usage:
                await _update_pinned_status(
                    context.bot, chat_id, ctx_name, ctx_config, db,
                    usage=result.usage,
                    total_cost_usd=result.total_cost_usd,
                )

        except asyncio.CancelledError:
            logger.info("Agent task cancelled for chat %d", chat_id)
        except Exception:
            logger.exception("Agent task failed for chat %d", chat_id)
            try:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text="An error occurred while processing your request\\.",
                    parse_mode="MarkdownV2",
                )
            except Exception:
                logger.exception("Failed to send error message")
        finally:
            # Persist session_id even on cancellation so the next
            # message can resume where this one left off.  The
            # draft_state captures session_id as early as possible
            # (from SystemMessage init), so it's usually available
            # even if we never reached ResultMessage.
            if draft_state.session_id:
                try:
                    await set_session_id(db, chat_id, ctx_name, draft_state.session_id)
                except Exception:
                    logger.debug(
                        "Failed to save session on cleanup for chat %d", chat_id
                    )
            _running_tasks.pop(chat_id, None)

    task = asyncio.create_task(_run())
    _running_tasks[chat_id] = task


# ── AskUserQuestion handling ──


def _build_question_keyboard(state: _QuestionState) -> InlineKeyboardMarkup:
    """Build inline keyboard for a question's options."""
    qid = state.question_id
    buttons: list[list[InlineKeyboardButton]] = []

    for i, opt in enumerate(state.options):
        label = opt.get("label", f"Option {i + 1}")
        if state.multi_select:
            prefix = "✓ " if i in state.selected else ""
            cb_data = f"q_toggle:{qid}:{i}"
        else:
            prefix = ""
            cb_data = f"q_opt:{qid}:{i}"
        buttons.append([InlineKeyboardButton(f"{prefix}{label}", callback_data=cb_data)])

    # Show any "Other" texts already entered (multi-select)
    for j, txt in enumerate(state.other_texts):
        display = txt[:30] + ("…" if len(txt) > 30 else "")
        buttons.append([InlineKeyboardButton(f"✓ {display}", callback_data=f"q_noop:{qid}")])

    # "Other" button for custom text input
    buttons.append([InlineKeyboardButton("Other…", callback_data=f"q_other:{qid}")])

    if state.multi_select:
        count = len(state.selected) + len(state.other_texts)
        done_label = f"Done ({count} selected)" if count else "Done"
        buttons.append([InlineKeyboardButton(done_label, callback_data=f"q_done:{qid}")])

    return InlineKeyboardMarkup(buttons)


def _format_question_text(question: dict[str, Any]) -> str:
    """Format a question with its header and option descriptions."""
    question_text = question.get("question", "")
    header = question.get("header", "")
    options = question.get("options", [])

    parts: list[str] = []
    if header:
        parts.append(f"❓ *{_escape_mdv2(header)}*")
    parts.append(_escape_mdv2(question_text))

    for opt in options:
        label = opt.get("label", "")
        desc = opt.get("description", "")
        if desc:
            parts.append(f"• *{_escape_mdv2(label)}* — {_escape_mdv2(desc)}")

    return "\n".join(parts)


async def _send_question_keyboard(
    bot: Bot,
    chat_id: int,
    question: dict[str, Any],
) -> str:
    """Present a question via inline keyboard and wait for the user's answer.

    Returns the selected option label (or custom "Other" text).
    """
    options = question.get("options", [])
    multi_select = question.get("multiSelect", False)
    question_id = uuid.uuid4().hex[:8]

    loop = asyncio.get_running_loop()
    future: asyncio.Future[str] = loop.create_future()

    state = _QuestionState(
        question_id=question_id,
        chat_id=chat_id,
        options=options,
        multi_select=multi_select,
        future=future,
    )
    _question_states[question_id] = state

    keyboard = _build_question_keyboard(state)
    text = _format_question_text(question)

    msg = await bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode="MarkdownV2",
        reply_markup=keyboard,
    )
    state.message_id = msg.message_id

    try:
        return await future
    finally:
        _question_states.pop(question_id, None)


async def _handle_ask_user_questions(
    bot: Bot,
    chat_id: int,
    questions: list[dict[str, Any]],
    draft_state: _DraftState,
) -> dict[str, str]:
    """Present AskUserQuestion questions via Telegram and collect answers.

    Called from the PreToolUse hook when AskUserQuestion is intercepted.
    Finalizes any in-progress draft before presenting questions.
    """
    await finalize_and_reset(bot, draft_state)

    answers: dict[str, str] = {}
    for q in questions:
        question_text = q.get("question", "")
        answer = await _send_question_keyboard(bot, chat_id, q)
        answers[question_text] = answer

    return answers


async def _handle_question_callback(
    query: Any, data: str, config: Config
) -> bool:
    """Handle question-related callback queries. Returns True if handled."""
    if not data.startswith("q_"):
        return False

    if not _is_authorized(query.from_user and query.from_user.id, config):
        await query.answer("Unauthorized.")
        return True

    # Parse callback data
    parts = data.split(":", 2)
    action = parts[0]  # q_opt, q_toggle, q_done, q_other, q_noop

    if action == "q_noop":
        await query.answer()
        return True

    if len(parts) < 2:
        await query.answer("Invalid callback data.")
        return True

    question_id = parts[1]
    state = _question_states.get(question_id)
    if not state or state.future.done():
        await query.answer("This question has expired.")
        return True

    if action == "q_opt":
        # Single-select: resolve immediately with the selected option label
        option_idx = int(parts[2]) if len(parts) > 2 else 0
        if 0 <= option_idx < len(state.options):
            label = state.options[option_idx].get("label", f"Option {option_idx + 1}")
            state.future.set_result(label)
            await query.answer(f"Selected: {label}")

            # Update message to show selection, remove keyboard
            if query.message:
                try:
                    original_md = query.message.text_markdown_v2 or query.message.text or ""
                    await query.message.edit_text(
                        text=original_md + f"\n\n✅ *Selected:* {_escape_mdv2(label)}",
                        parse_mode="MarkdownV2",
                        reply_markup=None,
                    )
                except Exception:
                    logger.exception("Failed to update question message")
        return True

    if action == "q_toggle":
        # Multi-select: toggle option
        option_idx = int(parts[2]) if len(parts) > 2 else 0
        if 0 <= option_idx < len(state.options):
            if option_idx in state.selected:
                state.selected.discard(option_idx)
            else:
                state.selected.add(option_idx)

            # Update keyboard to reflect toggled state
            keyboard = _build_question_keyboard(state)
            await query.answer()
            if query.message:
                try:
                    original_md = query.message.text_markdown_v2 or query.message.text or ""
                    await query.message.edit_reply_markup(reply_markup=keyboard)
                except Exception:
                    logger.exception("Failed to update question keyboard")
        return True

    if action == "q_done":
        # Multi-select: finalize with all selected options
        labels: list[str] = []
        for idx in sorted(state.selected):
            if 0 <= idx < len(state.options):
                labels.append(state.options[idx].get("label", f"Option {idx + 1}"))
        labels.extend(state.other_texts)

        result = ", ".join(labels) if labels else "None selected"
        state.future.set_result(result)
        await query.answer(f"Done: {result[:50]}")

        # Update message to show selections, remove keyboard
        if query.message:
            try:
                original_md = query.message.text_markdown_v2 or query.message.text or ""
                await query.message.edit_text(
                    text=original_md + f"\n\n✅ *Selected:* {_escape_mdv2(result)}",
                    parse_mode="MarkdownV2",
                    reply_markup=None,
                )
            except Exception:
                logger.exception("Failed to update question message")
        return True

    if action == "q_other":
        # "Other" — ask user to type a custom answer
        await query.answer("Type your answer below:")

        chat_id = state.chat_id
        loop = asyncio.get_running_loop()
        text_future: asyncio.Future[str] = loop.create_future()
        _pending_text_inputs[chat_id] = text_future

        try:
            custom_text = await text_future
        finally:
            _pending_text_inputs.pop(chat_id, None)

        if state.multi_select:
            # Add to other_texts and update keyboard
            state.other_texts.append(custom_text)
            keyboard = _build_question_keyboard(state)
            if query.message:
                try:
                    await query.message.edit_reply_markup(reply_markup=keyboard)
                except Exception:
                    logger.exception("Failed to update question keyboard after Other")
        else:
            # Single-select: resolve with custom text
            state.future.set_result(custom_text)
            if query.message:
                try:
                    original_md = query.message.text_markdown_v2 or query.message.text or ""
                    await query.message.edit_text(
                        text=original_md + f"\n\n✅ *Answer:* {_escape_mdv2(custom_text)}",
                        parse_mode="MarkdownV2",
                        reply_markup=None,
                    )
                except Exception:
                    logger.exception("Failed to update question message after Other")
        return True

    return False


# ── Tool approval ──


def _format_edit_approval(tool_input: dict[str, Any]) -> str:
    """Format an Edit tool call as a unified diff for the approval prompt."""
    file_path = tool_input.get("file_path", "unknown")
    old_string = tool_input.get("old_string", "")
    new_string = tool_input.get("new_string", "")

    escaped_path = _escape_mdv2(file_path)
    header = f"✏️ *Edit:* `{escaped_path}`"

    old_lines = old_string.splitlines()
    new_lines = new_string.splitlines()
    diff_lines = list(difflib.unified_diff(
        old_lines, new_lines, lineterm="",
    ))

    if diff_lines:
        # Drop the ---/+++ header lines from unified_diff, keep @@ and content
        diff_body = "\n".join(diff_lines[2:])
    else:
        diff_body = "(no diff)"

    # Truncate if the diff is too long for a single Telegram message.
    # Reserve space for the header, code fences, and buttons (~200 chars).
    max_diff_len = 4096 - 200
    if len(diff_body) > max_diff_len:
        diff_body = diff_body[:max_diff_len] + "\n..."

    escaped_diff = _escape_mdv2(diff_body)
    return f"{header}\n\n```diff\n{escaped_diff}\n```"


def _format_bash_approval(tool_input: dict[str, Any]) -> str:
    """Format a Bash tool call for the approval prompt."""
    command = tool_input.get("command", "")
    description = tool_input.get("description", "")

    parts: list[str] = []
    if description:
        parts.append(f"💻 *Bash:* {_escape_mdv2(description)}")
    else:
        parts.append("💻 *Bash*")

    # Show the command in a code block.
    max_cmd_len = 4096 - 200
    if len(command) > max_cmd_len:
        command = command[:max_cmd_len] + "\n..."
    escaped_cmd = _escape_mdv2(command)
    parts.append(f"```bash\n{escaped_cmd}\n```")

    return "\n\n".join(parts)


def _format_write_approval(tool_input: dict[str, Any]) -> str:
    """Format a Write tool call for the approval prompt."""
    file_path = tool_input.get("file_path", "unknown")
    content = tool_input.get("content", "")

    escaped_path = _escape_mdv2(file_path)
    header = f"📝 *Write:* `{escaped_path}`"

    # Truncate if the content is too long for a single Telegram message.
    max_content_len = 4096 - 200
    if len(content) > max_content_len:
        content = content[:max_content_len] + "\n..."

    escaped_content = _escape_mdv2(content)
    return f"{header}\n\n```\n{escaped_content}\n```"


def _format_agent_approval(tool_input: dict[str, Any], expanded: bool = False) -> str:
    """Format an Agent tool call for the approval prompt.

    Shows a compact view with description and subagent type by default.
    When expanded=True, appends the full prompt text.
    """
    description = tool_input.get("description", "")
    subagent_type = tool_input.get("subagent_type", "")
    prompt = tool_input.get("prompt", "")

    parts: list[str] = []

    # Header with subagent type
    if subagent_type:
        parts.append(f"🤖 *Agent* \\({_escape_mdv2(subagent_type)}\\)")
    else:
        parts.append("🤖 *Agent*")

    # Description line
    if description:
        parts.append(_escape_mdv2(description))

    # Full prompt (only when expanded)
    if expanded and prompt:
        max_prompt_len = 4096 - 300
        display_prompt = prompt
        if len(display_prompt) > max_prompt_len:
            display_prompt = display_prompt[:max_prompt_len] + "\n..."
        parts.append(f"```\n{_escape_mdv2(display_prompt)}\n```")

    return "\n\n".join(parts)


def _format_generic_approval(tool_name: str, tool_input: dict[str, Any]) -> str:
    """Format a generic tool call for the approval prompt."""
    summary_parts = [f"*Tool:* `{tool_name}`"]
    for key, val in tool_input.items():
        val_str = str(val)
        if len(val_str) > 200:
            val_str = val_str[:200] + "..."
        key_escaped = key.replace("_", "\\_")
        val_escaped = _escape_mdv2(val_str)
        summary_parts.append(f"*{key_escaped}:* {val_escaped}")
    return "\n".join(summary_parts)


async def _send_approval_keyboard(
    bot: Bot,
    chat_id: int,
    tool_name: str,
    tool_input: dict[str, Any],
    tool_use_id: str,
) -> bool:
    """Send an inline keyboard for tool approval and wait for response."""
    if tool_name == "Edit":
        text = _format_edit_approval(tool_input)
    elif tool_name == "Bash":
        text = _format_bash_approval(tool_input)
    elif tool_name == "Write":
        text = _format_write_approval(tool_input)
    elif tool_name == "Agent":
        text = _format_agent_approval(tool_input, expanded=False)
    else:
        text = _format_generic_approval(tool_name, tool_input)

    approve_data = f"approve:{tool_use_id}"
    deny_data = f"deny:{tool_use_id}"

    # Build keyboard buttons — Agent tool gets an extra "Show prompt" button
    buttons = []
    if tool_name == "Agent":
        show_prompt_data = f"show_prompt:{tool_use_id}"
        _pending_agent_inputs[tool_use_id] = tool_input
        buttons.append(InlineKeyboardButton("Show prompt", callback_data=show_prompt_data))
    buttons.append(InlineKeyboardButton("Approve", callback_data=approve_data))
    buttons.append(InlineKeyboardButton("Deny", callback_data=deny_data))

    keyboard = InlineKeyboardMarkup([buttons])

    await bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode="MarkdownV2",
        reply_markup=keyboard,
    )

    # Create a future and wait for the callback
    loop = asyncio.get_running_loop()
    future: asyncio.Future[bool] = loop.create_future()
    _approval_futures[approve_data] = future
    _approval_futures[deny_data] = future

    try:
        return await future
    finally:
        _approval_futures.pop(approve_data, None)
        _approval_futures.pop(deny_data, None)
        _pending_agent_inputs.pop(tool_use_id, None)


async def callback_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle inline keyboard button presses for tool approval and questions."""
    query = update.callback_query
    if not query or not query.data:
        return

    config: Config = context.bot_data["config"]
    data = query.data

    # Handle AskUserQuestion callbacks first
    if await _handle_question_callback(query, data, config):
        return

    if not _is_authorized(query.from_user and query.from_user.id, config):
        await query.answer("Unauthorized.")
        return

    # Handle "Show prompt" expansion for Agent tool
    if data.startswith("show_prompt:"):
        tool_use_id = data[len("show_prompt:"):]
        tool_input = _pending_agent_inputs.get(tool_use_id)
        if not tool_input:
            await query.answer("Prompt data no longer available.")
            return

        await query.answer()

        # Re-render the message with expanded prompt, remove "Show prompt" button
        if query.message:
            expanded_text = _format_agent_approval(tool_input, expanded=True)
            approve_data = f"approve:{tool_use_id}"
            deny_data = f"deny:{tool_use_id}"
            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("Approve", callback_data=approve_data),
                        InlineKeyboardButton("Deny", callback_data=deny_data),
                    ]
                ]
            )
            try:
                await query.message.edit_text(
                    text=expanded_text,
                    parse_mode="MarkdownV2",
                    reply_markup=keyboard,
                )
            except Exception:
                logger.exception("Failed to expand Agent prompt")
        return

    future = _approval_futures.get(data)
    if not future or future.done():
        await query.answer("This approval has expired.")
        return

    approved = data.startswith("approve:")
    future.set_result(approved)

    action = "Approved" if approved else "Denied"
    await query.answer(f"{action}.")

    # Update the message to show the decision (remove buttons, append status)
    if query.message:
        try:
            original_md = query.message.text_markdown_v2 or query.message.text or ""
            status = f"\n\n{'✅' if approved else '❌'} *{action}\\.*"
            await query.message.edit_text(
                text=original_md + status,
                parse_mode="MarkdownV2",
                reply_markup=None,
            )
        except Exception:
            # Fallback: just remove the keyboard without modifying text
            try:
                await query.message.edit_reply_markup(reply_markup=None)
            except Exception:
                logger.exception("Failed to edit approval message")


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

    # Callback query handler for tool approval buttons
    app.add_handler(CallbackQueryHandler(callback_query_handler))

    # Message handler (text, photos, and locations, non-command)
    app.add_handler(MessageHandler(
        (filters.TEXT | filters.PHOTO | filters.LOCATION) & ~filters.COMMAND, message_handler
    ))

    return app


async def run_bot(config: Config, db: aiosqlite.Connection) -> None:
    """Start the bot with long polling."""
    app = build_application(config, db)
    logger.info("Starting bot with long polling")
    await app.initialize()
    await app.bot.set_my_commands([
        BotCommand("context", "List or switch contexts"),
        BotCommand("clear", "Start a fresh session"),
        BotCommand("status", "Show current context, session, and state"),
        BotCommand("cancel", "Abort running Claude invocation"),
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
        await app.updater.stop()
        await app.stop()
        await app.shutdown()
