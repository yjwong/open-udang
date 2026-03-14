<p align="center">
  <img src="assets/logo.svg" alt="OpenUdang" width="480">
</p>

<p align="center">
  <strong>Claude Code in your pocket. No laptop required.</strong>
</p>

---

OpenUdang puts a full Claude coding agent in Telegram — complete with file editing, tool use, and project awareness. It's the prawn 🦐 to [OpenClaw](https://openclaw.ai/)'s lobster.

*Udang* is Malay for "prawn" — small, personal, gets the job done.

---

## OpenUdang vs OpenClaw

Both are self-hosted and open source. They solve different problems.

| | **OpenUdang** | **OpenClaw** |
|---|---|---|
| **Focus** | Code agent — reads, edits, and writes files in your projects | General-purpose assistant — browsing, memory, smart home, 50+ integrations |
| **Platform** | Telegram | WhatsApp, Telegram, Discord, Slack, Signal, iMessage |
| **AI model** | Claude only (via Agent SDK) | Claude, GPT, local models |
| **Tool approval** | Interactive — inline keyboard approve/deny per tool call | Autonomous by default |
| **Project awareness** | Full — CLAUDE.md, working directories, path-scoped permissions | Limited — general shell access |
| **Streaming** | Real-time via `sendMessageDraft` | Sends complete responses |
| **Written in** | Python | TypeScript |

**TL;DR:** OpenClaw is a Swiss Army knife for daily life. OpenUdang is a scalpel for code — it does one thing and does it well.

## Why OpenUdang?

You're away from your desk but need Claude to fix a bug, review a diff, or scaffold something quick. OpenUdang gives you a proper Claude Code session from any Telegram chat — on your phone, your tablet, wherever.

- **Real agent, not a chatbot.** Claude can read, edit, and write files in your actual project directories. Full tool use, not just text completion.
- **You stay in control.** Every file mutation requires your explicit approval via inline keyboard buttons. One tap to approve, one tap to deny. Or hit "Accept all edits" when you trust the flow.
- **Stream responses in real-time.** Responses stream directly into Telegram using `sendMessageDraft` — no waiting for the full response to generate.
- **Multiple projects, one bot.** Switch between project contexts on the fly with `/context`. Each context has its own working directory, CLAUDE.md, model, and tool permissions.
- **Persistent sessions.** Pick up where you left off. Sessions survive restarts, and you can `/resume` any previous conversation.
- **Group chat ready.** Add the bot to a team chat. It responds to @mentions and replies, so it stays out of the way until you need it.
- **Locked down by default.** User allowlist, path-scoped file access, and granular tool approval. The agent can't silently read your `~/.ssh` or write outside your project.

## Quick Start

### Prerequisites

- [Claude Code](https://docs.anthropic.com/en/docs/claude-code) CLI installed and authenticated
- An [Anthropic API key](https://console.anthropic.com/)
- A Telegram bot token from [@BotFather](https://t.me/BotFather)

### Option 1: Download Binary (recommended)

Grab the latest binary from [Releases](https://github.com/yjwong/open-udang/releases). No Python or package manager required — just download, configure, and run.

```bash
# Linux x86_64
curl -fsSL https://github.com/yjwong/open-udang/releases/latest/download/openudang-linux-x86_64 -o openudang
# Linux ARM64
curl -fsSL https://github.com/yjwong/open-udang/releases/latest/download/openudang-linux-aarch64 -o openudang
# macOS Apple Silicon
curl -fsSL https://github.com/yjwong/open-udang/releases/latest/download/openudang-macos-aarch64 -o openudang

chmod +x openudang

# Set up your config
mkdir -p ~/.config/openudang
curl -fsSL https://raw.githubusercontent.com/yjwong/open-udang/main/config.example.yaml -o ~/.config/openudang/config.yaml
```

On first run, the binary will automatically set up an isolated Python environment and install dependencies. Subsequent runs start instantly.

### Option 2: From Source

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/yjwong/open-udang.git
cd open-udang
uv sync

# Set up your config
cp config.example.yaml ~/.config/openudang/config.yaml
```

Edit `~/.config/openudang/config.yaml` with your bot token, Telegram user IDs, and project directories:

```yaml
telegram:
  token: "YOUR_BOT_TOKEN"

allowed_users:
  - 123456789  # Your Telegram user ID

contexts:
  my-project:
    directory: /home/you/projects/my-project
    description: "My awesome project"
    model: claude-sonnet-4-6

default_context: my-project
```

### Run

```bash
# Binary
ANTHROPIC_API_KEY=sk-ant-... ./openudang

# From source
ANTHROPIC_API_KEY=sk-ant-... uv run openudang
```

If no config file exists, OpenUdang starts an interactive setup wizard that walks you through creating one — no need to copy or edit YAML manually.

Or deploy as a systemd service for always-on access — see [Deployment](#deployment).

## Commands

| Command | Description |
|---------|-------------|
| `/context [name]` | List available contexts or switch to one |
| `/clear` | Start a fresh session in the current context |
| `/status` | Show current context, session, and running state |
| `/cancel` | Abort a running Claude invocation |
| `/resume` | List and resume a previous session |

## How Tool Approval Works

OpenUdang enforces a layered permission model:

- **Read-only tools** (Read, Glob, Grep) — auto-approved within the context directory
- **Write tools** (Edit, Write) — always require manual approval via Telegram inline buttons
- **Bash and other tools** — configurable per-context via `allowed_tools` patterns (e.g., `Bash(git *)`)
- **Paths outside the context directory** — always require manual approval, regardless of tool type

You can tap **"Accept all edits"** during a session to auto-approve subsequent Edit/Write calls. This resets on `/clear` or context switch.

## Deployment

The easiest way to deploy is with the built-in install command:

```bash
# Install as a systemd user service (Linux) or launchd agent (macOS)
openudang install

# Remove the service
openudang uninstall
```

This auto-detects your platform, finds the executable path, and sets everything up — including enabling lingering on Linux so the service runs without an active login session.

<details>
<summary>Manual setup</summary>

If you prefer to set up the service manually:

```ini
# ~/.config/systemd/user/open-udang.service
[Unit]
Description=OpenUdang Telegram Bot

[Service]
ExecStart=/path/to/uv run openudang
Environment=ANTHROPIC_API_KEY=sk-ant-...
Restart=always

[Install]
WantedBy=default.target
```

```bash
systemctl --user enable --now open-udang
```

</details>

## License

MIT
