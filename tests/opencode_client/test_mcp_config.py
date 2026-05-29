from __future__ import annotations

import pytest

from open_shrimp.opencode_client import OpenCodeClient, OpenCodeOptions

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
