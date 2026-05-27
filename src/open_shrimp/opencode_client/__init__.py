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
    PermissionResult,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    TextBlock,
    ToolPermissionContext,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from open_shrimp.opencode_client.options import (
    OpenCodeOptions,
    split_provider_model,
)
from open_shrimp.opencode_client.permission import PermissionBridge

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
    "PermissionBridge",
    "PermissionResult",
    "PermissionResultAllow",
    "PermissionResultDeny",
    "ToolPermissionContext",
    "split_provider_model",
]
