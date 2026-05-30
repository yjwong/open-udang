"""MCP Streamable HTTP proxy server.

A lightweight Starlette app that exposes two routes:

* ``POST /mcp/{context}/{server}`` — JSON-RPC bridge to a host-spawned
  stdio MCP server.  One request per HTTP call, no streaming.

* ``ANY /http/{context}/{server}`` — full reverse proxy for HTTP/SSE
  MCP servers (e.g. ``mcp.figma.com``).  Streams request and response
  bodies, propagates ``Mcp-Session-Id`` and SSE event streams, and
  injects the host's OAuth bearer token from
  ``~/.claude/.credentials.json`` so credentials never enter the
  sandbox.

Runs on a separate listener from the main review/config Starlette app
to minimise attack surface.
"""

from __future__ import annotations

import asyncio
import logging
import socket
from collections.abc import Callable
from typing import Any

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from open_shrimp.mcp_proxy.config_reader import (
    HttpServerConfig,
    StdioServerConfig,
)
from open_shrimp.mcp_proxy.credentials import get_oauth_credential, is_expired
from open_shrimp.mcp_proxy.registry import (
    ContextRegistration,
    ProxyRegistry,
    ToolScopeRegistration,
)
from open_shrimp.mcp_proxy.stdio_manager import StdioManager
from open_shrimp.tools import OpenShrimpTool

logger = logging.getLogger(__name__)


# Hop-by-hop headers (RFC 7230 §6.1) — never forward.
_HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}
# Headers we strip from the inbound request before forwarding upstream.
_STRIP_INBOUND = _HOP_BY_HOP | {"host", "authorization", "content-length"}
# Headers we strip from the upstream response before returning to the
# sandbox.  ``content-length`` is dropped because StreamingResponse uses
# chunked encoding.
_STRIP_OUTBOUND = _HOP_BY_HOP | {"content-length"}


# -----------------------------------------------------------------------
# HTTP handler — stdio bridge (existing simple POST-only route)
# -----------------------------------------------------------------------

def _authenticate(
    request: Request, registry: ProxyRegistry
) -> ContextRegistration | JSONResponse:
    """Return a registration on success or a JSONResponse error."""
    context_name: str = request.path_params["context_name"]
    auth_header = request.headers.get("authorization", "")
    if not auth_header.startswith("Bearer "):
        return JSONResponse(
            {"error": "missing or malformed Authorization header"},
            status_code=401,
        )
    token = auth_header[7:]
    reg = registry.authenticate(token)
    if reg is None:
        return JSONResponse({"error": "invalid token"}, status_code=401)
    if reg.context_name != context_name:
        return JSONResponse(
            {"error": "token/context mismatch"}, status_code=403
        )
    return reg


def _create_proxy_app(
    registry: ProxyRegistry,
    stdio_manager: StdioManager,
    http_client: httpx.AsyncClient,
) -> Starlette:
    """Build the Starlette ASGI app with the proxy routes."""

    async def stdio_endpoint(request: Request) -> Response:
        server_name: str = request.path_params["server_name"]
        auth = _authenticate(request, registry)
        if isinstance(auth, JSONResponse):
            return auth
        reg = auth

        config = reg.servers.get(server_name)
        if config is None:
            return JSONResponse(
                {"error": f"unknown server: {server_name}"}, status_code=404
            )

        try:
            body: dict[str, Any] = await request.json()
        except Exception:
            return JSONResponse(
                {"jsonrpc": "2.0", "error": {"code": -32700, "message": "Parse error"}},
                status_code=400,
            )

        try:
            proc = await stdio_manager.get_or_spawn(
                reg.context_name, server_name, config
            )
            response = await stdio_manager.send_message(proc, body)
        except Exception:
            logger.exception(
                "Error forwarding to MCP server '%s/%s'",
                reg.context_name,
                server_name,
            )
            error_response: dict[str, Any] = {
                "jsonrpc": "2.0",
                "error": {
                    "code": -32603,
                    "message": "Internal error: MCP server unavailable",
                },
            }
            if "id" in body:
                error_response["id"] = body["id"]
            return JSONResponse(error_response, status_code=502)

        if response is None:
            return Response(status_code=202)
        return JSONResponse(response)

    async def http_endpoint(request: Request) -> Response:
        server_name: str = request.path_params["server_name"]
        auth = _authenticate(request, registry)
        if isinstance(auth, JSONResponse):
            return auth
        reg = auth

        config = reg.http_servers.get(server_name)
        if config is None:
            return JSONResponse(
                {"error": f"unknown http server: {server_name}"},
                status_code=404,
            )

        return await _forward_http(request, reg.context_name, server_name, config, http_client)

    async def tools_endpoint(request: Request) -> Response:
        scope_token: str = request.path_params["scope_token"]
        reg = registry.get_tool_scope(scope_token)
        if reg is None:
            return JSONResponse({"error": "unknown tool scope"}, status_code=404)
        if request.method in {"GET", "DELETE"}:
            return Response(status_code=202)
        try:
            body = await request.json()
        except Exception:
            return JSONResponse(
                {"jsonrpc": "2.0", "error": {"code": -32700, "message": "Parse error"}},
                status_code=400,
            )
        if isinstance(body, list):
            responses = [await _handle_tools_rpc(item, reg) for item in body]
            responses = [r for r in responses if r is not None]
            if not responses:
                return Response(status_code=202)
            return JSONResponse(responses)
        response = await _handle_tools_rpc(body, reg)
        if response is None:
            return Response(status_code=202)
        return JSONResponse(response)

    routes = [
        Route(
            "/mcp/{context_name}/{server_name}",
            stdio_endpoint,
            methods=["POST"],
        ),
        Route(
            "/http/{context_name}/{server_name}",
            http_endpoint,
            methods=["GET", "POST", "DELETE", "OPTIONS", "HEAD"],
        ),
        Route(
            "/tools/{scope_token}",
            tools_endpoint,
            methods=["GET", "POST", "DELETE"],
        ),
    ]

    return Starlette(routes=routes)


async def _handle_tools_rpc(
    body: Any,
    reg: ToolScopeRegistration,
) -> dict[str, Any] | None:
    if not isinstance(body, dict):
        return _rpc_error(None, -32600, "Invalid Request")
    request_id = body.get("id")
    method = body.get("method")
    if not isinstance(method, str):
        return _rpc_error(request_id, -32600, "Invalid Request")

    if method == "notifications/initialized":
        return None
    if method == "initialize":
        return _rpc_result(request_id, {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "openshrimp", "version": "0.1.0"},
        })

    tools = _tools_for_registration(reg)
    by_name = {tool.name: tool for tool in tools}
    if method == "tools/list":
        return _rpc_result(request_id, {
            "tools": [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "inputSchema": tool.input_schema,
                    "annotations": {"readOnlyHint": tool.read_only},
                }
                for tool in tools
            ]
        })
    if method == "tools/call":
        params = body.get("params") or {}
        if not isinstance(params, dict):
            return _rpc_error(request_id, -32602, "Invalid params")
        name = params.get("name")
        args = params.get("arguments") or {}
        if not isinstance(name, str) or not isinstance(args, dict):
            return _rpc_error(request_id, -32602, "Invalid params")
        tool = by_name.get(name)
        if tool is None:
            return _rpc_error(request_id, -32602, f"Unknown tool: {name}")
        try:
            return _rpc_result(request_id, await tool.handler(args))
        except Exception as exc:
            logger.exception("OpenShrimp tool %s failed", name)
            return _rpc_result(request_id, {
                "content": [{"type": "text", "text": f"Error: {exc}"}],
                "is_error": True,
            })
    return _rpc_error(request_id, -32601, f"Method not found: {method}")


def _tools_for_registration(reg: ToolScopeRegistration) -> list[OpenShrimpTool]:
    return reg.tool_factory()


def _rpc_result(request_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _rpc_error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


# -----------------------------------------------------------------------
# HTTP reverse-proxy logic
# -----------------------------------------------------------------------

async def _forward_http(
    request: Request,
    context_name: str,
    server_name: str,
    config: HttpServerConfig,
    http_client: httpx.AsyncClient,
) -> Response:
    """Reverse-proxy *request* to *config.url* with OAuth injected."""
    cred = get_oauth_credential(server_name, config.url)
    if cred is None:
        return JSONResponse(
            {"error": f"no OAuth credential on host for '{server_name}' "
                      f"({config.url}). Run /mcp on the host to authenticate."},
            status_code=401,
        )
    if is_expired(cred):
        return JSONResponse(
            {"error": f"OAuth credential for '{server_name}' has expired. "
                      f"Run /mcp on the host to re-authenticate."},
            status_code=401,
        )

    outbound_headers: dict[str, str] = {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in _STRIP_INBOUND
    }
    outbound_headers.update(config.headers)
    outbound_headers["Authorization"] = f"Bearer {cred.access_token}"

    method = request.method
    req_kwargs: dict[str, Any] = {
        "method": method,
        "url": config.url,
        "headers": outbound_headers,
        "params": dict(request.query_params),
    }
    if method in ("POST", "PUT", "PATCH"):
        req_kwargs["content"] = await request.body()

    try:
        upstream_req = http_client.build_request(**req_kwargs)
        upstream_resp = await http_client.send(upstream_req, stream=True)
    except httpx.HTTPError:
        logger.exception(
            "Upstream HTTP error proxying %s/%s -> %s",
            context_name,
            server_name,
            config.url,
        )
        return JSONResponse(
            {"error": "upstream MCP server unreachable"}, status_code=502
        )

    response_headers = {
        k: v
        for k, v in upstream_resp.headers.items()
        if k.lower() not in _STRIP_OUTBOUND
    }

    async def body_stream() -> Any:
        try:
            async for chunk in upstream_resp.aiter_raw():
                yield chunk
        finally:
            await upstream_resp.aclose()

    return StreamingResponse(
        body_stream(),
        status_code=upstream_resp.status_code,
        headers=response_headers,
        media_type=upstream_resp.headers.get("content-type"),
    )


# -----------------------------------------------------------------------
# McpProxy — lifecycle wrapper
# -----------------------------------------------------------------------

class McpProxy:
    """Manages the MCP proxy HTTP server and backing stdio processes."""

    def __init__(self) -> None:
        self._registry = ProxyRegistry()
        self._stdio_manager = StdioManager()
        self._port: int | None = None
        self._server: uvicorn.Server | None = None
        self._serve_task: asyncio.Task[None] | None = None
        self._listen_socket: socket.socket | None = None
        self._http_client: httpx.AsyncClient | None = None

    @property
    def port(self) -> int:
        """The TCP port the proxy is listening on (set after ``start``)."""
        assert self._port is not None, "proxy not started"
        return self._port

    def register_context(
        self,
        context_name: str,
        servers: dict[str, StdioServerConfig] | None = None,
        http_servers: dict[str, HttpServerConfig] | None = None,
    ) -> str:
        """Register MCP servers for a context, return the auth token."""
        return self._registry.register_context(
            context_name, servers=servers, http_servers=http_servers
        )

    def register_tool_scope(
        self,
        *,
        context_name: str,
        chat_id: int,
        thread_id: int | None,
        user_id: int,
        tool_factory: Callable[[], list[OpenShrimpTool]],
    ) -> str:
        """Register a scope-bound OpenShrimp tools endpoint."""
        return self._registry.register_tool_scope(
            context_name=context_name,
            chat_id=chat_id,
            thread_id=thread_id,
            user_id=user_id,
            tool_factory=tool_factory,
        )

    async def unregister_context(self, context_name: str) -> None:
        """Unregister a context and stop its stdio processes."""
        self._registry.unregister_context(context_name)
        await self._stdio_manager.stop_context(context_name)

    def get_proxy_url(
        self,
        context_name: str,
        server_name: str,
        host_ip: str,
    ) -> str:
        """Build the URL a sandbox should use to reach a stdio-proxied server."""
        return f"http://{host_ip}:{self.port}/mcp/{context_name}/{server_name}"

    def get_http_proxy_url(
        self,
        context_name: str,
        server_name: str,
        host_ip: str,
    ) -> str:
        """Build the URL a sandbox should use to reach an HTTP-proxied server."""
        return f"http://{host_ip}:{self.port}/http/{context_name}/{server_name}"

    def get_tools_url(self, scope_token: str, host_ip: str) -> str:
        """Build the URL OpenCode should use for scope-bound OpenShrimp tools."""
        return f"http://{host_ip}:{self.port}/tools/{scope_token}"

    async def start(self) -> None:
        """Start the HTTP server on an OS-assigned port."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        sock.listen(128)
        self._port = sock.getsockname()[1]
        self._listen_socket = sock

        # read=None so SSE streams stay open indefinitely.
        self._http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=None, write=60.0, pool=10.0),
            follow_redirects=True,
        )

        app = _create_proxy_app(
            self._registry, self._stdio_manager, self._http_client
        )
        config = uvicorn.Config(
            app,
            log_level="warning",
            access_log=False,
            fd=sock.fileno(),
        )
        self._server = uvicorn.Server(config)

        self._serve_task = asyncio.create_task(
            self._server.serve(),
            name="mcp-proxy-server",
        )
        for _ in range(50):
            if self._server.started:
                break
            await asyncio.sleep(0.05)

        logger.info("MCP proxy listening on 127.0.0.1:%d", self._port)

    async def shutdown(self) -> None:
        """Stop the HTTP server and all stdio processes."""
        if self._server is not None:
            self._server.should_exit = True
        if self._serve_task is not None:
            try:
                await asyncio.wait_for(self._serve_task, timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._serve_task.cancel()
        if self._listen_socket is not None:
            self._listen_socket.close()
            self._listen_socket = None
        await self._stdio_manager.stop_all()
        if self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None
        logger.info("MCP proxy shut down")
