"""Read MCP server configurations for the MCP proxy.

Extracts global MCP servers from OpenCode's real ``mcp`` schema and
per-context MCP servers from OpenShrimp's private context config.  The MCP
proxy can then expose them to sandboxed OpenCode sessions without copying
credentials into project repositories or guest filesystems.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal, TypeVar

import json5

from open_shrimp.config import ContextConfig

logger = logging.getLogger(__name__)


@dataclass
class StdioServerConfig:
    """Parsed stdio MCP server entry from OpenCode config."""

    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)


@dataclass
class HttpServerConfig:
    """Parsed HTTP/SSE MCP server entry from OpenCode config.

    ``headers`` carries static headers from the config file.  OAuth
    credentials are resolved separately at proxy-forwarding time so tokens
    never enter the sandbox.
    """

    url: str
    transport: Literal["http", "sse"]
    headers: dict[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def get_opencode_config_path() -> Path:
    """Return the path to the OpenCode global config file.

    Respects ``OPENCODE_CONFIG_DIR`` if set, otherwise defaults to
    ``$XDG_CONFIG_HOME/opencode/opencode.json``.
    """
    config_dir = os.environ.get("OPENCODE_CONFIG_DIR")
    if config_dir:
        return Path(config_dir) / "opencode.json"
    xdg_config_home = os.environ.get("XDG_CONFIG_HOME")
    config_home = Path(xdg_config_home) if xdg_config_home else Path.home() / ".config"
    return config_home / "opencode" / "opencode.json"


def _get_existing_opencode_config_path() -> Path:
    """Return the OpenCode config path, accepting ``.jsonc`` as fallback."""
    path = get_opencode_config_path()
    if path.is_file():
        return path
    jsonc_path = path.with_suffix(".jsonc")
    if jsonc_path.is_file():
        return jsonc_path
    return path


# ---------------------------------------------------------------------------
# Environment variable expansion
# ---------------------------------------------------------------------------

_ENV_VAR_RE = re.compile(r"\$\{([^}]+)\}")


def _expand_env_vars(value: str) -> str:
    r"""Expand ``${VAR}`` and ``${VAR:-default}`` in *value*.

    Missing variables with no default are left as-is (``${VAR}``).
    """

    def _replace(match: re.Match[str]) -> str:
        content = match.group(1)
        parts = content.split(":-", 1)
        var_name = parts[0]
        default = parts[1] if len(parts) > 1 else None
        env_value = os.environ.get(var_name)
        if env_value is not None:
            return env_value
        if default is not None:
            return default
        logger.warning("MCP config references undefined env var: ${%s}", var_name)
        return match.group(0)  # leave as-is

    return _ENV_VAR_RE.sub(_replace, value)


def _expand_server_env(env: dict[str, str]) -> dict[str, str]:
    """Expand environment variables in all *env* values."""
    return {k: _expand_env_vars(v) for k, v in env.items()}


def _expand_server_args(args: list[str]) -> list[str]:
    """Expand environment variables in *args*."""
    return [_expand_env_vars(a) for a in args]


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def load_opencode_config() -> dict[str, Any]:
    """Load and return OpenCode config as a dict.

    Returns an empty dict if the file doesn't exist or can't be parsed.
    """
    config_path = _get_existing_opencode_config_path()
    if not config_path.is_file():
        return {}
    try:
        text = config_path.read_text(encoding="utf-8")
        if config_path.suffix == ".jsonc":
            parsed = json5.loads(text)
            return parsed if isinstance(parsed, dict) else {}
        return json.loads(text)  # type: ignore[no-any-return]
    except (ValueError, OSError) as exc:
        logger.warning("Failed to read %s: %s", config_path, exc)
        return {}


def _parse_claude_stdio_servers(
    raw_servers: dict[str, Any] | None,
) -> dict[str, StdioServerConfig]:
    """Extract stdio server configs from Claude/OpenShrimp-shaped entries.

    Servers with an explicit ``type`` other than ``"stdio"`` are skipped
    (they are http/sse/sdk servers that don't need proxying).
    """
    if not raw_servers:
        return {}

    result: dict[str, StdioServerConfig] = {}
    for name, entry in raw_servers.items():
        if not isinstance(entry, dict):
            continue
        server_type = entry.get("type")
        # stdio is the default when type is omitted
        if server_type is not None and server_type != "stdio":
            continue
        command = entry.get("command")
        if not command or not isinstance(command, str):
            logger.warning(
                "MCP server '%s' has no command, skipping", name
            )
            continue
        raw_args = entry.get("args", [])
        raw_env = entry.get("env", {})
        result[name] = StdioServerConfig(
            command=_expand_env_vars(command),
            args=_expand_server_args(raw_args if isinstance(raw_args, list) else []),
            env=_expand_server_env(raw_env if isinstance(raw_env, dict) else {}),
        )
    return result


def _parse_claude_http_servers(
    raw_servers: dict[str, Any] | None,
) -> dict[str, HttpServerConfig]:
    """Extract HTTP/SSE server configs from Claude/OpenShrimp-shaped entries."""
    if not raw_servers:
        return {}

    result: dict[str, HttpServerConfig] = {}
    for name, entry in raw_servers.items():
        if not isinstance(entry, dict):
            continue
        server_type = entry.get("type")
        if server_type not in ("http", "sse"):
            continue
        url = entry.get("url")
        if not url or not isinstance(url, str):
            logger.warning(
                "MCP server '%s' in OpenCode config has no url, skipping", name
            )
            continue
        raw_headers = entry.get("headers", {})
        headers = {
            k: _expand_env_vars(v)
            for k, v in (raw_headers if isinstance(raw_headers, dict) else {}).items()
            if isinstance(v, str)
        }
        result[name] = HttpServerConfig(
            url=_expand_env_vars(url),
            transport=server_type,
            headers=headers,
        )
    return result


def _parse_opencode_stdio_servers(
    raw_servers: dict[str, Any] | None,
) -> dict[str, StdioServerConfig]:
    """Extract local stdio servers from OpenCode's ``mcp`` schema."""
    if not raw_servers:
        return {}

    result: dict[str, StdioServerConfig] = {}
    for name, entry in raw_servers.items():
        if not isinstance(entry, dict):
            continue
        if entry.get("enabled") is False:
            continue
        if entry.get("type") != "local":
            continue
        command = entry.get("command")
        if not isinstance(command, list) or not command:
            logger.warning(
                "OpenCode MCP server '%s' has no local command list, skipping",
                name,
            )
            continue
        if not all(isinstance(part, str) for part in command):
            logger.warning(
                "OpenCode MCP server '%s' command must contain only strings, skipping",
                name,
            )
            continue
        raw_env = entry.get("environment", entry.get("env", {}))
        result[name] = StdioServerConfig(
            command=_expand_env_vars(command[0]),
            args=_expand_server_args(command[1:]),
            env=_expand_server_env(raw_env if isinstance(raw_env, dict) else {}),
        )
    return result


def _parse_opencode_http_servers(
    raw_servers: dict[str, Any] | None,
) -> dict[str, HttpServerConfig]:
    """Extract remote HTTP servers from OpenCode's ``mcp`` schema."""
    if not raw_servers:
        return {}

    result: dict[str, HttpServerConfig] = {}
    for name, entry in raw_servers.items():
        if not isinstance(entry, dict):
            continue
        if entry.get("enabled") is False:
            continue
        if entry.get("type") != "remote":
            continue
        url = entry.get("url")
        if not isinstance(url, str) or not url:
            logger.warning("OpenCode MCP server '%s' has no url, skipping", name)
            continue
        raw_headers = entry.get("headers", {})
        headers = {
            k: _expand_env_vars(v)
            for k, v in (raw_headers if isinstance(raw_headers, dict) else {}).items()
            if isinstance(v, str)
        }
        result[name] = HttpServerConfig(
            url=_expand_env_vars(url),
            transport="http",
            headers=headers,
        )
    return result


_T = TypeVar("_T")


def _merge_opencode_and_context(
    project_dir: str,
    context: ContextConfig | None,
    opencode_parser: Callable[[dict[str, Any] | None], dict[str, _T]],
    context_parser: Callable[[dict[str, Any] | None], dict[str, _T]],
    label: str,
) -> dict[str, _T]:
    """Merge OpenCode global servers with OpenShrimp per-context servers."""
    config = load_opencode_config()
    opencode_servers = opencode_parser(config.get("mcp")) if config else {}
    context_servers = context_parser(context.mcp) if context is not None else {}

    merged = {**opencode_servers, **context_servers}
    if merged:
        logger.info(
            "Found %d %s MCP server(s) for %s: %s",
            len(merged),
            label,
            project_dir,
            ", ".join(merged),
        )
    return merged


def get_mcp_servers_for_directory(
    project_dir: str,
    context: ContextConfig | None = None,
) -> dict[str, StdioServerConfig]:
    """Return stdio MCP servers applicable to *project_dir*."""
    return _merge_opencode_and_context(
        project_dir,
        context,
        _parse_opencode_stdio_servers,
        _parse_claude_stdio_servers,
        "stdio",
    )


def get_http_mcp_servers_for_directory(
    project_dir: str,
    context: ContextConfig | None = None,
) -> dict[str, HttpServerConfig]:
    """Return HTTP/SSE MCP servers applicable to *project_dir*."""
    return _merge_opencode_and_context(
        project_dir,
        context,
        _parse_opencode_http_servers,
        _parse_claude_http_servers,
        "HTTP",
    )
