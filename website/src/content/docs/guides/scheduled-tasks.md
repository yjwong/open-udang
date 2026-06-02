---
title: Scheduled Tasks
description: Set up cron-like recurring and one-shot agent prompts that run automatically.
sidebar:
  order: 7
---

Scheduled tasks let you set up recurring or one-shot agent prompts that run on a schedule. Describe what you want in natural language, and the agent creates the schedule using MCP tools.

## Creating a task

Just tell the bot what you want scheduled:

> Check the deployment status every 30 minutes and tell me if anything is failing.

The agent will call the `openshrimp_create_schedule` MCP tool to set it up. You'll get a confirmation with the task name, schedule, and context.

You can also be more specific:

> Every weekday at 9am, summarize the git log from the last 24 hours.

> In 2 hours, remind me to review the PR.

## Schedule types

### Interval

Runs repeatedly at a fixed interval:

- `30m` — every 30 minutes
- `1h` — every hour
- `2d` — every 2 days
- `90s` — every 90 seconds (but see minimum below)

**Minimum interval: 5 minutes.** Anything shorter is rejected.

### Cron

Standard 5-field cron expressions:

- `0 9 * * 1-5` — weekdays at 9:00 AM
- `*/30 * * * *` — every 30 minutes
- `0 0 1 * *` — first of each month at midnight

The minimum gap between fire times must be at least 5 minutes.

### One-shot

Runs once at a specific time, then auto-deletes:

- ISO 8601 format: `2026-03-21T09:00:00`
- Times are in UTC (naive datetimes treated as UTC)
- Skipped if the time is already past when the bot starts

## What tasks can do

Scheduled tasks run in isolated sessions with read-only tools:

- **read, glob, grep** — explore the codebase
- **webfetch** — access the web
- **bash** — only if the context has a sandbox enabled

Tasks cannot use mutating tools (`edit`, `write`, `apply_patch`) or interactive approval — they run unattended.

## Limits and safety

| Constraint | Value |
|-----------|-------|
| Minimum interval | 5 minutes |
| Max tasks per chat | 20 |
| Max concurrent executions | 3 (global) |
| Default timeout per task | 10 minutes |
| Max instances of same task | 1 (skipped if still running) |

If a task exceeds its timeout, it's cancelled and you get a notification with the duration. If it throws an error, you get the error message (first 200 characters).

## Managing tasks

### List tasks

```
/schedule
```

Shows all scheduled tasks for the current chat with their name, schedule, context, timeout, and prompt preview.

### Delete a task

Ask the agent:

> Delete the deployment check schedule.

Or use the command directly:

```
/schedule delete deployment-check
```

### Task output

When a scheduled task runs, its output is sent to the chat (or forum topic) where it was created. Each execution gets its own isolated session — it doesn't share context with your interactive conversations.

## Failure notifications

You'll be notified in the chat when a task fails:

- **Timeout** — "Task timed out after 10 minutes"
- **Error** — "Task failed: \<error message\>"
- **Missing context** — "Context no longer exists"

## One-shot tasks

One-shot tasks run once and auto-delete. They're useful for reminders or deferred actions:

> In 30 minutes, check if the CI pipeline has finished and report the result.

If the bot restarts after the scheduled time has passed, stale one-shot tasks are cleaned up automatically.
