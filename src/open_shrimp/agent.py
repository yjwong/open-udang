"""OpenCode-backed agent runner for OpenShrimp.

Provides an async generator interface over OpenCodeClient that yields
streaming messages (text chunks, tool events, results) for the caller
to consume and bridge to Telegram.
"""

import asyncio
import logging
import re
import tempfile
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Union

from open_shrimp.opencode_client import (
    AssistantMessage,
    OpenCodeClient,
    OpenCodeOptions,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    TextBlock,
    UserMessage,
    split_provider_model,
)


from open_shrimp.config import ContextConfig
from open_shrimp.hooks import (
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
    """Sanitize a filename for use in a temp file prefix."""
    return re.sub(r"[^\w.\-]", "_", name)


def save_attachments(
    attachments: list[FileAttachment],
    chat_id: int,
) -> list[Path]:
    """Save file attachments to temp files and return their paths."""
    from open_shrimp.hooks import ATTACHMENT_TEMP_DIR

    chat_dir = ATTACHMENT_TEMP_DIR / str(chat_id)
    chat_dir.mkdir(parents=True, exist_ok=True)

    paths: list[Path] = []
    for att in attachments:
        ext = _MIME_TO_EXT.get(att.mime_type, ".bin")
        safe_name = _sanitize_filename(att.filename) if att.filename else ""
        prefix = f"openshrimp_{safe_name}_" if safe_name else "openshrimp_"
        tmp = tempfile.NamedTemporaryFile(
            suffix=ext, prefix=prefix, delete=False,
            dir=chat_dir,
        )
        tmp.write(att.data)
        tmp.close()
        paths.append(Path(tmp.name))
        logger.info("Saved attachment to %s (%d bytes, %s)", tmp.name, len(att.data), att.mime_type)
    return paths


def build_prompt_with_attachments(prompt: str, attachment_paths: list[Path]) -> str:
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
    """Run the OpenCode-backed agent and yield streaming events."""
    can_use_tool = make_can_use_tool(
        request_approval=request_approval,
        cwd=context.directory,
        additional_directories=context.additional_directories or None,
        handle_user_questions=handle_user_questions,
        is_edit_auto_approved=is_edit_auto_approved,
        notify_auto_approved_edit=notify_auto_approved_edit,
    )

    def _log_stderr(line: str) -> None:
        logger.info("opencode stderr: %s", line.rstrip())

    provider, model = split_provider_model(context.model)
    options = OpenCodeOptions(
        cwd=context.directory,
        provider=provider,
        model=model,
        effort=context.effort,
        allowed_tools=context.allowed_tools,
        add_dirs=context.additional_directories,
        setting_sources=["project", "user", "local"],
        include_partial_messages=True,
        stderr=_log_stderr,
        can_use_tool=can_use_tool,
        max_buffer_size=10 * 1024 * 1024,  # 10MB
    )

    # Save attachments to temp files and build the prompt with file references.
    attachment_paths: list[Path] = []
    if attachments:
        attachment_paths = save_attachments(attachments, chat_id=0)
        actual_prompt = build_prompt_with_attachments(prompt, attachment_paths)
    else:
        actual_prompt = prompt

    if session_id:
        options.resume = session_id
        logger.info("Resuming session %s in %s", session_id, context.directory)
    else:
        logger.info("Starting new session in %s", context.directory)

    logger.info("Sending query: %s", actual_prompt[:200])

    try:
        async with OpenCodeClient(options=options) as client:
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
            async with OpenCodeClient(options=options) as client:
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
