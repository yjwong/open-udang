"""Tool permission callbacks for OpenUdang.

Implements the canUseTool callback for the Claude Agent SDK. When a tool is
not in the allowedTools list, the CLI asks for permission via this callback.
We present a Telegram inline keyboard and await the user's decision.

AskUserQuestion is handled specially: the hook presents questions to the user
via Telegram, collects answers, then denies the tool (to prevent the CLI from
trying its own interactive UI) while passing the answers back via the deny
message so Claude receives them.
"""

import logging
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


def make_can_use_tool(
    request_approval: ApprovalCallback,
    handle_user_questions: QuestionCallback | None = None,
) -> Callable[
    [str, dict[str, Any], ToolPermissionContext], Awaitable[PermissionResult]
]:
    """Create a canUseTool callback for the Claude Agent SDK.

    Tools already in allowedTools are handled by the CLI and never reach this
    callback. This handles everything else: interactive Telegram approval and
    special AskUserQuestion handling.

    Args:
        request_approval: Async callback that presents the tool call to the user
            and returns True to allow or False to deny.
        handle_user_questions: Optional async callback for AskUserQuestion.
            Receives the questions list, returns answers dict.
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

        logger.info("Requesting approval for tool: %s", tool_name)
        approved = await request_approval(tool_name, tool_input, "")
        decision = "allow" if approved else "deny"
        logger.info("Tool %s %s", tool_name, decision)

        if approved:
            return PermissionResultAllow()
        else:
            return PermissionResultDeny(message="User denied tool use.")

    return can_use_tool
