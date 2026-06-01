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

from open_shrimp.config import Config, ContextConfig
from open_shrimp.db import ChatScope, get_active_context
from open_shrimp.review.auth import AuthError, authenticate
from open_shrimp.review.git_diff import Hunk, get_hunks
from open_shrimp.review.git_stage import (
    stage_hunk,
    unstage_hunk,
    stage_file,
    unstage_file,
    remove_intent_to_add,
)

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


def _display_path(hunk: Hunk) -> str:
    """Return the user-facing path for a hunk.

    For submodule hunks this prefixes the in-repo file path with the
    submodule's location so the frontend sees one flat namespace.
    """
    if hunk.repo_path:
        return f"{hunk.repo_path}/{hunk.file_path}"
    return hunk.file_path


def _hunk_to_dict(hunk: Hunk) -> dict[str, Any]:
    """Convert a Hunk dataclass to a JSON-serialisable dict.

    ``file_path`` in the response is the display path (prefixed with
    the submodule path when applicable); ``repo_path`` is exposed
    separately so the UI can show a badge if it wants.
    """
    return {
        "id": hunk.id,
        "file_path": _display_path(hunk),
        "repo_path": hunk.repo_path,
        "language": hunk.language,
        "is_new_file": hunk.is_new_file,
        "is_deleted_file": hunk.is_deleted_file,
        "hunk_header": hunk.hunk_header,
        "lines": [dataclasses.asdict(line) for line in hunk.lines],
        "staged": hunk.staged,
        "is_binary": hunk.is_binary,
        "is_empty": hunk.is_empty,
    }


async def _authenticate(request: Request) -> int:
    """Validate the Authorization header and return the user ID.

    Raises AuthError on failure.
    """
    config: Config = request.app.state.config
    authorization = request.headers.get("authorization", "")
    return await authenticate(
        authorization, config.telegram.token, config.allowed_users
    )


async def _resolve_context(
    request: Request, chat_id: int, dir_index: int = 0,
    thread_id: int | None = None,
) -> tuple[str, str]:
    """Resolve the active context and directory for a chat.

    Args:
        request: The incoming HTTP request.
        chat_id: Telegram chat ID.
        dir_index: Zero-based directory index (0 = main directory,
            1+ = additional_directories).
        thread_id: Optional Telegram message thread ID (for forum topics).

    Returns:
        (context_name, working_directory).

    Raises:
        AuthError(404) if the context is not found.
        AuthError(400) if dir_index is out of range.
    """
    config: Config = request.app.state.config
    db: aiosqlite.Connection = request.app.state.db

    context_name = await get_active_context(db, ChatScope(chat_id, thread_id))
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
    thread_id_raw = request.query_params.get("thread_id")
    thread_id = int(thread_id_raw) if thread_id_raw is not None else None
    include_untracked = request.query_params.get(
        "include_untracked", "true"
    ).lower() in ("true", "1", "yes")

    try:
        context_name, directory = await _resolve_context(
            request, chat_id, dir_index, thread_id
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
        # pages have been loaded.  Use the display path so submodule
        # files namespace correctly under their submodule path.
        file_summary: list[dict[str, Any]] = []
        file_map: dict[str, dict[str, Any]] = {}
        for idx, h in enumerate(all_result.hunks):
            display = _display_path(h)
            if display not in file_map:
                entry = {
                    "path": display,
                    "first_hunk_index": idx,
                    "hunk_count": 1,
                    "staged_count": 1 if h.staged else 0,
                }
                file_map[display] = entry
                file_summary.append(entry)
            else:
                file_map[display]["hunk_count"] += 1
                if h.staged:
                    file_map[display]["staged_count"] += 1

        # Apply pagination, ensuring we never split a file's hunks
        # across page boundaries.
        total = all_result.total_hunks
        end = min(offset + limit, total)
        # Extend the page to include all remaining hunks of the last file.
        if end < total and end > offset:
            last_display = _display_path(all_result.hunks[end - 1])
            while end < total and _display_path(all_result.hunks[end]) == last_display:
                end += 1
        paginated = all_result.hunks[offset:end]

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

    thread_id_raw = body.get("thread_id")
    thread_id = int(thread_id_raw) if thread_id_raw is not None else None

    context_name_hint = None
    if chat_id is not None:
        try:
            chat_id = int(chat_id)
            context_name_hint, _ = await _resolve_context(
                request, chat_id, dir_index, thread_id
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

    thread_id_raw = body.get("thread_id")
    thread_id = int(thread_id_raw) if thread_id_raw is not None else None

    context_name_hint = None
    if chat_id is not None:
        try:
            chat_id = int(chat_id)
            context_name_hint, _ = await _resolve_context(
                request, chat_id, dir_index, thread_id
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


async def skip_endpoint(request: Request) -> JSONResponse:
    """POST /api/review/skip — skip a hunk, cleaning up intent-to-add for new files.

    For new files that were marked with ``git add --intent-to-add`` during
    hunk fetching, this removes the index entry so the file goes back to
    being truly untracked.  For non-new files, this is a no-op (the skip
    is purely a frontend concept).
    """
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

    thread_id_raw = body.get("thread_id")
    thread_id = int(thread_id_raw) if thread_id_raw is not None else None

    context_name_hint = None
    if chat_id is not None:
        try:
            chat_id = int(chat_id)
            context_name_hint, _ = await _resolve_context(
                request, chat_id, dir_index, thread_id
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

    # Only clean up intent-to-add for unstaged new files.
    if not hunk.is_new_file or hunk.staged:
        return JSONResponse({"ok": True})

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

    result = await remove_intent_to_add(directory, hunk)

    if not result.ok:
        # Not fatal — log but still return ok to the frontend.
        logger.warning(
            "Failed to remove intent-to-add for %s: %s",
            hunk.file_path, result.error,
        )

    return JSONResponse({"ok": True})


async def stage_file_endpoint(request: Request) -> JSONResponse:
    """POST /api/review/stage-file — stage all unstaged hunks for a file."""
    try:
        user_id = await _authenticate(request)
    except AuthError as e:
        return JSONResponse({"error": e.message}, status_code=e.status_code)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    file_path = body.get("file_path")
    if not file_path:
        return JSONResponse(
            {"error": "file_path is required"}, status_code=400
        )

    chat_id = body.get("chat_id")
    dir_index_raw = body.get("dir", 0)
    try:
        dir_index = int(dir_index_raw)
    except (TypeError, ValueError):
        dir_index = 0

    thread_id_raw = body.get("thread_id")
    thread_id = int(thread_id_raw) if thread_id_raw is not None else None

    context_name_hint = None
    if chat_id is not None:
        try:
            chat_id = int(chat_id)
            context_name_hint, _ = await _resolve_context(
                request, chat_id, dir_index, thread_id
            )
        except (ValueError, AuthError):
            pass

    # Find all unstaged hunks for this file in the cache.  ``file_path``
    # from the frontend is the display path (submodule-prefixed when
    # applicable), so match against ``_display_path(h)`` rather than
    # the raw in-repo ``h.file_path``.
    cache_key = (chat_id, context_name_hint, dir_index) if chat_id is not None and context_name_hint else None
    if cache_key is None:
        # Try to find any cache entry containing this file.
        for key, cached_hunks in _hunk_cache.items():
            for h in cached_hunks:
                if _display_path(h) == file_path:
                    cache_key = key
                    break
            if cache_key:
                break

    if cache_key is None:
        return JSONResponse(
            {"error": "No cached hunks found. Refresh to load hunks first."},
            status_code=409,
        )

    cached_hunks = _hunk_cache.get(cache_key, [])
    unstaged_hunks = [
        h for h in cached_hunks
        if _display_path(h) == file_path and not h.staged
    ]

    if not unstaged_hunks:
        return JSONResponse({"ok": True, "staged_ids": []})

    # Resolve the working directory.
    config: Config = request.app.state.config
    resolved_context = cache_key[1]
    if resolved_context not in config.contexts:
        return JSONResponse(
            {"error": f"Context '{resolved_context}' not found"},
            status_code=404,
        )
    ctx = config.contexts[resolved_context]
    dirs = _get_directories(ctx)
    didx = cache_key[2]
    if didx < 0 or didx >= len(dirs):
        return JSONResponse(
            {"error": f"Invalid directory index: {didx}"},
            status_code=400,
        )
    directory = dirs[didx]

    result = await stage_file(directory, unstaged_hunks)

    if not result.ok:
        status = 409 if result.stale else 500
        if result.stale:
            _hunk_cache.pop(cache_key, None)
        return JSONResponse({"error": result.error}, status_code=status)

    staged_ids = [h.id for h in unstaged_hunks]
    return JSONResponse({"ok": True, "staged_ids": staged_ids})


async def unstage_file_endpoint(request: Request) -> JSONResponse:
    """POST /api/review/unstage-file — unstage all staged hunks for a file."""
    try:
        user_id = await _authenticate(request)
    except AuthError as e:
        return JSONResponse({"error": e.message}, status_code=e.status_code)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    hunk_ids = body.get("hunk_ids")
    if not hunk_ids or not isinstance(hunk_ids, list):
        return JSONResponse(
            {"error": "hunk_ids is required (list of hunk IDs)"}, status_code=400
        )

    chat_id = body.get("chat_id")
    dir_index_raw = body.get("dir", 0)
    try:
        dir_index = int(dir_index_raw)
    except (TypeError, ValueError):
        dir_index = 0

    thread_id_raw = body.get("thread_id")
    thread_id = int(thread_id_raw) if thread_id_raw is not None else None

    context_name_hint = None
    if chat_id is not None:
        try:
            chat_id = int(chat_id)
            context_name_hint, _ = await _resolve_context(
                request, chat_id, dir_index, thread_id
            )
        except (ValueError, AuthError):
            pass

    # Find all requested hunks in the cache.
    hunk_id_set = set(hunk_ids)
    hunks_to_unstage: list[Hunk] = []
    resolved_cache_key = None

    cache_key = (chat_id, context_name_hint, dir_index) if chat_id is not None and context_name_hint else None
    if cache_key and cache_key in _hunk_cache:
        for h in _hunk_cache[cache_key]:
            if h.id in hunk_id_set:
                hunks_to_unstage.append(h)
        resolved_cache_key = cache_key
    else:
        for key, cached_hunks in _hunk_cache.items():
            for h in cached_hunks:
                if h.id in hunk_id_set:
                    hunks_to_unstage.append(h)
                    resolved_cache_key = key
            if hunks_to_unstage:
                break

    if not hunks_to_unstage:
        return JSONResponse(
            {"error": "Hunks not found. The diff may have changed — refresh to get current hunks."},
            status_code=409,
        )

    # Resolve the working directory.
    config: Config = request.app.state.config
    resolved_context = resolved_cache_key[1]
    if resolved_context not in config.contexts:
        return JSONResponse(
            {"error": f"Context '{resolved_context}' not found"},
            status_code=404,
        )
    ctx = config.contexts[resolved_context]
    dirs = _get_directories(ctx)
    didx = resolved_cache_key[2]
    if didx < 0 or didx >= len(dirs):
        return JSONResponse(
            {"error": f"Invalid directory index: {didx}"},
            status_code=400,
        )
    directory = dirs[didx]

    result = await unstage_file(directory, hunks_to_unstage)

    if not result.ok:
        status = 409 if result.stale else 500
        if result.stale:
            _hunk_cache.pop(resolved_cache_key, None)
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

    thread_id_raw = body.get("thread_id")
    thread_id = int(thread_id_raw) if thread_id_raw is not None else None

    from open_shrimp.dispatch_registry import dispatch as dispatch_to_agent

    prompt = (
        "Please commit the currently staged changes. "
        "Generate an appropriate commit message based on the staged diff."
    )
    try:
        await dispatch_to_agent(
            prompt, chat_id, thread_id,
            placeholder="\u23f3 Committing staged changes\\.\\.\\.",
        )
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


async def submit_comments_endpoint(request: Request) -> JSONResponse:
    """POST /api/review/submit-comments — send review comments to the agent.

    Expects JSON body::

        {
            "chat_id": <int>,
            "thread_id": <int|null>,
            "comments": [
                {
                    "file_path": "...",
                    "hunk_header": "...",
                    "comment": "..."
                },
                ...
            ]
        }

    Builds a structured prompt from the comments and dispatches it to the
    agent for the given chat.
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

    thread_id_raw = body.get("thread_id")
    thread_id = int(thread_id_raw) if thread_id_raw is not None else None

    comments = body.get("comments")
    if not comments or not isinstance(comments, list):
        return JSONResponse(
            {"error": "comments is required (non-empty list)"}, status_code=400
        )

    # Build a structured prompt from the review comments.
    parts: list[str] = [
        "I've reviewed the current changes and have the following comments. "
        "Please address each one:\n"
    ]
    for i, c in enumerate(comments, 1):
        file_path = c.get("file_path", "unknown")
        hunk_header = c.get("hunk_header", "")
        comment = c.get("comment", "").strip()
        if not comment:
            continue
        parts.append(f"{i}. **{file_path}**")
        if hunk_header:
            parts.append(f"   `{hunk_header}`")
        parts.append(f"   {comment}\n")

    if len(parts) <= 1:
        return JSONResponse(
            {"error": "No non-empty comments provided"}, status_code=400
        )

    prompt = "\n".join(parts)

    from open_shrimp.dispatch_registry import dispatch as dispatch_to_agent

    try:
        await dispatch_to_agent(
            prompt, chat_id, thread_id,
            placeholder="\u23f3 Processing review comments\\.\\.\\.",
        )
    except RuntimeError as e:
        logger.error("submit_comments_endpoint: %s", e)
        return JSONResponse(
            {"error": "Dispatch not available — bot may not be running"},
            status_code=503,
        )
    except Exception:
        logger.exception("Failed to dispatch review comments for chat %d", chat_id)
        return JSONResponse(
            {"error": "Failed to dispatch review comments"}, status_code=500
        )

    return JSONResponse({"ok": True})


def create_review_app(
    config: Config,
    db: aiosqlite.Connection,
    sandbox_managers: "dict[str, SandboxManager] | None" = None,
    config_path: str | None = None,
) -> Starlette:
    """Create the Starlette application for the review API.

    The config, db, and sandbox_managers are stored on app.state so
    route handlers can access them.
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
        Route("/api/review/stage-file", stage_file_endpoint, methods=["POST"]),
        Route("/api/review/unstage-file", unstage_file_endpoint, methods=["POST"]),
        Route("/api/review/skip", skip_endpoint, methods=["POST"]),
        Route("/api/review/commit", commit_endpoint, methods=["POST"]),
        Route("/api/review/submit-comments", submit_comments_endpoint, methods=["POST"]),
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

    # Add terminal Mini App routes.
    from open_shrimp.terminal.api import create_terminal_routes

    routes.extend(create_terminal_routes())

    # Add markdown preview Mini App routes.
    from open_shrimp.preview.api import create_preview_routes

    routes.extend(create_preview_routes())

    # Add VNC Mini App routes (WebSocket proxy + static frontend).
    from open_shrimp.vnc.api import create_vnc_routes

    routes.extend(create_vnc_routes())

    # Add config Mini App routes.
    from open_shrimp.config_app.api import create_config_routes

    routes.extend(create_config_routes())

    app = Starlette(routes=routes)
    app.state.config = config
    app.state.db = db
    app.state.sandbox_managers = sandbox_managers
    app.state.config_path = config_path

    return app
