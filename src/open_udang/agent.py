"""Claude Agent SDK wrapper for OpenUdang.

Provides an async generator interface over ClaudeSDKClient that yields
streaming messages (text chunks, tool events, results) for the caller
to consume and bridge to Telegram.
"""

import logging
import tempfile
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Union

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    SystemMessage,
    TextBlock,
    UserMessage,
)
from claude_agent_sdk.types import StreamEvent

from open_udang.config import ContextConfig
from open_udang.hooks import (
    ApprovalCallback,
    EditNotifyCallback,
    QuestionCallback,
    make_can_use_tool,
)

logger = logging.getLogger(__name__)


@dataclass
class FileAttachment:
    """A file attachment to include in the prompt (image, PDF, etc.)."""

    data: bytes  # raw file bytes
    mime_type: str  # e.g. "image/jpeg", "application/pdf"
    filename: str | None = None  # original filename, if available


# Keep backward-compatible alias.
ImageAttachment = FileAttachment


# Map MIME types to file extensions.
_MIME_TO_EXT: dict[str, str] = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "application/pdf": ".pdf",
    "text/plain": ".txt",
    "text/csv": ".csv",
    "text/html": ".html",
    "text/markdown": ".md",
    "application/json": ".json",
    "application/xml": ".xml",
    "text/xml": ".xml",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "application/zip": ".zip",
    "application/x-tar": ".tar",
    "application/gzip": ".tar.gz",
}


@dataclass
class AgentResult:
    """Final result from an agent invocation."""

    session_id: str
    result_text: str


# Union of message types yielded by run_agent.
AgentEvent = Union[AssistantMessage, UserMessage, SystemMessage, ResultMessage, StreamEvent]


def _save_attachments_to_temp(attachments: list[FileAttachment]) -> list[Path]:
    """Save file attachments to temp files and return their paths.

    Files are created with delete=False so they persist for the agent to
    read.  The caller is responsible for cleanup (or we rely on OS temp
    cleanup).
    """
    paths: list[Path] = []
    for att in attachments:
        ext = _MIME_TO_EXT.get(att.mime_type, ".bin")
        # Use original filename as part of the temp name if available.
        prefix = f"openudang_{att.filename}_" if att.filename else "openudang_"
        tmp = tempfile.NamedTemporaryFile(
            suffix=ext, prefix=prefix, delete=False
        )
        tmp.write(att.data)
        tmp.close()
        paths.append(Path(tmp.name))
        logger.info("Saved attachment to %s (%d bytes, %s)", tmp.name, len(att.data), att.mime_type)
    return paths


def _build_prompt_with_attachments(prompt: str, attachment_paths: list[Path]) -> str:
    """Prepend file references to the user prompt."""
    parts: list[str] = []
    if len(attachment_paths) == 1:
        parts.append(
            f"The user attached a file. Read it from: {attachment_paths[0]}"
        )
    else:
        parts.append("The user attached files. Read them from:")
        for p in attachment_paths:
            parts.append(f"  - {p}")
    parts.append("")
    parts.append(prompt)
    return "\n".join(parts)


async def run_agent(
    prompt: str,
    context: ContextConfig,
    request_approval: ApprovalCallback,
    session_id: str | None = None,
    images: list[FileAttachment] | None = None,
    handle_user_questions: QuestionCallback | None = None,
    is_edit_auto_approved: Callable[[], bool] | None = None,
    notify_auto_approved_edit: EditNotifyCallback | None = None,
) -> AsyncIterator[AgentEvent]:
    """Run the Claude agent and yield streaming events.

    Args:
        prompt: User message to send to Claude.
        context: Context config with directory, model, allowed_tools.
        request_approval: Async callback for interactive tool approval.
        session_id: Optional session ID to resume a previous conversation.
        images: Optional list of image attachments to include in the prompt.
        handle_user_questions: Optional callback for AskUserQuestion tool.
        is_edit_auto_approved: Optional callback returning True if the user
            has opted into "accept all edits" for the current session.
        notify_auto_approved_edit: Optional callback to display diffs for
            auto-approved edits without blocking the agent.

    Yields:
        AgentEvent messages (AssistantMessage, SystemMessage, ResultMessage)
        as they arrive from the SDK.

    The caller should inspect each event:
    - AssistantMessage: extract TextBlock content for streaming to Telegram.
    - SystemMessage (subtype "init"): contains session_id for new sessions.
    - ResultMessage: final result with session_id to persist.

    Supports cancellation via asyncio task cancellation — the async with
    block will clean up the client on CancelledError.
    """
    can_use_tool = make_can_use_tool(
        request_approval=request_approval,
        cwd=context.directory,
        handle_user_questions=handle_user_questions,
        is_edit_auto_approved=is_edit_auto_approved,
        notify_auto_approved_edit=notify_auto_approved_edit,
    )

    def _log_stderr(line: str) -> None:
        logger.info("CLI stderr: %s", line.rstrip())

    options = ClaudeAgentOptions(
        cwd=context.directory,
        model=context.model,
        allowed_tools=context.allowed_tools,
        setting_sources=["project"],
        include_partial_messages=True,
        stderr=_log_stderr,
        can_use_tool=can_use_tool,
    )

    # Save attachments to temp files and build the prompt with file references.
    attachment_paths: list[Path] = []
    if images:
        attachment_paths = _save_attachments_to_temp(images)
        actual_prompt = _build_prompt_with_attachments(prompt, attachment_paths)
    else:
        actual_prompt = prompt

    if session_id:
        options.resume = session_id
        logger.info("Resuming session %s in %s", session_id, context.directory)
    else:
        logger.info("Starting new session in %s", context.directory)

    logger.info("Sending query: %s", actual_prompt[:200])

    try:
        async with ClaudeSDKClient(options=options) as client:
            await client.query(actual_prompt)
            async for message in client.receive_response():
                yield message
    except Exception as e:
        if session_id:
            logger.warning(
                "Failed to resume session %s, retrying with new session: %s",
                session_id,
                e,
            )
            options.resume = None
            async with ClaudeSDKClient(options=options) as client:
                await client.query(actual_prompt)
                async for message in client.receive_response():
                    yield message
        else:
            raise
    finally:
        # Clean up temp files
        for p in attachment_paths:
            try:
                p.unlink(missing_ok=True)
            except Exception:
                logger.debug("Failed to remove temp image %s", p)
