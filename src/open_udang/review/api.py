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

from open_udang.config import Config, ContextConfig
from open_udang.db import get_active_context
from open_udang.review.auth import AuthError, validate_init_data
from open_udang.review.git_diff import Hunk, get_hunks
from open_udang.review.git_stage import stage_hunk, unstage_hunk

logger = logging.getLogger(__name__)

# In-memory hunk cache: (chat_id, context_name, dir_index) -> list[Hunk]
# Stores the full (unpaginated) hunk list so we can look up hunks by ID
# for stage/unstage operations.
_hunk_cache: dict[tuple[int, str, int], list[Hunk]] = {}


def _get_directories(ctx: ContextConfig) -> list[str]:
    """Return the ordered list of directories for a context.

    Index 0 is the main directory, followed by additional_directories.
    """
    return [ctx.directory] + ctx.additional_directories


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
    request: Request, chat_id: int, dir_index: int = 0
) -> tuple[str, str]:
    """Resolve the active context and directory for a chat.

    Args:
        request: The incoming HTTP request.
        chat_id: Telegram chat ID.
        dir_index: Zero-based directory index (0 = main directory,
            1+ = additional_directories).

    Returns:
        (context_name, working_directory).

    Raises:
        AuthError(404) if the context is not found.
        AuthError(400) if dir_index is out of range.
    """
    config: Config = request.app.state.config
    db: aiosqlite.Connection = request.app.state.db

    context_name = await get_active_context(db, chat_id)
    if context_name is None:
        context_name = config.default_context

    if context_name not in config.contexts:
        raise AuthError(404, f"Context '{context_name}' not found")

    ctx = config.contexts[context_name]
    dirs = _get_directories(ctx)
    if dir_index < 0 or dir_index >= len(dirs):
        raise AuthError(400, f"Invalid directory index: {dir_index}")
    return context_name, dirs[dir_index]


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
    dir_index = int(request.query_params.get("dir", "0"))
    include_untracked = request.query_params.get(
        "include_untracked", "true"
    ).lower() in ("true", "1", "yes")

    try:
        context_name, directory = await _resolve_context(
            request, chat_id, dir_index
        )
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
        _hunk_cache[(chat_id, context_name, dir_index)] = all_result.hunks

        # Build per-file summary from the full hunk list so the
        # frontend can populate the file picker even before all
        # pages have been loaded.
        file_summary: list[dict[str, Any]] = []
        file_map: dict[str, dict[str, Any]] = {}
        for idx, h in enumerate(all_result.hunks):
            if h.file_path not in file_map:
                entry = {
                    "path": h.file_path,
                    "first_hunk_index": idx,
                    "hunk_count": 1,
                    "staged_count": 1 if h.staged else 0,
                }
                file_map[h.file_path] = entry
                file_summary.append(entry)
            else:
                file_map[h.file_path]["hunk_count"] += 1
                if h.staged:
                    file_map[h.file_path]["staged_count"] += 1

        # Apply pagination.
        total = all_result.total_hunks
        paginated = all_result.hunks[offset : offset + limit]

        result = {
            "context": context_name,
            "directory": directory,
            "total_hunks": total,
            "offset": offset,
            "hunks": [_hunk_to_dict(h) for h in paginated],
            "files": file_summary,
        }
    except ValueError as e:
        logger.warning("Not a git repo: %s — %s", directory, e)
        return JSONResponse(
            {"error": str(e)}, status_code=400
        )
    except Exception:
        logger.exception("Failed to get hunks for %s", directory)
        return JSONResponse(
            {"error": "Failed to get diff hunks"}, status_code=500
        )

    return JSONResponse(result)


def _find_cached_hunk(
    hunk_id: str,
    chat_id: int | None = None,
    context_name: str | None = None,
    dir_index: int | None = None,
) -> tuple[Hunk | None, str | None, int | None, int | None]:
    """Find a hunk by ID in the cache.

    If chat_id, context_name, and dir_index are all provided, search only
    that specific cache entry.  Otherwise, search all cache entries.

    Returns (hunk, context_name, chat_id, dir_index) or
    (None, None, None, None).
    """
    if chat_id is not None and context_name is not None and dir_index is not None:
        hunks = _hunk_cache.get((chat_id, context_name, dir_index), [])
        for h in hunks:
            if h.id == hunk_id:
                return h, context_name, chat_id, dir_index
        return None, None, None, None

    # Search all cache entries.
    for (cid, cname, didx), hunks in _hunk_cache.items():
        for h in hunks:
            if h.id == hunk_id:
                return h, cname, cid, didx
    return None, None, None, None


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
    dir_index_raw = body.get("dir", 0)
    try:
        dir_index = int(dir_index_raw)
    except (TypeError, ValueError):
        dir_index = 0

    context_name_hint = None
    if chat_id is not None:
        try:
            chat_id = int(chat_id)
            context_name_hint, _ = await _resolve_context(
                request, chat_id, dir_index
            )
        except (ValueError, AuthError):
            pass

    hunk, context_name, resolved_chat_id, resolved_dir_index = _find_cached_hunk(
        hunk_id, chat_id, context_name_hint, dir_index
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
    ctx = config.contexts[context_name]
    dirs = _get_directories(ctx)
    didx = resolved_dir_index if resolved_dir_index is not None else 0
    if didx < 0 or didx >= len(dirs):
        return JSONResponse(
            {"error": f"Invalid directory index: {didx}"},
            status_code=400,
        )
    directory = dirs[didx]

    result = await stage_hunk(directory, hunk)

    if not result.ok:
        status = 409 if result.stale else 500
        if result.stale:
            # Invalidate cache so the next refresh fetches fresh hunks.
            cache_key = (resolved_chat_id, context_name, didx)
            _hunk_cache.pop(cache_key, None)
        return JSONResponse({"error": result.error}, status_code=status)

    return JSONResponse({"ok": True})


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
    dir_index_raw = body.get("dir", 0)
    try:
        dir_index = int(dir_index_raw)
    except (TypeError, ValueError):
        dir_index = 0

    context_name_hint = None
    if chat_id is not None:
        try:
            chat_id = int(chat_id)
            context_name_hint, _ = await _resolve_context(
                request, chat_id, dir_index
            )
        except (ValueError, AuthError):
            pass

    hunk, context_name, resolved_chat_id, resolved_dir_index = _find_cached_hunk(
        hunk_id, chat_id, context_name_hint, dir_index
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
    ctx = config.contexts[context_name]
    dirs = _get_directories(ctx)
    didx = resolved_dir_index if resolved_dir_index is not None else 0
    if didx < 0 or didx >= len(dirs):
        return JSONResponse(
            {"error": f"Invalid directory index: {didx}"},
            status_code=400,
        )
    directory = dirs[didx]

    result = await unstage_hunk(directory, hunk)

    if not result.ok:
        status = 409 if result.stale else 500
        if result.stale:
            cache_key = (resolved_chat_id, context_name, didx)
            _hunk_cache.pop(cache_key, None)
        return JSONResponse({"error": result.error}, status_code=status)

    return JSONResponse({"ok": True})


async def commit_endpoint(request: Request) -> JSONResponse:
    """POST /api/review/commit — request the bot to commit staged changes.

    The Mini App calls this endpoint instead of ``WebApp.sendData()``
    because ``sendData`` only works when the Mini App is opened from a
    ``KeyboardButton``, not an ``InlineKeyboardButton``.

    Expects JSON body: ``{"chat_id": <int>}``
    """
    try:
        user_id = await _authenticate(request)
    except AuthError as e:
        return JSONResponse({"error": e.message}, status_code=e.status_code)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    try:
        chat_id = int(body["chat_id"])
    except (KeyError, ValueError, TypeError):
        return JSONResponse(
            {"error": "chat_id is required (integer)"}, status_code=400
        )

    from open_udang.dispatch_registry import dispatch as dispatch_to_agent

    prompt = (
        "Please commit the currently staged changes. "
        "Generate an appropriate commit message based on the staged diff."
    )
    try:
        await dispatch_to_agent(prompt, chat_id)
    except RuntimeError as e:
        logger.error("commit_endpoint: %s", e)
        return JSONResponse(
            {"error": "Commit dispatch not available — bot may not be running"},
            status_code=503,
        )
    except Exception:
        logger.exception("Failed to dispatch commit for chat %d", chat_id)
        return JSONResponse(
            {"error": "Failed to dispatch commit"}, status_code=500
        )

    return JSONResponse({"ok": True})


def create_review_app(config: Config, db: aiosqlite.Connection) -> Starlette:
    """Create the Starlette application for the review API.

    The config and db are stored on app.state so route handlers can access them.
    Serves the review Mini App frontend at /app/ and API routes at /api/review/.
    """
    # Resolve the frontend dist directory.  Check the package-bundled location
    # first (used by PyApp / pip install), then fall back to the development
    # layout (git checkout with web/review-app/dist/ built locally).
    _pkg_static = Path(__file__).resolve().parent / "static"
    _dev_dist = Path(__file__).resolve().parent.parent.parent.parent / "web" / "review-app" / "dist"
    _dist_dir = _pkg_static if _pkg_static.is_dir() else _dev_dist

    routes: list[Route | Mount] = [
        Route("/api/review/hunks", hunks_endpoint, methods=["GET"]),
        Route("/api/review/stage", stage_endpoint, methods=["POST"]),
        Route("/api/review/unstage", unstage_endpoint, methods=["POST"]),
        Route("/api/review/commit", commit_endpoint, methods=["POST"]),
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
