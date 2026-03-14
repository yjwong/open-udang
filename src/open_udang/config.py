"""Config loading and validation for OpenUdang."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


DEFAULT_CONFIG_PATH = Path.home() / ".config" / "openudang" / "config.yaml"


@dataclass
class TelegramConfig:
    token: str


@dataclass
class ContextConfig:
    directory: str
    description: str
    model: str
    allowed_tools: list[str]
    additional_directories: list[str] = field(default_factory=list)
    default_for_chats: list[int] = field(default_factory=list)
    locked_for_chats: list[int] = field(default_factory=list)


@dataclass
class ReviewConfig:
    host: str = "127.0.0.1"
    port: int = 8080
    public_url: str | None = None
    tunnel: str | None = None  # "cloudflared" or None


@dataclass
class Config:
    telegram: TelegramConfig
    allowed_users: list[int]
    contexts: dict[str, ContextConfig]
    default_context: str
    review: ReviewConfig = field(default_factory=ReviewConfig)


def _validate_raw(raw: dict) -> None:
    """Validate raw YAML dict has all required fields."""
    if not isinstance(raw, dict):
        raise ValueError("Config must be a YAML mapping")

    # Top-level required fields
    for key in ("telegram", "allowed_users", "contexts", "default_context"):
        if key not in raw:
            raise ValueError(f"Missing required config field: {key}")

    # telegram.token
    telegram = raw["telegram"]
    if not isinstance(telegram, dict) or "token" not in telegram:
        raise ValueError("Missing required field: telegram.token")

    # allowed_users
    users = raw["allowed_users"]
    if not isinstance(users, list) or not users:
        raise ValueError("allowed_users must be a non-empty list of integers")
    for u in users:
        if not isinstance(u, int):
            raise ValueError(f"allowed_users entries must be integers, got: {u!r}")

    # contexts
    contexts = raw["contexts"]
    if not isinstance(contexts, dict) or not contexts:
        raise ValueError("contexts must be a non-empty mapping")
    for name, ctx in contexts.items():
        if not isinstance(ctx, dict):
            raise ValueError(f"Context '{name}' must be a mapping")
        for field_name in ("directory", "description", "model", "allowed_tools"):
            if field_name not in ctx:
                raise ValueError(
                    f"Context '{name}' missing required field: {field_name}"
                )
        if not isinstance(ctx["allowed_tools"], list):
            raise ValueError(f"Context '{name}': allowed_tools must be a list")
        add_dirs = ctx.get("additional_directories", [])
        if not isinstance(add_dirs, list):
            raise ValueError(
                f"Context '{name}': additional_directories must be a list"
            )
        for d in add_dirs:
            if not isinstance(d, str):
                raise ValueError(
                    f"Context '{name}': additional_directories entries must "
                    f"be strings, got: {d!r}"
                )

    # default_context references a defined context
    default = raw["default_context"]
    if default not in contexts:
        raise ValueError(
            f"default_context '{default}' not found in contexts: "
            f"{list(contexts.keys())}"
        )


def _parse(raw: dict) -> Config:
    """Parse validated raw dict into Config dataclass."""
    contexts = {}
    for name, ctx in raw["contexts"].items():
        contexts[name] = ContextConfig(
            directory=ctx["directory"],
            description=ctx["description"],
            model=ctx["model"],
            allowed_tools=ctx["allowed_tools"],
            additional_directories=ctx.get("additional_directories", []),
            default_for_chats=ctx.get("default_for_chats", []),
            locked_for_chats=ctx.get("locked_for_chats", []),
        )

    # Parse optional review config.
    review_raw: dict[str, Any] = raw.get("review", {})
    tunnel_raw = review_raw.get("tunnel")
    if tunnel_raw is not None and tunnel_raw not in ("cloudflared",):
        raise ValueError(
            f"Unsupported review.tunnel value: {tunnel_raw!r} "
            f"(supported: 'cloudflared')"
        )

    review = ReviewConfig(
        host=str(review_raw.get("host", "127.0.0.1")),
        port=int(review_raw.get("port", 8080)),
        public_url=review_raw.get("public_url"),
        tunnel=tunnel_raw,
    )

    return Config(
        telegram=TelegramConfig(token=raw["telegram"]["token"]),
        allowed_users=raw["allowed_users"],
        contexts=contexts,
        default_context=raw["default_context"],
        review=review,
    )


def load_config(path: str | None = None) -> Config:
    """Load and validate config from a YAML file.

    Args:
        path: Path to config file. Defaults to ~/.config/openudang/config.yaml.

    Returns:
        Parsed and validated Config.

    Raises:
        FileNotFoundError: If the config file does not exist.
        ValueError: If the config is invalid.
    """
    config_path = Path(path) if path else DEFAULT_CONFIG_PATH

    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    raw = yaml.safe_load(config_path.read_text())
    _validate_raw(raw)
    return _parse(raw)
