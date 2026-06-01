"""OpenCode wrapper used by OpenShrimp."""

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
from open_shrimp.opencode_client.process import OpenCodeEndpoint
from open_shrimp.opencode_client.sessions import SessionInfo, list_sessions

__all__ = [
    "OpenCodeClient",
    "OpenCodeOptions",
    "OpenCodeEndpoint",
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
    "SessionInfo",
    "ToolPermissionContext",
    "list_sessions",
    "split_provider_model",
]
