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
from collections.abc import Awaitable, Callable
from typing import Any

from claude_agent_sdk.types import (
    PermissionResult,
    PermissionResultAllow,
    PermissionResultDeny,
    ToolPermissionContext,
)

logger = logging.getLogger(__name__)

# Type for the approval callback: receives tool_name, tool_input dict,
# and tool_use_id; returns True (allow) or False (deny).
ApprovalCallback = Callable[[str, dict[str, Any], str], Awaitable[bool]]

# Type for the question callback: receives list of question dicts,
# returns answers dict mapping question text -> answer string.
QuestionCallback = Callable[[list[dict[str, Any]]], Awaitable[dict[str, str]]]

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


def make_can_use_tool(
    request_approval: ApprovalCallback,
    cwd: str,
    handle_user_questions: QuestionCallback | None = None,
    is_edit_auto_approved: Callable[[], bool] | None = None,
) -> Callable[
    [str, dict[str, Any], ToolPermissionContext], Awaitable[PermissionResult]
]:
    """Create a canUseTool callback for the Claude Agent SDK.

    Tools already in allowedTools are handled by the CLI and never reach this
    callback. This handles everything else:

    1. Path-scoped auto-approval for read-only tools: Read, Glob, and Grep
       are auto-approved when their target path resolves to within the context
       working directory. Mutating tools (Edit, Write) within cwd require
       explicit approval unless the user has opted into "accept all edits"
       for the session. Paths outside the cwd always fall through to the
       interactive approval prompt.

    2. AskUserQuestion: presents questions to the user via Telegram, collects
       answers, then denies the tool to prevent the CLI's own interactive UI.

    3. Everything else: sends a Telegram inline keyboard for manual approval.

    Args:
        request_approval: Async callback that presents the tool call to the user
            and returns True to allow or False to deny.
        cwd: The context working directory for path-scoped auto-approval.
        handle_user_questions: Optional async callback for AskUserQuestion.
            Receives the questions list, returns answers dict.
        is_edit_auto_approved: Optional callback that returns True if the user
            has opted into "accept all edits" for the current session. When
            set and returning True, mutating tools (Edit, Write) within cwd
            are auto-approved without prompting.
    """

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
        # cwd.  Mutating tools (Edit, Write) require explicit approval even
        # within cwd, unless the user has opted into "accept all edits".
        tool_path = _extract_path_for_tool(tool_name, tool_input, cwd)
        if tool_path is not None:
            if _is_path_within_directory(tool_path, cwd):
                if tool_name in _MUTATING_PATH_TOOLS:
                    # Check session-level "accept all edits" flag
                    if is_edit_auto_approved and is_edit_auto_approved():
                        logger.info(
                            "Auto-approved %s (accept-all-edits): "
                            "path %s is within cwd %s",
                            tool_name,
                            tool_path,
                            cwd,
                        )
                        return PermissionResultAllow()
                    logger.info(
                        "Mutating tool %s within cwd %s requires approval",
                        tool_name,
                        cwd,
                    )
                    # Fall through to interactive approval
                else:
                    logger.info(
                        "Auto-approved %s: path %s is within cwd %s",
                        tool_name,
                        tool_path,
                        cwd,
                    )
                    return PermissionResultAllow()
            else:
                logger.warning(
                    "Path-scoped tool %s targets %s outside cwd %s, "
                    "requiring manual approval",
                    tool_name,
                    tool_path,
                    cwd,
                )

        logger.info("Requesting approval for tool: %s", tool_name)
        approved = await request_approval(tool_name, tool_input, "")
        decision = "allow" if approved else "deny"
        logger.info("Tool %s %s", tool_name, decision)

        if approved:
            return PermissionResultAllow()
        else:
            return PermissionResultDeny(message="User denied tool use.")

    return can_use_tool
