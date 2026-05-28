"""Shared helpers for one-shot HTTP calls to ``opencode serve``.

``OpenCodeClient`` keeps a long-lived httpx client for its hot path,
but read-only endpoints (``GET /session``, ``GET /api/model``) are
called from outside that lifecycle. They all share the same auth
header + 401/4xx triage; this module is that boilerplate, once.
"""

from __future__ import annotations

from typing import Any

import httpx

from open_shrimp.opencode_client.errors import (
    CLIConnectionError,
    OpenCodeAuthError,
    ProcessError,
)
from open_shrimp.opencode_client.process import OpenCodeServer


async def get_json(path: str, *, params: dict[str, Any] | None = None) -> Any:
    """``GET <path>`` against the running ``opencode serve``, JSON-decoded.

    Raises ``OpenCodeAuthError`` on 401, ``ProcessError`` on any other
    4xx/5xx, and ``CLIConnectionError`` on transport failure.
    """
    server = await OpenCodeServer.get_or_start()
    async with httpx.AsyncClient(
        base_url=server.base_url,
        timeout=30.0,
        headers={"Authorization": server.auth_header},
    ) as http:
        try:
            r = await http.get(path, params=params)
        except httpx.HTTPError as exc:
            raise CLIConnectionError(f"GET {path} failed: {exc}") from exc

    if r.status_code == 401:
        raise OpenCodeAuthError("opencode serve rejected our credentials")
    if r.status_code >= 400:
        raise ProcessError(
            f"GET {path} returned {r.status_code}: {r.text[:300]}"
        )
    return r.json()
