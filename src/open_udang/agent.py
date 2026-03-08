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
class ImageAttachment:
    """An image attachment to include in the prompt."""

    data: bytes  # raw image bytes
    mime_type: str  # e.g. "image/jpeg", "image/png"


# Map MIME types to file extensions.
_MIME_TO_EXT: dict[str, str] = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
}


@dataclass
class AgentResult:
    """Final result from an agent invocation."""

    session_id: str
    result_text: str


# Union of message types yielded by run_agent.
AgentEvent = Union[AssistantMessage, SystemMessage, ResultMessage, StreamEvent]


def _save_images_to_temp(images: list[ImageAttachment]) -> list[Path]:
    """Save image attachments to temp files and return their paths.

    Files are created with delete=False so they persist for the agent to
    read.  The caller is responsible for cleanup (or we rely on OS temp
    cleanup).
    """
    paths: list[Path] = []
    for img in images:
        ext = _MIME_TO_EXT.get(img.mime_type, ".jpg")
        tmp = tempfile.NamedTemporaryFile(
            suffix=ext, prefix="openudang_img_", delete=False
        )
        tmp.write(img.data)
        tmp.close()
        paths.append(Path(tmp.name))
        logger.info("Saved image to %s (%d bytes)", tmp.name, len(img.data))
    return paths


def _build_prompt_with_images(prompt: str, image_paths: list[Path]) -> str:
    """Prepend image file references to the user prompt."""
    parts: list[str] = []
    if len(image_paths) == 1:
        parts.append(
            f"The user attached an image. Read it from: {image_paths[0]}"
        )
    else:
        parts.append("The user attached images. Read them from:")
        for p in image_paths:
            parts.append(f"  - {p}")
    parts.append("")
    parts.append(prompt)
    return "\n".join(parts)


async def run_agent(
    prompt: str,
    context: ContextConfig,
    request_approval: ApprovalCallback,
    session_id: str | None = None,
    images: list[ImageAttachment] | None = None,
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

    # Save images to temp files and build the prompt with file references.
    image_paths: list[Path] = []
    if images:
        image_paths = _save_images_to_temp(images)
        actual_prompt = _build_prompt_with_images(prompt, image_paths)
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
        for p in image_paths:
            try:
                p.unlink(missing_ok=True)
            except Exception:
                logger.debug("Failed to remove temp image %s", p)
