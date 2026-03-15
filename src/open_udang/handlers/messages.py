"""Message handling and agent dispatch."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import aiosqlite
from telegram import Bot, Update
from telegram.ext import ContextTypes

from open_udang.agent import FileAttachment, cleanup_attachments, prepare_prompt
from open_udang.client_manager import (
    CallbackContext,
    get_or_create_session,
    receive_events,
)
from open_udang.config import Config
from open_udang.db import get_pinned_message_id, get_session_id, set_session_id
from open_udang.handlers.approval import _send_approval_keyboard, _send_auto_approved_diff
from open_udang.handlers.questions import (
    _complete_other_input,
    _handle_ask_user_questions,
)
from open_udang.handlers.state import (
    _edit_approved_sessions,
    _injectable_sessions,
    _injected_attachment_paths,
    _media_group_messages,
    _media_group_tasks,
    _MEDIA_GROUP_WAIT,
    _pending_other_input,
    _question_states,
    _running_tasks,
    _setup_queues,
)
from open_udang.handlers.utils import (
    _get_context,
    _is_authorized,
    _is_bot_addressed,
    _strip_mention,
    _update_pinned_status,
)
from open_udang.stream import (
    _DraftState,
    finalize_and_reset,
    stream_response,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Attachment download helpers
# ---------------------------------------------------------------------------


async def _download_telegram_photos(
    messages: list[Any], bot: Bot
) -> list[FileAttachment]:
    """Download photos from one or more Telegram messages and return as FileAttachments.

    Each message may contain one photo (represented as a list of PhotoSize
    objects at different resolutions).  We take the largest resolution from
    each message.
    """
    attachments: list[FileAttachment] = []
    for message in messages:
        if not message.photo:
            continue
        # message.photo is a list of PhotoSize objects sorted by size.
        # Take the largest one (last element) for best quality.
        photo = message.photo[-1]
        file = await bot.get_file(photo.file_id)
        photo_bytes = bytes(await file.download_as_bytearray())
        attachments.append(FileAttachment(data=photo_bytes, mime_type="image/jpeg"))
    return attachments


async def _download_telegram_documents(
    messages: list[Any], bot: Bot
) -> list[FileAttachment]:
    """Download documents from one or more Telegram messages and return as FileAttachments.

    Telegram documents include PDFs, text files, and other non-photo file
    uploads.  Each message may have at most one document.
    """
    attachments: list[FileAttachment] = []
    for message in messages:
        if not message.document:
            continue
        doc = message.document
        file = await bot.get_file(doc.file_id)
        doc_bytes = bytes(await file.download_as_bytearray())
        mime_type = doc.mime_type or "application/octet-stream"
        filename = doc.file_name
        attachments.append(FileAttachment(data=doc_bytes, mime_type=mime_type, filename=filename))
    return attachments


async def _download_all_attachments(
    messages: list[Any], bot: Bot
) -> list[FileAttachment]:
    """Download all photos and documents from messages concurrently."""
    photos, docs = await asyncio.gather(
        _download_telegram_photos(messages, bot),
        _download_telegram_documents(messages, bot),
    )
    return photos + docs


# ---------------------------------------------------------------------------
# Message handler
# ---------------------------------------------------------------------------


async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle incoming text, photo, and document messages: route to Claude agent.

    For media groups (albums with multiple photos), messages are batched
    using a short delay so all photos are collected before processing.
    """
    config: Config = context.bot_data["config"]
    db: aiosqlite.Connection = context.bot_data["db"]
    message = update.effective_message
    if not message:
        return

    # Must have text, caption, photo, document, or location
    has_text = bool(message.text)
    has_photo = bool(message.photo)
    has_document = bool(message.document)
    has_caption = bool(message.caption)
    has_location = bool(message.location)

    logger.info(
        "message_handler: chat=%s has_text=%s has_photo=%s has_document=%s has_caption=%s has_location=%s media_group_id=%s",
        message.chat_id, has_text, has_photo, has_document, has_caption, has_location, message.media_group_id,
    )

    if not has_text and not has_photo and not has_document and not has_location:
        logger.info("message_handler: no text, photo, document, or location, ignoring")
        return

    if not _is_authorized(update.effective_user and update.effective_user.id, config):
        logger.info("message_handler: unauthorized user %s", update.effective_user)
        return

    # Check if this is a text response to an "Other..." question prompt.
    # If there's a pending "Other" input for this chat, resolve it inline
    # and don't dispatch to the agent.
    chat_id_for_input = message.chat_id
    if has_text and chat_id_for_input in _pending_other_input:
        question_id = _pending_other_input.pop(chat_id_for_input, None)
        if question_id:
            state = _question_states.get(question_id)
            if state and not state.future.done():
                custom_text = message.text or ""
                state.waiting_for_other = False
                await _complete_other_input(context.bot, state, custom_text)
                logger.info("Resolved pending 'Other' input for chat %d", chat_id_for_input)
                return

    bot_username = (await context.bot.get_me()).username or ""
    if not _is_bot_addressed(update, bot_username):
        logger.info("message_handler: bot not addressed, ignoring")
        return

    # If this message is part of a media group (album), batch it.
    if message.media_group_id and (has_photo or has_document):
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
            location_text += f" (heading: {loc.heading}\u00b0)"
        # Prepend location to any existing prompt text
        prompt = f"{location_text}\n\n{prompt}" if prompt else location_text
        logger.info("Location shared in chat %d: %s, %s", chat_id, loc.latitude, loc.longitude)

    # For photos without a caption, use a default prompt
    if not prompt and has_photo:
        prompt = "What's in this image?"
    elif not prompt and has_document:
        prompt = "What's in this file?"
    elif not prompt:
        return

    # Download photo and document attachments if present
    attachments: list[FileAttachment] = []
    if has_photo or has_document:
        attachments = await _download_all_attachments([message], context.bot)
        logger.info(
            "Downloaded %d attachment(s) for chat %d (%d bytes)",
            len(attachments), chat_id, sum(len(att.data) for att in attachments),
        )

    await _dispatch_to_agent(prompt, attachments, chat_id, config, db, context)


async def web_app_data_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle data sent from a Telegram Mini App (e.g. commit from review app).

    The review Mini App uses ``WebApp.sendData()`` to send a JSON payload
    back to the bot.  Telegram delivers this as a message with
    ``web_app_data`` set.
    """
    config: Config = context.bot_data["config"]
    db: aiosqlite.Connection = context.bot_data["db"]
    message = update.effective_message
    if not message or not message.web_app_data:
        return

    user_id = update.effective_user.id if update.effective_user else None
    if not _is_authorized(user_id, config):
        logger.info("web_app_data_handler: unauthorized user %s", update.effective_user)
        return

    try:
        payload = json.loads(message.web_app_data.data)
    except (json.JSONDecodeError, TypeError):
        logger.warning("Invalid web_app_data JSON: %s", message.web_app_data.data)
        return

    action = payload.get("action")
    if action == "commit":
        chat_id = message.chat_id
        prompt = (
            "Please commit the currently staged changes. "
            "Generate an appropriate commit message based on the staged diff."
        )
        await _dispatch_to_agent(prompt, [], chat_id, config, db, context)
    else:
        logger.warning("Unknown web_app_data action: %s", action)


# ---------------------------------------------------------------------------
# Media group batching
# ---------------------------------------------------------------------------


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
            has_any_photo = any(msg.photo for msg in messages)
            has_any_doc = any(msg.document for msg in messages)
            if has_any_photo and not has_any_doc:
                count = len(messages)
                prompt = f"What's in {'this image' if count == 1 else 'these images'}?"
            elif has_any_doc and not has_any_photo:
                count = sum(1 for msg in messages if msg.document)
                prompt = f"What's in {'this file' if count == 1 else 'these files'}?"
            else:
                prompt = "What's in these files?"

        attachments = await _download_all_attachments(messages, context.bot)
        logger.info(
            "Media group %s complete: %d attachment(s) for chat %d (%d bytes)",
            group_id, len(attachments), chat_id, sum(len(att.data) for att in attachments),
        )

        await _dispatch_to_agent(prompt, attachments, chat_id, config, db, context)

    _media_group_tasks[group_id] = asyncio.create_task(_process_group())


# ---------------------------------------------------------------------------
# Agent dispatch
# ---------------------------------------------------------------------------


async def _dispatch_to_agent(
    prompt: str,
    attachments: list[FileAttachment],
    chat_id: int,
    config: Config,
    db: aiosqlite.Connection,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Dispatch a message to the agent.

    If no task is running, start one.  If a task *is* running and the
    session is already live, inject the message directly via
    ``session.client.query()`` so it is processed at the next tool-call
    boundary (matching Claude Code's behavior).  If the session is still
    being set up, queue the message for injection once the session is
    ready.
    """
    # No task running -- start a new one.
    if chat_id not in _running_tasks or _running_tasks[chat_id].done():
        await _start_agent_task(prompt, attachments, chat_id, config, db, context)
        return

    # Task is running -- try to inject into the live session.
    session = _injectable_sessions.get(chat_id)
    if session is not None:
        await _inject_message(session, prompt, attachments, chat_id, context.bot)
    else:
        # Session is still being set up -- queue for injection once ready.
        if chat_id not in _setup_queues:
            _setup_queues[chat_id] = []
        _setup_queues[chat_id].append((prompt, attachments))
        logger.info(
            "Session not ready for chat %d, queued for injection (depth: %d)",
            chat_id, len(_setup_queues[chat_id]),
        )
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text="\u23f3 Setting up session\\.\\.\\. message will be injected shortly\\.",
                parse_mode="MarkdownV2",
            )
        except Exception:
            logger.debug("Failed to send setup-queue notification for chat %d", chat_id)


async def _inject_message(
    session: Any,
    prompt: str,
    attachments: list[FileAttachment],
    chat_id: int,
    bot: Bot,
) -> None:
    """Inject a user message into a live agent session.

    The message is sent via ``session.client.query()`` which writes to
    the CLI subprocess stdin.  The already-running ``receive_response()``
    iterator will pick up the resulting events naturally.
    """
    actual_prompt, attachment_paths = prepare_prompt(
        prompt, attachments if attachments else None, chat_id=chat_id,
    )

    # Track attachment paths for cleanup in _run()'s finally block.
    if attachment_paths:
        _injected_attachment_paths.setdefault(chat_id, []).extend(attachment_paths)

    try:
        await session.client.query(actual_prompt)
        logger.info(
            "Injected message into live session for chat %d: %s",
            chat_id, actual_prompt[:100],
        )
    except Exception:
        logger.exception("Failed to inject message for chat %d", chat_id)
        cleanup_attachments(attachment_paths)
        try:
            await bot.send_message(
                chat_id=chat_id,
                text="Failed to inject message into the running session\\.",
                parse_mode="MarkdownV2",
            )
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Agent task
# ---------------------------------------------------------------------------


async def _start_agent_task(
    prompt: str,
    attachments: list[FileAttachment],
    chat_id: int,
    config: Config,
    db: aiosqlite.Connection,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Start a new agent task for *chat_id*.  Must only be called when no
    task is currently running for this chat."""

    ctx_name, ctx_config = await _get_context(chat_id, config, db)
    session_id = await get_session_id(db, chat_id, ctx_name)

    # Ensure pinned status message exists (e.g. after a restart)
    if not await get_pinned_message_id(db, chat_id):
        await _update_pinned_status(context.bot, chat_id, ctx_name, ctx_config, db)

    async def _run() -> None:
        draft_state = _DraftState(chat_id=chat_id)
        actual_prompt, attachment_paths = prepare_prompt(
            prompt, attachments if attachments else None, chat_id=chat_id,
        )
        # Collect all attachment paths (original + injected) for cleanup.
        all_attachment_paths: list[Path] = list(attachment_paths)

        try:
            async def request_approval(
                tool_name: str, tool_input: dict[str, Any], tool_use_id: str
            ) -> bool:
                await finalize_and_reset(context.bot, draft_state)
                return await _send_approval_keyboard(
                    context.bot, chat_id, tool_name, tool_input, tool_use_id,
                    cwd=ctx_config.directory,
                )

            async def handle_questions(
                questions: list[dict[str, Any]],
            ) -> dict[str, str]:
                return await _handle_ask_user_questions(
                    context.bot, chat_id, questions, draft_state
                )

            async def notify_edit(
                tool_name: str, tool_input: dict[str, Any]
            ) -> None:
                await finalize_and_reset(context.bot, draft_state)
                await _send_auto_approved_diff(
                    context.bot, chat_id, tool_name, tool_input,
                    cwd=ctx_config.directory,
                )

            # Mutable container for the latest todo list from TodoWrite.
            # Preserved across stream_response iterations so the pinned
            # message retains the task list when usage is updated.
            latest_todos: list[dict[str, Any]] = []

            async def on_todo_update(todos: list[dict[str, Any]]) -> None:
                latest_todos.clear()
                latest_todos.extend(todos)
                await _update_pinned_status(
                    context.bot, chat_id, ctx_name, ctx_config, db,
                    todos=todos if todos else None,
                )

            cb_ctx = CallbackContext(
                request_approval=request_approval,
                handle_user_questions=handle_questions,
                is_edit_auto_approved=lambda: (chat_id, ctx_name) in _edit_approved_sessions,
                notify_auto_approved_edit=notify_edit,
            )

            session = await get_or_create_session(
                chat_id=chat_id,
                context_name=ctx_name,
                context=ctx_config,
                session_id=session_id,
                callback_context=cb_ctx,
                bot=context.bot,
            )

            # Send the primary query.
            await session.client.query(actual_prompt)

            # Mark session as injectable so concurrent messages are
            # injected via client.query() instead of queued.
            _injectable_sessions[chat_id] = session

            # Drain any messages that arrived during the setup phase.
            setup_queue = _setup_queues.pop(chat_id, [])
            for queued_prompt, queued_attachments in setup_queue:
                queued_actual, queued_paths = prepare_prompt(
                    queued_prompt, queued_attachments if queued_attachments else None,
                    chat_id=chat_id,
                )
                all_attachment_paths.extend(queued_paths)
                try:
                    await session.client.query(queued_actual)
                    logger.info(
                        "Injected setup-queued message for chat %d: %s",
                        chat_id, queued_actual[:100],
                    )
                except Exception:
                    logger.exception(
                        "Failed to inject setup-queued message for chat %d",
                        chat_id,
                    )

            while True:
                events = receive_events(session)
                result = await stream_response(
                    bot=context.bot,
                    chat_id=chat_id,
                    events=events,
                    draft_state=draft_state,
                    allowed_tools=ctx_config.allowed_tools,
                    cwd=ctx_config.directory,
                    on_todo_update=on_todo_update,
                )

                if result.session_id:
                    await set_session_id(db, chat_id, ctx_name, result.session_id)

                if result.usage:
                    await _update_pinned_status(
                        context.bot, chat_id, ctx_name, ctx_config, db,
                        usage=result.usage,
                        total_cost_usd=result.total_cost_usd,
                        todos=latest_todos if latest_todos else None,
                    )

                if result.num_turns == 0 and result.session_id is None:
                    break

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
            # Collect injected attachment paths and clean up everything.
            all_attachment_paths.extend(
                _injected_attachment_paths.pop(chat_id, [])
            )
            cleanup_attachments(all_attachment_paths)
            _injectable_sessions.pop(chat_id, None)
            _setup_queues.pop(chat_id, None)
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
