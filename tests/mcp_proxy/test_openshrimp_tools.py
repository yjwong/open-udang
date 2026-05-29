from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from open_shrimp.mcp_proxy.registry import ProxyRegistry
from open_shrimp.mcp_proxy.server import _create_proxy_app
from open_shrimp.mcp_proxy.stdio_manager import StdioManager

pytestmark = pytest.mark.asyncio


class FakeBot:
    def __init__(self) -> None:
        self.documents = []
        self.topic_edits = []

    async def send_document(self, **kwargs):
        self.documents.append(kwargs)

    async def send_photo(self, **kwargs):
        self.documents.append(kwargs)

    async def get_forum_topic_icon_stickers(self):
        return [SimpleNamespace(emoji="📝", custom_emoji_id="emoji-id")]

    async def edit_forum_topic(self, **kwargs):
        self.topic_edits.append(kwargs)


async def _client(registry: ProxyRegistry):
    http_client = httpx.AsyncClient()
    app = _create_proxy_app(registry, StdioManager(), http_client)
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://testserver"), http_client


async def _rpc(client: httpx.AsyncClient, token: str, method: str, params=None):
    response = await client.post(
        f"/tools/{token}",
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
    )
    assert response.status_code == 200
    return response.json()["result"]


async def test_tools_list_private_chat_excludes_edit_topic() -> None:
    registry = ProxyRegistry()
    token = registry.register_tool_scope(
        context_name="default",
        chat_id=1,
        thread_id=None,
        user_id=10,
        is_private_chat=True,
        bot=FakeBot(),
        db=object(),
        config=SimpleNamespace(default_context="default"),
        job_queue=object(),
    )
    client, backing_client = await _client(registry)
    try:
        result = await _rpc(client, token, "tools/list")
    finally:
        await client.aclose()
        await backing_client.aclose()

    names = {tool["name"] for tool in result["tools"]}
    assert "send_file" in names
    assert "edit_topic" not in names
    assert {"create_schedule", "list_schedules", "delete_schedule"} <= names


async def test_edit_topic_uses_registered_scope() -> None:
    bot = FakeBot()
    registry = ProxyRegistry()
    token = registry.register_tool_scope(
        context_name="default",
        chat_id=123,
        thread_id=456,
        user_id=10,
        is_private_chat=False,
        bot=bot,
    )
    client, backing_client = await _client(registry)
    try:
        result = await _rpc(
            client,
            token,
            "tools/call",
            {"name": "edit_topic", "arguments": {"title": "New Title", "icon": "📝"}},
        )
    finally:
        await client.aclose()
        await backing_client.aclose()

    assert result["content"][0]["text"].startswith("Topic updated")
    assert bot.topic_edits == [
        {
            "chat_id": 123,
            "message_thread_id": 456,
            "name": "New Title",
            "icon_custom_emoji_id": "emoji-id",
        }
    ]


async def test_send_file_missing_path_returns_tool_error() -> None:
    registry = ProxyRegistry()
    token = registry.register_tool_scope(
        context_name="default",
        chat_id=1,
        thread_id=None,
        user_id=10,
        is_private_chat=True,
        bot=FakeBot(),
    )
    client, backing_client = await _client(registry)
    try:
        result = await _rpc(
            client,
            token,
            "tools/call",
            {"name": "send_file", "arguments": {"file_path": "/nope"}},
        )
        invalid = await client.post(
            "/tools/not-a-token",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )
    finally:
        await client.aclose()
        await backing_client.aclose()

    assert result["is_error"] is True
    assert "File not found" in result["content"][0]["text"]
    assert invalid.status_code == 404
