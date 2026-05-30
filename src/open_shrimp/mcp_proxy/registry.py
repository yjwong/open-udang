"""Per-context authentication registry for the MCP proxy.

Each sandboxed context gets a cryptographically random token.
The proxy validates tokens on every request to prevent cross-context
access.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from dataclasses import dataclass, field

from open_shrimp.mcp_proxy.config_reader import (
    HttpServerConfig,
    StdioServerConfig,
)
from open_shrimp.tools import OpenShrimpTool


@dataclass
class ContextRegistration:
    """A registered context with its auth token and server configs."""

    context_name: str
    token: str
    servers: dict[str, StdioServerConfig]
    http_servers: dict[str, HttpServerConfig] = field(default_factory=dict)


@dataclass
class ToolScopeRegistration:
    """A scope-bound OpenShrimp tools registration."""

    token: str
    context_name: str
    chat_id: int
    thread_id: int | None
    user_id: int
    tool_factory: Callable[[], list[OpenShrimpTool]]


class ProxyRegistry:
    """Maps auth tokens to context registrations."""

    def __init__(self) -> None:
        self._by_token: dict[str, ContextRegistration] = {}
        self._by_context: dict[str, ContextRegistration] = {}
        self._tool_scopes_by_token: dict[str, ToolScopeRegistration] = {}
        self._tool_scopes_by_key: dict[
            tuple[str, int, int | None, int], ToolScopeRegistration
        ] = {}

    def register_context(
        self,
        context_name: str,
        servers: dict[str, StdioServerConfig] | None = None,
        http_servers: dict[str, HttpServerConfig] | None = None,
    ) -> str:
        """Register (or re-register) servers for *context_name*.

        Returns the auth token.  If the context is already registered,
        the existing token is returned and the server lists are merged
        (later calls overwrite entries with the same name).
        """
        stdio = servers or {}
        http = http_servers or {}
        existing = self._by_context.get(context_name)
        if existing is not None:
            existing.servers = {**existing.servers, **stdio}
            existing.http_servers = {**existing.http_servers, **http}
            return existing.token

        token = secrets.token_hex(32)
        reg = ContextRegistration(
            context_name=context_name,
            token=token,
            servers=stdio,
            http_servers=http,
        )
        self._by_token[token] = reg
        self._by_context[context_name] = reg
        return token

    def unregister_context(self, context_name: str) -> None:
        """Remove all registrations for *context_name*."""
        reg = self._by_context.pop(context_name, None)
        if reg is not None:
            self._by_token.pop(reg.token, None)
        for key, tool_reg in list(self._tool_scopes_by_key.items()):
            if tool_reg.context_name == context_name:
                self._tool_scopes_by_key.pop(key, None)
                self._tool_scopes_by_token.pop(tool_reg.token, None)

    def authenticate(self, token: str) -> ContextRegistration | None:
        """Look up a registration by token.  O(1)."""
        return self._by_token.get(token)

    def register_tool_scope(
        self,
        *,
        context_name: str,
        chat_id: int,
        thread_id: int | None,
        user_id: int,
        tool_factory: Callable[[], list[OpenShrimpTool]],
    ) -> str:
        """Register/reuse a scope-bound OpenShrimp tools endpoint."""
        key = (context_name, chat_id, thread_id, user_id)
        existing = self._tool_scopes_by_key.get(key)
        if existing is not None:
            existing.tool_factory = tool_factory
            return existing.token
        token = secrets.token_urlsafe(32)
        reg = ToolScopeRegistration(
            token=token,
            context_name=context_name,
            chat_id=chat_id,
            thread_id=thread_id,
            user_id=user_id,
            tool_factory=tool_factory,
        )
        self._tool_scopes_by_key[key] = reg
        self._tool_scopes_by_token[token] = reg
        return token

    def get_tool_scope(self, token: str) -> ToolScopeRegistration | None:
        """Look up a scope-bound tools registration by token."""
        return self._tool_scopes_by_token.get(token)
