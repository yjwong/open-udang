"""Wrapper message classes shaped like the Anthropic SDK's."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Union

# Re-export the SDK's permission types so hooks.py (which constructs
# PermissionResultAllow/PermissionResultDeny from the SDK) and our bridge
# (which isinstance-matches the results) see the same classes.
from claude_agent_sdk.types import (
    PermissionResult,
    PermissionResultAllow,
    PermissionResultDeny,
    ToolPermissionContext,
)


@dataclass
class TextBlock:
    text: str


@dataclass
class ToolUseBlock:
    id: str = ""
    name: str = ""
    input: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolResultBlock:
    tool_use_id: str = ""
    content: Any = None
    is_error: bool = False


ContentBlock = Union[TextBlock, ToolUseBlock, ToolResultBlock]


@dataclass
class AssistantMessage:
    """An assistant turn — text and/or tool use blocks.

    ``usage`` carries the OpenCode-native token shape
    ``{input, output, reasoning, cache: {read, write}}`` and is populated
    on the *final* AssistantMessage of each step (i.e. when
    ``session.next.step.ended`` fires). Mid-step messages (e.g. those
    bearing a single ToolUseBlock) leave it ``None``.

    ``error`` is a human-readable string set when
    ``session.next.step.failed`` fires for the step that produced this
    message. ``stream.py`` picks it up to render a friendly model-error
    UI.
    """

    content: list[ContentBlock]
    usage: dict[str, Any] | None = None
    error: str | None = None


@dataclass
class UserMessage:
    content: list[ContentBlock]


@dataclass
class SystemMessage:
    subtype: str
    data: dict[str, Any]


@dataclass
class ResultMessage:
    """End-of-turn result emitted once per ``prompt_async`` cycle.

    Fields use OpenCode-native shapes throughout:

    * ``usage`` — aggregate ``{input, output, reasoning, cache: {read,
      write}}`` summed across every step in this turn.
    * ``model_usage`` — per-model breakdown keyed by model id, value is
      ``{input, output, reasoning, cache: {read, write}, cost}``.
    * ``num_steps`` — count of ``session.next.step.started`` events seen
      during this turn (one per LLM call; a tool-using turn fires
      multiple).
    * ``errors`` — list of ``{message, when}`` dicts, one per
      ``step.failed``. Empty list on success.
    * ``total_cost_usd`` — sum of per-step costs from
      ``step.ended.data.cost``.
    """

    session_id: str
    total_cost_usd: float | None = None
    usage: dict[str, Any] | None = None
    model_usage: dict[str, dict[str, Any]] | None = None
    num_steps: int | None = None
    duration_ms: int | None = None
    errors: list[dict[str, Any]] | None = None
    is_error: bool = False


@dataclass
class StreamEvent:
    event: dict[str, Any]


Message = Union[AssistantMessage, UserMessage, SystemMessage, ResultMessage, StreamEvent]


__all__ = [
    "AssistantMessage",
    "ContentBlock",
    "Message",
    "PermissionResult",
    "PermissionResultAllow",
    "PermissionResultDeny",
    "ResultMessage",
    "StreamEvent",
    "SystemMessage",
    "TextBlock",
    "ToolPermissionContext",
    "ToolResultBlock",
    "ToolUseBlock",
    "UserMessage",
]
