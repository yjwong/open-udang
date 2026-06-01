from __future__ import annotations

import pytest

from open_shrimp.opencode_client import OpenCodeClient, OpenCodeOptions
from open_shrimp.opencode_client.errors import ProcessError

pytestmark = pytest.mark.asyncio


async def test_mcp_servers_registered_with_opencode(mock_server, wired_server) -> None:
    opts = OpenCodeOptions(
        cwd="/tmp/project",
        provider="openai",
        model="gpt-test",
        mcp_servers={
            "openshrimp": {
                "type": "remote",
                "url": "http://127.0.0.1:1234/tools/token",
                "oauth": False,
            }
        },
    )

    async with OpenCodeClient(opts):
        pass

    assert mock_server.mcp_registrations == [
        {
            "params": {"directory": "/tmp/project"},
            "body": {
                "name": "openshrimp",
                "config": {
                    "type": "remote",
                    "url": "http://127.0.0.1:1234/tools/token",
                    "oauth": False,
                },
            },
        }
    ]


async def test_stdio_mcp_config_converted_to_local(mock_server, wired_server) -> None:
    opts = OpenCodeOptions(
        cwd="/tmp/project",
        provider="openai",
        model="gpt-test",
        mcp_servers={"local": {"command": "npx", "args": ["server"], "env": {"A": 1}}},
    )

    async with OpenCodeClient(opts):
        pass

    assert mock_server.mcp_registrations[0]["body"] == {
        "name": "local",
        "config": {
            "type": "local",
            "command": ["npx", "server"],
            "environment": {"A": "1"},
        },
    }


async def test_bad_mcp_config_raises_before_session(mock_server, wired_server) -> None:
    opts = OpenCodeOptions(
        cwd="/tmp/project",
        provider="openai",
        model="gpt-test",
        mcp_servers={"bad": {"type": "remote"}},
    )

    with pytest.raises(ValueError, match="requires url"):
        async with OpenCodeClient(opts):
            pass
    assert mock_server.created_sessions == []


async def test_get_mcp_status_normalizes_opencode_map(mock_server, wired_server) -> None:
    mock_server.mcp_status = {"demo": {"status": "disabled"}}
    opts = OpenCodeOptions(cwd="/tmp/project", provider="openai", model="gpt-test")

    async with OpenCodeClient(opts) as client:
        status = await client.get_mcp_status()

    assert status == {"mcpServers": [{"name": "demo", "status": "disabled"}]}


async def test_get_mcp_status_preserves_error_and_tools(mock_server, wired_server) -> None:
    mock_server.mcp_status = {
        "bad": {
            "status": "failed",
            "error": "MCP error -32000: Connection closed",
            "tools": [{"name": "inspect"}],
        }
    }
    opts = OpenCodeOptions(cwd="/tmp/project", provider="openai", model="gpt-test")

    async with OpenCodeClient(opts) as client:
        status = await client.get_mcp_status()

    assert status == {
        "mcpServers": [
            {
                "name": "bad",
                "status": "failed",
                "error": "MCP error -32000: Connection closed",
                "tools": [{"name": "inspect"}],
            }
        ]
    }


async def test_reconnect_mcp_server_posts_connect(mock_server, wired_server) -> None:
    mock_server.mcp_status = {"demo": {"status": "disabled"}}
    opts = OpenCodeOptions(cwd="/tmp/project", provider="openai", model="gpt-test")

    async with OpenCodeClient(opts) as client:
        await client.reconnect_mcp_server("demo")

    assert mock_server.mcp_connects == [
        {"name": "demo", "params": {"directory": "/tmp/project"}, "raw_path": "/mcp/demo/connect"}
    ]


async def test_toggle_mcp_server_connects_or_disconnects(mock_server, wired_server) -> None:
    mock_server.mcp_status = {"demo": {"status": "disabled"}}
    opts = OpenCodeOptions(cwd="/tmp/project", provider="openai", model="gpt-test")

    async with OpenCodeClient(opts) as client:
        await client.toggle_mcp_server("demo", enabled=True)
        await client.toggle_mcp_server("demo", enabled=False)

    assert [call["name"] for call in mock_server.mcp_connects] == ["demo"]
    assert [call["name"] for call in mock_server.mcp_disconnects] == ["demo"]


async def test_mcp_server_names_are_url_quoted(mock_server, wired_server) -> None:
    name = "demo/server with spaces"
    mock_server.mcp_status = {name: {"status": "disabled"}}
    opts = OpenCodeOptions(cwd="/tmp/project", provider="openai", model="gpt-test")

    async with OpenCodeClient(opts) as client:
        await client.reconnect_mcp_server(name)

    assert mock_server.mcp_connects[0]["name"] == name
    assert mock_server.mcp_connects[0]["raw_path"] == "/mcp/demo%2Fserver%20with%20spaces/connect"


async def test_mcp_missing_server_raises_process_error(mock_server, wired_server) -> None:
    opts = OpenCodeOptions(cwd="/tmp/project", provider="openai", model="gpt-test")

    async with OpenCodeClient(opts) as client:
        with pytest.raises(ProcessError, match="McpServerNotFoundError"):
            await client.reconnect_mcp_server("missing")
