"""Log source abstraction for the terminal mini app.

Provides a unified ``LogSource`` type that the terminal API endpoints
use to resolve and tail different kinds of output: background task
output files, container build logs, etc.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from open_shrimp import agent_tasks
from open_shrimp.sandbox import SandboxManager
from open_shrimp.sandbox.manager import lookup_active_build
from open_shrimp.sandbox.skill_paths import SANDBOX_HOME, SANDBOX_TMP
from open_shrimp.handlers.state import is_task_active

logger = logging.getLogger(__name__)

# Task ID pattern: alphanumeric task handles from the agent runtime.
_TASK_ID_RE = re.compile(r"^[a-zA-Z0-9_-]+$")

# task_type values that indicate an agent transcript (JSONL format).
_AGENT_TASK_TYPES = {"local_agent", "remote_agent"}

# Base directory for agent tmp files.
_AGENT_TMP_BASE = Path(SANDBOX_TMP.replace("1000", str(os.getuid())))


@dataclass
class LogSource:
    """A resolved log source that the terminal API can tail or read."""

    path: Path
    is_active: Callable[[], bool]
    render: str = "raw"


# ---------------------------------------------------------------------------
# File discovery helpers (moved from api.py)
# ---------------------------------------------------------------------------


def _is_file_or_symlink(path: Path) -> bool:
    """Return True if *path* is a regular file or a symlink (even broken)."""
    return path.is_file() or path.is_symlink()


def _search_tmp_base(base: Path, filename: str) -> Path | None:
    """Search an agent tmp base directory for a task output file.

    Looks for ``<base>/<project>/tasks/<filename>`` and
    ``<base>/<project>/<session>/tasks/<filename>``.

    Also matches broken symlinks (common for agent tasks in containers
    where the symlink target uses a container-internal path).
    """
    if not base.is_dir():
        return None

    for project_dir in base.iterdir():
        if not project_dir.is_dir():
            continue

        candidate = project_dir / "tasks" / filename
        if _is_file_or_symlink(candidate):
            return candidate

        for sub in project_dir.iterdir():
            if not sub.is_dir():
                continue
            candidate = sub / "tasks" / filename
            if _is_file_or_symlink(candidate):
                return candidate

    return None


def _resolve_container_symlink(
    symlink: Path, context_dir: Path,
) -> Path | None:
    """Resolve a broken symlink created inside a container/VM to its host path.

    Inside the container/VM, OpenCode state is mounted from the host.
    Agent task ``.output`` files can be symlinks to ``.jsonl``
    session files under the sandbox home, which don't
    exist on the host at that path.  This function translates the
    container/VM path back to the host equivalent.

    The relative path resolves against the sandbox context state directory.
    """
    try:
        target = os.readlink(symlink)
    except OSError:
        return None

    container_prefix = f"{SANDBOX_HOME}/.local/share/opencode/"
    if target.startswith(container_prefix):
        relative = target[len(container_prefix):]
        # Docker layout: context_dir owns the sandbox state.
        host_path = context_dir / relative
        if host_path.is_file():
            return host_path
    return None


def _find_task_output_file(
    task_id: str,
    sandbox_managers: dict[str, SandboxManager] | None = None,
) -> Path | None:
    """Find the output file for a background task by ID.

    Searches the host agent tmp directory and all sandbox managers'
    state directories (where containerized/VM contexts write their tmp files).

    For containerized agent tasks the ``.output`` file is a symlink whose
    target uses a container-internal path.  When a broken symlink is found
    in a container state directory, the target is translated to the host
    equivalent so the caller can read the actual data.
    """
    if not _TASK_ID_RE.match(task_id):
        return None

    filename = f"{task_id}.output"

    # Search the host tmp directory first.
    result = _search_tmp_base(_AGENT_TMP_BASE, filename)
    if result:
        return result

    # Search all sandbox managers' state directories.
    if sandbox_managers:
        for mgr in sandbox_managers.values():
            state_dir = mgr.state_dir
            if not state_dir.is_dir():
                continue
            for context_dir in state_dir.iterdir():
                tmp_dir = context_dir / "tmp"
                result = _search_tmp_base(tmp_dir, filename)
                if result:
                    # Broken symlink — resolve container path to host path.
                    if result.is_symlink() and not result.exists():
                        resolved = _resolve_container_symlink(
                            result, context_dir,
                        )
                        if resolved:
                            return resolved
                    return result

    return None


def _is_agent_output(path: Path, task_type: str | None) -> bool:
    """Determine if a task output file is an agent JSONL transcript."""
    if task_type:
        return task_type in _AGENT_TASK_TYPES
    # Fallback: agent output files are symlinks to .jsonl files.
    try:
        return path.is_symlink() and os.readlink(path).endswith(".jsonl")
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Resolvers
# ---------------------------------------------------------------------------

# Context name pattern for build IDs.
_CONTEXT_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


def resolve_task(
    source_id: str,
    task_type: str | None = None,
    sandbox_managers: dict[str, SandboxManager] | None = None,
) -> LogSource | None:
    """Resolve a background task ID to a ``LogSource``."""
    agent_output_path = agent_tasks.get_task_output_path(source_id)
    if agent_output_path is not None:
        tid = source_id
        return LogSource(
            path=agent_output_path,
            is_active=lambda: agent_tasks.is_task_running(tid),
            render="openshrimp-agent-jsonl",
        )

    path = _find_task_output_file(source_id, sandbox_managers=sandbox_managers)
    if path is None:
        return None

    render = "jsonl" if _is_agent_output(path, task_type) else "raw"
    tid = source_id  # capture for closure

    return LogSource(
        path=path,
        is_active=lambda: is_task_active(tid),
        render=render,
    )


def resolve_container_build(
    source_id: str,
    sandbox_managers: dict[str, SandboxManager] | None = None,
) -> LogSource | None:
    """Resolve a container build context name to a ``LogSource``.

    Uses the global build registry as the authoritative source for active
    builds.  This avoids the bug where multiple sandbox managers share the
    same ``build_log_dir`` and the wrong manager's ``is_build_active`` is
    captured.  Falls back to scanning managers for finished builds (log
    file exists but no active registration).
    """
    if not _CONTEXT_NAME_RE.match(source_id):
        return None

    # Primary: look up the global build registry (populated by
    # register_build, cleared by unregister_build).
    entry = lookup_active_build(source_id)
    if entry is not None:
        log_path, mgr = entry
        if log_path.is_file():
            ctx = source_id  # capture for closure
            _mgr = mgr  # capture for closure
            return LogSource(
                path=log_path,
                is_active=lambda: _mgr.is_build_active(ctx),
                render="raw",
            )

    # Fallback: build already finished — find the log file for reading.
    if sandbox_managers:
        for mgr in sandbox_managers.values():
            log_path = mgr.build_log_dir / f"{source_id}.log"
            if log_path.is_file():
                return LogSource(
                    path=log_path,
                    is_active=lambda: False,
                    render="raw",
                )

    return None


def resolve(
    source_type: str,
    source_id: str,
    task_type: str | None = None,
    sandbox_managers: dict[str, SandboxManager] | None = None,
) -> LogSource | None:
    """Resolve a ``(type, id)`` pair to a ``LogSource``.

    Args:
        source_type: The type of log source (``"task"`` or
            ``"container_build"``).
        source_id: The identifier (task ID or context name).
        task_type: Optional task type hint (only for ``type=task``).
        sandbox_managers: Managers dict for build log and state dirs.

    Returns:
        A ``LogSource`` or ``None`` if the source cannot be found.
    """
    if source_type == "task":
        return resolve_task(
            source_id, task_type=task_type, sandbox_managers=sandbox_managers,
        )
    elif source_type == "container_build":
        return resolve_container_build(
            source_id, sandbox_managers=sandbox_managers,
        )
    else:
        logger.warning("Unknown log source type: %s", source_type)
        return None
