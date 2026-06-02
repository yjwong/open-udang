---
title: Contexts
description: Manage multiple projects with separate working directories, models, and sessions.
sidebar:
  order: 1
---

Contexts let you work on multiple projects through the same bot. Each context defines a working directory, allowed tools, and optionally a model override or sandbox. Switch between them with `/context`.

## Defining contexts

Add contexts to your `config.yaml`:

```yaml
contexts:
  frontend:
    directory: /home/you/Documents/frontend
    description: "React app"
    allowed_tools:
      - LSP
      - "bash(npm *)"

  backend:
    directory: /home/you/Documents/backend
    description: "API server"
    allowed_tools:
      - LSP
      - "bash(go *)"
    model: openai/gpt-5.5

  docs:
    directory: /home/you/Documents/docs
    description: "Documentation site"
    allowed_tools:
      - LSP

default_context: frontend
```

### Required fields

| Field | Description |
|-------|-------------|
| `directory` | Absolute path to the project directory |
| `description` | Short description shown when listing contexts |
| `allowed_tools` | Tools auto-approved without prompting |

### Optional fields

| Field | Description |
|-------|-------------|
| `model` | Override the model for this context in OpenCode provider/model form, such as `openai/gpt-5.5` |
| `additional_directories` | Extra directories the agent can access (path-scoped approval extends to these) |
| `default_for_chats` | Chat IDs where this context is auto-selected on first use |
| `locked_for_chats` | Chat IDs locked to this context — users cannot switch away |
| `sandbox` | Run the agent in an isolated environment (see sandbox guides) |

## Switching contexts

List available contexts:

```
/context
```

The current context is marked with a bullet. Switch to a different one:

```
/context backend
```

When you switch contexts, the bot starts a fresh session in the new working directory. If a previous session exists for that context, you'll be asked whether to resume it or start fresh.

## Additional directories

Give the agent access to directories outside the main project:

```yaml
contexts:
  frontend:
    directory: /home/you/Documents/frontend
    description: "React app"
    allowed_tools:
      - LSP
    additional_directories:
      - /home/you/Documents/shared-lib
      - /home/you/Documents/api-types
```

OpenShrimp grants the agent access to these directories and extends path-scoped auto-approval for `read`, `glob`, and `grep` to them.

## Default and locked contexts

### Default context

```yaml
default_context: frontend
```

The context used when no context has been selected. Must match a key in `contexts`.

### Default for specific chats

```yaml
contexts:
  team-project:
    directory: /home/you/Documents/team-project
    description: "Team project"
    allowed_tools:
      - LSP
    default_for_chats:
      - -1001234567890  # group chat ID
```

When someone in that group chat uses the bot for the first time, this context is auto-selected instead of the global default.

### Locked contexts

```yaml
contexts:
  production:
    directory: /home/you/Documents/production
    description: "Production codebase"
    allowed_tools:
      - LSP
    locked_for_chats:
      - -1001234567890
```

Users in the locked chat cannot switch to a different context. The `/context` command will show which context is active but won't allow changes.

## Model overrides

Each context can specify a default model:

```yaml
contexts:
  quick-tasks:
    directory: /home/you/Documents/scripts
    description: "Quick scripting tasks"
    allowed_tools:
      - LSP
    model: openai/gpt-5.5
```

Users can still override the model per-session with `/model openai/gpt-5.5`. The override is cleared on `/clear` or context switch.

## Per-context sessions

Each context maintains its own session history. When you switch contexts and come back, your previous conversation is still there. Use `/resume` to browse and resume past sessions for the current context.

OpenShrimp maps each `(chat, thread, context)` to an OpenCode session in its SQLite database. Host contexts use OpenCode's normal session storage; sandboxed contexts use per-context OpenCode storage managed by OpenShrimp.
