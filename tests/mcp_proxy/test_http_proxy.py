from __future__ import annotations

import httpx
import pytest

from open_shrimp.mcp_proxy.config_reader import HttpServerConfig
from open_shrimp.mcp_proxy.registry import ProxyRegistry
from open_shrimp.mcp_proxy.server import _create_proxy_app
from open_shrimp.mcp_proxy.stdio_manager import StdioManager

pytestmark = pytest.mark.asyncio


class AsyncBytes(httpx.AsyncByteStream):
    def __init__(self, content: bytes) -> None:
        self._content = content

    async def __aiter__(self):
        yield self._content


async def test_http_proxy_allows_static_authorization_header() -> None:
    seen_headers: dict[str, str] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen_headers.update(request.headers)
        return httpx.Response(
            200,
            stream=AsyncBytes(b'{"ok":true}'),
            headers={"content-type": "application/json"},
        )

    registry = ProxyRegistry()
    token = registry.register_context(
        "default",
        http_servers={
            "internal": HttpServerConfig(
                url="https://internal.example.test/mcp",
                transport="http",
                headers={"Authorization": "Bearer static-token"},
            )
        },
    )
    upstream_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    app = _create_proxy_app(registry, StdioManager(), upstream_client)
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    )

    try:
        response = await client.post(
            "/http/default/internal",
            headers={"Authorization": f"Bearer {token}"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )
    finally:
        await client.aclose()
        await upstream_client.aclose()

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert seen_headers["authorization"] == "Bearer static-token"
