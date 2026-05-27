"""Wrapper message classes shaped like the Anthropic SDK's."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Union


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
    content: list[ContentBlock]


@dataclass
class UserMessage:
    content: list[ContentBlock]


@dataclass
class SystemMessage:
    subtype: str
    data: dict[str, Any]


@dataclass
class ResultMessage:
    session_id: str
    total_cost_usd: float | None = None
    usage: dict[str, Any] | None = None
    duration_ms: int | None = None
    is_error: bool = False


@dataclass
class StreamEvent:
    event: dict[str, Any]


Message = Union[AssistantMessage, UserMessage, SystemMessage, ResultMessage, StreamEvent]
