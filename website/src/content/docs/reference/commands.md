---
title: Commands
description: All bot commands with syntax and examples.
---

## Session commands

### `/context [name]`

List available contexts or switch to a different one.

- **No arguments** — shows a paginated list of contexts. The current context is marked with a bullet.
- **With name** — switches to the named context. If a session exists for that context, you're prompted to resume or start fresh.

```
/context            # list contexts
/context myproject  # switch to myproject
```

:::note
If a context is locked to a chat via `locked_for_chats`, switching is not allowed in that chat.
:::

### `/clear`

Start a completely fresh session. Clears all state: session history, model overrides, edit approvals, and cancels any running tasks.

```
/clear
```

### `/model [name|reset]`

Show or change the model for the current session.

- **No arguments** — shows the current model and any active override.
- **With name** — sets a session-scoped model override. The current session is closed; the next message starts a new session with the new model.
- **`reset`** — clears the override and reverts to the context's default model.

```
/model          # show current model
/model openai/gpt-5.5  # switch to GPT-5.5 via OpenAI
/model reset    # revert to default
```

Models must use OpenCode's provider/model form, such as `openai/gpt-5.5` or `anthropic/claude-sonnet-4-6`.

### `/resume [session_id]`

List recent sessions or resume a specific one.

- **No arguments** — shows a paginated list of recent sessions for the current context, with metadata (title, summary, creation time, size).
- **With ID or prefix** — resumes the specified session.

```
/resume                      # list recent sessions
/resume abc123               # resume by ID prefix
```

## Task commands

### `/cancel`

Abort the currently running agent task. Also clears any queued setup messages.

```
/cancel
```

### `/tasks`

List active background tasks with their elapsed time and description.

```
/tasks
```

### `/schedule`

List and manage scheduled tasks. Scheduled tasks are cron-like recurring or one-shot agent prompts that run automatically.

```
/schedule
```

:::tip
To create a scheduled task, just describe it in natural language to the bot:

> Check the deployment status every 30 minutes and tell me if anything is failing.

The agent will use the scheduling MCP tools to set it up.
:::

## Information commands

### `/status`

Show current state: context name and directory, active model, session ID, running status, and any active background tasks.

```
/status
```

## Mini App commands

### `/review`

Open the Review Mini App — a web-based diff viewer for the current context's working directory. Shows one button per directory if the context has additional directories.

Requires `review.public_url` or `review.tunnel` to be configured.

```
/review
```

### `/vnc`

Open the VNC viewer Mini App for computer-use contexts. Only available when the context has `computer_use: true` and the sandbox is running with a desktop environment.

```
/vnc
```

### `/connect [provider]`

Open the provider connection Mini App. Used to add or refresh OpenCode model-provider credentials.

```
/connect
/connect openai
```

## Server commands

### `/mcp [subcommand] [server-name]`

List and manage MCP (Model Context Protocol) servers.

- **No arguments** — lists all MCP servers with their connection status, tool counts, and version info.
- **`reset <name>`** — reconnect a failed MCP server.
- **`enable <name>`** — enable a disabled MCP server.
- **`disable <name>`** — disable an MCP server.

```
/mcp                # list servers
/mcp reset github   # reconnect the github server
/mcp disable slack  # disable the slack server
```

### `/restart`

Restart the OpenShrimp process. The bot sends a confirmation message after it comes back online.

```
/restart
```
