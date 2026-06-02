<p align="center">
  <img src="assets/logo.svg" alt="OpenShrimp" width="480">
</p>

<p align="center">
  <strong>An OpenCode-backed coding agent in your pocket. No laptop required.</strong>
</p>

---

OpenShrimp puts a full OpenCode-backed coding agent in Telegram — complete with file editing, tool use, provider/model selection, and project awareness. It's the shrimp 🦐 to [OpenClaw](https://openclaw.ai/)'s lobster.

Small, personal, gets the job done.

<p align="center">
  <a href="#quick-start">Quick Start</a> · <a href="#commands">Commands</a> · <a href="#code-review">Code Review</a> · <a href="#scheduled-tasks">Scheduled Tasks</a> · <a href="#voice-notes">Voice Notes</a> · <a href="#macos-app">macOS App</a> · <a href="#deployment">Deployment</a>
</p>

<div align="center">
<table>
<tr>
<td align="center"><strong>Agent</strong></td>
<td align="center"><strong>Code Review</strong></td>
</tr>
<tr>
<td>

https://github.com/user-attachments/assets/2eaedb5a-cdff-4088-82c2-5cb4d6eee23a

</td>
<td>

https://github.com/user-attachments/assets/b8971e87-2003-4956-a449-8f8ca09a043f

</td>
</tr>
</table>
</div>

---

## OpenShrimp vs OpenClaw

Both are self-hosted and open source. They solve different problems.

| | **OpenShrimp** | **OpenClaw** |
|---|---|---|
| **Focus** | Code agent — reads, edits, and writes files in your projects | General-purpose assistant — browsing, memory, smart home, 50+ integrations |
| **Platform** | Telegram | WhatsApp, Telegram, Discord, Slack, Signal, iMessage |
| **AI model** | OpenCode provider/model support | Multiple hosted and local models |
| **Tool approval** | Interactive — inline keyboard approve/deny per tool call | Autonomous by default |
| **Project awareness** | Full — project instructions, working directories, path-scoped permissions | Limited — general shell access |

**TL;DR:** OpenClaw is a Swiss Army knife for daily life. OpenShrimp is a scalpel for code — it does one thing and does it well.

## Why OpenShrimp?

You're away from your desk but need an agent to fix a bug, review a diff, or scaffold something quick. OpenShrimp gives you a proper coding-agent session from any Telegram chat — on your phone, your tablet, wherever.

- **Real agent, not a chatbot.** The agent can read, edit, and write files in your actual project directories. Full tool use, not just text completion.
- **You stay in control.** Every file mutation requires your explicit approval via inline keyboard buttons. One tap to approve, one tap to deny. Or hit "Accept all edits" when you trust the flow. When you're ready to commit, `/review` opens a swipe-based UI to stage exactly the hunks you want.
- **Talk to it.** Send a voice note and it gets transcribed automatically as a prompt — no typing needed. Great for quick instructions when you're on the go.
- **Multiple projects, one bot.** Switch between project contexts on the fly with `/context`. Each context has its own working directory, project instructions, model, and tool permissions. Legacy `CLAUDE.md` project instruction files are still supported.
- **Persistent sessions.** Pick up where you left off. Sessions survive restarts, and you can `/resume` any previous conversation.
- **Forum topic support.** Use Telegram forum channels to organize conversations — each topic thread gets its own independent agent session. Run parallel tasks in the same chat without them stepping on each other. The agent can auto-title each topic for easy navigation.
- **Container isolation.** Run each context inside a Docker container with only the project directory mounted. On macOS, use Lima for full VM isolation via Apple's Virtualization.framework.
- **Computer use.** Enable a headless desktop inside the sandbox — the agent can launch Chromium, click around, take screenshots, and interact with GUIs. Watch live via VNC.
- **Group chat ready.** Add the bot to a team chat. It responds to @mentions and replies, so it stays out of the way until you need it.
- **Schedule recurring tasks.** Tell the agent to check your repo every morning, monitor a CI pipeline, or run a one-shot task later — all via natural language. Tasks run in isolated sessions automatically.
- **Watch background tasks.** When the agent runs a long command in the background, tap "View output" to open a live terminal viewer right in Telegram.
- **Locked down by default.** User allowlist, path-scoped file access, and granular tool approval. The agent can't silently read your `~/.ssh` or write outside your project.

## Code Review

OpenShrimp includes a mobile-first code review UI built as a Telegram Mini App. Send `/review` to open it.

It works like Tinder for diffs — each hunk is a card. Swipe right to stage, left to skip, down to undo. You review at the hunk level, not the file level, so you can cherry-pick exactly the changes you want — like `git add -p`, but designed for your phone.

## Voice Notes

Send a voice message instead of typing. OpenShrimp automatically transcribes it using [Moonshine](https://github.com/usefulsensors/moonshine) — a fast, lightweight speech-to-text model that runs locally. The transcribed text is sent to the agent as a prompt, prefixed with `[Transcribed from voice note]` so it knows the input came from speech.

The `moonshine-stt` binary is auto-downloaded on first use. No setup required.

## Scheduled Tasks

Set up recurring or one-shot tasks that the agent runs automatically. Just describe what you want in natural language — "check for broken tests every morning at 9am", "summarize the git log every Friday", or "run this migration in 30 minutes".

The agent manages schedules via built-in tools. Use `/schedule` to see what's active or remove tasks. Scheduled tasks run in isolated sessions with read-only access, so they can report but not modify your code without a follow-up conversation.

## Container Isolation

You can run each context inside a Docker container, libvirt VM, or Lima VM by adding a `sandbox:` block to your context config. OpenCode runs inside the sandbox with only the project directory bind-mounted — so it can't touch anything else on the host. The older `container:` key is still accepted as a legacy alias for Docker sandboxing.

Session state is stored separately per context under OpenShrimp's sandbox data directory, so sandboxed contexts don't interfere with each other or your host OpenCode sessions.

On Linux, this uses Docker or libvirt VMs. On macOS, use `backend: lima` for full VM isolation via Apple's Virtualization.framework.

## macOS App

On macOS, OpenShrimp is also available as a menu bar app. Download the `.dmg` from [Releases](https://github.com/yjwong/open-shrimp/releases), drag to Applications, and launch — no terminal needed.

- Lives in the menu bar (shrimp icon) with no Dock icon
- First-run setup wizard walks you through configuration with native macOS dialogs
- Start/stop the bot, open config, view logs — all from the menu bar
- "Start at Login" toggle for automatic launch

> **Note:** The macOS app is currently unsigned. On first launch, macOS will block it — right-click the app and choose "Open" to bypass Gatekeeper, or go to System Settings → Privacy & Security and click "Open Anyway".

## Quick Start

### Prerequisites

- [OpenCode](https://opencode.ai/) available on the host, or let OpenShrimp use its bundled OpenCode runtime where supported. Use `/connect` after setup to authenticate model providers.
- A Telegram bot token from [@BotFather](https://t.me/BotFather) — we strongly recommend enabling **Threaded Mode** (Settings → Bot Settings → Threads Settings → Threaded Mode). This lets each conversation run in its own forum topic with an independent agent session.

### Option 1: Download Binary (recommended)

Grab the latest binary from [Releases](https://github.com/yjwong/open-shrimp/releases). No Python or package manager required — just download, configure, and run.

> **Note:** The Linux binaries require glibc ≥ 2.39 (Ubuntu 24.04+, Debian 13+, Fedora 40+). On older distros, use the [from-source](#option-2-from-source) install instead.

```bash
# Linux x86_64
curl -fsSL https://github.com/yjwong/open-shrimp/releases/latest/download/openshrimp-linux-x86_64 -o openshrimp
# Linux ARM64
curl -fsSL https://github.com/yjwong/open-shrimp/releases/latest/download/openshrimp-linux-aarch64 -o openshrimp
# macOS Apple Silicon
curl -fsSL https://github.com/yjwong/open-shrimp/releases/latest/download/openshrimp-macos-aarch64 -o openshrimp

chmod +x openshrimp
```

On first run, the binary will automatically set up an isolated Python environment and install dependencies. If no config file exists, an interactive setup wizard walks you through creating one. Subsequent runs start instantly.

### Option 2: From Source

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/yjwong/open-shrimp.git
cd open-shrimp

# Build the web apps (requires Node.js 18+)
for app in review-app terminal-app markdown-app vnc-app; do
  (cd "web/$app" && npm install && npm run build)
done

# If you don't need the web features, create empty placeholders instead:
# for app in review-app terminal-app markdown-app vnc-app; do mkdir -p "web/$app/dist"; done

uv sync
```

Just like the binary, running `uv run openshrimp` without a config file will launch the interactive setup wizard. Or configure manually:

<details>
<summary>Manual config setup</summary>

```bash
cp config.example.yaml ~/.config/openshrimp/config.yaml
```

Edit `~/.config/openshrimp/config.yaml` with your bot token, Telegram user IDs, and project directories:

```yaml
telegram:
  token: "YOUR_BOT_TOKEN"

allowed_users:
  - 123456789  # Your Telegram user ID

contexts:
  my-project:
    directory: /home/you/projects/my-project
    description: "My awesome project"
    model: openai/gpt-5.5
    allowed_tools:
      - LSP

default_context: my-project
```

</details>

### Run

```bash
# Binary
./openshrimp

# From source
uv run openshrimp

# Connect model providers after the bot is running:
# send /connect in a private Telegram chat with the bot
```

If no config file exists, OpenShrimp starts an interactive setup wizard that walks you through creating one — no need to copy or edit YAML manually.

Or deploy as a systemd service for always-on access — see [Deployment](#deployment).

## Commands

| Command | Description |
|---------|-------------|
| `/context [name]` | List available contexts or switch to one |
| `/clear` | Start a fresh session in the current context |
| `/status` | Show current context, session, and running state |
| `/cancel` | Abort a running agent invocation |
| `/model [name]` | Show or override the model for this chat |
| `/effort [level]` | Show or override thinking effort for this chat |
| `/add_dir` | Add a working directory to the current context |
| `/resume` | List and resume a previous session |
| `/review` | Open the mobile code review UI |
| `/mcp` | List and manage MCP servers |
| `/schedule` | List and manage scheduled tasks |
| `/tasks` | List or stop background tasks |
| `/vnc` | View the computer-use desktop |
| `/connect` | Connect model providers |
| `/config` | Edit bot configuration |
| `/restart` | Restart the bot process |

## How Tool Approval Works

OpenShrimp enforces a layered permission model:

- **Read-only tools** (`read`, `glob`, `grep`) — auto-approved within the context directory
- **Write tools** (`edit`, `write`) — always require manual approval via Telegram inline buttons
- **Bash and other tools** — configurable per-context via `allowed_tools` patterns (for example, `bash(git *)`)
- **Paths outside the context directory** — always require manual approval, regardless of tool type

When a tool needs approval, you get three options: **Allow** (once), **Accept all [tool]** (auto-approve that tool for the session), or **Deny**. Edit/write tools get the familiar **"Accept all edits"** button instead. All session-level approvals reset on `/clear` or context switch.

## Deployment

The easiest way to deploy is with the built-in install command:

```bash
# Install as a systemd user service (Linux) or launchd agent (macOS)
openshrimp install

# Remove the service
openshrimp uninstall
```

This auto-detects your platform, finds the executable path, and sets everything up — including enabling lingering on Linux so the service runs without an active login session.

<details>
<summary>Manual setup</summary>

If you prefer to set up the service manually:

```ini
# ~/.config/systemd/user/open-shrimp.service
[Unit]
Description=OpenShrimp Telegram Bot

[Service]
ExecStart=/path/to/uv run openshrimp
# Provider credentials are managed by OpenCode. Use /connect in a private
# Telegram chat after the service is running, or provide provider-specific
# environment variables here if your OpenCode provider requires them.
Restart=always

[Install]
WantedBy=default.target
```

```bash
systemctl --user enable --now open-shrimp
```

</details>

## License

MIT
