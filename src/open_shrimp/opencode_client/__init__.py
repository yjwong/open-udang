"""OpenCode wrapper, surface-compatible with the Anthropic SDK where possible."""

from open_shrimp.opencode_client.client import OpenCodeClient
from open_shrimp.opencode_client.errors import (
    CLIConnectionError,
    OpenCodeAuthError,
    OpenCodeNotFoundError,
    ProcessError,
)
from open_shrimp.opencode_client.events import (
    AssistantMessage,
    ContentBlock,
    Message,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from open_shrimp.opencode_client.options import OpenCodeOptions

__all__ = [
    "OpenCodeClient",
    "OpenCodeOptions",
    "AssistantMessage",
    "UserMessage",
    "SystemMessage",
    "ResultMessage",
    "TextBlock",
    "ToolUseBlock",
    "ToolResultBlock",
    "StreamEvent",
    "ContentBlock",
    "Message",
    "CLIConnectionError",
    "ProcessError",
    "OpenCodeAuthError",
    "OpenCodeNotFoundError",
]
