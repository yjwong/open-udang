"""Persistent Claude Agent SDK client manager for OpenUdang.

Manages long-lived ClaudeSDKClient instances keyed by chat_id, so the CLI
subprocess stays alive across multiple messages in the same conversation.
This avoids the "Continue from where you left off." injection that the CLI
performs when it detects an interrupted turn on session resume.

Only the first message in a session uses ``--resume`` to restore history;
subsequent messages simply call ``client.query()`` on the already-connected
client.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

from claude_agent_sdk import (
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    SystemMessage,
)

from telegram import Bot

from open_udang.agent import AgentEvent
from open_udang.config import ContextConfig
from open_udang.hooks import (
    ApprovalCallback,
    EditNotifyCallback,
    QuestionCallback,
)
from open_udang.tools import create_openudang_mcp_server

logger = logging.getLogger(__name__)


@dataclass
class CallbackContext:
    """Mutable holder for per-message callback state.

    The ``canUseTool`` closure is bound at client creation time and cannot
    be changed.  This indirection lets per-message state (like
    ``draft_state``) be swapped in before each ``query()`` call while the
    same closure keeps referencing *this* object.
    """

    request_approval: ApprovalCallback | None = None
    handle_user_questions: QuestionCallback | None = None
    is_edit_auto_approved: Callable[[], bool] | None = None
    notify_auto_approved_edit: EditNotifyCallback | None = None


@dataclass
class AgentSession:
    """A long-lived SDK client associated with a chat."""

    client: ClaudeSDKClient
    session_id: str | None = None
    context_name: str = ""
    callback_context: CallbackContext = field(default_factory=CallbackContext)


_active_sessions: dict[int, AgentSession] = {}


async def get_or_create_session(
    chat_id: int,
    context_name: str,
    context: ContextConfig,
    session_id: str | None,
    callback_context: CallbackContext,
    bot: Bot | None = None,
) -> AgentSession:
    """Return an existing live session or create a new one.

    If a session already exists for *chat_id* with the same context,
    return it (after updating the callback context).  Otherwise create a
    fresh ``ClaudeSDKClient``, connect, and store it.

    Args:
        chat_id: Telegram chat ID.
        context_name: Name of the active context.
        context: Context configuration (directory, model, etc.).
        session_id: Session ID for ``--resume`` (only used when creating
            a new client).
        callback_context: Mutable callback holder to bind into hooks.

    Returns:
        An ``AgentSession`` with a connected client ready for ``query()``.
    """
    existing = _active_sessions.get(chat_id)
    if existing is not None:
        if existing.context_name == context_name:
            existing.callback_context.request_approval = callback_context.request_approval
            existing.callback_context.handle_user_questions = callback_context.handle_user_questions
            existing.callback_context.is_edit_auto_approved = callback_context.is_edit_auto_approved
            existing.callback_context.notify_auto_approved_edit = callback_context.notify_auto_approved_edit
            logger.info(
                "Reusing live client for chat %d context %s",
                chat_id,
                context_name,
            )
            return existing
        else:
            logger.info(
                "Context changed for chat %d (%s -> %s), closing old client",
                chat_id,
                existing.context_name,
                context_name,
            )
            await close_session(chat_id)

    from open_udang.hooks import make_can_use_tool

    can_use_tool = make_can_use_tool(
        request_approval=_make_approval_proxy(callback_context),
        cwd=context.directory,
        additional_directories=context.additional_directories or None,
        handle_user_questions=_make_questions_proxy(callback_context),
        is_edit_auto_approved=_make_edit_approved_proxy(callback_context),
        notify_auto_approved_edit=_make_edit_notify_proxy(callback_context),
    )

    def _log_stderr(line: str) -> None:
        logger.info("CLI stderr: %s", line.rstrip())

    # Auto-approve the built-in OpenUdang MCP tools (send_file, send_photo)
    # alongside whatever the user configured.
    allowed_tools = list(context.allowed_tools or [])
    allowed_tools.append("mcp__openudang__send_file")

    options = ClaudeAgentOptions(
        cwd=context.directory,
        model=context.model,
        allowed_tools=allowed_tools,
        add_dirs=context.additional_directories,
        setting_sources=["project", "user", "local"],
        include_partial_messages=True,
        stderr=_log_stderr,
        can_use_tool=can_use_tool,
    )

    if context.additional_directories:
        dirs_list = "\n".join(f"  - {d}" for d in context.additional_directories)
        options.system_prompt = (
            "You also have access to the following additional working "
            "directories:\n" + dirs_list + "\n"
            "You may read and search files in these directories as needed."
        )

    # Register in-process MCP tools (send_file, send_photo, etc.) so the
    # agent can send files directly to the Telegram chat.
    if bot is not None:
        openudang_server = create_openudang_mcp_server(bot=bot, chat_id=chat_id)
        options.mcp_servers = {"openudang": openudang_server}

    if session_id:
        options.resume = session_id
        logger.info(
            "Creating new client for chat %d: resuming session %s in %s",
            chat_id,
            session_id,
            context.directory,
        )
    else:
        logger.info(
            "Creating new client for chat %d: new session in %s",
            chat_id,
            context.directory,
        )

    client = ClaudeSDKClient(options=options)
    await client.connect()

    session = AgentSession(
        client=client,
        session_id=session_id,
        context_name=context_name,
        callback_context=callback_context,
    )
    _active_sessions[chat_id] = session
    return session


async def close_session(chat_id: int) -> None:
    """Close and remove the session for *chat_id*, if any."""
    session = _active_sessions.pop(chat_id, None)
    if session is None:
        return
    try:
        await session.client.disconnect()
        logger.info("Closed client for chat %d", chat_id)
    except Exception:
        logger.debug("Error closing client for chat %d", chat_id, exc_info=True)


async def close_all_sessions() -> None:
    """Close all active sessions (for shutdown)."""
    chat_ids = list(_active_sessions.keys())
    for chat_id in chat_ids:
        await close_session(chat_id)


def get_session(chat_id: int) -> AgentSession | None:
    """Return the active session for *chat_id*, or None."""
    return _active_sessions.get(chat_id)


def has_session(chat_id: int) -> bool:
    """Return True if *chat_id* has a live session."""
    return chat_id in _active_sessions


async def query_and_stream(
    session: AgentSession,
    prompt: str,
) -> AsyncIterator[AgentEvent]:
    """Send a query on an existing session and yield events."""
    logger.info("Sending query on live client: %s", prompt[:200])
    await session.client.query(prompt)
    async for message in session.client.receive_response():
        if isinstance(message, SystemMessage):
            sid = getattr(message, "session_id", None)
            if sid:
                session.session_id = sid
        elif isinstance(message, ResultMessage):
            if message.session_id:
                session.session_id = message.session_id
        yield message


def _make_approval_proxy(
    ctx: CallbackContext,
) -> ApprovalCallback:
    async def _proxy(
        tool_name: str, tool_input: dict[str, Any], tool_use_id: str
    ) -> bool:
        if ctx.request_approval is None:
            logger.warning("No approval callback set, denying tool %s", tool_name)
            return False
        return await ctx.request_approval(tool_name, tool_input, tool_use_id)

    return _proxy


def _make_questions_proxy(
    ctx: CallbackContext,
) -> QuestionCallback:
    async def _proxy(
        questions: list[dict[str, Any]],
    ) -> dict[str, str]:
        if ctx.handle_user_questions is None:
            logger.warning("No question callback set, returning empty answers")
            return {}
        return await ctx.handle_user_questions(questions)

    return _proxy


def _make_edit_approved_proxy(
    ctx: CallbackContext,
) -> Callable[[], bool]:
    def _proxy() -> bool:
        if ctx.is_edit_auto_approved is None:
            return False
        return ctx.is_edit_auto_approved()

    return _proxy


def _make_edit_notify_proxy(
    ctx: CallbackContext,
) -> EditNotifyCallback:
    async def _proxy(
        tool_name: str, tool_input: dict[str, Any]
    ) -> None:
        if ctx.notify_auto_approved_edit is None:
            return
        await ctx.notify_auto_approved_edit(tool_name, tool_input)

    return _proxy
