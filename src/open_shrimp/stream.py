"""Stream bridge between Agent SDK events and Telegram sendMessageDraft.

Consumes streaming events from agent.py, buffers text, and sends drafts
to Telegram at appropriate intervals. Handles message length limits,
tool call notifications, and final message delivery.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from open_shrimp.opencode_client import (
    AssistantMessage,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest

from open_shrimp.agent import AgentEvent
from open_shrimp.db import ChatScope
from open_shrimp.markdown import gfm_to_telegram
from open_shrimp.web_app_button import make_web_app_button

logger = logging.getLogger(__name__)


def _is_thread_not_found(exc: BaseException) -> bool:
    """Whether ``exc`` is Telegram's 'message thread not found' error.

    The scheduler relies on this propagating out so it can disable the
    task and stop re-firing into a deleted forum topic.
    """
    return (
        isinstance(exc, BadRequest)
        and "message thread not found" in str(exc).lower()
    )

TELEGRAM_MAX_LENGTH = 4096
DRAFT_INTERVAL_SECONDS = 0.5
# Maximum lines of Bash output to display.
BASH_OUTPUT_MAX_LINES = 50
# Maximum characters of Bash output to display.
BASH_OUTPUT_MAX_CHARS = 1500
# Tools whose blockquote notifications are suppressed because their output
# is shown directly (Bash output as code block, Write via edit notification).
_SUPPRESS_NOTIFICATION_TOOLS: set[str] = {"Bash", "Edit", "Write"}

# Stored Bash outputs for on-demand reveal via inline keyboard button.
# Keyed by a unique callback ID, value is the formatted GFM output string.
_bash_output_store: dict[str, str] = {}


@dataclass
class StreamResult:
    """Result from stream_response() with session and usage info."""

    session_id: str | None = None
    model_usage: dict[str, Any] | None = None
    #: Per-turn token usage from the last AssistantMessage. OpenCode-native
    #: shape: ``{input, output, reasoning, cache: {read, write}}``.
    turn_usage: dict[str, Any] | None = None
    num_steps: int = 0
    duration_ms: int = 0


@dataclass
class _DraftState:
    """Internal state for message drafting."""

    chat_id: int
    thread_id: int | None = None
    # Raw GFM text accumulated so far (before conversion).
    # Tool notifications are inlined as GFM blockquotes.
    raw_text: str = ""
    # Message IDs of finalized messages (for reference)
    sent_message_ids: list[int] = field(default_factory=list)
    # Draft ID for sendMessageDraft (non-zero integer, stable per draft)
    draft_id: int = field(default_factory=lambda: random.randint(1, 2**31 - 1))
    # Whether the draft needs to be flushed
    dirty: bool = False
    # Whether drafts are disabled (e.g. unsupported chat type)
    drafts_disabled: bool = False
    # Message ID of the current "live edit" message (fallback when drafts
    # are disabled — we send a real message and keep editing it).
    live_edit_message_id: int | None = None
    # Snapshot of raw_text that was last sent via editMessageText, so we
    # can skip no-op edits.
    live_edit_last_text: str = ""
    # Whether the last assistant turn has completed (AssistantMessage seen).
    # Used to insert a newline separator before text from the next turn.
    turn_complete: bool = False
    # Whether the last content appended to raw_text was a tool notification
    # blockquote ("> Tool: summary").  Used to insert a paragraph break
    # before the next assistant text so it doesn't get swallowed into the
    # notification blockquote.  We track this with a flag instead of
    # checking raw_text for ">" lines, because Claude's own response text
    # may also contain blockquotes that should NOT be broken.
    last_was_notification: bool = False
    # Session ID captured as early as possible (from SystemMessage init or
    # ResultMessage) so it survives task cancellation.
    session_id: str | None = None
    # Map tool_use_id -> (tool_name, tool_input) for correlating tool results
    # to invocations and displaying context (e.g. Bash command + output).
    tool_use_map: dict[str, tuple[str, dict[str, Any]]] = field(
        default_factory=dict
    )
    # Fields for web_app button fallback in group chats.
    user_id: int = 0
    is_private_chat: bool = True
    bot_token: str = ""

    @property
    def _thread_kwargs(self) -> dict[str, Any]:
        """Build message_thread_id kwargs for Telegram send methods."""
        if self.thread_id is not None:
            return {"message_thread_id": self.thread_id}
        return {}


def _build_full_text(state: _DraftState) -> str:
    """Build the full GFM text."""
    return state.raw_text


async def _send_draft(bot: Bot, state: _DraftState) -> None:
    """Send or update a draft message via sendMessageDraft.

    When drafts are disabled (unsupported chat type), falls back to
    sending a real message and editing it in-place for a streaming effect.
    """
    if state.drafts_disabled:
        await _send_live_edit(bot, state)
        return

    full_text = _build_full_text(state)
    if not full_text.strip():
        return

    # Convert to Telegram MarkdownV2
    chunks = gfm_to_telegram(full_text)
    if not chunks:
        return

    # Use only the first chunk for the current draft
    # (overflow is handled at finalization)
    text = chunks[0]

    try:
        await bot.do_api_request(
            "sendMessageDraft",
            api_kwargs={
                "chat_id": state.chat_id,
                "draft_id": state.draft_id,
                "text": text,
                "parse_mode": "MarkdownV2",
                **({"message_thread_id": state.thread_id} if state.thread_id is not None else {}),
            },
        )
        state.dirty = False
    except Exception as e:
        if _is_thread_not_found(e):
            raise
        error_msg = str(e).lower()
        if "draft_peer_invalid" in error_msg:
            # sendMessageDraft not supported for this chat type — disable drafts
            logger.info("Drafts not supported for chat %s, disabling", state.chat_id)
            state.drafts_disabled = True
            # Immediately try the live-edit fallback so the user doesn't
            # wait until the next periodic flush.
            await _send_live_edit(bot, state)
        else:
            logger.exception("Failed to send draft message")


async def _send_live_edit(bot: Bot, state: _DraftState) -> None:
    """Fallback streaming: send a message and keep editing it in-place.

    Used when sendMessageDraft is not supported (e.g. group chats).
    """
    full_text = _build_full_text(state)
    if not full_text.strip():
        return

    # Skip if nothing changed since last edit.
    if full_text == state.live_edit_last_text:
        return

    chunks = gfm_to_telegram(full_text)
    if not chunks:
        return

    # If the text overflows into multiple chunks, we need to finalize.
    if len(chunks) > 1:
        return

    text = chunks[0]

    if state.live_edit_message_id is None:
        # First flush — send a new message.
        try:
            msg = await bot.send_message(
                chat_id=state.chat_id,
                text=text,
                parse_mode="MarkdownV2",
                disable_notification=True,
                **state._thread_kwargs,
            )
            state.live_edit_message_id = msg.message_id
            state.live_edit_last_text = full_text
            state.dirty = False
        except Exception as e:
            if _is_thread_not_found(e):
                raise
            logger.exception("Failed to send live-edit message")
    else:
        # Update the existing message.
        try:
            await bot.edit_message_text(
                chat_id=state.chat_id,
                message_id=state.live_edit_message_id,
                text=text,
                parse_mode="MarkdownV2",
            )
            state.live_edit_last_text = full_text
            state.dirty = False
        except Exception as e:
            error_msg = str(e).lower()
            if "message is not modified" in error_msg:
                state.dirty = False
            else:
                logger.exception("Failed to edit live-edit message")


async def _finalize_message(
    bot: Bot, state: _DraftState, *, silent: bool = True,
) -> list[int]:
    """Finalize the draft by sending the full message.

    If a live-edit message exists, the first chunk is delivered by editing
    that message in-place (avoiding a duplicate), and any overflow chunks
    are sent as new messages.

    Args:
        silent: If True, send with ``disable_notification=True`` so the
            user's device doesn't buzz for intermediate messages.

    Returns list of sent message IDs.
    """
    full_text = _build_full_text(state)
    if not full_text.strip():
        return []

    chunks = gfm_to_telegram(full_text)
    if not chunks:
        return []

    notif_kwargs: dict[str, Any] = {}
    if silent:
        notif_kwargs["disable_notification"] = True

    message_ids: list[int] = []

    for i, chunk in enumerate(chunks):
        # Reuse the live-edit message for the first chunk.
        if i == 0 and state.live_edit_message_id is not None:
            try:
                await bot.edit_message_text(
                    chat_id=state.chat_id,
                    message_id=state.live_edit_message_id,
                    text=chunk,
                    parse_mode="MarkdownV2",
                )
            except Exception:
                logger.exception("Failed to finalize live-edit message")
                # Fallback: try without parse mode.
                try:
                    await bot.edit_message_text(
                        chat_id=state.chat_id,
                        message_id=state.live_edit_message_id,
                        text=chunk,
                    )
                except Exception:
                    logger.exception("Failed to finalize live-edit plaintext fallback")
            message_ids.append(state.live_edit_message_id)
            state.live_edit_message_id = None
            state.live_edit_last_text = ""
            continue

        try:
            msg = await bot.send_message(
                chat_id=state.chat_id,
                text=chunk,
                parse_mode="MarkdownV2",
                **state._thread_kwargs,
                **notif_kwargs,
            )
            message_ids.append(msg.message_id)
        except Exception as e:
            if _is_thread_not_found(e):
                raise
            logger.exception("Failed to send finalized message chunk")
            # Retry without MarkdownV2 as fallback
            try:
                msg = await bot.send_message(
                    chat_id=state.chat_id,
                    text=chunk,
                    **state._thread_kwargs,
                    **notif_kwargs,
                )
                message_ids.append(msg.message_id)
            except Exception as e2:
                if _is_thread_not_found(e2):
                    raise
                logger.exception("Failed to send plaintext fallback")

    return message_ids


def _relative_path(path: str, cwd: str | None) -> str:
    """Return *path* relative to *cwd* when it lives under that directory.

    If *cwd* is ``None`` or *path* is outside *cwd*, the original absolute
    path is returned unchanged.
    """
    if not cwd or not path:
        return path
    try:
        rel = os.path.relpath(path, cwd)
    except ValueError:
        # On Windows, relpath raises ValueError for paths on different drives.
        return path
    # Only use the relative form when the path is actually inside cwd
    # (i.e. doesn't start with "..").
    if rel.startswith(".."):
        return path
    return rel


def extract_tool_summary(
    tool_name: str, tool_input: dict[str, Any], cwd: str | None = None,
) -> str:
    """Extract a brief summary from tool input for notifications."""
    if tool_name == "Read":
        return _relative_path(tool_input.get("filePath", ""), cwd)
    if tool_name == "Glob":
        return tool_input.get("pattern", "")
    if tool_name == "Grep":
        pattern = tool_input.get("pattern", "")
        path = tool_input.get("path", "")
        if path:
            return f"{pattern} in {_relative_path(path, cwd)}"
        return pattern
    if tool_name == "Bash":
        cmd = tool_input.get("command", "")
        return cmd[:80] + ("..." if len(cmd) > 80 else "")
    if tool_name == "Write" or tool_name == "Edit":
        return _relative_path(tool_input.get("filePath", ""), cwd)
    if tool_name == "LSP":
        return tool_input.get("command", "")
    if tool_name == "Agent":
        desc = tool_input.get("description", "")
        subagent = tool_input.get("subagent_type", "")
        label = f"({subagent}) " if subagent else ""
        return f"{label}{desc}" if desc else subagent
    if tool_name == "AskUserQuestion":
        questions = tool_input.get("questions", [])
        if questions:
            return questions[0].get("header", questions[0].get("question", ""))[:60]
        return "asking user"
    if tool_name == "TodoWrite":
        todos = tool_input.get("todos", [])
        if not todos:
            return "clear all"
        completed = sum(
            1 for t in todos
            if isinstance(t, dict) and t.get("status") == "completed"
        )
        total = len(todos)
        return f"{completed}/{total} done"
    if tool_name == "mcp__openshrimp__send_file":
        path = tool_input.get("file_path", "")
        basename = os.path.basename(path) if path else ""
        caption = tool_input.get("caption", "")
        if caption:
            return f"{basename} — {caption[:40]}"
        return basename
    # Generic: show first key's value
    for key, val in tool_input.items():
        if isinstance(val, str):
            s = val[:60]
            return s + ("..." if len(val) > 60 else "")
    return ""


def _extract_bash_output_text(
    content: str | list[dict[str, Any]] | None,
) -> str:
    """Extract plain text from Bash tool result content."""
    if content is None:
        return ""
    if isinstance(content, list):
        parts = [
            block.get("text", "") for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        return "\n".join(parts)
    return content


def _format_bash_header(
    tool_input: dict[str, Any],
    icon: str = "💻",
    label: str = "Bash",
) -> str:
    """Format a compact Bash header with command for the button message."""
    command = tool_input.get("command", "")
    description = tool_input.get("description", "")

    if description:
        header = f"{icon} **{label}:** {description}"
    else:
        header = f"{icon} **{label}**"

    cmd_display = command[:200] + "..." if len(command) > 200 else command
    cmd_block = f"```bash\n{cmd_display}\n```"
    return f"{header}\n\n{cmd_block}"


def _format_bash_output(
    tool_input: dict[str, Any],
    content: str | list[dict[str, Any]] | None,
    icon: str = "💻",
    label: str = "Bash",
) -> str:
    """Format Bash tool invocation and output as GFM.

    Mirrors the approval prompt style: shows description (if any) and the
    command, followed by the output in a fenced code block. Truncates output
    to BASH_OUTPUT_MAX_LINES / BASH_OUTPUT_MAX_CHARS, keeping the tail
    (most recent output) when truncation is needed.
    """
    header_block = _format_bash_header(tool_input, icon=icon, label=label)

    output_text = _extract_bash_output_text(content).strip()
    if not output_text:
        return f"{header_block}\n_No output\\._"

    lines = output_text.splitlines()
    truncated = False
    if len(lines) > BASH_OUTPUT_MAX_LINES:
        lines = lines[-BASH_OUTPUT_MAX_LINES:]
        truncated = True

    result = "\n".join(lines)
    if len(result) > BASH_OUTPUT_MAX_CHARS:
        result = result[-BASH_OUTPUT_MAX_CHARS:]
        truncated = True

    prefix = "…(truncated)\n" if truncated else ""
    output_block = f"Output:\n```\n{prefix}{result}\n```"

    return f"{header_block}\n{output_block}"


async def _send_bash_button(
    bot: Bot,
    state: _DraftState,
    tool_input: dict[str, Any],
    content: str | list[dict[str, Any]] | None,
    icon: str = "💻",
    label: str = "Bash",
) -> None:
    """Send a compact Bash message with a 'Show output' inline button.

    Finalizes any in-progress draft first to preserve message ordering,
    then sends a standalone message showing the command with an inline
    keyboard button to reveal the output on demand.

    Background tasks get their "View output" button from the
    ``TaskStartedMessage`` handler instead.
    """
    # Finalize any in-progress draft so the bash button appears in order.
    await finalize_and_reset(bot, state)

    header = _format_bash_header(tool_input, icon=icon, label=label)
    header_chunks = gfm_to_telegram(header)
    header_text = header_chunks[0] if header_chunks else ""

    is_background = bool(tool_input.get("run_in_background"))

    # Check if there's any actual output to show.
    output_text = _extract_bash_output_text(content).strip()
    if not output_text and not is_background:
        # No output and not a background task — send header only.
        try:
            msg = await bot.send_message(
                chat_id=state.chat_id,
                text=header_text + "\n_No output\\._",
                parse_mode="MarkdownV2",
                disable_notification=True,
                **state._thread_kwargs,
            )
            state.sent_message_ids.append(msg.message_id)
        except Exception as e:
            if _is_thread_not_found(e):
                raise
            logger.exception("Failed to send bash header (no output)")
        return

    # Build keyboard buttons.
    buttons: list[InlineKeyboardButton] = []

    # "Show output" callback button (for inline reveal).
    # Skip for background tasks — the inline output is just the
    # "running in background" message which isn't useful.
    if output_text and not is_background:
        callback_id = f"show_bash:{random.randint(1, 2**63 - 1)}"
        _bash_output_store[callback_id] = _format_bash_output(
            tool_input, content, icon=icon, label=label,
        )
        buttons.append(
            InlineKeyboardButton("📋 Show output", callback_data=callback_id)
        )

    keyboard = InlineKeyboardMarkup([buttons]) if buttons else None

    try:
        msg = await bot.send_message(
            chat_id=state.chat_id,
            text=header_text,
            parse_mode="MarkdownV2",
            reply_markup=keyboard,
            disable_notification=True,
            **state._thread_kwargs,
        )
        state.sent_message_ids.append(msg.message_id)
    except Exception as e:
        if _is_thread_not_found(e):
            raise
        logger.exception("Failed to send bash button message")


async def finalize_and_reset(
    bot: Bot, state: _DraftState, *, silent: bool = True,
) -> None:
    """Finalize the current draft and reset state for a new message.

    Call this before sending an out-of-band message (e.g. tool approval
    keyboard) to ensure correct message ordering in Telegram.

    Args:
        silent: If True, send the finalized message silently (no notification).
    """
    if state.raw_text.strip():
        msg_ids = await _finalize_message(bot, state, silent=silent)
        state.sent_message_ids.extend(msg_ids)
    state.raw_text = ""
    state.draft_id = random.randint(1, 2**31 - 1)
    state.dirty = False
    state.turn_complete = False
    state.live_edit_message_id = None
    state.live_edit_last_text = ""


_ASSISTANT_ERROR_MESSAGES: dict[str, str] = {
    "authentication_failed": (
        "⚠️ **Authentication failed.** Claude was unable to authenticate. "
        "Check that your API key or OAuth session is valid. "
        "Run /login to re-authenticate Claude Code."
    ),
    "billing_error": (
        "⚠️ **Billing error.** There is a problem with your Anthropic account billing. "
        "Please check your account at console.anthropic.com."
    ),
    "rate_limit": (
        "⚠️ **Rate limited.** Too many requests — please wait a moment and try again."
    ),
    "invalid_request": (
        "⚠️ **Invalid request.** The request to Claude was rejected. "
        "This may indicate a configuration issue."
    ),
    "server_error": (
        "⚠️ **Server error.** Anthropic's servers returned an error. "
        "Please try again shortly."
    ),
    "unknown": (
        "⚠️ **Unknown error.** An unexpected error occurred while communicating "
        "with Claude."
    ),
}


async def _handle_assistant_error(
    bot: Bot, state: _DraftState, error: str,
    error_detail: str | None = None,
) -> None:
    """Send a user-friendly error message for AssistantMessage errors."""
    if error_detail:
        logger.warning(
            "AssistantMessage error for chat %d: %s (%s)",
            state.chat_id, error, error_detail,
        )
    else:
        logger.warning(
            "AssistantMessage error for chat %d: %s",
            state.chat_id, error,
        )

    msg_text = _ASSISTANT_ERROR_MESSAGES.get(
        error,
        f"⚠️ **Error:** {error}",
    )
    # Append the detail from the SDK (e.g. "Prompt is too long") so
    # the user knows *why* the request was rejected.
    if error_detail:
        msg_text += f"\n\n> {error_detail}"

    await finalize_and_reset(bot, state)
    try:
        chunks = gfm_to_telegram(msg_text)
        text = chunks[0] if chunks else msg_text
        await bot.send_message(
            chat_id=state.chat_id,
            text=text,
            parse_mode="MarkdownV2",
            **state._thread_kwargs,
        )
    except Exception as e:
        if _is_thread_not_found(e):
            raise
        logger.exception("Failed to send error message for %s", error)
        try:
            await bot.send_message(
                chat_id=state.chat_id,
                text=msg_text,
                **state._thread_kwargs,
            )
        except Exception as e2:
            if _is_thread_not_found(e2):
                raise
            logger.exception("Failed to send plaintext error fallback")


async def stream_response(
    bot: Bot,
    chat_id: int,
    events: AsyncIterator[AgentEvent],
    draft_state: _DraftState | None = None,
    allowed_tools: list[str] | None = None,
    cwd: str | None = None,
    on_todo_update: Callable[[list[dict[str, Any]]], Awaitable[None]] | None = None,
    terminal_base_url: str | None = None,
    scope: ChatScope | None = None,
) -> StreamResult:
    """Stream Agent SDK events to Telegram as draft messages.

    Consumes events from the agent, buffers text, sends drafts at intervals,
    and finalizes when the result is received.

    Args:
        bot: Telegram Bot instance.
        chat_id: Telegram chat ID to send messages to.
        events: Async iterator of AgentEvent from agent.run_agent().
        draft_state: Optional pre-created draft state. If provided, the
            same state can be shared with tool approval callbacks so they
            can finalize the draft before sending approval keyboards,
            ensuring correct message ordering.
        allowed_tools: List of allowed tool patterns from config.
            Used to tag inline tool notifications as "(auto)".
        cwd: Working directory for the current context. When set, file
            paths under this directory are shown as relative paths.

    Returns:
        StreamResult with session_id, usage, cost, and timing info.
    """
    state = draft_state or _DraftState(chat_id=chat_id)
    auto_set = set(allowed_tools or [])
    result = StreamResult()
    draft_task: asyncio.Task[None] | None = None

    async def periodic_flush() -> None:
        """Periodically flush dirty drafts."""
        while True:
            await asyncio.sleep(DRAFT_INTERVAL_SECONDS)
            if state.dirty:
                await _send_draft(bot, state)

    try:
        draft_task = asyncio.create_task(periodic_flush())

        async for event in events:
            if isinstance(event, AssistantMessage):
                # Capture per-turn token usage.
                turn_usage = event.usage
                if turn_usage:
                    result.turn_usage = turn_usage

                # Check for SDK-level errors (auth failures, billing,
                # rate limits, etc.) and surface them to the user.
                if event.error:
                    # Extract error detail from content blocks (the SDK
                    # puts the human-readable reason in a TextBlock, e.g.
                    # "Prompt is too long").
                    error_detail = None
                    for block in event.content:
                        if isinstance(block, TextBlock) and block.text:
                            error_detail = block.text
                            break
                    await _handle_assistant_error(
                        bot, state, event.error, error_detail,
                    )

                # Mark this turn's text as complete. When the next
                # turn's StreamEvent deltas arrive, we'll insert a
                # newline separator to prevent text concatenation.
                if state.raw_text:
                    state.turn_complete = True

                for block in event.content:
                    if isinstance(block, TextBlock):
                        # When include_partial_messages is enabled,
                        # text arrives via StreamEvent deltas. The
                        # AssistantMessage still arrives with the
                        # complete text, so we skip it here to avoid
                        # double-counting.
                        pass

                    elif isinstance(block, ToolUseBlock):
                        # Record the mapping so we can correlate tool
                        # results back to the tool that produced them.
                        state.tool_use_map[block.id] = (
                            block.name,
                            block.input,
                        )

                        # Add tool invocation as an inline notification,
                        # but suppress tools whose output is shown directly.
                        if block.name not in _SUPPRESS_NOTIFICATION_TOOLS:
                            add_tool_notification(
                                state,
                                tool_name=block.name,
                                tool_input=block.input,
                                auto=block.name in auto_set,
                                cwd=cwd,
                            )

                        # TodoWrite: update the pinned message with the
                        # current task list.
                        if block.name == "TodoWrite" and on_todo_update is not None:
                            todos = block.input.get("todos", [])
                            try:
                                await on_todo_update(todos)
                            except Exception:
                                logger.exception(
                                    "Failed to update todos for chat %d",
                                    state.chat_id,
                                )

            elif isinstance(event, UserMessage):
                # UserMessage carries tool results (ToolResultBlock).
                # For Bash, send a collapsible message with a "Show output"
                # button instead of embedding the output inline.
                if isinstance(event.content, list):
                    for block in event.content:
                        if isinstance(block, ToolResultBlock):
                            tool_info = state.tool_use_map.get(
                                block.tool_use_id
                            )
                            if tool_info and tool_info[0] == "Bash":
                                tool_input = tool_info[1]
                                await _send_bash_button(
                                    bot, state, tool_input,
                                    block.content,
                                )
                            elif (
                                tool_info
                                and tool_info[0]
                                == "mcp__openshrimp__host_bash"
                            ):
                                tool_input = tool_info[1]
                                await _send_bash_button(
                                    bot, state, tool_input,
                                    block.content,
                                    icon="🔓",
                                    label="host_bash",
                                )

            elif isinstance(event, StreamEvent):
                # Token-level streaming: extract text deltas from raw
                # OpenCode message.part.delta events (field=text).
                raw = event.event
                if (
                    raw.get("type") == "message.part.delta"
                    and (raw.get("properties") or {}).get("field") == "text"
                ):
                    text = (raw.get("properties") or {}).get("delta", "")
                    if isinstance(text, str) and text:
                            # Insert a newline separator if this is
                            # the first text from a new assistant turn
                            # to prevent concatenation with the
                            # previous turn's text.
                            if state.turn_complete:
                                state.raw_text += "\n\n"
                                state.turn_complete = False
                            # Ensure a blank line after a tool notification
                            # blockquote so assistant text isn't swallowed
                            # into it.  Only trigger for notifications (tracked
                            # via flag), NOT for Claude's own blockquote lines.
                            if state.last_was_notification:
                                stripped = state.raw_text.rstrip()
                                state.raw_text = stripped + "\n\n"
                                state.last_was_notification = False
                            state.raw_text += text
                            state.dirty = True

                            # Check if we're approaching the message limit
                            full = _build_full_text(state)
                            converted = gfm_to_telegram(full)
                            if len(converted) > 1:
                                await _finalize_current(bot, state)

            elif isinstance(event, ResultMessage):
                result.session_id = event.session_id
                state.session_id = event.session_id
                result.model_usage = event.model_usage
                result.num_steps = event.num_steps
                result.duration_ms = event.duration_ms
                if event.errors:
                    logger.warning(
                        "ResultMessage errors for chat %d: %s",
                        state.chat_id,
                        event.errors,
                    )

            elif isinstance(event, SystemMessage):
                # Capture session_id from init messages as early as
                # possible so it's available even if the task is
                # cancelled before ResultMessage arrives.
                sid = getattr(event, "session_id", None)
                if sid:
                    state.session_id = sid
                    result.session_id = sid

    finally:
        if draft_task:
            draft_task.cancel()
            try:
                await draft_task
            except asyncio.CancelledError:
                pass

        # Final send of any remaining text — notify since the task is done.
        if state.raw_text.strip():
            msg_ids = await _finalize_message(bot, state, silent=False)
            state.sent_message_ids.extend(msg_ids)

        # Reset for the next stream_response() iteration.
        state.raw_text = ""
        state.draft_id = random.randint(1, 2**31 - 1)
        state.dirty = False
        state.turn_complete = False
        state.live_edit_message_id = None
        state.live_edit_last_text = ""

    return result


def _find_gfm_split(gfm: str) -> int | None:
    """Find a position in raw GFM where the converted output fits in one message.

    Binary-searches over paragraph (``\\n\\n``) boundaries, falling back to
    single-newline boundaries.  Returns the split offset in *gfm*, or
    ``None`` if the entire text already fits in a single chunk.
    """
    if len(gfm_to_telegram(gfm)) <= 1:
        return None

    # Collect candidate split points: paragraph breaks first, then line breaks.
    candidates: list[int] = []
    idx = 0
    while True:
        pos = gfm.find("\n\n", idx)
        if pos == -1:
            break
        candidates.append(pos + 2)
        idx = pos + 2

    if not candidates:
        idx = 0
        while True:
            pos = gfm.find("\n", idx)
            if pos == -1:
                break
            candidates.append(pos + 1)
            idx = pos + 1

    if not candidates:
        return None

    # Binary search for the largest prefix that converts to one chunk.
    lo, hi = 0, len(candidates) - 1
    best: int | None = None
    while lo <= hi:
        mid = (lo + hi) // 2
        split_pos = candidates[mid]
        if len(gfm_to_telegram(gfm[:split_pos])) <= 1:
            best = split_pos
            lo = mid + 1
        else:
            hi = mid - 1

    return best


async def _finalize_current(bot: Bot, state: _DraftState) -> None:
    """Finalize the current draft and keep overflow GFM for the next message.

    Splits at the raw GFM level so that the remainder can seed the next draft
    without losing any content.
    """
    full_text = _build_full_text(state)
    chunks = gfm_to_telegram(full_text)

    if not chunks:
        return

    if len(chunks) <= 1:
        # Nothing to split — shouldn't normally be called in this case,
        # but be defensive.
        return

    # Find a split point in the raw GFM (including tool prefix) so that
    # everything before it converts to a single MarkdownV2 chunk.
    split_pos = _find_gfm_split(full_text)

    if split_pos is None or split_pos <= 0:
        # Can't find a clean GFM split — fall back to sending chunks[0]
        # from the already-converted output (loses overflow, but this is
        # the rare edge case where the text has no newline boundaries).
        prefix_text = chunks[0]
        remainder_gfm = ""
    else:
        prefix_gfm = full_text[:split_pos]
        remainder_gfm = full_text[split_pos:]
        converted = gfm_to_telegram(prefix_gfm)
        prefix_text = converted[0] if converted else chunks[0]

    # Send the first chunk as a finalized message (silently — intermediate).
    try:
        msg = await bot.send_message(
            chat_id=state.chat_id,
            text=prefix_text,
            parse_mode="MarkdownV2",
            disable_notification=True,
            **state._thread_kwargs,
        )
        state.sent_message_ids.append(msg.message_id)
    except Exception as e:
        if _is_thread_not_found(e):
            raise
        logger.exception("Failed to finalize message")
        try:
            msg = await bot.send_message(
                chat_id=state.chat_id,
                text=prefix_text,
                disable_notification=True,
                **state._thread_kwargs,
            )
            state.sent_message_ids.append(msg.message_id)
        except Exception as e2:
            if _is_thread_not_found(e2):
                raise
            logger.exception("Failed to send plaintext fallback")

    # Keep the remainder as raw GFM for the next message.
    state.raw_text = remainder_gfm
    state.draft_id = random.randint(1, 2**31 - 1)
    state.dirty = bool(remainder_gfm.strip())
    state.turn_complete = False


def add_tool_notification(
    state: _DraftState,
    tool_name: str,
    tool_input: dict[str, Any],
    auto: bool,
    cwd: str | None = None,
) -> None:
    """Add a tool call notification inline as a GFM blockquote."""
    summary = extract_tool_summary(tool_name, tool_input, cwd=cwd)
    suffix = " (auto)" if auto else ""
    line = f"> {tool_name}: {summary}{suffix}"

    # Group consecutive tool notifications into a single blockquote block.
    stripped = state.raw_text.rstrip()
    last_line = stripped.rsplit("\n", 1)[-1] if stripped else ""

    if last_line.startswith(">"):
        # Continue the existing blockquote (consecutive tool calls).
        state.raw_text = stripped + "\n" + line + "\n"
    elif stripped:
        # Paragraph break before starting a new blockquote.
        state.raw_text = stripped + "\n\n" + line + "\n"
    else:
        state.raw_text = line + "\n"

    state.last_was_notification = True

    state.dirty = True
