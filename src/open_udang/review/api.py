"""HTTP API routes for the review web app.

Provides endpoints for fetching diff hunks, staging, and unstaging,
all authenticated via Telegram initData.
"""

from __future__ import annotations

import dataclasses
import logging
from pathlib import Path
from typing import Any

import aiosqlite
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from open_udang.config import Config
from open_udang.db import get_active_context
from open_udang.review.auth import AuthError, validate_init_data
from open_udang.review.git_diff import Hunk, HunkResult, get_hunks
from open_udang.review.git_stage import stage_hunk, unstage_hunk

logger = logging.getLogger(__name__)

# In-memory hunk cache: (chat_id, context_name) -> list[Hunk]
# Stores the full (unpaginated) hunk list so we can look up hunks by ID
# for stage/unstage operations.
_hunk_cache: dict[tuple[int, str], list[Hunk]] = {}


def _hunk_to_dict(hunk: Hunk) -> dict[str, Any]:
    """Convert a Hunk dataclass to a JSON-serialisable dict."""
    return {
        "id": hunk.id,
        "file_path": hunk.file_path,
        "language": hunk.language,
        "is_new_file": hunk.is_new_file,
        "is_deleted_file": hunk.is_deleted_file,
        "hunk_header": hunk.hunk_header,
        "lines": [dataclasses.asdict(line) for line in hunk.lines],
        "staged": hunk.staged,
        "is_binary": hunk.is_binary,
    }


async def _authenticate(request: Request) -> int:
    """Validate the Authorization header and return the user ID.

    Raises AuthError on failure.
    """
    config: Config = request.app.state.config
    authorization = request.headers.get("authorization", "")
    return await validate_init_data(
        authorization, config.telegram.token, config.allowed_users
    )


async def _resolve_context(
    request: Request, chat_id: int
) -> tuple[str, str]:
    """Resolve the active context for a chat.

    Returns (context_name, working_directory).

    Raises AuthError(404) if the context is not found.
    """
    config: Config = request.app.state.config
    db: aiosqlite.Connection = request.app.state.db

    context_name = await get_active_context(db, chat_id)
    if context_name is None:
        context_name = config.default_context

    if context_name not in config.contexts:
        raise AuthError(404, f"Context '{context_name}' not found")

    ctx = config.contexts[context_name]
    return context_name, ctx.directory


async def hunks_endpoint(request: Request) -> JSONResponse:
    """GET /api/review/hunks — fetch paginated diff hunks."""
    try:
        user_id = await _authenticate(request)
    except AuthError as e:
        return JSONResponse({"error": e.message}, status_code=e.status_code)

    # Parse query params.
    try:
        chat_id = int(request.query_params["chat_id"])
    except (KeyError, ValueError):
        return JSONResponse(
            {"error": "chat_id query parameter is required (integer)"},
            status_code=400,
        )

    offset = int(request.query_params.get("offset", "0"))
    limit = int(request.query_params.get("limit", "20"))
    include_untracked = request.query_params.get(
        "include_untracked", "true"
    ).lower() in ("true", "1", "yes")

    try:
        context_name, directory = await _resolve_context(request, chat_id)
    except AuthError as e:
        return JSONResponse({"error": e.message}, status_code=e.status_code)

    try:
        # Fetch all hunks first for caching, then paginate.
        all_result = await get_hunks(
            directory, offset=0, limit=0, include_untracked=include_untracked
        )
        # get_hunks with limit=0 returns empty list; fetch all.
        all_result = await get_hunks(
            directory,
            offset=0,
            limit=all_result.total_hunks or 10000,
            include_untracked=include_untracked,
        )

        # Cache the full hunk list.
        _hunk_cache[(chat_id, context_name)] = all_result.hunks

        # Apply pagination.
        total = all_result.total_hunks
        paginated = all_result.hunks[offset : offset + limit]

        result = {
            "context": context_name,
            "directory": directory,
            "total_hunks": total,
            "offset": offset,
            "hunks": [_hunk_to_dict(h) for h in paginated],
        }
    except Exception:
        logger.exception("Failed to get hunks for %s", directory)
        return JSONResponse(
            {"error": "Failed to get diff hunks"}, status_code=500
        )

    return JSONResponse(result)


def _find_cached_hunk(
    hunk_id: str, chat_id: int | None = None, context_name: str | None = None
) -> tuple[Hunk | None, str | None, int | None]:
    """Find a hunk by ID in the cache.

    If chat_id and context_name are provided, search only that entry.
    Otherwise, search all cache entries.

    Returns (hunk, context_name, chat_id) or (None, None, None).
    """
    if chat_id is not None and context_name is not None:
        hunks = _hunk_cache.get((chat_id, context_name), [])
        for h in hunks:
            if h.id == hunk_id:
                return h, context_name, chat_id
        return None, None, None

    # Search all cache entries.
    for (cid, cname), hunks in _hunk_cache.items():
        for h in hunks:
            if h.id == hunk_id:
                return h, cname, cid
    return None, None, None


async def stage_endpoint(request: Request) -> JSONResponse:
    """POST /api/review/stage — stage a hunk by ID."""
    try:
        user_id = await _authenticate(request)
    except AuthError as e:
        return JSONResponse({"error": e.message}, status_code=e.status_code)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            {"error": "Invalid JSON body"}, status_code=400
        )

    hunk_id = body.get("hunk_id")
    if not hunk_id:
        return JSONResponse(
            {"error": "hunk_id is required"}, status_code=400
        )

    chat_id = body.get("chat_id")
    context_name_hint = None
    if chat_id is not None:
        try:
            chat_id = int(chat_id)
            context_name_hint, _ = await _resolve_context(request, chat_id)
        except (ValueError, AuthError):
            pass

    hunk, context_name, resolved_chat_id = _find_cached_hunk(
        hunk_id, chat_id, context_name_hint
    )
    if hunk is None:
        return JSONResponse(
            {"error": "Hunk not found. The diff may have changed — refresh to get current hunks."},
            status_code=409,
        )

    # Resolve the working directory.
    config: Config = request.app.state.config
    if context_name not in config.contexts:
        return JSONResponse(
            {"error": f"Context '{context_name}' not found"},
            status_code=404,
        )
    directory = config.contexts[context_name].directory

    result = await stage_hunk(directory, hunk)

    if not result.ok:
        if result.stale:
            # Invalidate cache on stale hunk.
            cache_key = (resolved_chat_id, context_name)
            _hunk_cache.pop(cache_key, None)
            return JSONResponse(
                {"error": result.error}, status_code=409
            )
        return JSONResponse(
            {"error": result.error}, status_code=500
        )

    # Re-fetch to get updated counts.
    try:
        updated = await get_hunks(directory, offset=0, limit=10000, include_untracked=True)
        _hunk_cache[(resolved_chat_id, context_name)] = updated.hunks
        staged_count = sum(1 for h in updated.hunks if h.staged)
        total_count = updated.total_hunks
    except Exception:
        logger.exception("Failed to refresh hunks after staging")
        staged_count = 0
        total_count = 0

    return JSONResponse({
        "ok": True,
        "staged_hunks": staged_count,
        "total_hunks": total_count,
    })


async def unstage_endpoint(request: Request) -> JSONResponse:
    """POST /api/review/unstage — unstage a hunk by ID."""
    try:
        user_id = await _authenticate(request)
    except AuthError as e:
        return JSONResponse({"error": e.message}, status_code=e.status_code)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            {"error": "Invalid JSON body"}, status_code=400
        )

    hunk_id = body.get("hunk_id")
    if not hunk_id:
        return JSONResponse(
            {"error": "hunk_id is required"}, status_code=400
        )

    chat_id = body.get("chat_id")
    context_name_hint = None
    if chat_id is not None:
        try:
            chat_id = int(chat_id)
            context_name_hint, _ = await _resolve_context(request, chat_id)
        except (ValueError, AuthError):
            pass

    hunk, context_name, resolved_chat_id = _find_cached_hunk(
        hunk_id, chat_id, context_name_hint
    )
    if hunk is None:
        return JSONResponse(
            {"error": "Hunk not found. The diff may have changed — refresh to get current hunks."},
            status_code=409,
        )

    # Resolve the working directory.
    config: Config = request.app.state.config
    if context_name not in config.contexts:
        return JSONResponse(
            {"error": f"Context '{context_name}' not found"},
            status_code=404,
        )
    directory = config.contexts[context_name].directory

    result = await unstage_hunk(directory, hunk)

    if not result.ok:
        if result.stale:
            cache_key = (resolved_chat_id, context_name)
            _hunk_cache.pop(cache_key, None)
            return JSONResponse(
                {"error": result.error}, status_code=409
            )
        return JSONResponse(
            {"error": result.error}, status_code=500
        )

    # Re-fetch to get updated counts.
    try:
        updated = await get_hunks(directory, offset=0, limit=10000, include_untracked=True)
        _hunk_cache[(resolved_chat_id, context_name)] = updated.hunks
        staged_count = sum(1 for h in updated.hunks if h.staged)
        total_count = updated.total_hunks
    except Exception:
        logger.exception("Failed to refresh hunks after unstaging")
        staged_count = 0
        total_count = 0

    return JSONResponse({
        "ok": True,
        "staged_hunks": staged_count,
        "total_hunks": total_count,
    })


def create_review_app(config: Config, db: aiosqlite.Connection) -> Starlette:
    """Create the Starlette application for the review API.

    The config and db are stored on app.state so route handlers can access them.
    Serves the review Mini App frontend at /app/ and API routes at /api/review/.
    """
    # Resolve the frontend dist directory relative to this package.
    _dist_dir = Path(__file__).resolve().parent.parent.parent.parent / "web" / "review-app" / "dist"

    routes: list[Route | Mount] = [
        Route("/api/review/hunks", hunks_endpoint, methods=["GET"]),
        Route("/api/review/stage", stage_endpoint, methods=["POST"]),
        Route("/api/review/unstage", unstage_endpoint, methods=["POST"]),
    ]

    if _dist_dir.is_dir():
        routes.append(
            Mount("/app", app=StaticFiles(directory=str(_dist_dir), html=True), name="review-app")
        )
        logger.info("Serving review Mini App from %s", _dist_dir)
    else:
        logger.warning(
            "Review Mini App dist directory not found at %s — "
            "run 'npm run build' in web/review-app/ to build the frontend",
            _dist_dir,
        )

    app = Starlette(routes=routes)
    app.state.config = config
    app.state.db = db

    return app
