# OpenUdang

Telegram bot for remote Claude access via the Agent SDK. A personal, self-hosted alternative to OpenClaw.

## Project Overview

- **PRD**: `docs/prd.md` - full requirements, architecture, feasibility assessment
- **Language**: Python 3.11+, managed with `uv`
- **Key deps**: `claude-agent-sdk`, `python-telegram-bot[httpx]`, `aiosqlite`, `pyyaml`

## Architecture

```
Telegram <-> OpenUdang (Python, async) <-> Claude Agent SDK
                |
                +-- Config (YAML: contexts, ACL)
                +-- Session store (SQLite: chat -> session_id mapping)
                +-- PreToolUse hooks (tool approval)
```

### Core Concepts

- **Context**: A working directory + CLAUDE.md. Switch with `/context <name>`. Each context has its own model, auto-approve list, and tools.
- **Session**: A persistent Claude conversation. The Agent SDK handles persistence as `.jsonl` files under `~/.claude/projects/<encoded-cwd>/`. OpenUdang maps `(chat_id, context_name) -> session_id` in SQLite.
- **Tool approval**: Uses the SDK's `allowedTools` for auto-approved tools (patterns like `Bash(git *)`) and a `canUseTool` callback for everything else. Read-only file tools (Read, Glob, Grep) are auto-approved within the context working directory. Mutating tools (Edit, Write) always require explicit approval via Telegram inline keyboard, even within cwd, unless the user opts into "accept all edits" for the session. The callback sends Telegram inline keyboards and `await`s the user's response.

### Key SDK Patterns

```python
# Multi-turn session
async with ClaudeSDKClient(options=options) as client:
    await client.query("prompt here")
    async for message in client.receive_response():
        if isinstance(message, AssistantMessage):
            # stream text to Telegram
        elif isinstance(message, ResultMessage):
            session_id = message.session_id  # save for resume

# Resume across restarts
options = ClaudeAgentOptions(resume=session_id, cwd="/path/to/context")

# canUseTool callback for tool approval (tools not in allowedTools)
async def can_use_tool(tool_name, tool_input, context):
    # Send Telegram inline keyboard, await callback
    approved = await wait_for_telegram_approval(tool_name, tool_input)
    if approved:
        return PermissionResultAllow()
    else:
        return PermissionResultDeny(message="User denied tool use.")
```

## Project Structure

```
src/open_udang/
    __init__.py
    main.py          # Entry point, arg parsing, config loading
    bot.py            # Telegram bot setup, handlers, long polling
    agent.py          # Claude Agent SDK wrapper, session management
    hooks.py          # canUseTool callback, tool approval logic
    stream.py         # Stream bridge: SDK messages -> sendMessageDraft
    config.py         # Config loading and validation (YAML)
    db.py             # SQLite session ID mapping
    markdown.py       # GFM -> Telegram MarkdownV2 conversion
```

## Config

Config lives at `~/.config/openudang/config.yaml`. See `config.example.yaml` for schema.

Key fields:
- `telegram.token` - Bot token from @BotFather
- `allowed_users` - List of Telegram user IDs (integers)
- `contexts` - Map of context name -> {directory, description, model, allowed_tools, default_for_chats}
- `default_context` - Context name to use when none is specified

`ANTHROPIC_API_KEY` is read from the environment, not the config file.

## Telegram API Notes

- **Streaming**: Use `sendMessageDraft` (Bot API 9.5). Not natively supported in `python-telegram-bot` v22.6 yet. Use raw API via `bot.do_api_request("sendMessageDraft", ...)` or direct `httpx` POST.
- **Long messages**: Telegram max is 4096 chars. Auto-split at paragraph/code block boundaries. Finalize current draft, start new one.
- **Group chats**: Only respond to @mentions and replies. Check `message.entities` for bot mention or `message.reply_to_message`.
- **Inline keyboards**: Use `InlineKeyboardMarkup` for tool approval buttons. Handle via `CallbackQueryHandler`.
- **Parse mode**: Use `MarkdownV2` parse mode. Escape special characters: `_*[]()~>#+-=|{}.!`

## Commands

| Command | Handler | Description |
|---------|---------|-------------|
| `/context` | `context_handler` | List or switch contexts |
| `/clear` | `clear_handler` | Fresh session in current context |
| `/status` | `status_handler` | Current context, session, running state |
| `/cancel` | `cancel_handler` | Abort running Claude invocation |

## Conventions

- All async. Use `asyncio` throughout, no blocking calls.
- Type hints on all function signatures.
- Structured logging via `logging` module to stderr.
- No classes where a function will do. Keep it simple.
- Config is loaded once at startup, passed as a dict/dataclass.
- SQLite access through `aiosqlite` only.
- Error handling: catch at the handler level, log, send user-friendly error message to Telegram. Never crash the bot.

## Testing

- Use `pytest` + `pytest-asyncio`.
- Mock the Agent SDK and Telegram API for unit tests.
- Integration test: real bot token + real Agent SDK against a test context directory.

## Deployment

- Run as a systemd service on the home server.
- `uv run openudang` as the ExecStart command.
- Environment: `ANTHROPIC_API_KEY` in systemd unit or `.env` file loaded by the service.
- Restart the service: `systemctl --user restart open-udang`
