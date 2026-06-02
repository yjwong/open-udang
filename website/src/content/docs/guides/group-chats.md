---
title: Group Chats
description: Using OpenShrimp in group chats and forum topics.
sidebar:
  order: 9
---

OpenShrimp works in group chats and forum topics, with each conversation scope getting its own independent session.

## Group chats

In regular group chats, the bot responds to:

- **@mentions** — messages that mention the bot
- **Replies** — messages that reply to the bot's messages

The bot ignores other messages in the group to avoid noise.

## Forum topics

Telegram forum chats (groups with topics enabled) get special treatment:

- **Each topic is independent** — separate context, session, conversation history, and approval state
- **No @mention needed** — the bot responds to all messages in forum topics
- **Topic titles** — the agent can set descriptive topic titles with emoji icons via the `openshrimp_edit_topic` MCP tool

This means you can have multiple conversations running in different forum topics, each working on a different project.

### openshrimp_edit_topic tool

In forum topics, an `openshrimp_edit_topic` MCP tool is automatically registered. The agent can use it to set a concise title (max 128 characters) and an optional icon emoji. This happens automatically — the agent will typically set the topic title based on what you're working on.

## Default and locked contexts

### Default context for a group

Set a context that's auto-selected when someone first uses the bot in a specific group:

```yaml
contexts:
  team-project:
    directory: /home/you/Documents/team-project
    description: "Team project"
    allowed_tools:
      - LSP
    default_for_chats:
      - -1001234567890  # the group chat ID
```

### Lock a group to a context

Prevent users from switching contexts in a group:

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

When locked, `/context` shows the current context but doesn't allow switching.

## Authorization

Every user in the group must be in the `allowed_users` list to interact with the bot. Messages from unauthorized users are silently ignored.

```yaml
allowed_users:
  - 123456789   # you
  - 987654321   # your teammate
```

## Finding your group chat ID

The easiest way to find a group chat ID is to add the bot to the group and check the bot's logs — the chat ID appears in log messages when anyone sends a message. Group chat IDs are negative numbers (e.g. `-1001234567890`).

## Per-topic sessions in forum chats

In forum chats, OpenShrimp uses a `(chat_id, thread_id)` pair to identify each conversation scope. This means:

- Topic A can be working on the `frontend` context
- Topic B can be working on the `backend` context
- Each has its own session, approval state, and model override
- `/clear` in one topic doesn't affect others
