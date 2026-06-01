"""Shared mutable state for all bot handler modules.

All module-level dictionaries, sets, and constants that are shared across
handler modules live here.  This makes cross-module coupling explicit and
avoids circular imports.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Forward references (to avoid importing heavy modules at module level)
# ---------------------------------------------------------------------------
# AgentSession is referenced by type only; the actual import happens at
# usage sites in the handler modules.
from open_shrimp.client_manager import AgentSession
from open_shrimp.db import ChatScope

# ---------------------------------------------------------------------------
# Per-scope running asyncio task (for cancellation)
# ---------------------------------------------------------------------------
_running_tasks: dict[ChatScope, asyncio.Task[Any]] = {}

# ---------------------------------------------------------------------------
# Per-scope dispatch lock: serialises _dispatch_to_agent so two messages
# for the same scope cannot both slip through the "no task running" check
# before either sets _running_tasks[scope].
# ---------------------------------------------------------------------------
_scope_dispatch_locks: dict[ChatScope, asyncio.Lock] = {}

# ---------------------------------------------------------------------------
# Per-scope live session reference for message injection.
# Set once get_or_create_session + initial query() completes inside _run(),
# cleared in the finally block.
# ---------------------------------------------------------------------------
_injectable_sessions: dict[ChatScope, AgentSession] = {}

# ---------------------------------------------------------------------------
# Per-scope queue for messages that arrive during brief setup.
# (before the session is ready for injection).  Drained immediately once
# the session becomes injectable.
# ---------------------------------------------------------------------------
from open_shrimp.agent import FileAttachment

_setup_queues: dict[ChatScope, list[tuple[str, list[FileAttachment]]]] = {}

# ---------------------------------------------------------------------------
# Attachment temp-file paths created by injected messages.  Cleaned up in
# _run()'s finally block after the agent has finished processing.
# ---------------------------------------------------------------------------
_injected_attachment_paths: dict[ChatScope, list[Path]] = {}

# ---------------------------------------------------------------------------
# Pending tool approval futures: callback_data -> asyncio.Future[bool]
# ---------------------------------------------------------------------------
_approval_futures: dict[str, asyncio.Future[bool]] = {}

# ---------------------------------------------------------------------------
# Pending Agent tool inputs for "Show prompt" expansion: tool_use_id -> tool_input
# ---------------------------------------------------------------------------
_pending_agent_inputs: dict[str, dict[str, Any]] = {}

# ---------------------------------------------------------------------------
# Tool name for each pending approval: tool_use_id -> tool_name.
# Used to collapse verbose approval messages (e.g. Bash) to a compact
# one-liner after the user approves/denies.
# ---------------------------------------------------------------------------
_approval_tool_names: dict[str, str] = {}

# ---------------------------------------------------------------------------
# Extended metadata for pending approvals: tool_use_id -> dict with
# tool_name, tool_input, chat_id, and message_id.  Used to auto-resolve
# parallel pending approvals when an "accept all" action is taken.
# ---------------------------------------------------------------------------
_approval_metadata: dict[str, dict[str, Any]] = {}

# ---------------------------------------------------------------------------
# Sessions where the user has opted into "accept all edits" for mutating
# file-access tools (Edit, Write) within the context working directory.
# Keyed by (scope, context_name).  Cleared on /clear or context switch.
# ---------------------------------------------------------------------------
_edit_approved_sessions: set[tuple[ChatScope, str]] = set()

# ---------------------------------------------------------------------------
# Per-session auto-approval rules for non-path-scoped tools (e.g.
# WebFetch, WebSearch, Bash).  Each rule can optionally carry a pattern
# (e.g. "git *" for Bash) so approval can be scoped to command prefixes.
# Cleared on /clear or context switch.
# ---------------------------------------------------------------------------
from open_shrimp.hooks import ApprovalRule

_tool_approved_sessions: dict[tuple[ChatScope, str], list[ApprovalRule]] = {}

# ---------------------------------------------------------------------------
# Session-approved directories: paths the user explicitly opted into via the
# "Allow <reading from|all edits in> <dir>/ this session" button on an
# out-of-scope file approval prompt.  Membership grants both read AND write
# access for the rest of the session. Cleared on /clear or context switch.
# ---------------------------------------------------------------------------
_session_approved_dirs: dict[tuple[ChatScope, str], set[str]] = {}

# ---------------------------------------------------------------------------
# Pending session-dir approvals: short key -> (scope, ctx_name, directory).
# Telegram callback_data is limited to 64 bytes, so the directory path is
# stashed here and only a short UUID rides in the callback.
# ---------------------------------------------------------------------------
_pending_session_dirs: dict[str, tuple[ChatScope, str, str]] = {}

# ---------------------------------------------------------------------------
# Pending "Accept all <tool>" approvals: short key -> tool_name.
# Telegram callback_data is limited to 64 bytes, which long MCP names like
# ``mcp__playwright__browser_navigate`` blow past once tacked onto
# ``accept_all_tool:<tool_use_id>:``.  Stash the name here and put only a
# short UUID in the callback so the button is always offered.
# ---------------------------------------------------------------------------
_pending_tool_approvals: dict[str, str] = {}


def clear_session_approvals(scope: ChatScope, context_name: str) -> None:
    """Drop every session-scoped approval for *(scope, context_name)*.

    Called on /clear and on context switch so that auto-approval rules,
    accept-all-edits, and session-approved directories don't leak across
    contexts or persist after the user explicitly resets.
    """
    _edit_approved_sessions.discard((scope, context_name))
    _tool_approved_sessions.pop((scope, context_name), None)
    _session_approved_dirs.pop((scope, context_name), None)

# ---------------------------------------------------------------------------
# Per-scope model override: scope -> model name.  Set via /model command.
# Cleared on /clear or context switch.  Takes precedence over context config.
# ---------------------------------------------------------------------------
_model_overrides: dict[ChatScope, str] = {}

# ---------------------------------------------------------------------------
# Per-scope effort override: scope -> effort level ("low", "medium", "high",
# "max").  Set via /effort command.  Cleared on /clear or context switch.
# Takes precedence over context config.
# ---------------------------------------------------------------------------
_effort_overrides: dict[ChatScope, str] = {}

# ---------------------------------------------------------------------------
# Per-scope additional directory overrides: (scope, context_name) -> dirs.
# Set via /add_dir command.  Persisted in DB, cached here for fast lookup.
# A key present with an empty list means "loaded, no overrides".
# A key absent means "not loaded yet" (will be populated from DB on access).
# ---------------------------------------------------------------------------
_additional_dir_cache: dict[tuple[ChatScope, str], list[str]] = {}

# ---------------------------------------------------------------------------
# Pending /add_dir confirmations: short key -> (scope, ctx_name, path).
# Telegram callback_data is limited to 64 bytes, so we store the full
# details here and put only a short UUID in the callback data.
# ---------------------------------------------------------------------------
_pending_add_dirs: dict[str, tuple[ChatScope, str, str]] = {}

# ---------------------------------------------------------------------------
# Per-scope active background tasks.  Populated by TaskStartedMessage,
# updated by TaskProgressMessage, removed by TaskNotificationMessage.
# Cleared on /clear.
# ---------------------------------------------------------------------------


@dataclass
class TrackedTask:
    """A background task being tracked."""

    task_id: str
    description: str
    task_type: str | None  # "local_bash", "local_agent", "remote_agent"
    started_at: float  # time.monotonic()
    tool_use_id: str | None = None
    session_id: str | None = None
    last_tool_name: str | None = None  # updated by TaskProgressMessage


_active_bg_tasks: dict[ChatScope, dict[str, TrackedTask]] = {}


def is_task_active(task_id: str) -> bool:
    """Check whether a background task is still active (any scope)."""
    for scope_tasks in _active_bg_tasks.values():
        if task_id in scope_tasks:
            return True
    return False


# ---------------------------------------------------------------------------
# Media group batching: media_group_id -> list of messages received so far.
# ---------------------------------------------------------------------------
_media_group_messages: dict[str, list[Any]] = {}
_media_group_tasks: dict[str, asyncio.Task[Any]] = {}

# How long to wait for additional media group messages (seconds).
_MEDIA_GROUP_WAIT: float = 0.5

# ---------------------------------------------------------------------------
# OpenCode question state
# ---------------------------------------------------------------------------


@dataclass
class _QuestionState:
    """State for an active OpenCode question inline keyboard."""

    question_id: str
    scope: ChatScope
    options: list[dict[str, Any]]
    multi_select: bool
    future: asyncio.Future[Any]
    selected: set[int] = field(default_factory=set)
    other_texts: list[str] = field(default_factory=list)
    structured_answers: bool = False
    allow_custom: bool = True
    message_id: int | None = None
    waiting_for_other: bool = False
    """True when the user clicked "Other..." and we're waiting for their text input."""
    other_query: Any = None
    """The callback query that triggered the "Other..." flow, used to edit the message afterward."""
    original_text_md: str = ""
    """The original MarkdownV2 message text, saved so we can restore it after Other input."""


# Pending question states: question_id -> _QuestionState
_question_states: dict[str, _QuestionState] = {}

# Pending "Other" text input: scope -> question_id.
# When message_handler sees a text message for a scope with a pending "Other"
# input, it resolves the question instead of dispatching to the agent.
_pending_other_input: dict[ChatScope, str] = {}

# ---------------------------------------------------------------------------
# Resume command state
# ---------------------------------------------------------------------------

# Number of sessions per page in /resume list.
_RESUME_LIST_LIMIT = 5

# Pending resume selections: callback_data -> session_id
_resume_selections: dict[str, str] = {}

# Cached session info for detail view: session_id -> SDKSessionInfo
_resume_session_cache: dict[str, Any] = {}

# Page each session was listed on: session_id -> (ctx_name, page)
_resume_page_cache: dict[str, tuple[str, int]] = {}

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Default context window used when the configured model limit is unknown.
_DEFAULT_CONTEXT_LIMIT = 200_000

# Status emoji map for MCP server connection status.
_MCP_STATUS_EMOJI: dict[str, str] = {
    "connected": "\U0001f7e2",
    "connecting": "\U0001f7e1",
    "pending": "\U0001f7e1",
    "failed": "\U0001f534",
    "needs-auth": "\U0001f7e0",
    "disabled": "\u26aa",
    "disconnected": "\u26aa",
}
