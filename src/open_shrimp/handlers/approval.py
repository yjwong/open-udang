"""Tool approval via Telegram inline keyboards."""

from __future__ import annotations

import asyncio
import difflib
import logging
import os
from typing import Any

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup

from open_shrimp.db import ChatScope
from open_shrimp.web_app_button import make_web_app_button

from open_shrimp.handlers.state import (
    _approval_futures,
    _approval_metadata,
    _approval_tool_names,
    _pending_agent_inputs,
    _pending_session_dirs,
    _pending_tool_approvals,
)
from open_shrimp.handlers.utils import _escape_mdv2
from open_shrimp.hooks import (
    ACCEPT_ALL_EDITS_TOOLS,
    _PATH_SCOPED_TOOLS,
    ApprovalRule,
    HostBashOutcome,
    parse_apply_patch_files,
)
from open_shrimp.stream import _relative_path
from open_shrimp.sudo_audit import log_sudo

logger = logging.getLogger(__name__)


# Prefixes to skip when extracting the bash command name (e.g. "sudo git").
_BASH_SKIP_PREFIXES = {"sudo", "env", "nohup", "nice", "ionice", "time", "strace"}


def _extract_bash_prefix(command: str) -> str | None:
    """Extract the primary command name from a bash command string.

    Handles chained commands (``&&``, ``||``, ``;``), skips common prefixes
    like ``sudo`` and ``env VAR=val``, and returns the first significant
    word.  Returns None if the command is too complex to extract a useful
    prefix (e.g. starts with a subshell or heredoc).
    """
    cmd = command.strip()
    if not cmd or cmd.startswith("(") or cmd.startswith("{"):
        return None

    # Take only the first command in a chain.
    for sep in ("&&", "||", ";"):
        cmd = cmd.split(sep, 1)[0].strip()

    # Handle pipes: take only the first segment.
    cmd = cmd.split("|", 1)[0].strip()

    words = cmd.split()
    if not words:
        return None

    # Skip common prefixes and their flags/arguments.
    idx = 0
    in_prefix = True
    while idx < len(words) and in_prefix:
        word = words[idx]
        if word in _BASH_SKIP_PREFIXES:
            idx += 1
            # Skip any flags that belong to the prefix command
            # (e.g. "nice -n 10", "sudo -u user").
            while idx < len(words) and words[idx].startswith("-"):
                idx += 1
                # Skip the flag's argument if it looks like a value
                if idx < len(words) and not words[idx].startswith("-"):
                    idx += 1
            continue
        # env VAR=val ... — skip variable assignments.
        if "=" in word and idx > 0:
            idx += 1
            continue
        in_prefix = False

    if idx >= len(words):
        return None

    prefix = words[idx]
    # Reject if it looks like a path to a script rather than a command name.
    if "/" in prefix and not prefix.startswith("./"):
        return None

    return prefix


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _format_edit_approval(
    tool_input: dict[str, Any], cwd: str | None = None,
) -> str:
    """Format an Edit tool call as a unified diff for the approval prompt."""
    file_path = _relative_path(tool_input.get("filePath", "unknown"), cwd)
    old_string = tool_input.get("oldString", "")
    new_string = tool_input.get("newString", "")

    escaped_path = _escape_mdv2(file_path)
    header = f"\u270f\ufe0f *Edit:* `{escaped_path}`"

    old_lines = old_string.splitlines()
    new_lines = new_string.splitlines()
    diff_lines = list(difflib.unified_diff(
        old_lines, new_lines, lineterm="",
    ))

    if diff_lines:
        # Drop the ---/+++ header lines from unified_diff, keep @@ and content
        diff_body = "\n".join(diff_lines[2:])
    else:
        diff_body = "(no diff)"

    # Truncate if the diff is too long for a single Telegram message.
    # Reserve space for the header, code fences, and buttons (~200 chars).
    max_diff_len = 4096 - 200
    if len(diff_body) > max_diff_len:
        diff_body = diff_body[:max_diff_len] + "\n..."

    escaped_diff = _escape_mdv2(diff_body)
    return f"{header}\n\n```diff\n{escaped_diff}\n```"


def _format_bash_approval(tool_input: dict[str, Any]) -> str:
    """Format a Bash tool call for the approval prompt."""
    command = tool_input.get("command", "")
    description = tool_input.get("description", "")

    parts: list[str] = []
    if description:
        parts.append(f"\U0001f4bb *Bash:* {_escape_mdv2(description)}")
    else:
        parts.append("\U0001f4bb *Bash*")

    # Show the command in a code block.
    max_cmd_len = 4096 - 200
    if len(command) > max_cmd_len:
        command = command[:max_cmd_len] + "\n..."
    escaped_cmd = _escape_mdv2(command)
    parts.append(f"```bash\n{escaped_cmd}\n```")

    return "\n\n".join(parts)


def _format_monitor_approval(tool_input: dict[str, Any]) -> str:
    """Format a Monitor tool call for the approval prompt.

    Monitor runs an arbitrary shell command whose stdout is streamed as
    events. Render the description as the header and the command in a
    bash code block, mirroring Bash so the user can review what will run.
    """
    command = tool_input.get("command", "")
    description = tool_input.get("description", "")
    persistent = tool_input.get("persistent", False)

    parts: list[str] = []
    header = "\U0001f4e1 *Monitor*"
    if description:
        header = f"{header}: {_escape_mdv2(description)}"
    if persistent:
        header = f"{header} _\\(persistent\\)_"
    parts.append(header)

    max_cmd_len = 4096 - 200
    if len(command) > max_cmd_len:
        command = command[:max_cmd_len] + "\n..."
    parts.append(f"```bash\n{_escape_mdv2(command)}\n```")

    return "\n\n".join(parts)


def _format_write_approval(
    tool_input: dict[str, Any], cwd: str | None = None,
) -> str:
    """Format a Write tool call for the approval prompt."""
    file_path = _relative_path(tool_input.get("filePath", "unknown"), cwd)
    content = tool_input.get("content", "")

    escaped_path = _escape_mdv2(file_path)
    header = f"\U0001f4dd *Write:* `{escaped_path}`"

    # Truncate if the content is too long for a single Telegram message.
    max_content_len = 4096 - 200
    if len(content) > max_content_len:
        content = content[:max_content_len] + "\n..."

    escaped_content = _escape_mdv2(content)
    return f"{header}\n\n```\n{escaped_content}\n```"


_APPLY_PATCH_ACTION_ICONS = {"add": "+", "update": "~", "delete": "-", "move": ">"}


def _format_apply_patch_approval(
    tool_input: dict[str, Any], cwd: str | None = None,
) -> str:
    """Format an ApplyPatch tool call for the approval prompt."""
    patch_text = tool_input.get("patchText", "")
    files = parse_apply_patch_files(patch_text)
    # Exclude move targets from the file count so a rename reads as one file.
    file_count = sum(1 for action, _ in files if action != "move")

    parts: list[str] = []
    if files:
        summary_lines = [
            f"{_APPLY_PATCH_ACTION_ICONS.get(action, '?')} {_relative_path(path, cwd)}"
            for action, path in files
        ]
        summary = "\n".join(summary_lines)
        plural = "" if file_count == 1 else "s"
        parts.append(
            f"\U0001fa84 *ApplyPatch* \\({file_count} file{plural}\\)"
        )
        parts.append(f"```\n{_escape_mdv2(summary)}\n```")
    else:
        parts.append("\U0001fa84 *ApplyPatch*")

    max_body_len = 4096 - 400
    body = patch_text
    if len(body) > max_body_len:
        body = body[:max_body_len] + "\n..."
    parts.append(f"```diff\n{_escape_mdv2(body)}\n```")

    return "\n\n".join(parts)


def _format_agent_approval(tool_input: dict[str, Any], expanded: bool = False) -> str:
    """Format an Agent tool call for the approval prompt.

    Shows a compact view with description and subagent type by default.
    When expanded=True, appends the full prompt text.
    """
    description = tool_input.get("description", "")
    subagent_type = tool_input.get("subagent_type", "")
    prompt = tool_input.get("prompt", "")

    parts: list[str] = []

    # Header with subagent type
    if subagent_type:
        parts.append(f"\U0001f916 *Agent* \\({_escape_mdv2(subagent_type)}\\)")
    else:
        parts.append("\U0001f916 *Agent*")

    # Description line
    if description:
        parts.append(_escape_mdv2(description))

    # Full prompt (only when expanded)
    if expanded and prompt:
        max_prompt_len = 4096 - 300
        display_prompt = prompt
        if len(display_prompt) > max_prompt_len:
            display_prompt = display_prompt[:max_prompt_len] + "\n..."
        parts.append(f"```\n{_escape_mdv2(display_prompt)}\n```")

    return "\n\n".join(parts)


def _format_plan_approval(tool_input: dict[str, Any]) -> str:
    """Format an ExitPlanMode tool call for the approval prompt.

    Shows a compact header — the full plan content is viewed via the
    Mini App "View plan" button.
    """
    plan = tool_input.get("plan", "")
    # Show a brief preview of the plan title (first heading or first line).
    preview = ""
    for line in plan.splitlines():
        stripped = line.strip().lstrip("# ").strip()
        if stripped:
            preview = stripped
            break
    if len(preview) > 80:
        preview = preview[:77] + "..."
    header = "\U0001f4cb *Plan*"
    if preview:
        header += f": {_escape_mdv2(preview)}"
    return header


def _format_generic_approval(tool_name: str, tool_input: dict[str, Any]) -> str:
    """Format a generic tool call for the approval prompt."""
    summary_parts = [f"*Tool:* `{tool_name}`"]
    for key, val in tool_input.items():
        val_str = str(val)
        if len(val_str) > 200:
            val_str = val_str[:200] + "..."
        key_escaped = key.replace("_", "\\_")
        val_escaped = _escape_mdv2(val_str)
        summary_parts.append(f"*{key_escaped}:* {val_escaped}")
    return "\n".join(summary_parts)


# ---------------------------------------------------------------------------
# Approval keyboard & auto-approved diff notification
# ---------------------------------------------------------------------------


async def _send_auto_approved_diff(
    bot: Bot,
    chat_id: int,
    tool_name: str,
    tool_input: dict[str, Any],
    cwd: str | None = None,
    thread_id: int | None = None,
) -> None:
    """Send a read-only diff message for an auto-approved edit.

    Similar to the approval keyboard but without buttons -- just shows the
    diff so the user can see what changed even when "accept all edits" is
    active.
    """
    if tool_name == "Edit":
        text = _format_edit_approval(tool_input, cwd=cwd)
    elif tool_name == "Write":
        text = _format_write_approval(tool_input, cwd=cwd)
    elif tool_name == "ApplyPatch":
        text = _format_apply_patch_approval(tool_input, cwd=cwd)
    else:
        text = _format_generic_approval(tool_name, tool_input)

    text += f"\n\u2705 _Auto\\-approved_"

    thread_kwargs: dict[str, Any] = {}
    if thread_id is not None:
        thread_kwargs["message_thread_id"] = thread_id

    try:
        await bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode="MarkdownV2",
            disable_notification=True,
            **thread_kwargs,
        )
    except Exception:
        logger.exception("Failed to send auto-approved diff notification")


async def _send_approval_keyboard(
    bot: Bot,
    chat_id: int,
    tool_name: str,
    tool_input: dict[str, Any],
    tool_use_id: str,
    cwd: str | None = None,
    thread_id: int | None = None,
    base_url: str | None = None,
    user_id: int = 0,
    is_private_chat: bool = True,
    bot_token: str = "",
    suggested_session_dir: str | None = None,
    scope: ChatScope | None = None,
    context_name: str | None = None,
) -> bool:
    """Send an inline keyboard for tool approval and wait for response.

    When ``suggested_session_dir`` is set (the file tool's target is
    outside the approved directories), an extra "Allow <dir>/ this
    session" button is added that, when clicked, adds the directory to
    the session-approved set so subsequent tool calls in that directory
    auto-approve.  ``scope`` and ``context_name`` are required to scope
    that approval state.
    """
    import uuid
    if tool_name == "Edit":
        text = _format_edit_approval(tool_input, cwd=cwd)
    elif tool_name == "Bash":
        text = _format_bash_approval(tool_input)
    elif tool_name == "Monitor":
        text = _format_monitor_approval(tool_input)
    elif tool_name == "Write":
        text = _format_write_approval(tool_input, cwd=cwd)
    elif tool_name == "ApplyPatch":
        text = _format_apply_patch_approval(tool_input, cwd=cwd)
    elif tool_name == "Agent":
        text = _format_agent_approval(tool_input, expanded=False)
    elif tool_name == "ExitPlanMode":
        text = _format_plan_approval(tool_input)
    else:
        text = _format_generic_approval(tool_name, tool_input)

    approve_data = f"approve:{tool_use_id}"
    deny_data = f"deny:{tool_use_id}"
    _approval_tool_names[tool_use_id] = tool_name
    _approval_metadata[tool_use_id] = {
        "tool_name": tool_name,
        "tool_input": tool_input,
        "chat_id": chat_id,
    }

    # Build keyboard rows -- primary actions on top, session-scoped on bottom.
    # Row 1: [Approve] [Deny] (and optional [Show prompt] for Agent)
    primary_row: list[InlineKeyboardButton] = []
    if tool_name == "Agent":
        show_prompt_data = f"show_prompt:{tool_use_id}"
        _pending_agent_inputs[tool_use_id] = tool_input
        primary_row.append(InlineKeyboardButton("Show prompt", callback_data=show_prompt_data))
    primary_row.append(InlineKeyboardButton("Approve", callback_data=approve_data))
    primary_row.append(InlineKeyboardButton("Deny", callback_data=deny_data))

    # Row 2: session-scoped auto-approval buttons
    session_row: list[InlineKeyboardButton] = []
    if tool_name in ACCEPT_ALL_EDITS_TOOLS:
        accept_all_data = f"accept_all_edits:{tool_use_id}"
        session_row.append(InlineKeyboardButton("Accept all edits", callback_data=accept_all_data))
    # Bash: prefix-specific persistent rule only (no blanket "Accept all
    # Bash") — over-broad Bash approval is a security risk.
    if tool_name == "Bash":
        command = tool_input.get("command", "")
        prefix = _extract_bash_prefix(command)
        if prefix:
            accept_prefix_data = f"accept_bash_pfx:{tool_use_id}:{prefix}"
            if len(accept_prefix_data.encode()) <= 64:
                session_row.append(InlineKeyboardButton(
                    f"Allow & remember: {prefix} *",
                    callback_data=accept_prefix_data,
                ))
    # Path-scoped tools are excluded from the generic "Accept all <tool>"
    # button: an in-scope path auto-approves already, and an out-of-scope
    # path must go through the directory-scoped button below — blanket
    # per-tool rules must not bypass the directory boundary.  Bash and
    # ExitPlanMode have their own dedicated approval flows.
    _no_accept_all = _PATH_SCOPED_TOOLS.keys() | {"ExitPlanMode", "Bash", "Monitor"}
    accept_all_tool_key = ""
    accept_all_tool_data = ""
    if tool_name not in _no_accept_all:
        accept_all_tool_key = uuid.uuid4().hex[:12]
        _pending_tool_approvals[accept_all_tool_key] = tool_name
        accept_all_tool_data = f"accept_all_tool:{accept_all_tool_key}"
        # The short token keeps callback_data well under 64 bytes even for
        # MCP names like ``mcp__playwright__browser_navigate``.
        session_row.append(InlineKeyboardButton(
            f"Accept all {tool_name}", callback_data=accept_all_tool_data,
        ))

    # Out-of-scope file access: offer to approve the entire directory
    # for the rest of the session (mirrors Claude Code).  Both readers
    # and editors get the same underlying grant — the wording differs
    # so the button reads naturally per tool family.
    accept_dir_data = ""
    accept_dir_key = ""
    if suggested_session_dir and scope is not None and context_name is not None:
        accept_dir_key = uuid.uuid4().hex[:12]
        _pending_session_dirs[accept_dir_key] = (
            scope, context_name, suggested_session_dir,
        )
        accept_dir_data = f"accept_dir:{tool_use_id}:{accept_dir_key}"
        if len(accept_dir_data.encode()) <= 64:
            dir_label = os.path.basename(
                suggested_session_dir.rstrip(os.sep)
            ) or suggested_session_dir
            # Truncate the directory label so the button stays readable.
            if len(dir_label) > 24:
                dir_label = "\u2026" + dir_label[-23:]
            if tool_name in ("Edit", "Write"):
                btn_label = f"Allow all edits in {dir_label}/"
            else:
                btn_label = f"Allow reading from {dir_label}/"
            session_row.append(InlineKeyboardButton(
                btn_label, callback_data=accept_dir_data,
            ))
        else:
            # Couldn't fit — drop the pending entry.
            _pending_session_dirs.pop(accept_dir_key, None)
            accept_dir_data = ""
            accept_dir_key = ""

    rows = [primary_row]
    # ExitPlanMode: add "View plan" as its own row (web_app buttons need space).
    if tool_name == "ExitPlanMode" and base_url:
        plan = tool_input.get("plan", "")
        if plan:
            from open_shrimp.preview.api import store_ephemeral_content

            content_id = store_ephemeral_content(
                "Plan", plan,
                chat_id=chat_id,
                thread_id=thread_id,
                tool_use_id=tool_use_id,
            )
            thread_param = (
                f"&thread_id={thread_id}" if thread_id is not None else ""
            )
            app_url = (
                f"{base_url}/preview/"
                f"?content_id={content_id}"
                f"&chat_id={chat_id}"
                f"{thread_param}"
            )
            rows.append([make_web_app_button(
                "\U0001f4cb View plan",
                app_url,
                chat_id=chat_id,
                user_id=user_id,
                bot_token=bot_token,
                is_private_chat=is_private_chat,
            )])
    if session_row:
        rows.append(session_row)
    keyboard = InlineKeyboardMarkup(rows)

    thread_kwargs: dict[str, Any] = {}
    if thread_id is not None:
        thread_kwargs["message_thread_id"] = thread_id

    sent_msg = await bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode="MarkdownV2",
        reply_markup=keyboard,
        **thread_kwargs,
    )
    _approval_metadata[tool_use_id]["message_id"] = sent_msg.message_id

    # Create a future and wait for the callback
    loop = asyncio.get_running_loop()
    future: asyncio.Future[bool] = loop.create_future()
    _approval_futures[approve_data] = future
    _approval_futures[deny_data] = future
    if tool_name in ACCEPT_ALL_EDITS_TOOLS:
        _approval_futures[f"accept_all_edits:{tool_use_id}"] = future
    if accept_all_tool_data:
        _approval_futures[accept_all_tool_data] = future
    # Register prefix-specific key for Bash.
    accept_prefix_key = ""
    if tool_name == "Bash":
        command = tool_input.get("command", "")
        prefix = _extract_bash_prefix(command)
        if prefix:
            accept_prefix_key = f"accept_bash_pfx:{tool_use_id}:{prefix}"
            if len(accept_prefix_key.encode()) <= 64:
                _approval_futures[accept_prefix_key] = future
    if accept_dir_data:
        _approval_futures[accept_dir_data] = future

    try:
        return await future
    finally:
        _approval_futures.pop(approve_data, None)
        _approval_futures.pop(deny_data, None)
        _approval_futures.pop(f"accept_all_edits:{tool_use_id}", None)
        if accept_all_tool_data:
            _approval_futures.pop(accept_all_tool_data, None)
        if accept_all_tool_key:
            # The user may have resolved via approve/deny — drop the stash.
            _pending_tool_approvals.pop(accept_all_tool_key, None)
        if accept_prefix_key:
            _approval_futures.pop(accept_prefix_key, None)
        if accept_dir_data:
            _approval_futures.pop(accept_dir_data, None)
            # If the user resolved via approve/deny instead of the
            # session-dir button, the pending entry is now dead weight.
            _pending_session_dirs.pop(accept_dir_key, None)
        _pending_agent_inputs.pop(tool_use_id, None)
        _approval_tool_names.pop(tool_use_id, None)
        _approval_metadata.pop(tool_use_id, None)


# ---------------------------------------------------------------------------
# host_bash (sudo mode) approval — dedicated flow with 10s auto-deny + live
# countdown. Uses its own callback prefixes (hb_approve:/hb_deny:) so the
# standard approve/deny handler doesn't fight with the countdown task over
# message edits.
# ---------------------------------------------------------------------------


_HOST_BASH_TIMEOUT_SECONDS = 10.0
_HOST_BASH_TICK_SECONDS = 2.0
_HOST_BASH_APPROVE_PREFIX = "hb_approve:"
_HOST_BASH_DENY_PREFIX = "hb_deny:"


def _render_command_block(command: str, max_len: int) -> str:
    """Render a bash command as a MarkdownV2 code block with truncation."""
    shown = command
    if len(shown) > max_len:
        shown = shown[:max_len] + "\n..."
    return f"```bash\n{_escape_mdv2(shown)}\n```"


def _format_host_bash_approval(
    tool_input: dict[str, Any], remaining: float,
) -> str:
    """Render the host_bash approval prompt with a countdown line."""
    command = tool_input.get("command", "")
    description = tool_input.get("description", "")
    cwd = tool_input.get("cwd", "")

    header = "\u26a0\ufe0f *HOST shell* \\(sudo mode\\)"
    parts: list[str] = [header]
    if description:
        parts.append(_escape_mdv2(description))
    parts.append(_render_command_block(command, 4096 - 400))
    if cwd:
        parts.append(f"_cwd:_ `{_escape_mdv2(cwd)}`")
    secs = max(0, int(round(remaining)))
    parts.append(
        f"_Auto\\-deny in {secs}s \u2014 this command runs OUTSIDE the "
        f"sandbox\\._"
    )
    return "\n\n".join(parts)


def _format_host_bash_final(
    tool_input: dict[str, Any], outcome: HostBashOutcome,
) -> str:
    """Render the final state of the host_bash approval message."""
    icon = {
        "approved": "\u2705",
        "denied": "\u274c",
        "timeout": "\u23f1\ufe0f",
    }[outcome]
    verb = {
        "approved": "Approved",
        "denied": "Denied",
        "timeout": "Auto\\-denied \\(no response within 10s\\)",
    }[outcome]
    block = _render_command_block(tool_input.get("command", ""), 4096 - 200)
    return f"{icon} *HOST shell* \u2014 {verb}\n\n{block}"


async def _host_bash_countdown(
    bot: Bot,
    chat_id: int,
    message_id: int,
    tool_use_id: str,
    tool_input: dict[str, Any],
    deadline: float,
    future: asyncio.Future[bool],
) -> None:
    """Edit the approval message every tick with the remaining countdown.

    Exits as soon as ``future`` is done (user clicked, or timer fired). All
    Telegram errors are swallowed — the countdown is purely cosmetic.
    """
    loop = asyncio.get_running_loop()
    last_secs = int(round(_HOST_BASH_TIMEOUT_SECONDS))
    # Skip the first edit since the initial send already shows the countdown
    # — go straight to sleeping.
    while True:
        try:
            await asyncio.wait_for(
                asyncio.shield(future), timeout=_HOST_BASH_TICK_SECONDS,
            )
            return
        except asyncio.TimeoutError:
            pass
        except Exception:
            return
        if future.done():
            return
        remaining = deadline - loop.time()
        if remaining <= 0:
            return
        secs = max(0, int(round(remaining)))
        # Telegram rejects edits with identical text — skip when the
        # rounded second hasn't advanced (e.g. consecutive 2s ticks both
        # round to the same display value).
        if secs == last_secs:
            continue
        last_secs = secs
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=_format_host_bash_approval(tool_input, remaining),
                parse_mode="MarkdownV2",
                reply_markup=_host_bash_keyboard(tool_use_id),
            )
        except Exception:
            # Likely a rate-limit or "message not modified" — ignore.
            pass


def _host_bash_keyboard(tool_use_id: str) -> InlineKeyboardMarkup:
    """Build the two-button [Approve] [Deny] keyboard for host_bash.

    No pattern/session rules — every host-escape command needs a fresh,
    intentional approval.
    """
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "Approve",
            callback_data=f"{_HOST_BASH_APPROVE_PREFIX}{tool_use_id}",
        ),
        InlineKeyboardButton(
            "Deny",
            callback_data=f"{_HOST_BASH_DENY_PREFIX}{tool_use_id}",
        ),
    ]])


async def _send_host_bash_approval(
    bot: Bot,
    chat_id: int,
    context_name: str,
    tool_input: dict[str, Any],
    tool_use_id: str,
    thread_id: int | None = None,
) -> HostBashOutcome:
    """Send a host_bash approval prompt and resolve to approved/denied/timeout.

    Blocks until the user clicks one of the buttons or the 10-second timer
    fires. Edits the message with a live countdown while waiting and writes
    an audit entry to ``~/.config/openshrimp/sudo.log`` on resolution.
    """
    loop = asyncio.get_running_loop()
    future: asyncio.Future[bool] = loop.create_future()
    timed_out = [False]

    def _auto_deny() -> None:
        if not future.done():
            timed_out[0] = True
            future.set_result(False)

    timer = loop.call_later(_HOST_BASH_TIMEOUT_SECONDS, _auto_deny)
    deadline = loop.time() + _HOST_BASH_TIMEOUT_SECONDS

    approve_data = f"{_HOST_BASH_APPROVE_PREFIX}{tool_use_id}"
    deny_data = f"{_HOST_BASH_DENY_PREFIX}{tool_use_id}"
    _approval_futures[approve_data] = future
    _approval_futures[deny_data] = future
    _approval_tool_names[tool_use_id] = "mcp__openshrimp__host_bash"
    _approval_metadata[tool_use_id] = {
        "tool_name": "mcp__openshrimp__host_bash",
        "tool_input": tool_input,
        "chat_id": chat_id,
    }

    thread_kwargs: dict[str, Any] = {}
    if thread_id is not None:
        thread_kwargs["message_thread_id"] = thread_id

    sent_msg = await bot.send_message(
        chat_id=chat_id,
        text=_format_host_bash_approval(tool_input, _HOST_BASH_TIMEOUT_SECONDS),
        parse_mode="MarkdownV2",
        reply_markup=_host_bash_keyboard(tool_use_id),
        **thread_kwargs,
    )
    message_id = sent_msg.message_id
    _approval_metadata[tool_use_id]["message_id"] = message_id

    countdown_task = asyncio.create_task(_host_bash_countdown(
        bot, chat_id, message_id, tool_use_id, tool_input, deadline, future,
    ))

    try:
        approved = await future
    finally:
        timer.cancel()
        countdown_task.cancel()
        try:
            await countdown_task
        except (asyncio.CancelledError, Exception):
            pass
        _approval_futures.pop(approve_data, None)
        _approval_futures.pop(deny_data, None)
        _approval_tool_names.pop(tool_use_id, None)
        _approval_metadata.pop(tool_use_id, None)

    if timed_out[0]:
        outcome: HostBashOutcome = "timeout"
    elif approved:
        outcome = "approved"
    else:
        outcome = "denied"

    try:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=_format_host_bash_final(tool_input, outcome),
            parse_mode="MarkdownV2",
            reply_markup=None,
        )
    except Exception:
        logger.debug(
            "Failed to edit host_bash approval message", exc_info=True,
        )

    await log_sudo(
        chat_id=chat_id,
        context_name=context_name,
        command=tool_input.get("command", ""),
        outcome=outcome,
    )
    return outcome


# ---------------------------------------------------------------------------
# Auto-resolve parallel pending approvals after "accept all" actions
# ---------------------------------------------------------------------------


async def _auto_resolve_pending_approvals(
    bot: Bot,
    rule: ApprovalRule | None,
    is_edit_rule: bool,
    chat_id: int,
    approved_dir: str | None = None,
) -> None:
    """Resolve all pending approval futures that match a newly created rule.

    Called after an "accept all" action to automatically approve parallel
    tool calls that are still waiting for user input.

    Args:
        bot: Telegram bot instance for editing messages.
        rule: The approval rule to match against (for tool rules).
            None when is_edit_rule is True.
        is_edit_rule: True for "accept all edits" (matches Edit/Write).
        chat_id: Only resolve approvals in this chat.
        approved_dir: When set, auto-resolve any pending path-scoped tool
            whose target file/directory resolves to within this directory.
            Used by the session-dir approval button.
    """
    from open_shrimp.hooks import matches_approval_rule, tool_path_within_dir

    # Snapshot the metadata keys to avoid mutating dict during iteration.
    for tool_use_id, meta in list(_approval_metadata.items()):
        if meta.get("chat_id") != chat_id:
            continue

        t_name = meta["tool_name"]
        t_input = meta["tool_input"]
        msg_id = meta.get("message_id")

        # Check if this pending approval matches the new rule.
        matched = False
        if is_edit_rule and t_name in ACCEPT_ALL_EDITS_TOOLS:
            matched = True
        elif rule is not None and matches_approval_rule(rule, t_name, t_input):
            matched = True
        elif approved_dir is not None and tool_path_within_dir(
            t_name, t_input, approved_dir,
        ):
            matched = True

        if not matched:
            continue

        # Find and resolve the future for this tool_use_id.
        approve_key = f"approve:{tool_use_id}"
        future = _approval_futures.get(approve_key)
        if future is None or future.done():
            continue

        future.set_result(True)
        logger.info(
            "Auto-resolved pending approval for %s (tool_use_id=%s)",
            t_name,
            tool_use_id,
        )

        # Update the Telegram message to show auto-approved status.
        if msg_id:
            try:
                escaped_tool = _escape_mdv2(t_name)
                icon = '\u2705'
                compact = f"{icon} *{escaped_tool}* \u2014 Auto\\-approved\\."
                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=msg_id,
                    text=compact,
                    parse_mode="MarkdownV2",
                    reply_markup=None,
                )
            except Exception:
                logger.exception(
                    "Failed to edit auto-resolved approval message"
                )


# ---------------------------------------------------------------------------
# Callback query handling for approval-related buttons
# ---------------------------------------------------------------------------


async def handle_approval_callback(
    query: Any,
    data: str,
    config: Any,
    context: Any,
) -> bool:
    """Handle approval-related callback queries.

    Handles: approve:*, deny:*, show_prompt:*, show_bash:*,
    accept_all_edits:*, accept_bash_pfx:*, accept_all_tool:*.
    Returns True if the callback was handled.
    """
    import aiosqlite

    from open_shrimp.db import ChatScope
    from open_shrimp.handlers.state import _edit_approved_sessions
    from open_shrimp.handlers.utils import _get_context, chat_scope_from_message
    from open_shrimp.stream import _bash_output_store

    # Handle "Show prompt" expansion for Agent tool
    if data.startswith("show_prompt:"):
        tool_use_id = data[len("show_prompt:"):]
        tool_input = _pending_agent_inputs.get(tool_use_id)
        if not tool_input:
            await query.answer("Prompt data no longer available.")
            return True

        await query.answer()

        # Re-render the message with expanded prompt, remove "Show prompt" button
        if query.message:
            expanded_text = _format_agent_approval(tool_input, expanded=True)
            approve_data = f"approve:{tool_use_id}"
            deny_data = f"deny:{tool_use_id}"
            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("Approve", callback_data=approve_data),
                        InlineKeyboardButton("Deny", callback_data=deny_data),
                    ]
                ]
            )
            try:
                await query.message.edit_text(
                    text=expanded_text,
                    parse_mode="MarkdownV2",
                    reply_markup=keyboard,
                )
            except Exception:
                logger.exception("Failed to expand Agent prompt")
        return True

    # Handle "Show output" for Bash tool results
    if data.startswith("show_bash:"):
        formatted_output = _bash_output_store.pop(data, None)
        if not formatted_output:
            await query.answer("Output data no longer available.")
            return True

        await query.answer()

        if query.message:
            from open_shrimp.markdown import gfm_to_telegram

            chunks = gfm_to_telegram(formatted_output)
            expanded_text = chunks[0] if chunks else ""
            try:
                await query.message.edit_text(
                    text=expanded_text,
                    parse_mode="MarkdownV2",
                    reply_markup=None,
                )
            except Exception:
                logger.exception("Failed to expand Bash output")
                # Fallback: just remove the button
                try:
                    await query.message.edit_reply_markup(reply_markup=None)
                except Exception:
                    logger.exception("Failed to remove bash button")
        return True

    # Handle "Accept all edits" -- approve this tool and enable auto-approval
    # for all future Edit/Write calls within cwd for this session.
    if data.startswith("accept_all_edits:"):
        future = _approval_futures.get(data)
        if not future or future.done():
            await query.answer("This approval has expired.")
            return True

        # Determine the chat's active context to scope the flag
        if query.message:
            scope = chat_scope_from_message(query.message)
            db: aiosqlite.Connection = context.bot_data["db"]
            ctx_name, _ = await _get_context(scope, config, db)
            _edit_approved_sessions.add((scope, ctx_name))
            logger.info(
                "Accept-all-edits enabled for scope %s context %s",
                scope,
                ctx_name,
            )

        future.set_result(True)
        await query.answer("Approved. All future edits will be auto-approved.")

        if query.message:
            try:
                original_md = query.message.text_markdown_v2 or query.message.text or ""
                status = "\n\n\u2705 *Approved\\.* _All future edits auto\\-approved\\._"
                await query.message.edit_text(
                    text=original_md + status,
                    parse_mode="MarkdownV2",
                    reply_markup=None,
                )
            except Exception:
                try:
                    await query.message.edit_reply_markup(reply_markup=None)
                except Exception:
                    logger.exception("Failed to edit approval message")

        # Auto-resolve other pending parallel approvals for Edit/Write.
        chat_id = query.message.chat_id if query.message else None
        if chat_id is not None:
            await _auto_resolve_pending_approvals(
                query.get_bot(), rule=None, is_edit_rule=True, chat_id=chat_id,
            )
        return True

    # Handle "Accept all <prefix>" for Bash commands — approve this tool and
    # enable auto-approval for future Bash commands matching "<prefix> *".
    if data.startswith("accept_bash_pfx:"):
        future = _approval_futures.get(data)
        if not future or future.done():
            await query.answer("This approval has expired.")
            return True

        # Parse: "accept_bash_pfx:<id>:<prefix>"
        parts = data.split(":", 2)
        prefix = parts[2] if len(parts) >= 3 else ""

        if query.message and prefix:
            from open_shrimp.handlers.state import _tool_approved_sessions
            from open_shrimp.settings_local import save_persistent_rule

            scope = chat_scope_from_message(query.message)
            db: aiosqlite.Connection = context.bot_data["db"]
            ctx_name, ctx_config = await _get_context(scope, config, db)
            rule = ApprovalRule(tool_name="Bash", pattern=f"{prefix} *")
            _tool_approved_sessions.setdefault((scope, ctx_name), []).append(rule)

            # Persist to .claude/settings.local.json so the rule survives
            # restarts and is also respected by the Claude CLI directly.
            try:
                persisted = await save_persistent_rule(ctx_config.directory, rule)
            except OSError:
                logger.exception("Failed to persist rule to settings.local.json")
                persisted = False

            logger.info(
                "Saved persistent Bash(%s:*) rule for scope %s context %s (persisted=%s)",
                prefix,
                scope,
                ctx_name,
                persisted,
            )

        future.set_result(True)
        escaped_prefix = _escape_mdv2(prefix)
        await query.answer(
            f"Approved. Rule saved: {prefix} * auto-approved."
        )

        if query.message:
            try:
                icon = '\u2705'
                compact = (
                    f"{icon} *Bash* \u2014 Approved\\. "
                    f"_Rule saved: {escaped_prefix} \\* auto\\-approved\\._"
                )
                await query.message.edit_text(
                    text=compact,
                    parse_mode="MarkdownV2",
                    reply_markup=None,
                )
            except Exception:
                try:
                    await query.message.edit_reply_markup(reply_markup=None)
                except Exception:
                    logger.exception("Failed to edit approval message")

        # Auto-resolve other pending parallel Bash approvals matching this prefix.
        chat_id = query.message.chat_id if query.message else None
        if chat_id is not None and prefix:
            await _auto_resolve_pending_approvals(
                query.get_bot(), rule=rule, is_edit_rule=False, chat_id=chat_id,
            )
        return True

    # Handle "Allow <reading from|all edits in> <dir>/ this session" --
    # approve this tool and grant full read+write access to the directory
    # for the remainder of the session.
    if data.startswith("accept_dir:"):
        future = _approval_futures.get(data)
        if not future or future.done():
            await query.answer("This approval has expired.")
            return True

        # Parse: "accept_dir:<tool_use_id>:<short_key>"
        parts = data.split(":", 2)
        short_key = parts[2] if len(parts) >= 3 else ""

        from open_shrimp.handlers.state import (
            _pending_session_dirs,
            _session_approved_dirs,
        )

        pending = _pending_session_dirs.pop(short_key, None)
        if pending is None:
            await query.answer("This action has expired.")
            return True

        scope, ctx_name, directory = pending
        _session_approved_dirs.setdefault((scope, ctx_name), set()).add(
            directory,
        )
        logger.info(
            "Session-approved dir %s for scope %s context %s",
            directory,
            scope,
            ctx_name,
        )

        future.set_result(True)
        escaped_dir = _escape_mdv2(directory)
        await query.answer(
            f"Approved. {directory}/ allowed for this session."
        )

        if query.message:
            try:
                original_md = (
                    query.message.text_markdown_v2
                    or query.message.text
                    or ""
                )
                status = (
                    f"\n\n\u2705 *Approved\\.* "
                    f"_All future tool calls in `{escaped_dir}` "
                    f"auto\\-approved this session\\._"
                )
                await query.message.edit_text(
                    text=original_md + status,
                    parse_mode="MarkdownV2",
                    reply_markup=None,
                )
            except Exception:
                try:
                    await query.message.edit_reply_markup(reply_markup=None)
                except Exception:
                    logger.exception("Failed to edit approval message")

        # Auto-resolve other pending approvals whose target is in this dir.
        chat_id = query.message.chat_id if query.message else None
        if chat_id is not None:
            await _auto_resolve_pending_approvals(
                query.get_bot(),
                rule=None,
                is_edit_rule=False,
                chat_id=chat_id,
                approved_dir=directory,
            )
        return True

    # Handle "Accept all <tool>" -- approve this tool and enable auto-approval
    # for all future uses of that specific tool for this session.
    if data.startswith("accept_all_tool:"):
        future = _approval_futures.get(data)
        if not future or future.done():
            await query.answer("This approval has expired.")
            return True

        # Callback data is "accept_all_tool:<token>" — look the tool name up
        # in the side dict so MCP names that wouldn't fit in 64 bytes still
        # work.
        token = data.split(":", 1)[1]
        accepted_tool_name = _pending_tool_approvals.pop(token, "")

        # Determine the chat's active context to scope the flag
        if query.message and accepted_tool_name:
            from open_shrimp.handlers.state import _tool_approved_sessions

            scope = chat_scope_from_message(query.message)
            db: aiosqlite.Connection = context.bot_data["db"]
            ctx_name, _ = await _get_context(scope, config, db)
            rule = ApprovalRule(tool_name=accepted_tool_name, pattern=None)
            _tool_approved_sessions.setdefault((scope, ctx_name), []).append(rule)
            logger.info(
                "Accept-all-%s enabled for scope %s context %s",
                accepted_tool_name,
                scope,
                ctx_name,
            )

        future.set_result(True)
        escaped_tool = _escape_mdv2(accepted_tool_name)
        await query.answer(
            f"Approved. All future {accepted_tool_name} calls will be auto-approved."
        )

        if query.message:
            try:
                original_md = query.message.text_markdown_v2 or query.message.text or ""
                status = (
                    f"\n\n\u2705 *Approved\\.* _All future {escaped_tool} "
                    f"calls auto\\-approved\\._"
                )
                await query.message.edit_text(
                    text=original_md + status,
                    parse_mode="MarkdownV2",
                    reply_markup=None,
                )
            except Exception:
                try:
                    await query.message.edit_reply_markup(reply_markup=None)
                except Exception:
                    logger.exception("Failed to edit approval message")

        # Auto-resolve other pending parallel approvals for this tool.
        chat_id = query.message.chat_id if query.message else None
        if chat_id is not None and accepted_tool_name:
            await _auto_resolve_pending_approvals(
                query.get_bot(), rule=rule, is_edit_rule=False, chat_id=chat_id,
            )
        return True

    # Handle host_bash (sudo mode) approve/deny — resolve the future only;
    # the helper function is responsible for the final message edit and the
    # audit log entry so the countdown task and the edit can't race.
    if data.startswith(_HOST_BASH_APPROVE_PREFIX) or data.startswith(
        _HOST_BASH_DENY_PREFIX,
    ):
        future = _approval_futures.get(data)
        if not future or future.done():
            await query.answer("This approval has expired.")
            return True
        approved = data.startswith(_HOST_BASH_APPROVE_PREFIX)
        future.set_result(approved)
        await query.answer("Approved." if approved else "Denied.")
        return True

    # Handle approve/deny
    if data.startswith("approve:") or data.startswith("deny:"):
        future = _approval_futures.get(data)
        if not future or future.done():
            await query.answer("This approval has expired.")
            return True

        approved = data.startswith("approve:")
        future.set_result(approved)

        # Extract tool_use_id from callback data (format: "approve:<id>" or "deny:<id>")
        tool_use_id = data.split(":", 1)[1] if ":" in data else ""
        tool_name = _approval_tool_names.get(tool_use_id, "")

        action = "Approved" if approved else "Denied"
        await query.answer(f"{action}.")

        # Update the message to show the decision (remove buttons, append status).
        # For Bash, collapse to a compact one-liner since the "Show output" button
        # message that follows will show the command again -- avoids duplication.
        if query.message:
            try:
                if tool_name == "Bash":
                    icon = '\u2705' if approved else '\u274c'
                    compact = f"{icon} *{_escape_mdv2(tool_name)}* \u2014 {action}\\."
                    await query.message.edit_text(
                        text=compact,
                        parse_mode="MarkdownV2",
                        reply_markup=None,
                    )
                else:
                    original_md = query.message.text_markdown_v2 or query.message.text or ""
                    icon = '\u2705' if approved else '\u274c'
                    status = f"\n\n{icon} *{action}\\.*"
                    await query.message.edit_text(
                        text=original_md + status,
                        parse_mode="MarkdownV2",
                        reply_markup=None,
                    )
            except Exception:
                # Fallback: just remove the keyboard without modifying text
                try:
                    await query.message.edit_reply_markup(reply_markup=None)
                except Exception:
                    logger.exception("Failed to edit approval message")
        return True

    return False
