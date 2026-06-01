"""MCP proxy for sandboxed contexts.

Spawns stdio MCP servers on the host and exposes them to sandboxed
OpenCode sandbox instances via MCP Streamable HTTP transport. Credentials
stay on the host; the sandbox only sees HTTP endpoints.
"""

from open_shrimp.mcp_proxy.server import McpProxy

__all__ = ["McpProxy"]
