---
title: MCP Servers
description: Manage Model Context Protocol servers that extend the agent's capabilities.
sidebar:
  order: 11
---

MCP (Model Context Protocol) servers extend the agent's capabilities with additional tools. OpenShrimp exposes its own MCP tools and can merge user MCP servers from OpenCode global config plus per-context `mcp:` entries in `config.yaml`.

## How MCP servers work

MCP servers are external processes that provide tools to the agent via the Model Context Protocol. Examples include:

- GitHub integration (create PRs, read issues)
- Slack messaging
- Database access
- Custom project-specific tools

The agent discovers available tools from connected MCP servers and can call them during conversations.

## Viewing MCP servers

List all configured MCP servers and their status:

```
/mcp
```

Each server shows:

- **Name** — the server identifier
- **Status** — connection state with emoji indicators:
  - Connected and operational
  - Warning (partial issues)
  - Disconnected or failed
- **Tool count** — number of tools the server provides
- **Version** — server version info

## Managing servers

### Reset a server

If a server has disconnected or is in an error state, reconnect it:

```
/mcp reset github
```

This terminates the existing connection and starts a fresh one.

### Disable a server

Temporarily disable a server without removing its configuration:

```
/mcp disable slack
```

Disabled servers don't start on new sessions.

### Enable a server

Re-enable a previously disabled server:

```
/mcp enable slack
```

## Configuration

MCP servers can be configured globally in OpenCode or per context in OpenShrimp's `config.yaml`:

```yaml
contexts:
  myproject:
    directory: /home/you/Documents/myproject
    description: "My project"
    allowed_tools:
      - LSP
    mcp:
      github:
        type: local
        command:
          - npx
          - -y
          - '@modelcontextprotocol/server-github'
        enabled: true
```

The servers available depend on which context you're in. Use `/clear` after changing MCP config so the next session starts with the updated tool list.

## Built-in MCP tools

OpenShrimp registers its own MCP server (`openshrimp`) that provides:

| Tool | Description |
|------|-------------|
| `openshrimp_send_file` | Send files to Telegram (photos, documents) |
| `openshrimp_edit_topic` | Set forum topic title and icon (forum topics only) |
| `openshrimp_create_schedule` | Create a scheduled task |
| `openshrimp_list_schedules` | List scheduled tasks |
| `openshrimp_delete_schedule` | Delete a scheduled task |
| `openshrimp_computer_screenshot` | Take a screenshot (computer-use contexts only) |
| `openshrimp_computer_click` | Click at coordinates (computer-use contexts only) |
| `openshrimp_computer_type` | Type text (computer-use contexts only) |
| `openshrimp_computer_key` | Press keys (computer-use contexts only) |
| `openshrimp_computer_scroll` | Scroll (computer-use contexts only) |
| `openshrimp_computer_toplevel` | Focus a window (computer-use contexts only) |

These tools are registered automatically based on your context configuration.

## Troubleshooting

### Server won't connect

1. Check that the server process is available in the PATH
2. Verify the server configuration in OpenCode global config or the context's `mcp:` config
3. Try `/mcp reset <name>` to force a reconnection
4. Check OpenShrimp logs for error details

### Tools not appearing

MCP tools are discovered when a session starts. If you added a new server, use `/clear` to start a fresh session and pick up the new tools.
