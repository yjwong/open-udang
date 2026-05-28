"""Tool permission callbacks for OpenShrimp.

Implements the tool permission policy for OpenCode. When a tool is not covered
by session permission rules, OpenCode asks for permission via this callback. We
present a Telegram inline keyboard and await the user's decision.

Path-scoped auto-approval: read-only file-access tools (Read, Glob, Grep)
are auto-approved when their target paths resolve to within the context's
working directory. Mutating tools (Edit, Write) always require explicit
approval, even within the working directory, unless the user has opted into
"accept all edits" for the current session. In containerized contexts, all
path-scoped tools (including Edit/Write) are auto-approved regardless of
path, since Docker provides the safety boundary. Paths outside the working
directory always fall through to the interactive Telegram approval prompt.
This prevents the agent from silently reading arbitrary files (e.g. ~/.ssh/*,
config files with secrets) when these tools are removed from allowedTools and
handled here instead.
"""

import fnmatch
import logging
import os
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from open_shrimp.opencode_client.events import (
    PermissionResult,
    PermissionResultAllow,
    PermissionResultDeny,
    ToolPermissionContext,
)

logger = logging.getLogger(__name__)

# Dedicated temp directory for file uploads.  Read access to files within
# this directory is auto-approved so the agent doesn't need extra
# permission to read user-uploaded attachments.
ATTACHMENT_TEMP_DIR = Path(tempfile.gettempdir()) / "openshrimp_uploads"

# Type for the approval callback: receives tool_name, tool_input dict,
# tool_use_id, and an optional ``suggested_session_dir`` (set when the
# tool's target path is outside the approved directories — the caller
# may offer a "Allow <dir>/ this session" button); returns True (allow)
# or False (deny).
ApprovalCallback = Callable[
    [str, dict[str, Any], str, str | None], Awaitable[bool]
]

# Outcome of a host_bash approval prompt.
HostBashOutcome = Literal["approved", "denied", "timeout"]

# Type for the host_bash approval callback: receives the host-escape tool's
# input dict and a tool_use_id, returns the resolution outcome. Distinct
# from the generic approval callback so the host-escape flow can implement
# its own UI (live countdown, no pattern rules, audit logging) without
# complicating the standard approval path.
HostBashApprovalCallback = Callable[
    [dict[str, Any], str], Awaitable[HostBashOutcome]
]

# Fully-qualified name of the host_bash MCP tool — used in several places to
# special-case the host-escape path.
HOST_BASH_TOOL_NAME = "mcp__openshrimp__host_bash"

# Type for the auto-approved edit notification callback: receives tool_name
# and tool_input dict. Called (fire-and-forget) when a mutating tool is
# auto-approved so the user can still see the diff without blocking.
EditNotifyCallback = Callable[[str, dict[str, Any]], Awaitable[None]]


async def _notify_edit(
    notify: EditNotifyCallback | None,
    tool_name: str,
    tool_input: dict[str, Any],
) -> None:
    """Fire the auto-approved-edit notification, swallowing errors."""
    if notify is None:
        return
    try:
        await notify(tool_name, tool_input)
    except Exception:
        logger.exception("Failed to send auto-approved edit notification")

# Type for the per-tool auto-approval check: receives tool_name and
# tool_input, returns True if the user has opted into auto-approval for
# that tool (possibly with a pattern constraint) this session.
ToolAutoApprovedCallback = Callable[[str, dict[str, Any]], bool]


# ---------------------------------------------------------------------------
# Pattern-based approval rules
# ---------------------------------------------------------------------------


@dataclass
class ApprovalRule:
    """A session-scoped auto-approval rule.

    ``tool_name`` must always match.  When ``pattern`` is set, it is matched
    against the tool's input using fnmatch glob semantics (for Bash this is
    the command string).  A ``None`` pattern means blanket approval for the
    tool.
    """

    tool_name: str
    pattern: str | None = None


# Bash commands that are auto-approved when "accept all edits" is active.
# Mirrors Claude Code's acceptEdits mode allowlist — these are common
# file-manipulation commands that complement Edit/Write auto-approval.
_ACCEPT_EDITS_BASH_COMMANDS: set[str] = {
    "mkdir", "touch", "rm", "rmdir", "mv", "cp", "sed", "chmod",
}


def _extract_bash_base_command(command: str) -> str | None:
    """Extract the base command name from a bash command string.

    Strips any leading path (e.g. ``/bin/mkdir`` → ``mkdir``) and returns
    the first word.  Returns None if the command is empty.
    """
    cmd = command.strip()
    if not cmd:
        return None
    base = cmd.split()[0]
    # Strip any leading path (e.g. /bin/mkdir -> mkdir)
    return base.rsplit("/", 1)[-1] or None


def _extract_bash_path_args(command: str) -> list[str]:
    """Extract positional (non-flag) arguments from a bash command.

    Strips the base command and any flags (words starting with ``-``).
    Returns the remaining words as path arguments.  This is intentionally
    simple — it doesn't handle quoting or escaping, which means edge
    cases fall through to the interactive approval prompt (safe default).
    """
    words = command.strip().split()
    if len(words) <= 1:
        return []
    # Skip the base command, collect non-flag arguments.
    return [w for w in words[1:] if not w.startswith("-")]


def _is_dangerous_rm_target(path: str) -> bool:
    """Return True if *path* is a dangerous target for rm/rmdir.

    Mirrors Claude Code's ``f8f`` function: catches ``/``, home dir,
    top-level directories, and dangerous globs.
    """
    if path == "*" or path.endswith("/*"):
        return True
    normalized = path.rstrip("/") or "/"
    if normalized == "/":
        return True
    home = os.path.expanduser("~")
    if os.path.realpath(normalized) == os.path.realpath(home):
        return True
    # Top-level directories (e.g. /usr, /etc, /bin)
    parent = os.path.dirname(normalized)
    if parent == "/":
        return True
    return False


def _is_single_subcommand_safe(
    subcommand: str, approved_dirs: list[str]
) -> bool:
    """Check whether a single (non-compound) subcommand is safe.

    Returns True only if the base command is in the allowlist and all
    path arguments resolve to within the approved directories.
    """
    base = _extract_bash_base_command(subcommand)
    if base is None or base not in _ACCEPT_EDITS_BASH_COMMANDS:
        return False

    path_args = _extract_bash_path_args(subcommand)

    for arg in path_args:
        # Reject shell expansion characters — we can't reliably resolve
        # the actual path without executing the shell.
        if "$" in arg or "`" in arg or "~" in arg or "%" in arg:
            return False

        # For rm/rmdir, check for dangerous targets before path resolution.
        if base in ("rm", "rmdir") and _is_dangerous_rm_target(arg):
            return False

    # Validate that all path arguments resolve to within approved dirs.
    for arg in path_args:
        # Glob patterns (containing * or ?) can't be reliably resolved —
        # reject them for write operations.
        if "*" in arg or "?" in arg:
            return False

        resolved = os.path.realpath(arg)
        if not any(
            _is_path_within_directory(resolved, d)
            for d in approved_dirs
        ):
            return False

    return True


def _is_safe_bash_for_accept_edits(
    command: str, approved_dirs: list[str]
) -> bool:
    """Return True if *command* is safe to auto-approve in accept-all-edits mode.

    Uses tree-sitter to parse compound commands into structured
    ParsedCommand objects with resolved arguments.  Every command must
    pass the allowlist and path checks, and compound command safety
    rules (cd + write, cd + git, multiple cd, subcommand cap) must pass.

    If any check fails, returns False so the command falls through to
    the interactive approval prompt.
    """
    from open_shrimp.bash_parse import (
        check_compound_safety,
        parse_command,
    )

    result = parse_command(command)
    if result.kind != "simple":
        return False

    subcommands = [cmd.text for cmd in result.commands]
    if not subcommands:
        return False

    # Compound command safety checks (cd+write, cd+git, multiple cd, cap).
    safety_reason = check_compound_safety(subcommands)
    if safety_reason is not None:
        return False

    # Every subcommand must individually pass the allowlist + path check.
    return all(
        _is_single_subcommand_safe(sub, approved_dirs)
        for sub in subcommands
    )


def matches_approval_rule(
    rule: ApprovalRule,
    tool_name: str,
    tool_input: dict[str, Any],
) -> bool:
    """Return True if *rule* matches the given tool invocation.

    For Bash pattern rules (e.g. ``git *``), compound commands are
    **not** matched — prefix/wildcard allow rules skip compound
    commands to prevent ``git *``
    from auto-approving ``git status && rm -rf /``.  Blanket rules
    (``pattern is None``) still match compound commands.
    """
    if rule.tool_name != tool_name:
        return False
    if rule.pattern is None:
        return True
    # For Bash, match the pattern against the full command string,
    # but skip compound commands for safety.
    if tool_name == "Bash":
        command = tool_input.get("command", "")
        from open_shrimp.bash_parse import is_compound_command
        if is_compound_command(command):
            return False
        return fnmatch.fnmatch(command, rule.pattern)
    # For other tools, pattern is currently unused — treat as match.
    return True

# Tools that access the filesystem, mapped to the input key(s) containing
# the path to check. Each value is a list of keys to try (first match wins).
_PATH_SCOPED_TOOLS: dict[str, list[str]] = {
    "Read": ["filePath"],
    "Write": ["filePath"],
    "Edit": ["filePath"],
    "Glob": ["path"],     # optional; defaults to cwd when absent
    "Grep": ["path"],     # optional; defaults to cwd when absent
}

# Mutating file-access tools that require explicit approval even when the
# target path is within the context working directory.  Read-only tools
# (Read, Glob, Grep) are still auto-approved within cwd.
_MUTATING_PATH_TOOLS: set[str] = {"Edit", "Write"}


# Tools that get session-scoped "Accept all edits" coverage.  Same
# vocabulary as ``_MUTATING_PATH_TOOLS`` (Edit/Write are path-scoped via
# ``filePath``) plus ApplyPatch, which carries its own multi-file
# envelope and is handled out-of-band.
ACCEPT_ALL_EDITS_TOOLS: frozenset[str] = frozenset({"Edit", "Write", "ApplyPatch"})


_APPLY_PATCH_HEADERS: tuple[tuple[str, str], ...] = (
    ("add", "Add File: "),
    ("update", "Update File: "),
    ("delete", "Delete File: "),
    ("move", "Move to: "),
)


def parse_apply_patch_files(patch_text: str) -> list[tuple[str, str]]:
    """Return ``(action, path)`` pairs from an apply_patch envelope.

    Recognises ``*** Add File: <path>``, ``*** Update File: <path>``,
    ``*** Delete File: <path>``, and ``*** Move to: <path>`` headers
    (see opencode's apply_patch.txt). Paths are returned verbatim — the
    caller decides whether to resolve them against a cwd, render them
    relative, or check them against approved directories.
    """
    out: list[tuple[str, str]] = []
    for line in patch_text.splitlines():
        if not line.startswith("*** "):
            continue
        rest = line[4:]
        for action, prefix in _APPLY_PATCH_HEADERS:
            if rest.startswith(prefix):
                path = rest[len(prefix):].strip()
                if path:
                    out.append((action, path))
                break
    return out

# Path-scoped tools whose path argument identifies a single file (vs a
# directory).  For these, the parent directory is the right granularity
# when suggesting a session-wide approval.
_FILE_TARGETED_PATH_TOOLS: set[str] = {"Read", "Edit", "Write"}


def _is_path_within_directory(path: str, directory: str) -> bool:
    """Check if a resolved path is within the given directory.

    Uses os.path.realpath to resolve symlinks and normalise, then checks
    that the path starts with the directory prefix (with a trailing separator
    to avoid prefix false positives like /home/user2 matching /home/user).
    Also allows an exact match (e.g. Glob on the cwd itself).
    """
    real_path = os.path.realpath(path)
    real_dir = os.path.realpath(directory)
    return real_path == real_dir or real_path.startswith(real_dir + os.sep)


def _extract_path_for_tool(
    tool_name: str, tool_input: dict[str, Any], cwd: str
) -> str | None:
    """Extract the filesystem path from a tool's input.

    Returns the path string if one is found, or the cwd as default for
    tools where the path is optional (Glob, Grep). Returns None if the
    tool is not path-scoped.
    """
    keys = _PATH_SCOPED_TOOLS.get(tool_name)
    if keys is None:
        return None
    for key in keys:
        value = tool_input.get(key)
        if value is not None:
            return str(value)
    # Glob and Grep default to cwd when no path is provided
    if tool_name in ("Glob", "Grep"):
        return cwd
    return None


def _is_path_within_any_directory(
    path: str, directories: list[str]
) -> bool:
    """Check if a resolved path is within any of the given directories."""
    return any(_is_path_within_directory(path, d) for d in directories)


def tool_path_within_dir(
    tool_name: str, tool_input: dict[str, Any], directory: str,
) -> bool:
    """Public: True if *tool_name*'s target path resolves inside *directory*.

    Used by the approval handlers to auto-resolve pending tool calls
    after a session-wide directory approval.  Returns False for tools
    that aren't path-scoped or that have no resolvable path.
    """
    path = _extract_path_for_tool(tool_name, tool_input, directory)
    return path is not None and _is_path_within_directory(path, directory)


def _suggested_session_dir(
    tool_name: str, tool_input: dict[str, Any]
) -> str | None:
    """Return the directory to suggest for "Allow <dir>/ this session".

    For file-targeted tools (Read, Edit, Write) this is the parent of the
    target file.  For directory-targeted tools (Glob, Grep) this is the
    path itself.  Returns None for tools without a meaningful path.
    """
    keys = _PATH_SCOPED_TOOLS.get(tool_name)
    if keys is None:
        return None
    for key in keys:
        value = tool_input.get(key)
        if value is None:
            continue
        real = os.path.realpath(str(value))
        if tool_name in _FILE_TARGETED_PATH_TOOLS:
            return os.path.dirname(real) or None
        return real
    return None


def make_can_use_tool(
    request_approval: ApprovalCallback,
    cwd: str,
    additional_directories: list[str] | None = None,
    is_edit_auto_approved: Callable[[], bool] | None = None,
    notify_auto_approved_edit: EditNotifyCallback | None = None,
    chat_id: int | None = None,
    is_tool_auto_approved: ToolAutoApprovedCallback | None = None,
    is_containerized: bool = False,
    get_session_approved_dirs: Callable[[], list[str]] | None = None,
    request_host_bash_approval: HostBashApprovalCallback | None = None,
) -> Callable[
    [str, dict[str, Any], ToolPermissionContext], Awaitable[PermissionResult]
]:
    """Create a canUseTool callback for the Claude Agent SDK.

    Tools already in allowedTools are handled by the CLI and never reach this
    callback. This handles everything else:

    1. Path-scoped auto-approval for read-only tools: Read, Glob, and Grep
       are auto-approved when their target path resolves to within the context
       working directory or any additional directory. Mutating tools (Edit,
       Write) within those directories require explicit approval unless the
       user has opted into "accept all edits" for the session. Paths outside
       all approved directories always fall through to the interactive
       approval prompt.

    2. Everything else: sends a Telegram inline keyboard for manual approval.

    Args:
        request_approval: Async callback that presents the tool call to the user
            and returns True to allow or False to deny.
        cwd: The context working directory for path-scoped auto-approval.
        additional_directories: Optional list of extra directories that are
            also approved for path-scoped auto-approval (mirrors the SDK's
            add_dirs / --add-dir).
        is_edit_auto_approved: Optional callback that returns True if the user
            has opted into "accept all edits" for the current session. When
            set and returning True, mutating tools (Edit, Write) within
            approved directories are auto-approved without prompting.
        notify_auto_approved_edit: Optional async callback called when a
            mutating tool is auto-approved (accept-all-edits mode). Receives
            the tool name and input dict so the caller can display the diff
            without blocking the agent.
        chat_id: Optional Telegram chat ID. When provided, the per-chat
            upload directory (``ATTACHMENT_TEMP_DIR/<chat_id>/``) is added
            to the approved directories so Read access to uploaded files is
            auto-approved.
        is_tool_auto_approved: Optional callback that receives a tool name
            and tool input dict, returns True if the user has opted into
            auto-approval for that specific tool (possibly with a pattern
            constraint) in the current session. Used for non-path-scoped
            tools (e.g. WebFetch, WebSearch, Bash).
        is_containerized: When True, all Bash commands are auto-approved
            because Docker isolation provides the safety boundary.
        get_session_approved_dirs: Optional callback returning the list of
            directories the user opted into via the "Allow <dir>/ this
            session" button on a previous out-of-scope approval prompt.
            Membership grants both read and write access — file tools
            (including Edit/Write) are auto-approved silently for paths
            within these directories, mirroring Claude Code's session-
            scoped directory approval.
    """
    static_approved_dirs = [cwd] + (additional_directories or [])
    if chat_id is not None:
        upload_dir = str(ATTACHMENT_TEMP_DIR / str(chat_id))
        static_approved_dirs.append(upload_dir)

    async def can_use_tool(
        tool_name: str,
        tool_input: dict[str, Any],
        context: ToolPermissionContext,
    ) -> PermissionResult:
        # host_bash (sudo mode): always route to the dedicated approval
        # callback. Never auto-approved by patterns, session rules, or the
        # containerized fast-path — the whole point is that this tool
        # escapes the sandbox, so every invocation gets a fresh Telegram
        # prompt with a 10-second auto-deny timer.
        if tool_name == HOST_BASH_TOOL_NAME:
            if request_host_bash_approval is None:
                logger.warning(
                    "host_bash invoked but no approval callback wired; denying"
                )
                return PermissionResultDeny(
                    message="host_bash approval is not configured.",
                )
            outcome = await request_host_bash_approval(
                tool_input, context.tool_use_id,
            )
            if outcome == "approved":
                return PermissionResultAllow()
            if outcome == "timeout":
                return PermissionResultDeny(
                    message=(
                        "Auto-denied: the user did not respond to the "
                        "host_bash approval prompt within 10 seconds. "
                        "They may be away — try again later, or fall "
                        "back to the sandboxed Bash tool."
                    ),
                )
            return PermissionResultDeny(
                message="User denied the host_bash command.",
            )

        # port_forward: list/remove don't expose new attack surface — only
        # create needs the approval prompt.
        if tool_name == "mcp__openshrimp__port_forward":
            if tool_input.get("action") in ("list", "remove"):
                return PermissionResultAllow()

        # Recompute approved directories on every call so newly-added
        # session-approved dirs (from "Allow <dir>/ this session" clicks)
        # take effect immediately for subsequent tool invocations.
        session_dirs: list[str] = (
            list(get_session_approved_dirs())
            if get_session_approved_dirs is not None
            else []
        )
        approved_dirs = static_approved_dirs + session_dirs

        # Containerized contexts: auto-approve all path-scoped tools
        # regardless of path, since Docker isolation provides the safety
        # boundary — consistent with Bash being fully auto-approved in
        # containerized contexts.  For mutating tools (Edit, Write), still
        # fire the edit notification so the user sees diffs.
        if is_containerized and tool_name in _PATH_SCOPED_TOOLS:
            if tool_name in _MUTATING_PATH_TOOLS:
                await _notify_edit(notify_auto_approved_edit, tool_name, tool_input)
            logger.info(
                "Auto-approved %s in containerized context", tool_name
            )
            return PermissionResultAllow()

        # Path-scoped approval for file-access tools.
        # - Paths inside session-approved dirs (user explicitly opted in via
        #   the "Allow <dir>/ this session" button) auto-approve for any
        #   tool, including Edit/Write — mirrors Claude Code's session-
        #   scoped directory approval.
        # - Read-only tools (Read, Glob, Grep) within static approved dirs
        #   (cwd + additional_directories + chat upload dir) auto-approve.
        # - Mutating tools (Edit, Write) within static approved dirs still
        #   require explicit approval unless the user has opted into
        #   "accept all edits".
        # - Paths outside all approved dirs always prompt — the
        #   ``is_tool_auto_approved`` rule check below is skipped so that
        #   blanket "Approve all <Tool>" rules cannot bypass the directory
        #   boundary.  The approval prompt offers the user a directory-
        #   scoped session approval as the standard way to broaden access.
        tool_path = _extract_path_for_tool(tool_name, tool_input, cwd)
        path_scoped_out_of_scope = False
        if tool_path is not None:
            if _is_path_within_any_directory(tool_path, session_dirs):
                if tool_name in _MUTATING_PATH_TOOLS:
                    await _notify_edit(notify_auto_approved_edit, tool_name, tool_input)
                logger.info(
                    "Auto-approved %s: path %s is within a session-approved dir",
                    tool_name,
                    tool_path,
                )
                return PermissionResultAllow()
            if _is_path_within_any_directory(tool_path, static_approved_dirs):
                if tool_name in _MUTATING_PATH_TOOLS:
                    # Check session-level "accept all edits" flag
                    if is_edit_auto_approved and is_edit_auto_approved():
                        logger.info(
                            "Auto-approved %s (accept-all-edits): "
                            "path %s is within approved dirs",
                            tool_name,
                            tool_path,
                        )
                        await _notify_edit(notify_auto_approved_edit, tool_name, tool_input)
                        return PermissionResultAllow()
                    logger.info(
                        "Mutating tool %s within approved dirs requires "
                        "approval",
                        tool_name,
                    )
                    # Fall through to interactive approval
                else:
                    logger.info(
                        "Auto-approved %s: path %s is within approved dirs",
                        tool_name,
                        tool_path,
                    )
                    return PermissionResultAllow()
            else:
                path_scoped_out_of_scope = True
                logger.warning(
                    "Path-scoped tool %s targets %s outside approved dirs, "
                    "requiring manual approval",
                    tool_name,
                    tool_path,
                )

        # Accept-all-edits mode: also auto-approve common safe Bash
        # commands (mkdir, touch, rm, mv, cp, sed, etc.) that complement
        # file editing.  Mirrors Claude Code's acceptEdits allowlist.
        # Path arguments must resolve to within the approved directories.
        if (
            tool_name == "Bash"
            and is_edit_auto_approved
            and is_edit_auto_approved()
            and _is_safe_bash_for_accept_edits(
                tool_input.get("command", ""), approved_dirs
            )
        ):
            logger.info(
                "Auto-approved safe Bash command (accept-all-edits): %s",
                tool_input.get("command", "")[:100],
            )
            return PermissionResultAllow()

        # Containerized contexts: auto-approve all shell-executing tools
        # (Bash and Monitor) since the sandbox provides the safety
        # boundary.  Monitor runs arbitrary shell commands just like Bash;
        # the only difference is that its stdout is streamed as events.
        if is_containerized and tool_name in ("Bash", "Monitor"):
            logger.info(
                "Auto-approved %s in containerized context", tool_name
            )
            return PermissionResultAllow()

        # ApplyPatch carries its own multi-file envelope, so it bypasses
        # ``_PATH_SCOPED_TOOLS`` and gets a dedicated branch.  Containerized
        # contexts allow outright (sandbox boundary).  Otherwise accept-all-
        # edits allows only when every target path resolves inside
        # ``static_approved_dirs`` — same boundary Edit/Write get.
        if tool_name == "ApplyPatch":
            if is_containerized:
                logger.info("Auto-approved ApplyPatch in containerized context")
                await _notify_edit(notify_auto_approved_edit, tool_name, tool_input)
                return PermissionResultAllow()
            if is_edit_auto_approved and is_edit_auto_approved():
                patch_text = str(tool_input.get("patchText", ""))
                patch_files = parse_apply_patch_files(patch_text)
                resolved = [
                    p if os.path.isabs(p) else os.path.join(cwd, p)
                    for _, p in patch_files
                ]
                if resolved and all(
                    _is_path_within_any_directory(p, static_approved_dirs)
                    for p in resolved
                ):
                    logger.info(
                        "Auto-approved ApplyPatch (accept-all-edits): "
                        "%d path(s) within approved dirs", len(resolved),
                    )
                    await _notify_edit(notify_auto_approved_edit, tool_name, tool_input)
                    return PermissionResultAllow()

        # Per-tool session-scoped auto-approval (e.g. "Accept all git").
        # Checked for tools that reach the interactive approval stage,
        # including mutating path tools that weren't caught by the
        # accept-all-edits check above.  Skipped for path-scoped tools
        # whose target was out-of-scope: we don't let blanket per-tool
        # rules bypass the directory boundary — the user must explicitly
        # opt into the directory via the dedicated session-dir button on
        # the prompt instead.
        if (
            not path_scoped_out_of_scope
            and is_tool_auto_approved
            and is_tool_auto_approved(tool_name, tool_input)
        ):
            logger.info(
                "Auto-approved %s (per-tool session approval)", tool_name
            )
            return PermissionResultAllow()

        logger.info("Requesting approval for tool: %s", tool_name)
        # Use the SDK-provided tool_use_id to distinguish parallel
        # approval requests (each gets its own Future in
        # _approval_futures).
        tool_use_id = context.tool_use_id
        suggested_dir = (
            _suggested_session_dir(tool_name, tool_input)
            if path_scoped_out_of_scope
            else None
        )
        approved = await request_approval(
            tool_name, tool_input, tool_use_id, suggested_dir
        )
        decision = "allow" if approved else "deny"
        logger.info("Tool %s %s", tool_name, decision)

        if approved:
            return PermissionResultAllow()
        else:
            return PermissionResultDeny(message="User denied tool use.")

    return can_use_tool
