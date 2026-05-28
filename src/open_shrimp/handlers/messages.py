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

from open_shrimp.agent import (
    FileAttachment,
    build_prompt_with_attachments,
    cleanup_attachments,
    save_attachments,
)
from open_shrimp.stt import transcribe as stt_transcribe
from claude_agent_sdk import CLIConnectionError, ProcessError
from open_shrimp.client_manager import (
    CallbackContext,
    close_session,
    get_or_create_session,
    receive_events,
    reconnect_session,
)
from open_shrimp.config import Config, ContextConfig, is_sandboxed
from open_shrimp.db import ChatScope, get_pinned_message_id, get_session_id, set_session_id
from open_shrimp.handlers.approval import (
    _send_approval_keyboard,
    _send_auto_approved_diff,
    _send_host_bash_approval,
)
from open_shrimp.hooks import matches_approval_rule as _matches_rule
from open_shrimp.handlers.questions import (
    _complete_other_input,
    _handle_ask_user_questions,
)
from open_shrimp.handlers.state import (
    _edit_approved_sessions,
    _injectable_sessions,
    _injected_attachment_paths,
    _media_group_messages,
    _media_group_tasks,
    _MEDIA_GROUP_WAIT,
    _scope_dispatch_locks,
    _session_approved_dirs,
    _tool_approved_sessions,
    _pending_other_input,
    _question_states,
    _running_tasks,
    _setup_queues,
)
from open_shrimp.handlers.utils import (
    _cancel_running,
    _get_context,
    _is_authorized,
    _is_bot_addressed,
    _strip_mention,
    _thread_kwargs,
    _update_pinned_status,
    chat_scope_from_message,
)
from open_shrimp.stream import (
    _DraftState,
    finalize_and_reset,
    stream_response,
)

logger = logging.getLogger(__name__)


def _select_sandbox_manager(
    bot_data: dict[str, Any],
    ctx_config: ContextConfig,
) -> "SandboxManager | None":
    """Pick the right SandboxManager for a context's backend."""
    from open_shrimp.sandbox import SandboxManager

    managers: dict[str, SandboxManager] | None = bot_data.get("sandbox_managers")
    if managers and ctx_config.sandbox:
        return managers.get(ctx_config.sandbox.backend)
    return None


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


async def _download_telegram_voice(
    message: Any, bot: Bot
) -> bytes | None:
    """Download a voice note or video note from a Telegram message.

    Returns the raw audio bytes, or None if the message has no voice/video note.
    """
    voice = message.voice or message.video_note
    if not voice:
        return None
    file = await bot.get_file(voice.file_id)
    return bytes(await file.download_as_bytearray())


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

    # Must have text, caption, photo, document, location, or voice
    has_text = bool(message.text)
    has_photo = bool(message.photo)
    has_document = bool(message.document)
    has_caption = bool(message.caption)
    has_location = bool(message.location)
    has_voice = bool(message.voice or message.video_note)

    logger.info(
        "message_handler: chat=%s has_text=%s has_photo=%s has_document=%s has_caption=%s has_location=%s has_voice=%s media_group_id=%s",
        message.chat_id, has_text, has_photo, has_document, has_caption, has_location, has_voice, message.media_group_id,
    )

    if not has_text and not has_photo and not has_document and not has_location and not has_voice:
        logger.info("message_handler: no text, photo, document, location, or voice, ignoring")
        return

    if not _is_authorized(update.effective_user and update.effective_user.id, config):
        logger.info("message_handler: unauthorized user %s", update.effective_user)
        return

    scope = chat_scope_from_message(message)

    # Check if this is a text response to an "Other..." question prompt.
    # If there's a pending "Other" input for this scope, resolve it inline
    # and don't dispatch to the agent.
    if has_text and scope in _pending_other_input:
        question_id = _pending_other_input.pop(scope, None)
        if question_id:
            state = _question_states.get(question_id)
            if state and not state.future.done():
                custom_text = message.text or ""
                state.waiting_for_other = False
                await _complete_other_input(context.bot, state, custom_text)
                logger.info("Resolved pending 'Other' input for scope %s", scope)
                return

    bot_username = (await context.bot.get_me()).username or ""
    if not _is_bot_addressed(update, bot_username):
        logger.info("message_handler: bot not addressed, ignoring")
        return

    # If this message is part of a media group (album), batch it.
    if message.media_group_id and (has_photo or has_document):
        await _handle_media_group_message(update, context, message)
        return

    # Transcribe voice notes to text.
    if has_voice:
        try:
            voice_data = await _download_telegram_voice(message, context.bot)
            if voice_data:
                transcription = await stt_transcribe(voice_data)
                if transcription:
                    logger.info(
                        "Voice transcription for scope %s: %s",
                        scope, transcription[:100],
                    )
                    prompt = f"[Transcribed from voice note] {transcription}"
                    await _dispatch_to_agent(
                        prompt, [], scope, config, db, context,
                        user_id=update.effective_user.id,
                        is_private_chat=update.effective_chat.type == "private" if update.effective_chat else True,
                    )
                    return
                else:
                    logger.warning("Empty transcription for voice note in scope %s", scope)
        except Exception:
            logger.exception("Voice transcription failed for scope %s", scope)
            try:
                await context.bot.send_message(
                    chat_id=scope.chat_id,
                    text="Failed to transcribe voice note\\. Is moonshine\\-stt installed?",
                    parse_mode="MarkdownV2",
                    **_thread_kwargs(scope),
                )
            except Exception:
                pass
        return

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
        logger.info("Location shared in scope %s: %s, %s", scope, loc.latitude, loc.longitude)

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
            "Downloaded %d attachment(s) for scope %s (%d bytes)",
            len(attachments), scope, sum(len(att.data) for att in attachments),
        )

    await _dispatch_to_agent(
        prompt, attachments, scope, config, db, context,
        user_id=update.effective_user.id if update.effective_user else 0,
        is_private_chat=update.effective_chat.type == "private" if update.effective_chat else True,
    )


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
        scope = chat_scope_from_message(message)
        prompt = (
            "Please commit the currently staged changes. "
            "Generate an appropriate commit message based on the staged diff."
        )
        await _dispatch_to_agent(
            prompt, [], scope, config, db, context,
            user_id=update.effective_user.id if update.effective_user else 0,
            is_private_chat=message.chat.type == "private" if message.chat else True,
        )
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
        scope = chat_scope_from_message(messages[0])
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
            "Media group %s complete: %d attachment(s) for scope %s (%d bytes)",
            group_id, len(attachments), scope, sum(len(att.data) for att in attachments),
        )

        await _dispatch_to_agent(
            prompt, attachments, scope, config, db, context,
            user_id=update.effective_user.id if update.effective_user else 0,
            is_private_chat=update.effective_chat.type == "private" if update.effective_chat else True,
        )

    _media_group_tasks[group_id] = asyncio.create_task(_process_group())


# ---------------------------------------------------------------------------
# Agent dispatch
# ---------------------------------------------------------------------------


async def _dispatch_to_agent(
    prompt: str,
    attachments: list[FileAttachment],
    scope: ChatScope,
    config: Config,
    db: aiosqlite.Connection,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    placeholder: str | None = None,
    user_id: int = 0,
    is_private_chat: bool = True,
) -> None:
    """Dispatch a message to the agent.

    If no task is running, start one.  If a task *is* running and the
    session is already live, inject the message directly via
    ``session.client.query()`` so it is processed at the next tool-call
    boundary (matching Claude Code's behavior).  If the session is still
    being set up, queue the message for injection once the session is
    ready.

    If *placeholder* is given and a new task must be started (cold
    start), a short feedback message is sent to the chat immediately
    so the user doesn't see silence while the session initialises.
    """
    # Serialise per scope so two messages arriving together cannot both
    # slip through the "no task running" check before either has set
    # _running_tasks[scope].
    lock = _scope_dispatch_locks.setdefault(scope, asyncio.Lock())
    async with lock:
        # No task running -- start a new one.
        if scope not in _running_tasks or _running_tasks[scope].done():
            if placeholder:
                try:
                    await context.bot.send_message(
                        chat_id=scope.chat_id,
                        text=placeholder,
                        parse_mode="MarkdownV2",
                        **_thread_kwargs(scope),
                    )
                except Exception:
                    logger.debug("Failed to send placeholder for scope %s", scope)
            await _start_agent_task(
                prompt, attachments, scope, config, db, context,
                user_id=user_id, is_private_chat=is_private_chat,
            )
            return

        # Task is running -- try to inject into the live session.
        session = _injectable_sessions.get(scope)
        if session is not None:
            try:
                await _inject_message(
                    session, prompt, attachments, scope, context.bot, config,
                )
            except _DeadTransport:
                # Transport is dead — the existing task is stuck in
                # receive_response() against a closed subprocess. Tear it
                # down and start a fresh task; the SDK will resume the
                # same session_id from disk so conversation continuity is
                # preserved.
                await _tear_down_dead_task(scope)
                try:
                    await context.bot.send_message(
                        chat_id=scope.chat_id,
                        text="↻ Session dropped, resuming\\.\\.\\.",
                        parse_mode="MarkdownV2",
                        **_thread_kwargs(scope),
                    )
                except Exception:
                    logger.debug(
                        "Failed to send session-restart notice for %s", scope,
                    )
                await _start_agent_task(
                    prompt, attachments, scope, config, db, context,
                    user_id=user_id, is_private_chat=is_private_chat,
                )
        else:
            # Session is still being set up -- queue for injection once ready.
            if scope not in _setup_queues:
                _setup_queues[scope] = []
            _setup_queues[scope].append((prompt, attachments))
            logger.info(
                "Session not ready for scope %s, queued for injection (depth: %d)",
                scope, len(_setup_queues[scope]),
            )
            try:
                await context.bot.send_message(
                    chat_id=scope.chat_id,
                    text="\u23f3 Setting up session\\.\\.\\. message will be injected shortly\\.",
                    parse_mode="MarkdownV2",
                    **_thread_kwargs(scope),
                )
            except Exception:
                logger.debug("Failed to send setup-queue notification for scope %s", scope)


class _DeadTransport(Exception):
    """Raised by _inject_message when the SDK transport is no longer writable.

    Signals to the dispatcher that the existing agent task is dead and the
    message should be redelivered via a fresh session.
    """


async def _tear_down_dead_task(scope: ChatScope) -> None:
    """Cancel a stuck agent task whose subprocess transport is dead.

    After this returns, _start_agent_task can be called safely —
    get_or_create_session detects the dead client via _is_client_alive
    and creates a fresh one, resuming the same session_id.
    """
    # Evict from injectable map first so no other concurrent dispatcher
    # tries to inject into the dead session.
    _injectable_sessions.pop(scope, None)
    _setup_queues.pop(scope, None)
    await _cancel_running(scope)


async def _inject_message(
    session: Any,
    prompt: str,
    attachments: list[FileAttachment],
    scope: ChatScope,
    bot: Bot,
    config: Config | None = None,
) -> None:
    """Inject a user message into a live agent session.

    The message is sent via ``session.client.query()`` which writes to
    the CLI subprocess stdin.  The already-running ``receive_response()``
    iterator will pick up the resulting events naturally.

    Raises _DeadTransport if the CLI subprocess is no longer writable
    (sandbox exited, subprocess crashed). The caller is responsible for
    tearing down the stale task and restarting with a fresh session.
    """
    attachment_paths: list[Path] = []
    if attachments:
        attachment_paths = save_attachments(attachments, scope.chat_id)

        # For containerized contexts, copy into the container and use
        # container-side paths in the prompt.
        prompt_paths = attachment_paths
        if session.sandbox is not None:
            prompt_paths = await session.sandbox.copy_files_in(
                attachment_paths,
            )

        actual_prompt = build_prompt_with_attachments(prompt, prompt_paths)
    else:
        actual_prompt = prompt

    # Track attachment paths for cleanup in _run()'s finally block.
    if attachment_paths:
        _injected_attachment_paths.setdefault(scope, []).extend(attachment_paths)

    try:
        await session.client.query(actual_prompt)
        logger.info(
            "Injected message into live session for scope %s: %s",
            scope, actual_prompt[:100],
        )
    except (CLIConnectionError, BrokenPipeError) as exc:
        logger.warning(
            "Dead transport on inject for scope %s (%s); will restart session",
            scope, exc,
        )
        # Roll back the attachment-path tracking — the restarted task will
        # re-copy its own attachments from prompt/attachments.
        if attachment_paths:
            tracked = _injected_attachment_paths.get(scope)
            if tracked:
                for p in attachment_paths:
                    try:
                        tracked.remove(p)
                    except ValueError:
                        pass
        raise _DeadTransport() from exc
    except Exception:
        logger.exception("Failed to inject message for scope %s", scope)
        cleanup_attachments(attachment_paths)
        try:
            await bot.send_message(
                chat_id=scope.chat_id,
                text="Failed to inject message into the running session\\.",
                parse_mode="MarkdownV2",
                **_thread_kwargs(scope),
            )
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Agent task
# ---------------------------------------------------------------------------


async def _start_agent_task(
    prompt: str,
    attachments: list[FileAttachment],
    scope: ChatScope,
    config: Config,
    db: aiosqlite.Connection,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int = 0,
    is_private_chat: bool = True,
) -> None:
    """Start a new agent task for *scope*.  Must only be called when no
    task is currently running for this scope."""

    ctx_name, ctx_config = await _get_context(scope, config, db)
    session_id = await get_session_id(db, scope, ctx_name)

    # Ensure pinned status message exists (e.g. after a restart)
    if not await get_pinned_message_id(db, scope):
        await _update_pinned_status(context.bot, scope, ctx_name, ctx_config, db)

    async def _run() -> None:
        draft_state = _DraftState(
            chat_id=scope.chat_id, thread_id=scope.thread_id,
            user_id=user_id, is_private_chat=is_private_chat,
            bot_token=config.telegram.token,
        )
        is_containerized = is_sandboxed(ctx_config)

        attachment_paths: list[Path] = []
        if attachments:
            attachment_paths = save_attachments(attachments, scope.chat_id)

        # Collect all attachment paths (original + injected) for cleanup.
        all_attachment_paths: list[Path] = list(attachment_paths)

        try:
            if config.review.public_url:
                _base_url: str | None = config.review.public_url.rstrip("/")
            else:
                _base_url = f"https://{config.review.host}:{config.review.port}"

            async def request_approval(
                tool_name: str,
                tool_input: dict[str, Any],
                tool_use_id: str,
                suggested_session_dir: str | None = None,
            ) -> bool:
                await finalize_and_reset(context.bot, draft_state)
                return await _send_approval_keyboard(
                    context.bot, scope.chat_id, tool_name, tool_input, tool_use_id,
                    cwd=ctx_config.directory,
                    thread_id=scope.thread_id,
                    base_url=_base_url,
                    user_id=user_id,
                    is_private_chat=is_private_chat,
                    bot_token=config.telegram.token,
                    suggested_session_dir=suggested_session_dir,
                    scope=scope,
                    context_name=ctx_name,
                )

            async def handle_questions(
                questions: list[dict[str, Any]],
            ) -> dict[str, str]:
                return await _handle_ask_user_questions(
                    context.bot, scope, questions, draft_state
                )

            async def notify_edit(
                tool_name: str, tool_input: dict[str, Any]
            ) -> None:
                await finalize_and_reset(context.bot, draft_state)
                await _send_auto_approved_diff(
                    context.bot, scope.chat_id, tool_name, tool_input,
                    cwd=ctx_config.directory,
                    thread_id=scope.thread_id,
                )

            async def request_host_bash(
                tool_input: dict[str, Any], tool_use_id: str,
            ) -> Any:
                await finalize_and_reset(context.bot, draft_state)
                return await _send_host_bash_approval(
                    bot=context.bot,
                    chat_id=scope.chat_id,
                    context_name=ctx_name,
                    tool_input=tool_input,
                    tool_use_id=tool_use_id,
                    thread_id=scope.thread_id,
                )

            # Mutable container for the latest todo list from TodoWrite.
            # Preserved across stream_response iterations so the pinned
            # message retains the task list when usage is updated.
            latest_todos: list[dict[str, Any]] = []

            async def on_todo_update(todos: list[dict[str, Any]]) -> None:
                latest_todos.clear()
                latest_todos.extend(todos)
                await _update_pinned_status(
                    context.bot, scope, ctx_name, ctx_config, db,
                    todos=todos if todos else None,
                )

            # Load persistent approval rules from .claude/settings.local.json
            # into the in-memory cache so they are checked alongside
            # session-scoped rules.
            from open_shrimp.settings_local import load_persistent_rules

            persistent_rules = await load_persistent_rules(ctx_config.directory)
            if persistent_rules:
                existing = _tool_approved_sessions.setdefault((scope, ctx_name), [])
                # Avoid duplicates if rules were already loaded.
                existing_set = {(r.tool_name, r.pattern) for r in existing}
                for rule in persistent_rules:
                    if (rule.tool_name, rule.pattern) not in existing_set:
                        existing.append(rule)

            cb_ctx = CallbackContext(
                request_approval=request_approval,
                handle_user_questions=handle_questions,
                is_edit_auto_approved=lambda: (scope, ctx_name) in _edit_approved_sessions,
                notify_auto_approved_edit=notify_edit,
                is_tool_auto_approved=lambda tn, ti: any(
                    _matches_rule(rule, tn, ti)
                    for rule in _tool_approved_sessions.get((scope, ctx_name), [])
                ),
                get_session_approved_dirs=lambda: list(
                    _session_approved_dirs.get((scope, ctx_name), set())
                ),
                request_host_bash_approval=request_host_bash,
            )

            session = await get_or_create_session(
                scope=scope,
                context_name=ctx_name,
                context=ctx_config,
                session_id=session_id,
                callback_context=cb_ctx,
                bot=context.bot,
                db=db,
                config=config,
                job_queue=getattr(context, "job_queue", None),
                terminal_base_url=_base_url,
                user_id=user_id,
                is_private_chat=is_private_chat,
                sandbox_manager=_select_sandbox_manager(context.bot_data, ctx_config),
                mcp_proxy=context.bot_data.get("mcp_proxy"),
            )

            # Copy attachments into sandbox (if applicable) and build prompt.
            if attachment_paths:
                prompt_paths = attachment_paths
                if session.sandbox is not None:
                    prompt_paths = await session.sandbox.copy_files_in(
                        attachment_paths,
                    )
                actual_prompt = build_prompt_with_attachments(
                    prompt, prompt_paths,
                )
            else:
                actual_prompt = prompt

            # Send the primary query.
            await session.client.query(actual_prompt)

            # Mark session as injectable so concurrent messages are
            # injected via client.query() instead of queued.
            _injectable_sessions[scope] = session

            # Drain any messages that arrived during the setup phase.
            setup_queue = _setup_queues.pop(scope, [])
            for queued_prompt, queued_attachments in setup_queue:
                queued_paths: list[Path] = []
                if queued_attachments:
                    queued_paths = save_attachments(
                        queued_attachments, scope.chat_id,
                    )
                    prompt_paths = queued_paths
                    if session.sandbox is not None:
                        prompt_paths = await session.sandbox.copy_files_in(
                            queued_paths,
                        )
                    queued_actual = build_prompt_with_attachments(
                        queued_prompt, prompt_paths,
                    )
                else:
                    queued_actual = queued_prompt
                all_attachment_paths.extend(queued_paths)
                try:
                    await session.client.query(queued_actual)
                    logger.info(
                        "Injected setup-queued message for scope %s: %s",
                        scope, queued_actual[:100],
                    )
                except Exception:
                    logger.exception(
                        "Failed to inject setup-queued message for scope %s",
                        scope,
                    )

            container_retries = 0
            max_container_retries = 2

            while True:
                try:
                    events = receive_events(session)
                    # Build terminal base URL for background task
                    # "View output" buttons.
                    if config.review.public_url:
                        terminal_url: str | None = config.review.public_url.rstrip("/")
                    else:
                        terminal_url = f"https://{config.review.host}:{config.review.port}"

                    result = await stream_response(
                        bot=context.bot,
                        chat_id=scope.chat_id,
                        events=events,
                        draft_state=draft_state,
                        allowed_tools=ctx_config.allowed_tools,
                        cwd=ctx_config.directory,
                        on_todo_update=on_todo_update,
                        terminal_base_url=terminal_url,
                        scope=scope,
                    )

                    if result.session_id:
                        await set_session_id(db, scope, ctx_name, result.session_id)

                    if result.model_usage or result.turn_usage:
                        await _update_pinned_status(
                            context.bot, scope, ctx_name, ctx_config, db,
                            model_usage=result.model_usage,
                            turn_usage=result.turn_usage,
                            todos=latest_todos if latest_todos else None,
                        )

                    if result.num_steps == 0 and result.session_id is None:
                        break

                    # Reset retry counter on successful iteration.
                    container_retries = 0

                except (ProcessError, CLIConnectionError):
                    if not is_containerized or container_retries >= max_container_retries:
                        raise
                    container_retries += 1
                    logger.warning(
                        "Sandbox crash detected for scope %s "
                        "(attempt %d/%d), reconnecting...",
                        scope, container_retries, max_container_retries,
                    )
                    await finalize_and_reset(context.bot, draft_state)
                    new_session = await reconnect_session(
                        scope=scope,
                        context_name=ctx_name,
                        context=ctx_config,
                        bot=context.bot,
                        db=db,
                        config=config,
                        job_queue=getattr(context, "job_queue", None),
                        terminal_base_url=_base_url,
                        user_id=user_id,
                        is_private_chat=is_private_chat,
                        sandbox_manager=_select_sandbox_manager(context.bot_data, ctx_config),
                        mcp_proxy=context.bot_data.get("mcp_proxy"),
                    )
                    if new_session is None:
                        raise
                    session = new_session
                    _injectable_sessions[scope] = session
                    try:
                        await context.bot.send_message(
                            chat_id=scope.chat_id,
                            text="Container restarted, resuming session\\.\\.\\.",
                            parse_mode="MarkdownV2",
                            **_thread_kwargs(scope),
                        )
                    except Exception:
                        logger.debug("Failed to send reconnect notice")

        except asyncio.CancelledError:
            logger.info("Agent task cancelled for scope %s", scope)
        except (CLIConnectionError, ProcessError) as exc:
            logger.exception("Agent task failed for scope %s", scope)
            # Close the dead session so the next message starts fresh
            # instead of reusing a broken client.
            try:
                await close_session(scope)
            except Exception:
                logger.debug("Failed to close dead session for scope %s", scope)
            try:
                if is_sandboxed(ctx_config):
                    error_text = (
                        "The sandbox process terminated unexpectedly "
                        "\\(possibly due to a VM shutdown\\)\\. "
                        "Send a new message to restart the session\\."
                    )
                else:
                    error_text = (
                        "The Claude process terminated unexpectedly\\. "
                        "Send a new message to restart the session\\."
                    )
                await context.bot.send_message(
                    chat_id=scope.chat_id,
                    text=error_text,
                    parse_mode="MarkdownV2",
                    **_thread_kwargs(scope),
                )
            except Exception:
                logger.exception("Failed to send error message")
        except Exception:
            logger.exception("Agent task failed for scope %s", scope)
            try:
                await context.bot.send_message(
                    chat_id=scope.chat_id,
                    text="An error occurred while processing your request\\.",
                    parse_mode="MarkdownV2",
                    **_thread_kwargs(scope),
                )
            except Exception:
                logger.exception("Failed to send error message")
        finally:
            # Collect injected attachment paths and clean up everything.
            all_attachment_paths.extend(
                _injected_attachment_paths.pop(scope, [])
            )
            cleanup_attachments(all_attachment_paths)
            _injectable_sessions.pop(scope, None)
            _setup_queues.pop(scope, None)
            if draft_state.session_id:
                try:
                    await set_session_id(db, scope, ctx_name, draft_state.session_id)
                except Exception:
                    logger.debug(
                        "Failed to save session on cleanup for scope %s", scope
                    )
            _running_tasks.pop(scope, None)

    task = asyncio.create_task(_run())
    _running_tasks[scope] = task
