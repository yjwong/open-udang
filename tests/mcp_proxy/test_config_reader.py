from __future__ import annotations

from open_shrimp.config import ContextConfig
from open_shrimp.mcp_proxy.config_reader import (
    get_http_mcp_servers_for_directory,
    get_mcp_servers_for_directory,
)


def test_reads_opencode_mcp_schema_from_jsonc(monkeypatch, tmp_path) -> None:
    config_dir = tmp_path / "opencode"
    config_dir.mkdir()
    (config_dir / "opencode.jsonc").write_text(
        """
        {
          // OpenCode configs are commonly JSONC.
          "mcp": {
            "local": {
              "type": "local",
              "command": ["uvx", "server", "--flag"],
              "environment": {"TOKEN": "${MCP_TOKEN}"}
            },
            "remote": {
              "type": "remote",
              "url": "https://example.test/mcp",
              "headers": {"Authorization": "Bearer ${MCP_TOKEN}"}
            },
            "off": {
              "type": "local",
              "enabled": false,
              "command": ["ignored"]
            }
          }
        }
        """,
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENCODE_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("MCP_TOKEN", "secret")

    stdio = get_mcp_servers_for_directory("/tmp/project")
    http = get_http_mcp_servers_for_directory("/tmp/project")

    assert set(stdio) == {"local"}
    assert stdio["local"].command == "uvx"
    assert stdio["local"].args == ["server", "--flag"]
    assert stdio["local"].env == {"TOKEN": "secret"}
    assert set(http) == {"remote"}
    assert http["remote"].url == "https://example.test/mcp"
    assert http["remote"].headers == {"Authorization": "Bearer secret"}


def test_openshrimp_context_mcp_overrides_opencode_global(monkeypatch, tmp_path) -> None:
    config_dir = tmp_path / "opencode"
    config_dir.mkdir()
    (config_dir / "opencode.json").write_text(
        """
        {
          "mcp": {
            "shared": {
              "type": "local",
              "command": ["global-cmd", "global-arg"]
            },
            "figma": {
              "type": "remote",
              "url": "https://mcp.figma.com/mcp"
            }
          }
        }
        """,
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENCODE_CONFIG_DIR", str(config_dir))
    context = ContextConfig(
        directory="/tmp/project",
        description="Project",
        allowed_tools=[],
        mcp={
            "shared": {
                "command": "context-cmd",
                "args": ["context-arg"],
                "env": {"A": "1"},
            },
            "project-http": {
                "type": "http",
                "url": "https://project.example.test/mcp",
            },
        },
    )

    stdio = get_mcp_servers_for_directory(context.directory, context)
    http = get_http_mcp_servers_for_directory(context.directory, context)

    assert stdio["shared"].command == "context-cmd"
    assert stdio["shared"].args == ["context-arg"]
    assert stdio["shared"].env == {"A": "1"}
    assert set(http) == {"figma", "project-http"}
