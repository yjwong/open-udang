from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from open_shrimp.mcp_proxy.registry import ProxyRegistry
from open_shrimp.mcp_proxy.server import _create_proxy_app
from open_shrimp.mcp_proxy.stdio_manager import StdioManager
from open_shrimp.tools import create_openshrimp_tools

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


class FakeSandbox:
    def __init__(self, screenshots_dir) -> None:
        self.screenshots_dir = screenshots_dir
        self.clicked = None

    def get_screenshots_dir(self):
        return self.screenshots_dir

    def take_screenshot(self, output_path):
        output_path.write_bytes(b"png")

    def send_click(self, x, y, button="left"):
        self.clicked = (x, y, button)


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


def _register_tools(
    registry: ProxyRegistry,
    *,
    context_name: str = "default",
    chat_id: int = 1,
    thread_id: int | None = None,
    user_id: int = 10,
    is_private_chat: bool = True,
    bot=None,
    db=None,
    config=None,
    job_queue=None,
    sandbox=None,
) -> str:
    bot = bot or FakeBot()

    def tool_factory():
        return create_openshrimp_tools(
            bot=bot,
            chat_id=chat_id,
            thread_id=thread_id,
            db=db,
            config=config,
            job_queue=job_queue,
            context_name=context_name,
            user_id=user_id,
            is_private_chat=is_private_chat,
            include_sandbox_tools=sandbox is not None,
            sandbox=sandbox,
        )

    return registry.register_tool_scope(
        context_name=context_name,
        chat_id=chat_id,
        thread_id=thread_id,
        user_id=user_id,
        tool_factory=tool_factory,
    )


async def test_tools_list_private_chat_excludes_edit_topic() -> None:
    registry = ProxyRegistry()
    token = _register_tools(
        registry,
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
    token = _register_tools(
        registry,
        chat_id=123,
        thread_id=456,
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
    token = _register_tools(registry)
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


async def test_sandbox_computer_tools_are_scope_bound(tmp_path) -> None:
    sandbox = FakeSandbox(tmp_path)
    registry = ProxyRegistry()
    token = _register_tools(
        registry,
        sandbox=sandbox,
    )
    client, backing_client = await _client(registry)
    try:
        listed = await _rpc(client, token, "tools/list")
        shot = await _rpc(
            client,
            token,
            "tools/call",
            {"name": "computer_screenshot", "arguments": {}},
        )
        click = await _rpc(
            client,
            token,
            "tools/call",
            {"name": "computer_click", "arguments": {"x": 12, "y": 34}},
        )
    finally:
        await client.aclose()
        await backing_client.aclose()

    names = {tool["name"] for tool in listed["tools"]}
    assert "computer_screenshot" in names
    assert "Screenshot saved" in shot["content"][0]["text"]
    assert click["content"][0]["text"] == "Click sent."
    assert sandbox.clicked == (12, 34, "left")
