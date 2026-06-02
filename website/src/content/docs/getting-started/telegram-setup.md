---
title: Telegram Setup
description: Create a Telegram bot and find your user ID.
sidebar:
  order: 2
---

## Create a bot with BotFather

1. Open Telegram and search for [@BotFather](https://t.me/BotFather)
2. Send `/newbot`
3. Choose a display name (e.g. "My OpenShrimp")
4. Choose a username ending in `bot` (e.g. `my_agent_bot`)
5. Copy the **bot token** — it looks like `123456789:ABCdefGHIjklMNOpqrsTUVwxyz`

:::tip
Keep your bot token secret. Anyone with it can control your bot.
:::

### Recommended bot settings

While you're in BotFather, configure these:

- **Threaded Mode** — Settings → Bot Settings → Threads Settings → **Threaded Mode**, then turn it on. This is what unlocks parallel conversations (see below). Strongly recommended.
- **Privacy** — `/setprivacy` and set to **Disable**. Lets the bot see every message in groups, which is required for forum topic support and for group chats to work without @mentions.

## Run conversations in parallel with Threaded Mode (strongly recommended)

OpenShrimp comes alive when you can run more than one conversation at a time, and **Threaded Mode** is what makes that possible — even in a 1-on-1 private chat with the bot.

With Threaded Mode enabled in BotFather, your private chat with the bot can hold many separate threads. Each thread is an independent conversation with its own context, working directory, OpenCode session, and tool-approval state. Think one thread per project, per task, or per investigation.

Why this matters: without threads, every message lands in the same agent session, so a long-running task blocks anything else you'd want to do. With threads, the agent can refactor one repo while you ask it questions about another — neither conversation interferes with the other.

The same model extends to **forum groups** (a group with Topics enabled): each topic is a separate thread, and the bot responds to every message inside a topic without needing an @mention. Use a forum group if you want to share a workspace with other allowed users; otherwise a private chat with Threaded Mode is the simplest setup.

## Find your Telegram user ID

OpenShrimp only responds to users in the `allowed_users` list. To find your user ID:

1. Open Telegram and search for [@userinfobot](https://t.me/userinfobot)
2. Send any message
3. It replies with your numeric user ID (e.g. `123456789`)

Add this number to your config file's `allowed_users` list.

## Next steps

Now configure the bot — see [Configuration](/getting-started/configuration/).
