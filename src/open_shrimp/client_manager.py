"""Persistent Claude Agent SDK client manager for OpenShrimp.

Manages long-lived ClaudeSDKClient instances keyed by ChatScope, so the CLI
subprocess stays alive across multiple messages in the same conversation.
This avoids the "Continue from where you left off." injection that the CLI
performs when it detects an interrupted turn on session resume.

Only the first message in a session uses ``--resume`` to restore history;
subsequent messages simply call ``client.query()`` on the already-connected
client.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import threading
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    CLIConnectionError,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ProcessError,
    ResultMessage,
    SystemMessage,
)

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup

from open_shrimp.web_app_button import make_web_app_button

from open_shrimp.agent import AgentEvent
from open_shrimp.config import ContextConfig, is_sandboxed
from open_shrimp.db import ChatScope
from open_shrimp.hooks import (
    ApprovalCallback,
    EditNotifyCallback,
    HostBashApprovalCallback,
    QuestionCallback,
)
from open_shrimp.sandbox import Sandbox, SandboxManager
from open_shrimp.tools import create_openshrimp_mcp_server

logger = logging.getLogger(__name__)

# Host-side credentials file that needs to be synced into sandboxes.
# (Linux/Windows; macOS uses Keychain — see ``_watch_credentials_macos``.)
_HOST_CREDENTIALS = Path.home() / ".claude" / ".credentials.json"

# macOS login Keychain DB path.  Mtime is bumped on every Keychain mutation,
# so FSEvents on the parent directory wake us up on token refresh.
_MACOS_KEYCHAIN_DIR = Path.home() / "Library" / "Keychains"
_MACOS_KEYCHAIN_DB_NAME = "login.keychain-db"

# Single credentials watcher shared across all sandboxed sessions.
# Maps context_name -> claude_home_dir for all active sandbox sessions.
_cred_sync_targets: dict[str, Path] = {}
_cred_sync_lock = threading.Lock()
_cred_watcher_stop: threading.Event | None = None
_cred_watcher_thread: threading.Thread | None = None


def _host_creds_available() -> bool:
    """Whether host-side credentials exist to sync into sandboxes."""
    if sys.platform == "darwin":
        # The login keychain DB always exists for a logged-in user; we
        # don't gate on the actual ``Claude Code-credentials`` entry —
        # if it's missing, the watcher simply won't propagate anything.
        return (_MACOS_KEYCHAIN_DIR / _MACOS_KEYCHAIN_DB_NAME).exists()
    return _HOST_CREDENTIALS.exists()


def _propagate_credentials(payload: str) -> None:
    """Write the given credentials JSON into every registered sandbox."""
    with _cred_sync_lock:
        targets = list(_cred_sync_targets.items())
    for ctx_name, claude_home in targets:
        try:
            dest = claude_home / ".credentials.json"
            dest.write_text(payload, encoding="utf-8")
            logger.debug(
                "Synced credentials to %s (context %s)",
                dest, ctx_name,
            )
        except Exception:
            logger.debug(
                "Failed to sync credentials for context %s",
                ctx_name, exc_info=True,
            )


def _watch_credentials_linux(stop: threading.Event) -> None:
    """Watch ``~/.claude/.credentials.json`` for atomic-replace writes.

    We watch the **parent directory** rather than the credentials file
    itself because Claude Code refreshes credentials via atomic replace
    (write tmp + rename).  Watching the file directly loses track after
    the first rename — inotify is bound to the old inode.
    """
    from watchfiles import watch

    cred_dir = _HOST_CREDENTIALS.parent
    cred_name = _HOST_CREDENTIALS.name

    if not cred_dir.exists():
        return

    try:
        for changes in watch(
            cred_dir, stop_event=stop, rust_timeout=1000,
        ):
            if stop.is_set():
                break
            if not any(Path(path).name == cred_name for _ct, path in changes):
                continue
            if not _HOST_CREDENTIALS.exists():
                continue
            try:
                payload = _HOST_CREDENTIALS.read_text(encoding="utf-8")
            except OSError:
                continue
            _propagate_credentials(payload)
    except Exception:
        if not stop.is_set():
            logger.debug("Credentials watcher exited", exc_info=True)


def _watch_credentials_macos(stop: threading.Event) -> None:
    """Watch the macOS login Keychain for ``Claude Code-credentials`` updates.

    The Claude Code app on macOS stores OAuth tokens in the login
    Keychain rather than ``~/.claude/.credentials.json``.  Any Keychain
    mutation rewrites ``login.keychain-db``, so FSEvents on the
    Keychains directory wakes us on token refresh.  We re-extract via
    ``security`` and only propagate when the parsed ``expiresAt``
    differs from the last known value, which filters out the noise
    from unrelated keychain activity (Safari saving passwords, etc.).
    """
    from watchfiles import watch

    from open_shrimp.sandbox.lima_helpers import _read_credentials_json

    if not _MACOS_KEYCHAIN_DIR.exists():
        return

    last_expires_at: int | None = None

    try:
        for changes in watch(
            _MACOS_KEYCHAIN_DIR, stop_event=stop, rust_timeout=1000,
        ):
            if stop.is_set():
                break
            if not any(
                Path(path).name == _MACOS_KEYCHAIN_DB_NAME
                for _ct, path in changes
            ):
                continue
            payload = _read_credentials_json()
            if not payload:
                continue
            try:
                expires_at = int(
                    json.loads(payload)
                    .get("claudeAiOauth", {})
                    .get("expiresAt", 0)
                )
            except (ValueError, json.JSONDecodeError):
                continue
            if expires_at == last_expires_at:
                continue
            last_expires_at = expires_at
            _propagate_credentials(payload)
    except Exception:
        if not stop.is_set():
            logger.debug("Keychain credentials watcher exited", exc_info=True)


def _watch_credentials(stop: threading.Event) -> None:
    """Background thread: sync host credentials into all active sandboxes.

    Keeps long-lived SDK clients (where the wrapper script doesn't
    re-run) in sync with host-side token refreshes.  Uses native OS
    change-notification (FSEvents on macOS, inotify on Linux) so we
    wake immediately on refresh rather than polling.
    """
    if sys.platform == "darwin":
        _watch_credentials_macos(stop)
    else:
        _watch_credentials_linux(stop)


def _register_cred_sync(context_name: str, claude_home_dir: Path) -> None:
    """Register a sandbox for credential syncing, starting the watcher if needed."""
    global _cred_watcher_stop, _cred_watcher_thread

    with _cred_sync_lock:
        _cred_sync_targets[context_name] = claude_home_dir

        if _cred_watcher_thread is None or not _cred_watcher_thread.is_alive():
            _cred_watcher_stop = threading.Event()
            _cred_watcher_thread = threading.Thread(
                target=_watch_credentials,
                args=(_cred_watcher_stop,),
                daemon=True,
            )
            _cred_watcher_thread.start()
            logger.debug("Started credentials watcher thread")


def _unregister_cred_sync(context_name: str) -> None:
    """Unregister a sandbox; stop the watcher if no targets remain."""
    global _cred_watcher_stop, _cred_watcher_thread

    with _cred_sync_lock:
        _cred_sync_targets.pop(context_name, None)

        if not _cred_sync_targets and _cred_watcher_stop is not None:
            _cred_watcher_stop.set()
            if _cred_watcher_thread is not None:
                _cred_watcher_thread.join(timeout=2)
            _cred_watcher_stop = None
            _cred_watcher_thread = None
            logger.debug("Stopped credentials watcher thread")


@dataclass
class CallbackContext:
    """Mutable holder for per-message callback state.

    The ``canUseTool`` closure is bound at client creation time and cannot
    be changed.  This indirection lets per-message state (like
    ``draft_state``) be swapped in before each ``query()`` call while the
    same closure keeps referencing *this* object.
    """

    request_approval: ApprovalCallback | None = None
    handle_user_questions: QuestionCallback | None = None
    is_edit_auto_approved: Callable[[], bool] | None = None
    notify_auto_approved_edit: EditNotifyCallback | None = None
    is_tool_auto_approved: Callable[[str, dict[str, Any]], bool] | None = None
    get_session_approved_dirs: Callable[[], list[str]] | None = None
    request_host_bash_approval: HostBashApprovalCallback | None = None


@dataclass
class AgentSession:
    """A long-lived SDK client associated with a chat scope."""

    client: ClaudeSDKClient
    session_id: str | None = None
    context_name: str = ""
    callback_context: CallbackContext = field(default_factory=CallbackContext)
    sandbox: Sandbox | None = None
    mcp_proxy: Any | None = None
    wrapper_cleanup_paths: list[str] = field(default_factory=list)
    last_activity: float = field(default_factory=time.monotonic)


_active_sessions: dict[ChatScope, AgentSession] = {}

# Idle session timeout: sessions with no activity for this long are closed.
_IDLE_TIMEOUT: float = 30 * 60  # 30 minutes
_idle_sweep_task: asyncio.Task[None] | None = None

# Per-context lock: serialises sandbox creation so two scopes sharing
# the same libvirt context don't race on VM boot / virtiofsd / ports.
_context_locks: dict[str, asyncio.Lock] = {}


def _is_client_alive(client: ClaudeSDKClient) -> bool:
    """Check if the CLI subprocess is still running.

    Pokes into the transport's private state to detect a terminated
    process.  Returns True if the process appears healthy or if the
    state cannot be determined (fail-open).
    """
    try:
        transport = client._transport
        if transport is None:
            return False
        process = getattr(transport, "_process", None)
        if process is None:
            return False
        return process.returncode is None
    except Exception:
        return True


async def get_or_create_session(
    scope: ChatScope,
    context_name: str,
    context: ContextConfig,
    session_id: str | None,
    callback_context: CallbackContext,
    bot: Bot | None = None,
    db: Any | None = None,
    config: Any | None = None,
    job_queue: Any | None = None,
    terminal_base_url: str | None = None,
    user_id: int = 0,
    is_private_chat: bool = True,
    sandbox_manager: SandboxManager | None = None,
    mcp_proxy: Any | None = None,
) -> AgentSession:
    """Return an existing live session or create a new one.

    If a session already exists for *scope* with the same context,
    return it (after updating the callback context).  Otherwise create a
    fresh ``ClaudeSDKClient``, connect, and store it.

    Args:
        scope: ChatScope identifying the chat/thread.
        context_name: Name of the active context.
        context: Context configuration (directory, model, etc.).
        session_id: Session ID for ``--resume`` (only used when creating
            a new client).
        callback_context: Mutable callback holder to bind into hooks.

    Returns:
        An ``AgentSession`` with a connected client ready for ``query()``.
    """
    existing = _active_sessions.get(scope)
    if existing is not None:
        if existing.context_name == context_name:
            if _is_client_alive(existing.client):
                existing.callback_context.request_approval = callback_context.request_approval
                existing.callback_context.handle_user_questions = callback_context.handle_user_questions
                existing.callback_context.is_edit_auto_approved = callback_context.is_edit_auto_approved
                existing.callback_context.notify_auto_approved_edit = callback_context.notify_auto_approved_edit
                existing.callback_context.is_tool_auto_approved = callback_context.is_tool_auto_approved
                existing.callback_context.get_session_approved_dirs = callback_context.get_session_approved_dirs
                existing.callback_context.request_host_bash_approval = callback_context.request_host_bash_approval
                existing.last_activity = time.monotonic()
                logger.info(
                    "Reusing live client for scope %s context %s",
                    scope,
                    context_name,
                )
                return existing
            else:
                logger.warning(
                    "CLI process dead for scope %s context %s, closing stale session",
                    scope,
                    context_name,
                )
                await close_session(scope)
        else:
            logger.info(
                "Context changed for scope %s (%s -> %s), closing old client",
                scope,
                existing.context_name,
                context_name,
            )
            await close_session(scope)

    from open_shrimp.hooks import make_can_use_tool

    can_use_tool = make_can_use_tool(
        request_approval=_make_approval_proxy(callback_context),
        cwd=context.directory,
        additional_directories=context.additional_directories or None,
        handle_user_questions=_make_questions_proxy(callback_context),
        is_edit_auto_approved=_make_edit_approved_proxy(callback_context),
        notify_auto_approved_edit=_make_edit_notify_proxy(callback_context),
        chat_id=scope.chat_id,
        is_tool_auto_approved=_make_tool_approved_proxy(callback_context),
        is_containerized=is_sandboxed(context),
        get_session_approved_dirs=_make_session_dirs_proxy(callback_context),
        request_host_bash_approval=_make_host_bash_approval_proxy(callback_context),
    )

    _last_stderr: list[str] = [""]
    _stderr_repeat_count: list[int] = [0]

    def _log_stderr(line: str) -> None:
        stripped = line.rstrip()
        if stripped == _last_stderr[0]:
            _stderr_repeat_count[0] += 1
            if _stderr_repeat_count[0] in (10, 50, 100):
                logger.info(
                    "CLI stderr (repeated %d times): %s",
                    _stderr_repeat_count[0], stripped,
                )
            return
        if _stderr_repeat_count[0] > 1:
            logger.info(
                "CLI stderr (repeated %d times total): %s",
                _stderr_repeat_count[0], _last_stderr[0],
            )
        _last_stderr[0] = stripped
        _stderr_repeat_count[0] = 1
        logger.info("CLI stderr: %s", stripped)

    # Auto-approve the built-in OpenShrimp MCP tools (send_file, send_photo)
    # alongside whatever the user configured.
    allowed_tools = list(context.allowed_tools or [])
    allowed_tools.append("mcp__openshrimp__send_file")
    if scope.thread_id is not None:
        allowed_tools.append("mcp__openshrimp__edit_topic")
    # Auto-approve scheduling tools when available.
    if db is not None and config is not None and job_queue is not None:
        allowed_tools.extend([
            "mcp__openshrimp__create_schedule",
            "mcp__openshrimp__list_schedules",
            "mcp__openshrimp__delete_schedule",
        ])
    # Auto-approve computer use tools when enabled.
    _computer_use_enabled = (
        (context.container is not None and context.container.computer_use)
        or (context.sandbox is not None and context.sandbox.computer_use)
    )
    if _computer_use_enabled:
        allowed_tools.extend([
            "mcp__openshrimp__computer_screenshot",
            "mcp__openshrimp__computer_click",
            "mcp__openshrimp__computer_type",
            "mcp__openshrimp__computer_key",
            "mcp__openshrimp__computer_scroll",
            "mcp__openshrimp__computer_toplevel",
        ])
        # Auto-approve Playwright MCP browser tools (core + tabs,
        # always enabled).  Tool names from microsoft/playwright-mcp.
        allowed_tools.extend([
            # Core automation
            "mcp__playwright__browser_click",
            "mcp__playwright__browser_close",
            "mcp__playwright__browser_console_messages",
            "mcp__playwright__browser_drag",
            "mcp__playwright__browser_evaluate",
            "mcp__playwright__browser_file_upload",
            "mcp__playwright__browser_fill_form",
            "mcp__playwright__browser_handle_dialog",
            "mcp__playwright__browser_hover",
            "mcp__playwright__browser_navigate",
            "mcp__playwright__browser_navigate_back",
            "mcp__playwright__browser_network_requests",
            "mcp__playwright__browser_press_key",
            "mcp__playwright__browser_resize",
            "mcp__playwright__browser_run_code",
            "mcp__playwright__browser_select_option",
            "mcp__playwright__browser_snapshot",
            "mcp__playwright__browser_take_screenshot",
            "mcp__playwright__browser_type",
            "mcp__playwright__browser_wait_for",
            # Tab management
            "mcp__playwright__browser_tabs",
            # PDF (opt-in via --caps=pdf)
            "mcp__playwright__browser_pdf_save",
            # Testing assertions (opt-in via --caps=testing)
            "mcp__playwright__browser_generate_locator",
            "mcp__playwright__browser_verify_element_visible",
            "mcp__playwright__browser_verify_list_visible",
            "mcp__playwright__browser_verify_text_visible",
            "mcp__playwright__browser_verify_value",
        ])

    # When sandboxed, generate a wrapper script that runs the Claude CLI
    # in an isolated environment.  The wrapper is pointed at via cli_path;
    # all other SDK machinery (stdin/stdout streaming, canUseTool, MCP) is
    # unchanged.
    sandbox: Sandbox | None = None
    cli_path: str | None = None
    wrapper_cleanup_paths: list[str] = []
    is_containerized = is_sandboxed(context)
    if is_containerized:
        assert sandbox_manager is not None, (
            "sandbox_manager is required for containerized contexts"
        )

        # Serialise sandbox boot per context_name so two scopes sharing
        # the same libvirt domain (or Docker container) don't race on VM
        # boot, virtiofsd startup, port allocation, etc.
        ctx_lock = _context_locks.setdefault(context_name, asyncio.Lock())
        async with ctx_lock:
            sandbox = sandbox_manager.create_sandbox(context_name, context)

            # Check if the environment needs building or the sandbox
            # needs starting — send user feedback before potentially
            # slow operations.
            needs_build = not sandbox.environment_ready()
            needs_start = not needs_build and not sandbox.running()
            if (needs_build or needs_start) and bot is not None:
                log_file = sandbox_manager.register_build(context_name)

                if needs_build:
                    progress_text = (
                        "Building container image for the first time, "
                        "this may take a few minutes\\.\\.\\."
                    )
                else:
                    progress_text = "Starting sandbox\\.\\.\\."

                keyboard = None
                if terminal_base_url and config is not None:
                    app_url = (
                        f"{terminal_base_url}/terminal/"
                        f"?type=container_build&id={context_name}"
                    )
                    keyboard = InlineKeyboardMarkup([[
                        make_web_app_button(
                            "📺 View build log",
                            app_url,
                            chat_id=scope.chat_id,
                            user_id=user_id,
                            bot_token=config.telegram.token,
                            is_private_chat=is_private_chat,
                        )
                    ]])
                await bot.send_message(
                    chat_id=scope.chat_id,
                    message_thread_id=scope.thread_id,
                    text=progress_text,
                    parse_mode="MarkdownV2",
                    reply_markup=keyboard,
                )
            else:
                log_file = None

            _sandbox = sandbox  # capture for closure
            _mgr = sandbox_manager  # capture for closure

            def _ensure_and_build_wrapper() -> tuple[str, list[str]]:
                try:
                    _sandbox.ensure_environment(log_file=log_file)
                    _sandbox.ensure_running(log_file=log_file)
                finally:
                    if log_file is not None:
                        assert _mgr is not None
                        _mgr.unregister_build(context_name)
                _sandbox.provision_workspace()
                return _sandbox.build_cli_wrapper()

            cli_path, wrapper_cleanup_paths = await asyncio.to_thread(
                _ensure_and_build_wrapper,
            )
            logger.info(
                "Sandbox context '%s': using wrapper %s",
                context_name,
                cli_path,
            )

    options = ClaudeAgentOptions(
        cwd=context.directory,
        model=context.model,
        effort=context.effort,
        allowed_tools=allowed_tools,
        add_dirs=context.additional_directories,
        setting_sources=["project", "user", "local"],
        include_partial_messages=True,
        stderr=_log_stderr,
        can_use_tool=can_use_tool,
        cli_path=cli_path,
        max_buffer_size=10 * 1024 * 1024,  # 10MB
    )

    system_prompt_parts: list[str] = []

    if scope.thread_id is not None:
        system_prompt_parts.append(
            "This conversation is in a Telegram forum topic. "
            "After your first response, use the edit_topic tool to set "
            "a concise title (max 128 chars) summarizing the conversation, "
            "and optionally an icon using a standard emoji (e.g. 📝, 🔥, "
            "🤖, 💬). If the topic changes significantly later, update "
            "the title again."
        )

    # Check if this sandbox supports computer-use (has a screenshots dir).
    _computer_use_sandbox = sandbox if (
        sandbox is not None and sandbox.get_screenshots_dir() is not None
    ) else None
    if _computer_use_sandbox is not None:
        system_prompt_parts.append(
            "This context has computer use (GUI interaction) enabled. "
            "You have access to a headless 1280x720 Linux desktop with "
            "a Wayland compositor (labwc), a web browser (Chromium), "
            "and a terminal (foot).\n\n"
            "For browser/web testing, prefer the Playwright MCP tools "
            "(browser_navigate, browser_click, browser_type, browser_snapshot, "
            "browser_screenshot, etc.) — they provide structured DOM access "
            "via accessibility snapshots which is far more reliable than "
            "pixel-based interaction. Use browser_snapshot to read the page "
            "structure before interacting.\n\n"
            "For non-browser GUI interaction (terminal, native apps, or when "
            "Playwright tools are insufficient), use the pixel-based tools: "
            "computer_screenshot to see the screen, computer_click to click "
            "at coordinates, computer_type to type text, computer_key for "
            "special keys and combos, computer_scroll to scroll, and "
            "computer_toplevel to switch between windows. Always take a "
            "screenshot first to understand the current state."
        )

    if system_prompt_parts:
        options.system_prompt = {
            "type": "preset",
            "preset": "claude_code",
            "append": "\n\n".join(system_prompt_parts),
        }

    # Register in-process MCP tools (send_file, send_photo, etc.) so the
    # agent can send files directly to the Telegram chat.
    if bot is not None:
        # Sudo mode (host_bash) is registered only when the context's
        # sandbox config explicitly opts in. Commands run with cwd set to
        # the context's source directory so they operate on the same tree
        # the agent sees inside the sandbox, just unsandboxed.
        _host_bash_workdir: str | None = None
        if (
            context.sandbox is not None
            and context.sandbox.allow_host_escape
        ):
            _host_bash_workdir = context.directory

        openshrimp_server = create_openshrimp_mcp_server(
            bot=bot, chat_id=scope.chat_id, thread_id=scope.thread_id,
            db=db, config=config, job_queue=job_queue,
            sandbox=sandbox,
            context_name=context_name,
            user_id=user_id,
            is_private_chat=is_private_chat,
            host_bash_workdir=_host_bash_workdir,
        )
        mcp_servers: dict[str, Any] = {"openshrimp": openshrimp_server}

        # Add Playwright MCP for structured browser automation in
        # computer-use contexts.  The CLI runs inside the sandbox,
        # so it spawns the Playwright MCP server as a child process
        # inside the sandbox automatically.
        if _computer_use_sandbox is not None:
            mcp_servers["playwright"] = {
                "command": "npx",
                "args": [
                    "@playwright/mcp",
                    "--cdp-endpoint", "http://localhost:9222",
                    "--caps", "pdf",
                ],
            }

        # Keep MCP server credentials on the host; sandbox sees only
        # HTTP endpoints via the proxy.  Covers stdio MCP servers
        # (spawned on the host) and HTTP/SSE MCP servers (reverse-
        # proxied so OAuth tokens stay on the host).
        if is_containerized and mcp_proxy is not None and sandbox is not None:
            from open_shrimp.mcp_proxy.config_reader import (
                get_http_mcp_servers_for_directory,
                get_mcp_servers_for_directory,
            )

            stdio_servers = get_mcp_servers_for_directory(context.directory)
            http_servers = get_http_mcp_servers_for_directory(
                context.directory
            )
            if stdio_servers or http_servers:
                token = mcp_proxy.register_context(
                    context_name,
                    servers=stdio_servers or None,
                    http_servers=http_servers or None,
                )
                host_ip = sandbox.host_address
                for name in stdio_servers:
                    mcp_servers[name] = {
                        "type": "http",
                        "url": mcp_proxy.get_proxy_url(
                            context_name, name, host_ip
                        ),
                        "headers": {
                            "Authorization": f"Bearer {token}",
                        },
                    }
                for name, http_cfg in http_servers.items():
                    mcp_servers[name] = {
                        "type": http_cfg.transport,
                        "url": mcp_proxy.get_http_proxy_url(
                            context_name, name, host_ip
                        ),
                        "headers": {
                            "Authorization": f"Bearer {token}",
                        },
                    }
                logger.info(
                    "Injected %d stdio + %d HTTP proxied MCP server(s) "
                    "for sandboxed context '%s': stdio=[%s] http=[%s]",
                    len(stdio_servers),
                    len(http_servers),
                    context_name,
                    ", ".join(stdio_servers),
                    ", ".join(http_servers),
                )

        options.mcp_servers = mcp_servers

    if session_id:
        options.resume = session_id
        logger.info(
            "Creating new client for scope %s: resuming session %s in %s",
            scope,
            session_id,
            context.directory,
        )
    else:
        logger.info(
            "Creating new client for scope %s: new session in %s",
            scope,
            context.directory,
        )

    client = ClaudeSDKClient(options=options)
    try:
        await client.connect()
    except ProcessError:
        if not session_id:
            raise
        # The session file may no longer exist (e.g. container state was
        # rebuilt, or the .jsonl was deleted).  Fall back to a fresh
        # session instead of surfacing a cryptic error.
        logger.warning(
            "Failed to resume session %s for scope %s – starting fresh",
            session_id,
            scope,
        )
        session_id = None
        options.resume = None
        client = ClaudeSDKClient(options=options)
        await client.connect()

    session = AgentSession(
        client=client,
        session_id=session_id,
        context_name=context_name,
        callback_context=callback_context,
        sandbox=sandbox,
        mcp_proxy=mcp_proxy if is_containerized else None,
        wrapper_cleanup_paths=wrapper_cleanup_paths,
    )

    # Register this sandbox for credential syncing (starts the watcher
    # if not already running).
    if sandbox is not None and sandbox_manager is not None and _host_creds_available():
        claude_home = sandbox_manager.claude_home_dir(context_name)
        if claude_home.exists():
            _register_cred_sync(context_name, claude_home)

    _active_sessions[scope] = session
    return session


async def reconnect_session(
    scope: ChatScope,
    context_name: str,
    context: ContextConfig,
    bot: Bot | None = None,
    db: Any | None = None,
    config: Any | None = None,
    job_queue: Any | None = None,
    terminal_base_url: str | None = None,
    user_id: int = 0,
    is_private_chat: bool = True,
    sandbox_manager: SandboxManager | None = None,
    mcp_proxy: Any | None = None,
) -> AgentSession | None:
    """Reconnect after a mid-session container crash.

    Closes the dead session, ensures the container is running again,
    and creates a new client that resumes the existing session.

    Returns the new ``AgentSession``, or ``None`` if reconnection fails.
    """
    old_session = _active_sessions.get(scope)
    if old_session is None:
        return None

    session_id = old_session.session_id
    callback_context = old_session.callback_context

    # Tear down the dead client (ignore errors — it's already dead).
    await close_session(scope)

    if not session_id:
        logger.warning(
            "Cannot reconnect scope %s: no session_id to resume", scope
        )
        return None

    logger.info(
        "Reconnecting scope %s: resuming session %s after container crash",
        scope, session_id,
    )

    try:
        return await get_or_create_session(
            scope=scope,
            context_name=context_name,
            context=context,
            session_id=session_id,
            callback_context=callback_context,
            bot=bot,
            db=db,
            config=config,
            job_queue=job_queue,
            terminal_base_url=terminal_base_url,
            user_id=user_id,
            is_private_chat=is_private_chat,
            sandbox_manager=sandbox_manager,
            mcp_proxy=mcp_proxy,
        )
    except Exception:
        logger.exception(
            "Failed to reconnect session for scope %s", scope
        )
        return None


async def close_session(scope: ChatScope) -> None:
    """Close and remove the session for *scope*, if any."""
    session = _active_sessions.pop(scope, None)
    if session is None:
        return
    # Unregister from credential syncing if this was a sandboxed session.
    # Check if any other active session still uses the same context before
    # removing the sync target.
    if session.sandbox is not None:
        ctx = session.context_name
        still_used = any(
            s.context_name == ctx and s.sandbox is not None
            for s in _active_sessions.values()
        )
        if not still_used:
            _unregister_cred_sync(ctx)
    # Unregister proxied MCP servers when no other session needs them.
    if session.mcp_proxy is not None:
        ctx = session.context_name
        still_used = any(
            s.context_name == ctx and s.mcp_proxy is not None
            for s in _active_sessions.values()
        )
        if not still_used:
            await session.mcp_proxy.unregister_context(ctx)
    try:
        async with asyncio.timeout(5):
            await session.client.disconnect()
        logger.info("Closed client for scope %s", scope)
    except (Exception, TimeoutError):
        logger.debug("Error/timeout closing client for scope %s", scope, exc_info=True)
    # Clean up per-session temp files (wrapper script, sandbox profile, etc.).
    # The sandbox itself is shared across sessions and managed by the
    # SandboxManager.
    for path in session.wrapper_cleanup_paths:
        Path(path).unlink(missing_ok=True)
        logger.debug("Removed temp file %s", path)


async def close_all_sessions() -> None:
    """Close all active sessions (for shutdown).

    Runs all disconnects in parallel so shutdown latency is dominated by
    the slowest single client (up to the 5s per-client timeout in
    ``close_session``), not the sum across every active scope.
    """
    scopes = list(_active_sessions.keys())
    if not scopes:
        return
    await asyncio.gather(
        *(close_session(scope) for scope in scopes),
        return_exceptions=True,
    )


async def close_sessions_for_context(context_name: str) -> int:
    """Close all active sessions bound to *context_name*.

    Returns the number of sessions closed.  Used before sandbox
    reboot/reset so SDK subprocesses don't orphan onto a dead runtime.
    """
    scopes = [
        scope for scope, session in _active_sessions.items()
        if session.context_name == context_name
    ]
    if not scopes:
        return 0
    await asyncio.gather(
        *(close_session(scope) for scope in scopes),
        return_exceptions=True,
    )
    return len(scopes)


async def _sweep_idle_sessions() -> None:
    """Periodically close sessions that have been idle too long."""
    while True:
        await asyncio.sleep(60)
        now = time.monotonic()
        stale = [
            scope for scope, session in _active_sessions.items()
            if now - session.last_activity > _IDLE_TIMEOUT
        ]
        for scope in stale:
            logger.info(
                "Closing idle session for scope %s (idle %.0fs)",
                scope,
                now - _active_sessions[scope].last_activity,
            )
            await close_session(scope)


def start_idle_sweep() -> None:
    """Start the background idle-session sweep task."""
    global _idle_sweep_task
    if _idle_sweep_task is None or _idle_sweep_task.done():
        _idle_sweep_task = asyncio.create_task(_sweep_idle_sessions())
        logger.info("Started idle session sweep (timeout=%ds)", _IDLE_TIMEOUT)


def stop_idle_sweep() -> None:
    """Cancel the idle-session sweep task."""
    global _idle_sweep_task
    if _idle_sweep_task is not None:
        _idle_sweep_task.cancel()
        _idle_sweep_task = None


def get_session(scope: ChatScope) -> AgentSession | None:
    """Return the active session for *scope*, or None."""
    return _active_sessions.get(scope)


async def stop_background_task(scope: ChatScope, task_id: str) -> bool:
    """Send a stop signal for a background task.  Returns True on success."""
    session = _active_sessions.get(scope)
    if session is None:
        logger.warning("No active session for scope %s to stop task %s", scope, task_id)
        return False
    try:
        logger.info("Sending stop signal for task %s in scope %s", task_id, scope)
        await session.client.stop_task(task_id)
        logger.info("Stop signal sent successfully for task %s", task_id)
        return True
    except Exception:
        logger.exception("Failed to stop task %s for scope %s", task_id, scope)
        return False


def has_session(scope: ChatScope) -> bool:
    """Return True if *scope* has a live session."""
    return scope in _active_sessions


async def query_and_stream(
    session: AgentSession,
    prompt: str,
) -> AsyncIterator[AgentEvent]:
    """Send a query on an existing session and yield events."""
    session.last_activity = time.monotonic()
    logger.info("Sending query on live client: %s", prompt[:200])
    await session.client.query(prompt)
    async for message in receive_events(session):
        yield message


async def receive_events(
    session: AgentSession,
) -> AsyncIterator[AgentEvent]:
    """Yield events from an existing session without sending a new query.

    Use this when ``session.client.query()`` has already been called
    separately (e.g. for message-injection support).
    """
    async for message in session.client.receive_response():
        if isinstance(message, SystemMessage):
            sid = getattr(message, "session_id", None)
            if sid:
                session.session_id = sid
        elif isinstance(message, ResultMessage):
            if message.session_id:
                session.session_id = message.session_id
        session.last_activity = time.monotonic()
        yield message


def _make_approval_proxy(
    ctx: CallbackContext,
) -> ApprovalCallback:
    async def _proxy(
        tool_name: str,
        tool_input: dict[str, Any],
        tool_use_id: str,
        suggested_session_dir: str | None = None,
    ) -> bool:
        if ctx.request_approval is None:
            logger.warning("No approval callback set, denying tool %s", tool_name)
            return False
        return await ctx.request_approval(
            tool_name, tool_input, tool_use_id, suggested_session_dir,
        )

    return _proxy


def _make_questions_proxy(
    ctx: CallbackContext,
) -> QuestionCallback:
    async def _proxy(
        questions: list[dict[str, Any]],
    ) -> dict[str, str]:
        if ctx.handle_user_questions is None:
            logger.warning("No question callback set, returning empty answers")
            return {}
        return await ctx.handle_user_questions(questions)

    return _proxy


def _make_edit_approved_proxy(
    ctx: CallbackContext,
) -> Callable[[], bool]:
    def _proxy() -> bool:
        if ctx.is_edit_auto_approved is None:
            return False
        return ctx.is_edit_auto_approved()

    return _proxy


def _make_edit_notify_proxy(
    ctx: CallbackContext,
) -> EditNotifyCallback:
    async def _proxy(
        tool_name: str, tool_input: dict[str, Any]
    ) -> None:
        if ctx.notify_auto_approved_edit is None:
            return
        await ctx.notify_auto_approved_edit(tool_name, tool_input)

    return _proxy


def _make_tool_approved_proxy(
    ctx: CallbackContext,
) -> Callable[[str, dict[str, Any]], bool]:
    def _proxy(tool_name: str, tool_input: dict[str, Any]) -> bool:
        if ctx.is_tool_auto_approved is None:
            return False
        return ctx.is_tool_auto_approved(tool_name, tool_input)

    return _proxy


def _make_session_dirs_proxy(
    ctx: CallbackContext,
) -> Callable[[], list[str]]:
    def _proxy() -> list[str]:
        if ctx.get_session_approved_dirs is None:
            return []
        return ctx.get_session_approved_dirs()

    return _proxy


def _make_host_bash_approval_proxy(
    ctx: CallbackContext,
) -> HostBashApprovalCallback:
    async def _proxy(
        tool_input: dict[str, Any], tool_use_id: str,
    ) -> Any:
        if ctx.request_host_bash_approval is None:
            logger.warning(
                "host_bash invoked but no approval callback set; denying"
            )
            return "denied"
        return await ctx.request_host_bash_approval(tool_input, tool_use_id)

    return _proxy
