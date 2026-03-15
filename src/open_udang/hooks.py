"""Tool permission callbacks for OpenUdang.

Implements the canUseTool callback for the Claude Agent SDK. When a tool is
not in the allowedTools list, the CLI asks for permission via this callback.
We present a Telegram inline keyboard and await the user's decision.

AskUserQuestion is handled specially: the hook presents questions to the user
via Telegram, collects answers, then denies the tool (to prevent the CLI from
trying its own interactive UI) while passing the answers back via the deny
message so Claude receives them.

Path-scoped auto-approval: read-only file-access tools (Read, Glob, Grep)
are auto-approved when their target paths resolve to within the context's
working directory. Mutating tools (Edit, Write) always require explicit
approval, even within the working directory, unless the user has opted into
"accept all edits" for the current session. Paths outside the working
directory always fall through to the interactive Telegram approval prompt.
This prevents the agent from silently reading arbitrary files (e.g. ~/.ssh/*,
config files with secrets) when these tools are removed from allowedTools and
handled here instead.
"""

import logging
import os
import tempfile
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from claude_agent_sdk.types import (
    PermissionResult,
    PermissionResultAllow,
    PermissionResultDeny,
    ToolPermissionContext,
)

logger = logging.getLogger(__name__)

# Dedicated temp directory for file uploads.  Read access to files within
# this directory is auto-approved so the agent doesn't need extra
# permission to read user-uploaded attachments.
ATTACHMENT_TEMP_DIR = Path(tempfile.gettempdir()) / "openudang_uploads"

# Type for the approval callback: receives tool_name, tool_input dict,
# and tool_use_id; returns True (allow) or False (deny).
ApprovalCallback = Callable[[str, dict[str, Any], str], Awaitable[bool]]

# Type for the question callback: receives list of question dicts,
# returns answers dict mapping question text -> answer string.
QuestionCallback = Callable[[list[dict[str, Any]]], Awaitable[dict[str, str]]]

# Type for the auto-approved edit notification callback: receives tool_name
# and tool_input dict. Called (fire-and-forget) when a mutating tool is
# auto-approved so the user can still see the diff without blocking.
EditNotifyCallback = Callable[[str, dict[str, Any]], Awaitable[None]]

# Tools that access the filesystem, mapped to the input key(s) containing
# the path to check. Each value is a list of keys to try (first match wins).
_PATH_SCOPED_TOOLS: dict[str, list[str]] = {
    "Read": ["file_path"],
    "Write": ["file_path"],
    "Edit": ["file_path"],
    "Glob": ["path"],     # optional; defaults to cwd when absent
    "Grep": ["path"],     # optional; defaults to cwd when absent
}

# Mutating file-access tools that require explicit approval even when the
# target path is within the context working directory.  Read-only tools
# (Read, Glob, Grep) are still auto-approved within cwd.
_MUTATING_PATH_TOOLS: set[str] = {"Edit", "Write"}


def _is_path_within_directory(path: str, directory: str) -> bool:
    """Check if a resolved path is within the given directory.

    Uses os.path.realpath to resolve symlinks and normalise, then checks
    that the path starts with the directory prefix (with a trailing separator
    to avoid prefix false positives like /home/user2 matching /home/user).
    Also allows an exact match (e.g. Glob on the cwd itself).
    """
    real_path = os.path.realpath(path)
    real_dir = os.path.realpath(directory)
    return real_path == real_dir or real_path.startswith(real_dir + os.sep)


def _extract_path_for_tool(
    tool_name: str, tool_input: dict[str, Any], cwd: str
) -> str | None:
    """Extract the filesystem path from a tool's input.

    Returns the path string if one is found, or the cwd as default for
    tools where the path is optional (Glob, Grep). Returns None if the
    tool is not path-scoped.
    """
    keys = _PATH_SCOPED_TOOLS.get(tool_name)
    if keys is None:
        return None
    for key in keys:
        value = tool_input.get(key)
        if value is not None:
            return str(value)
    # Glob and Grep default to cwd when no path is provided
    if tool_name in ("Glob", "Grep"):
        return cwd
    return None


def _is_path_within_any_directory(
    path: str, directories: list[str]
) -> bool:
    """Check if a resolved path is within any of the given directories."""
    return any(_is_path_within_directory(path, d) for d in directories)


def make_can_use_tool(
    request_approval: ApprovalCallback,
    cwd: str,
    additional_directories: list[str] | None = None,
    handle_user_questions: QuestionCallback | None = None,
    is_edit_auto_approved: Callable[[], bool] | None = None,
    notify_auto_approved_edit: EditNotifyCallback | None = None,
    chat_id: int | None = None,
) -> Callable[
    [str, dict[str, Any], ToolPermissionContext], Awaitable[PermissionResult]
]:
    """Create a canUseTool callback for the Claude Agent SDK.

    Tools already in allowedTools are handled by the CLI and never reach this
    callback. This handles everything else:

    1. Path-scoped auto-approval for read-only tools: Read, Glob, and Grep
       are auto-approved when their target path resolves to within the context
       working directory or any additional directory. Mutating tools (Edit,
       Write) within those directories require explicit approval unless the
       user has opted into "accept all edits" for the session. Paths outside
       all approved directories always fall through to the interactive
       approval prompt.

    2. AskUserQuestion: presents questions to the user via Telegram, collects
       answers, then denies the tool to prevent the CLI's own interactive UI.

    3. Everything else: sends a Telegram inline keyboard for manual approval.

    Args:
        request_approval: Async callback that presents the tool call to the user
            and returns True to allow or False to deny.
        cwd: The context working directory for path-scoped auto-approval.
        additional_directories: Optional list of extra directories that are
            also approved for path-scoped auto-approval (mirrors the SDK's
            add_dirs / --add-dir).
        handle_user_questions: Optional async callback for AskUserQuestion.
            Receives the questions list, returns answers dict.
        is_edit_auto_approved: Optional callback that returns True if the user
            has opted into "accept all edits" for the current session. When
            set and returning True, mutating tools (Edit, Write) within
            approved directories are auto-approved without prompting.
        notify_auto_approved_edit: Optional async callback called when a
            mutating tool is auto-approved (accept-all-edits mode). Receives
            the tool name and input dict so the caller can display the diff
            without blocking the agent.
        chat_id: Optional Telegram chat ID. When provided, the per-chat
            upload directory (``ATTACHMENT_TEMP_DIR/<chat_id>/``) is added
            to the approved directories so Read access to uploaded files is
            auto-approved.
    """
    approved_dirs = [cwd] + (additional_directories or [])
    if chat_id is not None:
        upload_dir = str(ATTACHMENT_TEMP_DIR / str(chat_id))
        approved_dirs.append(upload_dir)

    async def can_use_tool(
        tool_name: str,
        tool_input: dict[str, Any],
        context: ToolPermissionContext,
    ) -> PermissionResult:
        # Special handling for AskUserQuestion: present questions to user
        # via Telegram, collect answers, then DENY the tool to prevent the
        # CLI from trying its own interactive UI.  The user's answers are
        # passed back to Claude via the deny message so it can use them.
        if tool_name == "AskUserQuestion" and handle_user_questions:
            questions = tool_input.get("questions", [])
            logger.info("AskUserQuestion with %d question(s)", len(questions))
            answers = await handle_user_questions(questions)
            logger.info("Collected answers for AskUserQuestion: %s", answers)

            # Format answers for Claude to consume
            answer_lines = []
            for question_text, answer in answers.items():
                answer_lines.append(f"Q: {question_text}\nA: {answer}")
            answers_text = "\n\n".join(answer_lines)

            return PermissionResultDeny(
                message=(
                    "The user has already answered these questions via the "
                    "Telegram interface. Do not retry this tool call. "
                    "Here are their responses:\n\n" + answers_text
                ),
            )

        # Path-scoped approval for file-access tools.
        # Read-only tools (Read, Glob, Grep) are auto-approved when within
        # an approved directory (cwd + additional_directories).  Mutating
        # tools (Edit, Write) require explicit approval even within approved
        # dirs, unless the user has opted into "accept all edits".
        tool_path = _extract_path_for_tool(tool_name, tool_input, cwd)
        if tool_path is not None:
            if _is_path_within_any_directory(tool_path, approved_dirs):
                if tool_name in _MUTATING_PATH_TOOLS:
                    # Check session-level "accept all edits" flag
                    if is_edit_auto_approved and is_edit_auto_approved():
                        logger.info(
                            "Auto-approved %s (accept-all-edits): "
                            "path %s is within approved dirs",
                            tool_name,
                            tool_path,
                        )
                        # Notify the user with the diff (non-blocking)
                        if notify_auto_approved_edit:
                            try:
                                await notify_auto_approved_edit(
                                    tool_name, tool_input
                                )
                            except Exception:
                                logger.exception(
                                    "Failed to send auto-approved edit "
                                    "notification"
                                )
                        return PermissionResultAllow()
                    logger.info(
                        "Mutating tool %s within approved dirs requires "
                        "approval",
                        tool_name,
                    )
                    # Fall through to interactive approval
                else:
                    logger.info(
                        "Auto-approved %s: path %s is within approved dirs",
                        tool_name,
                        tool_path,
                    )
                    return PermissionResultAllow()
            else:
                logger.warning(
                    "Path-scoped tool %s targets %s outside approved dirs, "
                    "requiring manual approval",
                    tool_name,
                    tool_path,
                )

        logger.info("Requesting approval for tool: %s", tool_name)
        # Generate a unique tool_use_id so that parallel approval requests
        # each get their own Future in _approval_futures (keyed by this id).
        # The SDK's canUseTool callback does not provide a tool_use_id, so
        # without this, concurrent approvals would collide on the same
        # dict key ("approve:", "deny:") causing the second future to
        # overwrite the first — resulting in a hang / "expired" error.
        tool_use_id = uuid.uuid4().hex[:12]
        approved = await request_approval(tool_name, tool_input, tool_use_id)
        decision = "allow" if approved else "deny"
        logger.info("Tool %s %s", tool_name, decision)

        if approved:
            return PermissionResultAllow()
        else:
            return PermissionResultDeny(message="User denied tool use.")

    return can_use_tool
