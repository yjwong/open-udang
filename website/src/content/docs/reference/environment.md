---
title: Environment Variables
description: Environment variables used by OpenShrimp.
---

OpenShrimp itself needs very few environment variables. Model-provider credentials are handled by OpenCode.

## Provider credentials

The recommended way to connect model providers is to start the bot and send `/connect` in a private Telegram chat. This opens OpenCode's provider connection flow and stores credentials where OpenCode expects them.

You can also set provider-specific API keys in the OpenShrimp process environment when your chosen OpenCode provider supports them. Common examples include:

| Variable | Used for |
|----------|----------|
| `OPENAI_API_KEY` | OpenAI models, such as `openai/gpt-5.5` |
| `ANTHROPIC_API_KEY` | Anthropic models, when using OpenCode's Anthropic provider |

For other providers, use the environment variable names documented by that OpenCode provider.

Example:

```bash
export OPENAI_API_KEY="sk-..."
```

:::tip
For systemd services, set provider keys in the unit file's `Environment=` directive or use `EnvironmentFile=` to load them from a protected file. See [systemd deployment](/deployment/systemd/) for details.
:::

## Internal variables

These are used internally by OpenShrimp and generally don't need to be set manually.

| Variable | Description |
|----------|-------------|
| `OPENSHRIMP_RESTART_CHAT_ID` | Chat ID for post-restart confirmation (set by `/restart`) |
| `OPENSHRIMP_RESTART_THREAD_ID` | Thread ID for post-restart confirmation (set by `/restart`) |
