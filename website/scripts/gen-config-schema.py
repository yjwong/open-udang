#!/usr/bin/env python3
"""Generate a JSON schema from OpenShrimp config dataclasses.

Introspects the dataclasses in open_shrimp.config and produces a JSON schema
that documents the structure of config.yaml. The output is written to
website/public/config-schema.json.

Usage:
    python website/scripts/gen-config-schema.py

Requires the open_shrimp package to be importable (run from the repo root
with `uv run`).
"""

from __future__ import annotations

import dataclasses
import json
import sys
from pathlib import Path
from typing import Any, Union, get_args, get_origin

# Ensure the source is importable.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / ".." / "src"))

from open_shrimp.config import (
    Config,
    ContextConfig,
    ReviewConfig,
    SandboxConfig,
    TelegramConfig,
)

# Fields that are required (no default value) per dataclass.
_DESCRIPTIONS: dict[str, dict[str, str]] = {
    "TelegramConfig": {
        "token": "Bot token from @BotFather.",
    },
    "SandboxConfig": {
        "backend": 'Sandbox backend: "docker", "libvirt", or "lima".',
        "enabled": "Enable or disable the sandbox.",
        "docker_in_docker": "Enable rootless Docker inside the container (Docker backend).",
        "dockerfile": "Path to a custom Dockerfile (Docker backend).",
        "computer_use": "Enable GUI interaction with a headless desktop.",
        "memory": "Memory ceiling in MB (Libvirt backend).",
        "cpus": "Number of vCPUs (Libvirt backend).",
        "disk_size": "Disk size in GB for qcow2 overlay (Libvirt backend).",
        "base_image": "Path to base qcow2/cloud image (Libvirt backend).",
        "provision": "Shell script to run on first boot (Libvirt backend).",
    },
    "ContextConfig": {
        "directory": "Absolute path to the project directory.",
        "description": "Short description shown in the context list.",
        "allowed_tools": "Tools auto-approved without prompting. Glob patterns supported.",
        "model": "Model override in provider/model form, e.g. openai/gpt-5.5.",
        "additional_directories": "Extra directories the agent can access.",
        "default_for_chats": "Chat IDs where this context is auto-selected.",
        "locked_for_chats": "Chat IDs locked to this context.",
        "container": "Legacy Docker sandbox config (use sandbox instead).",
        "sandbox": "Sandbox configuration.",
    },
    "ReviewConfig": {
        "host": "HTTP server bind address.",
        "port": "HTTP server port.",
        "public_url": "Public URL for Mini Apps (behind a reverse proxy).",
        "tunnel": 'Auto-start a public tunnel. Supported: "cloudflared".',
    },
    "Config": {
        "telegram": "Telegram bot settings.",
        "allowed_users": "Telegram user IDs allowed to use the bot.",
        "contexts": "Named project contexts.",
        "default_context": "Context to use when none is selected.",
        "review": "Mini App HTTP server settings.",
        "instance_name": "Display name for this instance.",
    },
}


def _python_type_to_json_schema(tp: Any) -> dict[str, Any]:
    """Convert a Python type annotation to a JSON schema fragment."""
    origin = get_origin(tp)
    args = get_args(tp)

    if tp is str:
        return {"type": "string"}
    if tp is int:
        return {"type": "integer"}
    if tp is bool:
        return {"type": "boolean"}
    if tp is float:
        return {"type": "number"}

    # Optional[X] = Union[X, None]
    if origin is Union:
        non_none = [a for a in args if a is not type(None)]
        if len(non_none) == 1:
            schema = _python_type_to_json_schema(non_none[0])
            return {"oneOf": [schema, {"type": "null"}]}
        return {"oneOf": [_python_type_to_json_schema(a) for a in non_none]}

    # list[X]
    if origin is list:
        if args:
            return {"type": "array", "items": _python_type_to_json_schema(args[0])}
        return {"type": "array"}

    # dict[str, X]
    if origin is dict:
        if len(args) == 2:
            return {
                "type": "object",
                "additionalProperties": _python_type_to_json_schema(args[1]),
            }
        return {"type": "object"}

    # Dataclass reference
    if dataclasses.is_dataclass(tp):
        return {"$ref": f"#/$defs/{tp.__name__}"}

    return {}


def _dataclass_to_schema(cls: type) -> dict[str, Any]:
    """Convert a dataclass to a JSON schema object definition."""
    properties: dict[str, Any] = {}
    required: list[str] = []
    descriptions = _DESCRIPTIONS.get(cls.__name__, {})

    for f in dataclasses.fields(cls):
        prop = _python_type_to_json_schema(f.type)
        if f.name in descriptions:
            prop["description"] = descriptions[f.name]

        if f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING:
            required.append(f.name)
        elif f.default is not dataclasses.MISSING:
            if not dataclasses.is_dataclass(f.default):
                prop["default"] = f.default
        elif f.default_factory is not dataclasses.MISSING:
            val = f.default_factory()
            if not dataclasses.is_dataclass(val):
                prop["default"] = val

        properties[f.name] = prop

    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
    }
    if required:
        schema["required"] = required
    return schema


def generate_schema() -> dict[str, Any]:
    """Generate the full JSON schema for config.yaml."""
    defs = {}
    for cls in (TelegramConfig, SandboxConfig, ContextConfig, ReviewConfig):
        defs[cls.__name__] = _dataclass_to_schema(cls)

    root = _dataclass_to_schema(Config)
    root["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    root["title"] = "OpenShrimp Configuration"
    root["description"] = "Schema for ~/.config/openshrimp/config.yaml"
    root["$defs"] = defs

    return root


def main() -> None:
    schema = generate_schema()
    output = Path(__file__).resolve().parents[1] / "public" / "config-schema.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(schema, indent=2) + "\n")
    print(f"Schema written to {output}")


if __name__ == "__main__":
    main()
