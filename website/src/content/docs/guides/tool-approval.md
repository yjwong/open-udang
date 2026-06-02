---
title: Tool Approval
description: How OpenShrimp controls which tools the agent can use and when it asks for permission.
sidebar:
  order: 2
---

OpenShrimp gives you fine-grained control over what the agent can do. Every tool call is either auto-approved or sent to you for explicit approval via Telegram inline keyboard.

## How it works

Tool approval has three layers:

1. **`allowed_tools`** — tools listed here are always approved, no questions asked
2. **Path-scoped auto-approval** — read-only tools within the project directory are auto-approved
3. **Interactive approval** — everything else goes through Telegram

## allowed_tools

Tools in the `allowed_tools` list become OpenCode permission rules and never prompt for approval:

```yaml
contexts:
  myproject:
    allowed_tools:
      - LSP
      - "bash(git *)"       # all git commands
      - "bash(npm test)"    # specific command
      - "bash(make *)"      # all make targets
```

Patterns use glob matching — `*` matches any characters.

:::caution
Adding `read`, `write`, `edit`, `glob`, or `grep` to `allowed_tools` bypasses path checking entirely. The agent can then access any file the process can read, including `~/.ssh/`, config secrets, etc.
:::

## Path-scoped auto-approval

For tools **not** in `allowed_tools`, OpenShrimp applies path-scoped rules:

| Tool | Within project directory | Outside project directory |
|------|------------------------|--------------------------|
| `read`, `glob`, `grep` | Auto-approved | Requires approval |
| `edit`, `write`, `apply_patch` | Requires approval | Requires approval |
| `bash` | Requires approval | Requires approval |

The "project directory" includes the context's `directory` and any `additional_directories`.

## Interactive approval

When a tool needs approval, you see an inline keyboard in Telegram with these options:

### For most tools

- **Allow** — approve this specific call
- **Deny** — reject it (the agent will try a different approach)
- **Accept all `<tool>`** — auto-approve all future calls of this tool type for the session

### For bash commands

- **Allow** — approve this specific command
- **Deny** — reject it
- **Accept all `<prefix>`** — approve all commands starting with this prefix (e.g. "Accept all `git`" creates a `git *` pattern)
- **Accept all bash** — approve all bash commands for the session

:::note
Compound bash commands (using `&&`, `||`, `;`, or pipes) cannot match prefix patterns for safety. They can only be approved individually or via blanket "Accept all bash".
:::

### For edit/write within the project

- **Allow** / **Deny** — as above
- **Accept all edits** — auto-approve all future edit and write calls within the context directory for the session

## Session-scoped rules

All approval rules created during a session (via "Accept all..." buttons) are cleared when you:

- Use `/clear` to start a fresh session
- Switch to a different context with `/context`

This ensures you consciously re-approve tools each session.

## Sandbox auto-approval

When a context has a [sandbox](/guides/docker-sandbox/) configured, all bash commands and path-scoped tools are **automatically approved** — the sandbox provides the safety boundary instead of manual approval.

```yaml
contexts:
  sandboxed:
    directory: /home/you/Documents/project
    description: "Sandboxed project"
    allowed_tools:
      - LSP
    sandbox:
      backend: docker
```

## Dangerous operation blocking

OpenShrimp blocks certain dangerous operations regardless of approval state:

- `rm` or `rmdir` targeting `/`, the home directory, or top-level directories
- Dangerous glob patterns like `/*` or `*` in destructive commands
- Shell expansion characters (`$`, backticks, `~`, `%`) in file paths for write operations
- Glob patterns in write operation paths

These are never auto-approved. They always fall through to the interactive Telegram approval prompt, where you can still manually approve them if needed.
