"""Skill directory discovery shared by sandbox backends."""

from __future__ import annotations

import os
from pathlib import Path

SANDBOX_USER = "openshrimp"
SANDBOX_UID = 1000
SANDBOX_HOME = f"/home/{SANDBOX_USER}"
SANDBOX_TMP = f"/tmp/{SANDBOX_USER}-{SANDBOX_UID}"


def global_skill_dir_candidates(
    *, guest_home: str = SANDBOX_HOME,
) -> list[tuple[Path, str]]:
    """Return host global skill directories and matching sandbox paths."""
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return [
        (Path.home() / ".claude" / "skills", f"{guest_home}/.claude/skills"),
        (Path.home() / ".agents" / "skills", f"{guest_home}/.agents/skills"),
        (config_home / "opencode" / "skills", f"{guest_home}/.config/opencode/skills"),
        (config_home / "opencode" / "skill", f"{guest_home}/.config/opencode/skill"),
    ]


def existing_global_skill_dirs(
    *, guest_home: str = SANDBOX_HOME,
) -> list[tuple[Path, str]]:
    """Return existing host global skill dirs and their sandbox targets."""
    return [
        (host, guest)
        for host, guest in global_skill_dir_candidates(guest_home=guest_home)
        if host.is_dir()
    ]
