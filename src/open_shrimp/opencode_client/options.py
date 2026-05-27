from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass
class OpenCodeOptions:
    cwd: str
    provider: str
    model: str
    resume: str | None = None
    query_timeout: float = 300.0

    # Fields below are accepted but unused: they let agent.py construct an
    # OpenCodeOptions with the same kwargs it passes to ClaudeAgentOptions.
    effort: str | None = None
    allowed_tools: list[str] | None = None
    add_dirs: list[str] | None = None
    setting_sources: list[str] | None = None
    include_partial_messages: bool = True
    stderr: Callable[[str], None] | None = None
    can_use_tool: Callable[..., Any] | None = None
    max_buffer_size: int | None = None
    system_prompt: str | dict[str, Any] | None = None
    extra: dict[str, Any] = field(default_factory=dict)
