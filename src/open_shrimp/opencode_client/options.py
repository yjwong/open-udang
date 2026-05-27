from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


def split_provider_model(model: str | None) -> tuple[str, str]:
    """Split a context's ``model`` field into ``(provider, model)``.

    Expects ``provider/model``. Raises if the field is missing or
    unqualified — open-shrimp configs must name a provider explicitly
    under OpenCode.
    """
    if not model:
        raise ValueError(
            "context.model is required and must be 'provider/model'"
        )
    if "/" not in model:
        raise ValueError(
            f"context.model {model!r} must be 'provider/model' "
            f"(e.g. 'openai/gpt-5', 'anthropic/claude-sonnet-4-6')"
        )
    provider, _, rest = model.partition("/")
    return provider, rest


@dataclass
class OpenCodeOptions:
    cwd: str
    provider: str
    model: str
    resume: str | None = None
    query_timeout: float = 300.0

    # Honoured fields.
    effort: str | None = None  # → variant on prompt_async
    allowed_tools: list[str] | None = None  # → session permission rules
    add_dirs: list[str] | None = None  # → external_directory allow rules
    stderr: Callable[[str], None] | None = None
    can_use_tool: Callable[..., Any] | None = None
    system_prompt: str | dict[str, Any] | None = None  # → system on prompt_async

    # Accepted-but-ignored fields (kwargs-compat with ClaudeAgentOptions).
    setting_sources: list[str] | None = None
    include_partial_messages: bool = True
    max_buffer_size: int | None = None
    cli_path: str | None = None
    mcp_servers: dict[str, Any] | None = None

    extra: dict[str, Any] = field(default_factory=dict)
