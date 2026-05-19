"""Read OAuth tokens for HTTP MCP servers from ``~/.claude/.credentials.json``.

The Claude CLI stores per-server OAuth tokens under the top-level
``mcpOAuth`` key.  Each entry is keyed by ``<serverName>|<hash>`` and
carries the access token, refresh token, client id/secret, and expiry.

The proxy reads these on the host and injects them as upstream
``Authorization`` headers — sandboxes never see the tokens directly.

Token refresh is out of scope for now: if the access token has expired,
the user should re-authenticate via ``/mcp`` on the host so the CLI
refreshes the credentials file.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class OAuthCredential:
    """Resolved OAuth credential for a single HTTP MCP server."""

    server_name: str
    server_url: str
    access_token: str
    expires_at_ms: int | None  # epoch milliseconds, or None if unknown


def _credentials_path() -> Path:
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    if config_dir:
        return Path(config_dir) / ".credentials.json"
    return Path.home() / ".claude" / ".credentials.json"


# Cache the parsed credentials JSON keyed on (path, mtime_ns) so SSE
# streams that issue many requests don't re-read and re-parse the file
# on every hit.  Re-reads only happen when the user re-authenticates.
_creds_cache: tuple[tuple[str, int], dict[str, Any]] | None = None


def _load_credentials() -> dict[str, Any]:
    global _creds_cache
    path = _credentials_path()
    try:
        mtime_ns = path.stat().st_mtime_ns
    except FileNotFoundError:
        _creds_cache = None
        return {}
    except OSError as exc:
        logger.warning("Failed to stat %s: %s", path, exc)
        return {}

    cache_key = (str(path), mtime_ns)
    if _creds_cache is not None and _creds_cache[0] == cache_key:
        return _creds_cache[1]

    try:
        data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Failed to read %s: %s", path, exc)
        return {}

    _creds_cache = (cache_key, data)
    return data


def get_oauth_credential(
    server_name: str, server_url: str
) -> OAuthCredential | None:
    """Return the stored OAuth credential for *(server_name, server_url)*.

    Matches on both fields because the same logical name (e.g. "figma")
    could in principle point at different URLs across config scopes.
    Returns ``None`` if no credential is found.
    """
    creds = _load_credentials()
    mcp_oauth = creds.get("mcpOAuth")
    if not isinstance(mcp_oauth, dict):
        return None

    for _key, entry in mcp_oauth.items():
        if not isinstance(entry, dict):
            continue
        if entry.get("serverName") != server_name:
            continue
        if entry.get("serverUrl") != server_url:
            continue
        access_token = entry.get("accessToken")
        if not isinstance(access_token, str) or not access_token:
            continue
        expires_at = entry.get("expiresAt")
        return OAuthCredential(
            server_name=server_name,
            server_url=server_url,
            access_token=access_token,
            expires_at_ms=(
                int(expires_at) if isinstance(expires_at, (int, float)) else None
            ),
        )
    return None


def is_expired(cred: OAuthCredential, skew_seconds: int = 60) -> bool:
    """Return True if the credential is expired (with skew tolerance)."""
    if cred.expires_at_ms is None:
        return False
    now_ms = int(time.time() * 1000)
    return now_ms >= cred.expires_at_ms - skew_seconds * 1000
