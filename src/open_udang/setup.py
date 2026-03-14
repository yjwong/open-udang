"""Interactive setup wizard for first-time OpenUdang configuration."""

from __future__ import annotations

import random
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any


_MODELS: tuple[tuple[str, str], ...] = (
    ("claude-sonnet-4-6", "recommended, fast and capable"),
    ("claude-opus-4-6", "most capable, slower"),
    ("claude-haiku-4-5", "fastest, least capable"),
)


def _prompt(
    label: str,
    *,
    default: str | None = None,
    validator: Callable[[str], str | None] | None = None,
) -> str:
    """Prompt the user for input with optional default and validation.

    Args:
        label: The prompt text shown to the user.
        default: Default value if the user presses Enter without typing.
        validator: A callable that returns an error message string if the
            value is invalid, or None if it's valid.

    Returns:
        The validated user input.

    Raises:
        SystemExit: If the user presses Ctrl-C or Ctrl-D.
    """
    suffix = f" [{default}]: " if default else ": "
    while True:
        try:
            value = input(f"{label}{suffix}").strip()
        except (KeyboardInterrupt, EOFError):
            print("\n\nSetup cancelled.")
            raise SystemExit(0)

        if not value:
            if default is not None:
                return default
            print("  This field is required.")
            continue

        if validator:
            error = validator(value)
            if error:
                print(f"  {error}")
                continue

        return value


def _validate_token(value: str) -> str | None:
    """Validate a Telegram bot token has the expected format."""
    if ":" not in value:
        return "Token should look like '123456:ABC-DEF...' — get one from @BotFather."
    return None


def _validate_user_id(value: str) -> str | None:
    """Validate a Telegram user ID is a positive integer."""
    try:
        uid = int(value)
    except ValueError:
        return "Must be an integer."
    if uid <= 0:
        return "Must be a positive integer."
    return None


def _validate_directory(value: str) -> str | None:
    """Validate a directory path exists."""
    p = Path(value).expanduser()
    if not p.is_dir():
        return f"Directory does not exist: {p}"
    return None


def _validate_context_name(value: str) -> str | None:
    """Validate a context name is a simple identifier."""
    if not value.replace("-", "").replace("_", "").isalnum():
        return "Use only letters, numbers, hyphens, and underscores."
    return None


def _prompt_token() -> str:
    """Prompt for the Telegram bot token."""
    print("  Create a bot via @BotFather on Telegram and paste the token here.")
    return _prompt("Telegram bot token", validator=_validate_token)


def _prompt_user_id() -> int:
    """Prompt for the user's Telegram user ID."""
    print("  Send /start to @userinfobot on Telegram to find your user ID.")
    return int(_prompt("Your Telegram user ID", validator=_validate_user_id))


def _prompt_context() -> tuple[str, dict[str, Any]]:
    """Prompt for the first context configuration.

    Returns:
        A tuple of (context_name, context_dict) ready for the config YAML.
    """
    print("\nSet up your first context (a project directory for Claude to work in).\n")

    name = _prompt("Context name", default="default", validator=_validate_context_name)
    directory = _prompt("Project directory (absolute path)", validator=_validate_directory)
    description = _prompt("Short description", default="Default context")

    # Model selection
    print("\nSelect a model:")
    for i, (model_name, model_desc) in enumerate(_MODELS, 1):
        print(f"  {i}. {model_name} ({model_desc})")
    print(f"  {len(_MODELS) + 1}. Enter a custom model name")

    def _validate_model_choice(value: str) -> str | None:
        try:
            choice = int(value)
        except ValueError:
            return f"Enter a number between 1 and {len(_MODELS) + 1}."
        if choice < 1 or choice > len(_MODELS) + 1:
            return f"Enter a number between 1 and {len(_MODELS) + 1}."
        return None

    choice = int(_prompt("Choice", default="1", validator=_validate_model_choice))
    if choice <= len(_MODELS):
        model = _MODELS[choice - 1][0]
    else:
        model = _prompt("Custom model name")

    # Resolve the directory so we store an absolute path.
    resolved_dir = str(Path(directory).expanduser().resolve())

    context_dict: dict[str, Any] = {
        "directory": resolved_dir,
        "description": description,
        "model": model,
        "allowed_tools": ["LSP", "AskUserQuestion"],
    }
    return name, context_dict


def _build_config_dict(
    token: str,
    user_id: int,
    context_name: str,
    context: dict[str, Any],
) -> dict[str, Any]:
    """Assemble the full config dictionary for YAML serialization."""
    return {
        "telegram": {"token": token},
        "allowed_users": [user_id],
        "contexts": {context_name: context},
        "default_context": context_name,
        "review": {
            "port": random.randint(49152, 65535),
            "tunnel": "cloudflared",
        },
    }


def run_setup_wizard(config_path: Path) -> None:
    """Run the interactive setup wizard.

    Prompts the user for essential configuration and writes the config file.

    Args:
        config_path: Destination path for the config YAML file.

    Raises:
        SystemExit: If the user cancels the wizard.
    """
    print()
    print("Welcome to OpenUdang!")
    print("No config file found — let's set one up.")
    print("Press Ctrl-C at any time to cancel.")
    print()

    token = _prompt_token()
    print()
    user_id = _prompt_user_id()
    context_name, context = _prompt_context()

    config_dict = _build_config_dict(token, user_id, context_name, context)

    from open_udang.config import write_config

    try:
        write_config(config_path, config_dict)
    except OSError as e:
        print(f"\nFailed to write config file: {e}", file=sys.stderr)
        raise SystemExit(1)

    print(f"\nConfig written to {config_path}")
    print("You can edit it later to add more contexts, tools, or users.")
    print("Starting OpenUdang...\n")
