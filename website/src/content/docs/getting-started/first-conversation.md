---
title: First Conversation
description: Send your first message and understand the basics.
sidebar:
  order: 4
---

## Send a message

With the bot running from [Installation](/getting-started/installation/), open Telegram and send any message to your bot. For example:

> What files are in this project?

The bot will:

1. Start an OpenCode session in your default context's working directory
2. Stream the response back to Telegram as it's generated
3. The agent may call tools (`read`, `glob`, `grep`, `bash`, etc.) to explore your codebase

## Tool approval

When the agent wants to use a tool that isn't auto-approved, you'll see an inline keyboard with options:

- **Allow** — approve this specific tool call
- **Deny** — reject the tool call (the agent will try a different approach)
- **Accept all `<prefix>`** — auto-approve all future calls matching this prefix for the session (e.g. "Accept all `git`" for bash commands)
- **Accept all `<tool>`** — auto-approve all future calls of this tool type for the session

For edit and write tools within your project directory, you'll also see:

- **Accept all edits** — auto-approve all future edit/write calls within the context directory for the session

:::note
Session approvals are cleared when you use `/clear` or switch contexts.
:::

## Multi-turn conversation

The bot maintains a persistent OpenCode session. Follow-up messages continue the same conversation, so the agent keeps the full context of what you've discussed.

To start fresh:

```
/clear
```

## Useful commands

| Command | What it does |
|---------|-------------|
| `/status` | Show current context, model, session, and running state |
| `/context` | List available contexts or switch to a different one |
| `/cancel` | Abort a running agent task |
| `/model openai/gpt-5.5` | Switch to a different model for this session |

See the full [Commands reference](/reference/commands/) for all available commands.

## Forum topics

If your bot is in a Telegram group with forum topics enabled, each topic gets its own independent session — separate context, conversation history, and approval state. The bot responds to all messages in forum topics without needing @mentions.

## Next steps

You're up and running! Explore the [Guides](/guides/contexts/) to learn about multi-project contexts, sandboxed execution, scheduled tasks, and more.
