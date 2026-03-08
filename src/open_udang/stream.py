"""Stream bridge between Agent SDK events and Telegram sendMessageDraft.

Consumes streaming events from agent.py, buffers text, and sends drafts
to Telegram at appropriate intervals. Handles message length limits,
tool call notifications, and final message delivery.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from claude_agent_sdk import AssistantMessage, ResultMessage, SystemMessage, TextBlock, ToolUseBlock
from claude_agent_sdk.types import StreamEvent
from telegram import Bot

from open_udang.agent import AgentEvent
from open_udang.markdown import gfm_to_telegram

logger = logging.getLogger(__name__)

TELEGRAM_MAX_LENGTH = 4096
DRAFT_INTERVAL_SECONDS = 0.5


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


def _extract_tool_summary(tool_name: str, tool_input: dict[str, Any]) -> str:
    """Extract a brief summary from tool input for notifications."""
    if tool_name == "Read":
        return tool_input.get("file_path", "")
    if tool_name == "Glob":
        return tool_input.get("pattern", "")
    if tool_name == "Grep":
        pattern = tool_input.get("pattern", "")
        path = tool_input.get("path", "")
        return f"{pattern} in {path}" if path else pattern
    if tool_name == "Bash":
        cmd = tool_input.get("command", "")
        return cmd[:80] + ("..." if len(cmd) > 80 else "")
    if tool_name == "Write" or tool_name == "Edit":
        return tool_input.get("file_path", "")
    if tool_name == "LSP":
        return tool_input.get("command", "")
    # Generic: show first key's value
    for key, val in tool_input.items():
        if isinstance(val, str):
            s = val[:60]
            return s + ("..." if len(val) > 60 else "")
    return ""


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


async def stream_response(
    bot: Bot,
    chat_id: int,
    events: AsyncIterator[AgentEvent],
    draft_state: _DraftState | None = None,
    auto_approve_tools: list[str] | None = None,
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
        auto_approve_tools: List of tool names that are auto-approved.
            Used to tag inline tool notifications as "(auto)".

    Returns:
        StreamResult with session_id, usage, cost, and timing info.
    """
    state = draft_state or _DraftState(chat_id=chat_id)
    auto_set = set(auto_approve_tools or [])
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
                        # Add tool invocation as an inline notification.
                        # By this point the PreToolUse hook has already
                        # run (auto-approved or user-approved via keyboard).
                        # For manually-approved tools, the approval callback
                        # finalized the draft before sending the keyboard,
                        # so this notification appears in the new draft
                        # after the approval message — preserving ordering.
                        add_tool_notification(
                            state,
                            tool_name=block.name,
                            tool_input=block.input,
                            auto=block.name in auto_set,
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
) -> None:
    """Add a tool call notification to the current draft state."""
    summary = _extract_tool_summary(tool_name, tool_input)
    state.tool_notifications.append(
        ToolNotification(tool_name=tool_name, summary=summary, auto=auto)
    )
    state.dirty = True
