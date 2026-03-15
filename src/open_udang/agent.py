"""Claude Agent SDK wrapper for OpenUdang.

Provides an async generator interface over ClaudeSDKClient that yields
streaming messages (text chunks, tool events, results) for the caller
to consume and bridge to Telegram.
"""

import logging
import re
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


def _sanitize_filename(name: str) -> str:
    """Sanitize a filename for use in a temp file prefix.

    Strips path separators, null bytes, and other characters that are
    unsafe in file names, keeping only alphanumerics, hyphens, underscores,
    and dots.
    """
    return re.sub(r"[^\w.\-]", "_", name)


def _save_attachments_to_temp(
    attachments: list[FileAttachment],
    chat_id: int,
) -> list[Path]:
    """Save file attachments to temp files and return their paths.

    Files are saved into a per-chat subdirectory of
    :data:`~open_udang.hooks.ATTACHMENT_TEMP_DIR` so the canUseTool hook
    can auto-approve Read access for uploaded files without granting
    access to the entire ``/tmp`` tree.  Per-chat scoping prevents one
    agent session from accessing another session's uploads.

    Files are created with delete=False so they persist for the agent to
    read.  The caller is responsible for cleanup.
    """
    from open_udang.hooks import ATTACHMENT_TEMP_DIR

    chat_dir = ATTACHMENT_TEMP_DIR / str(chat_id)
    chat_dir.mkdir(parents=True, exist_ok=True)

    paths: list[Path] = []
    for att in attachments:
        ext = _MIME_TO_EXT.get(att.mime_type, ".bin")
        # Use sanitized original filename as part of the temp name if available.
        safe_name = _sanitize_filename(att.filename) if att.filename else ""
        prefix = f"openudang_{safe_name}_" if safe_name else "openudang_"
        tmp = tempfile.NamedTemporaryFile(
            suffix=ext, prefix=prefix, delete=False,
            dir=chat_dir,
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


def prepare_prompt(
    prompt: str,
    attachments: list[FileAttachment] | None = None,
    *,
    chat_id: int = 0,
) -> tuple[str, list[Path]]:
    """Build the actual prompt and save attachments to temp files.

    Args:
        prompt: The user's message text.
        attachments: Optional file attachments to include.
        chat_id: Telegram chat ID, used to scope the temp directory so
            each chat's uploads are isolated.

    Returns ``(actual_prompt, attachment_paths)`` where *attachment_paths*
    should be cleaned up by the caller after the query completes.
    """
    attachment_paths: list[Path] = []
    if attachments:
        attachment_paths = _save_attachments_to_temp(attachments, chat_id)
        actual_prompt = _build_prompt_with_attachments(prompt, attachment_paths)
    else:
        actual_prompt = prompt
    return actual_prompt, attachment_paths


def cleanup_attachments(paths: list[Path]) -> None:
    """Remove temporary attachment files."""
    for p in paths:
        try:
            p.unlink(missing_ok=True)
        except Exception:
            logger.debug("Failed to remove temp file %s", p)


async def run_agent(
    prompt: str,
    context: ContextConfig,
    request_approval: ApprovalCallback,
    session_id: str | None = None,
    attachments: list[FileAttachment] | None = None,
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
        attachments: Optional list of file attachments to include in the prompt.
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
        additional_directories=context.additional_directories or None,
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
        add_dirs=context.additional_directories,
        setting_sources=["project", "user", "local"],
        include_partial_messages=True,
        stderr=_log_stderr,
        can_use_tool=can_use_tool,
    )

    # Build a system prompt supplement that tells the agent about additional
    # working directories so it knows they exist (--add-dir only grants
    # permission, it doesn't inform the agent).
    if context.additional_directories:
        dirs_list = "\n".join(f"  - {d}" for d in context.additional_directories)
        options.system_prompt = (
            "You also have access to the following additional working "
            "directories:\n" + dirs_list + "\n"
            "You may read and search files in these directories as needed."
        )

    # Save attachments to temp files and build the prompt with file references.
    attachment_paths: list[Path] = []
    if attachments:
        attachment_paths = _save_attachments_to_temp(attachments, chat_id=0)
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
                logger.debug("Failed to remove temp file %s", p)
