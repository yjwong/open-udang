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
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from claude_agent_sdk.types import StreamEvent, TaskNotificationMessage
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup

from open_udang.agent import AgentEvent
from open_udang.markdown import gfm_to_telegram

logger = logging.getLogger(__name__)

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
    usage: dict[str, Any] | None = None
    total_cost_usd: float | None = None
    num_turns: int = 0
    duration_ms: int = 0


@dataclass
class ToolNotification:
    """A tool call notification to show in the message."""

    tool_name: str
    summary: str
    auto: bool


@dataclass
class _DraftState:
    """Internal state for message drafting."""

    chat_id: int
    # Raw GFM text accumulated so far (before conversion)
    raw_text: str = ""
    # Tool notifications collected for the current message
    tool_notifications: list[ToolNotification] = field(default_factory=list)
    # Message IDs of finalized messages (for reference)
    sent_message_ids: list[int] = field(default_factory=list)
    # Draft ID for sendMessageDraft (non-zero integer, stable per draft)
    draft_id: int = field(default_factory=lambda: random.randint(1, 2**31 - 1))
    # Whether the draft needs to be flushed
    dirty: bool = False
    # Whether drafts are disabled (e.g. unsupported chat type)
    drafts_disabled: bool = False
    # Whether the last assistant turn has completed (AssistantMessage seen).
    # Used to insert a newline separator before text from the next turn.
    turn_complete: bool = False
    # Session ID captured as early as possible (from SystemMessage init or
    # ResultMessage) so it survives task cancellation.
    session_id: str | None = None
    # Map tool_use_id -> (tool_name, tool_input) for correlating tool results
    # to invocations and displaying context (e.g. Bash command + output).
    tool_use_map: dict[str, tuple[str, dict[str, Any]]] = field(
        default_factory=dict
    )


def _format_tool_prefix(notifications: list[ToolNotification]) -> str:
    """Format tool notifications as a GFM blockquote prefix."""
    if not notifications:
        return ""
    lines = []
    for n in notifications:
        suffix = " (auto)" if n.auto else ""
        lines.append(f"> {n.tool_name}: {n.summary}{suffix}")
    return "\n".join(lines) + "\n\n"


def _build_full_text(state: _DraftState) -> str:
    """Build the full GFM text including tool prefix."""
    prefix = _format_tool_prefix(state.tool_notifications)
    return prefix + state.raw_text


async def _send_draft(bot: Bot, state: _DraftState) -> None:
    """Send or update a draft message via sendMessageDraft."""
    if state.drafts_disabled:
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
            },
        )
        state.dirty = False
    except Exception as e:
        error_msg = str(e).lower()
        if "draft_peer_invalid" in error_msg:
            # sendMessageDraft not supported for this chat type — disable drafts
            logger.info("Drafts not supported for chat %s, disabling", state.chat_id)
            state.drafts_disabled = True
        else:
            logger.exception("Failed to send draft message")


async def _finalize_message(bot: Bot, state: _DraftState) -> list[int]:
    """Finalize the draft by sending the full message.

    Returns list of sent message IDs.
    """
    full_text = _build_full_text(state)
    if not full_text.strip():
        return []

    chunks = gfm_to_telegram(full_text)
    if not chunks:
        return []

    message_ids: list[int] = []
    for chunk in chunks:
        try:
            msg = await bot.send_message(
                chat_id=state.chat_id,
                text=chunk,
                parse_mode="MarkdownV2",
            )
            message_ids.append(msg.message_id)
        except Exception:
            logger.exception("Failed to send finalized message chunk")
            # Retry without MarkdownV2 as fallback
            try:
                msg = await bot.send_message(
                    chat_id=state.chat_id,
                    text=chunk,
                )
                message_ids.append(msg.message_id)
            except Exception:
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


def _extract_tool_summary(
    tool_name: str, tool_input: dict[str, Any], cwd: str | None = None,
) -> str:
    """Extract a brief summary from tool input for notifications."""
    if tool_name == "Read":
        return _relative_path(tool_input.get("file_path", ""), cwd)
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
        return _relative_path(tool_input.get("file_path", ""), cwd)
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
    if tool_name == "mcp__openudang__send_file":
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


def _format_bash_header(tool_input: dict[str, Any]) -> str:
    """Format a compact Bash header with command for the button message."""
    command = tool_input.get("command", "")
    description = tool_input.get("description", "")

    if description:
        header = f"💻 **Bash:** {description}"
    else:
        header = "💻 **Bash**"

    cmd_display = command[:200] + "..." if len(command) > 200 else command
    cmd_block = f"```bash\n{cmd_display}\n```"
    return f"{header}\n\n{cmd_block}"


def _format_bash_output(
    tool_input: dict[str, Any],
    content: str | list[dict[str, Any]] | None,
) -> str:
    """Format Bash tool invocation and output as GFM.

    Mirrors the approval prompt style: shows description (if any) and the
    command, followed by the output in a fenced code block. Truncates output
    to BASH_OUTPUT_MAX_LINES / BASH_OUTPUT_MAX_CHARS, keeping the tail
    (most recent output) when truncation is needed.
    """
    header_block = _format_bash_header(tool_input)

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
) -> None:
    """Send a compact Bash message with a 'Show output' inline button.

    Finalizes any in-progress draft first to preserve message ordering,
    then sends a standalone message showing the command with an inline
    keyboard button to reveal the output on demand.
    """
    # Finalize any in-progress draft so the bash button appears in order.
    await finalize_and_reset(bot, state)

    header = _format_bash_header(tool_input)
    header_chunks = gfm_to_telegram(header)
    header_text = header_chunks[0] if header_chunks else ""

    # Check if there's any actual output to show.
    output_text = _extract_bash_output_text(content).strip()
    if not output_text:
        # No output — send the header without a button.
        try:
            msg = await bot.send_message(
                chat_id=state.chat_id,
                text=header_text + "\n_No output\\._",
                parse_mode="MarkdownV2",
            )
            state.sent_message_ids.append(msg.message_id)
        except Exception:
            logger.exception("Failed to send bash header (no output)")
        return

    # Store the full formatted output for later retrieval.
    callback_id = f"show_bash:{random.randint(1, 2**63 - 1)}"
    _bash_output_store[callback_id] = _format_bash_output(tool_input, content)

    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("📋 Show output", callback_data=callback_id)]]
    )

    try:
        msg = await bot.send_message(
            chat_id=state.chat_id,
            text=header_text,
            parse_mode="MarkdownV2",
            reply_markup=keyboard,
        )
        state.sent_message_ids.append(msg.message_id)
    except Exception:
        logger.exception("Failed to send bash button message")


async def finalize_and_reset(bot: Bot, state: _DraftState) -> None:
    """Finalize the current draft and reset state for a new message.

    Call this before sending an out-of-band message (e.g. tool approval
    keyboard) to ensure correct message ordering in Telegram.
    """
    if state.raw_text.strip() or state.tool_notifications:
        msg_ids = await _finalize_message(bot, state)
        state.sent_message_ids.extend(msg_ids)
    state.raw_text = ""
    state.tool_notifications = []
    state.draft_id = random.randint(1, 2**31 - 1)
    state.dirty = False
    state.turn_complete = False


_ASSISTANT_ERROR_MESSAGES: dict[str, str] = {
    "authentication_failed": (
        "⚠️ **Authentication failed.** Claude was unable to authenticate. "
        "Check that your API key or OAuth session is valid."
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
) -> None:
    """Send a user-friendly error message for AssistantMessage errors."""
    logger.warning("AssistantMessage error for chat %d: %s", state.chat_id, error)

    msg_text = _ASSISTANT_ERROR_MESSAGES.get(
        error,
        f"⚠️ **Error:** {error}",
    )

    await finalize_and_reset(bot, state)
    try:
        chunks = gfm_to_telegram(msg_text)
        text = chunks[0] if chunks else msg_text
        await bot.send_message(
            chat_id=state.chat_id,
            text=text,
            parse_mode="MarkdownV2",
        )
    except Exception:
        logger.exception("Failed to send error message for %s", error)
        try:
            await bot.send_message(
                chat_id=state.chat_id,
                text=msg_text,
            )
        except Exception:
            logger.exception("Failed to send plaintext error fallback")


async def stream_response(
    bot: Bot,
    chat_id: int,
    events: AsyncIterator[AgentEvent],
    draft_state: _DraftState | None = None,
    allowed_tools: list[str] | None = None,
    cwd: str | None = None,
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
                # Check for SDK-level errors (auth failures, billing,
                # rate limits, etc.) and surface them to the user.
                if event.error:
                    await _handle_assistant_error(bot, state, event.error)

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

            elif isinstance(event, StreamEvent):
                # Token-level streaming: extract text deltas from raw
                # Anthropic API stream events.
                raw = event.event
                if raw.get("type") == "content_block_delta":
                    delta = raw.get("delta", {})
                    if delta.get("type") == "text_delta":
                        text = delta.get("text", "")
                        if text:
                            # Insert a newline separator if this is
                            # the first text from a new assistant turn
                            # to prevent concatenation with the
                            # previous turn's text.
                            if state.turn_complete:
                                state.raw_text += "\n\n"
                                state.turn_complete = False
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
                result.usage = event.usage
                result.total_cost_usd = event.total_cost_usd
                result.num_turns = event.num_turns
                result.duration_ms = event.duration_ms

            elif isinstance(event, SystemMessage):
                # Capture session_id from init messages as early as
                # possible so it's available even if the task is
                # cancelled before ResultMessage arrives.
                sid = getattr(event, "session_id", None)
                if sid:
                    state.session_id = sid
                    result.session_id = sid

                if isinstance(event, TaskNotificationMessage):
                    logger.info(
                        "Background task %s %s for chat %d: %s",
                        event.task_id,
                        event.status,
                        state.chat_id,
                        event.summary,
                    )
                    await finalize_and_reset(bot, state)
                    try:
                        summary = event.summary or event.status
                        chunks = gfm_to_telegram(f"📋 {summary}")
                        text = chunks[0] if chunks else f"📋 {summary}"
                        await bot.send_message(
                            chat_id=state.chat_id,
                            text=text,
                            parse_mode="MarkdownV2",
                        )
                    except Exception:
                        logger.exception(
                            "Failed to send task notification message"
                        )

    finally:
        if draft_task:
            draft_task.cancel()
            try:
                await draft_task
            except asyncio.CancelledError:
                pass

        # Final send of any remaining text
        if state.raw_text.strip() or state.tool_notifications:
            msg_ids = await _finalize_message(bot, state)
            state.sent_message_ids.extend(msg_ids)

        # Reset for the next stream_response() iteration.
        state.raw_text = ""
        state.tool_notifications = []
        state.draft_id = random.randint(1, 2**31 - 1)
        state.dirty = False
        state.turn_complete = False

    return result


async def _finalize_current(bot: Bot, state: _DraftState) -> None:
    """Finalize the current draft (first chunk) and keep overflow for next."""
    full_text = _build_full_text(state)
    chunks = gfm_to_telegram(full_text)

    if not chunks:
        return

    # Send the first chunk as a finalized message
    try:
        msg = await bot.send_message(
            chat_id=state.chat_id,
            text=chunks[0],
            parse_mode="MarkdownV2",
        )
        state.sent_message_ids.append(msg.message_id)
    except Exception:
        logger.exception("Failed to finalize message")
        try:
            msg = await bot.send_message(
                chat_id=state.chat_id,
                text=chunks[0],
            )
            state.sent_message_ids.append(msg.message_id)
        except Exception:
            logger.exception("Failed to send plaintext fallback")

    # Reset state: keep remaining text for the next message
    # We can't perfectly reconstruct the raw GFM that maps to chunks[1:],
    # so we reset and re-accumulate from the agent
    state.raw_text = ""
    state.tool_notifications = []
    state.draft_id = random.randint(1, 2**31 - 1)
    state.dirty = False
    state.turn_complete = False


def add_tool_notification(
    state: _DraftState,
    tool_name: str,
    tool_input: dict[str, Any],
    auto: bool,
    cwd: str | None = None,
) -> None:
    """Add a tool call notification to the current draft state."""
    summary = _extract_tool_summary(tool_name, tool_input, cwd=cwd)
    state.tool_notifications.append(
        ToolNotification(tool_name=tool_name, summary=summary, auto=auto)
    )
    state.dirty = True
