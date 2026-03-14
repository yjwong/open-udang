"""Tests for the review HTTP API routes."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from unittest.mock import AsyncMock, patch
from urllib.parse import urlencode

import pytest
from httpx import ASGITransport, AsyncClient

from open_udang.config import Config, ContextConfig, ReviewConfig, TelegramConfig
from open_udang.review.api import _hunk_cache, create_review_app
from open_udang.review.git_diff import Hunk, HunkLine, HunkResult
from open_udang.review.git_stage import StageResult

BOT_TOKEN = "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"
ALLOWED_USER_ID = 111222333
CHAT_ID = 99887766


def _make_config() -> Config:
    return Config(
        telegram=TelegramConfig(token=BOT_TOKEN),
        allowed_users=[ALLOWED_USER_ID],
        contexts={
            "default": ContextConfig(
                directory="/tmp/test-repo",
                description="Test context",
                model="claude-sonnet-4-6",
                allowed_tools=[],
            ),
        },
        default_context="default",
        review=ReviewConfig(host="127.0.0.1", port=8080),
    )


def _build_init_data(
    bot_token: str = BOT_TOKEN,
    user_id: int = ALLOWED_USER_ID,
    auth_date: int | None = None,
    tamper_hash: bool = False,
) -> str:
    """Build a valid initData query string."""
    if auth_date is None:
        auth_date = int(time.time())

    user_obj = json.dumps(
        {"id": user_id, "first_name": "Test", "username": "testuser"},
        separators=(",", ":"),
    )

    params: dict[str, str] = {
        "auth_date": str(auth_date),
        "user": user_obj,
        "query_id": "AAHQ",
    }

    data_check_string = "\n".join(
        f"{k}={params[k]}" for k in sorted(params)
    )

    secret_key = hmac.new(
        b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256
    ).digest()
    computed_hash = hmac.new(
        secret_key, data_check_string.encode("utf-8"), hashlib.sha256
    ).hexdigest()

    if tamper_hash:
        computed_hash = "a" * 64

    params["hash"] = computed_hash
    return urlencode(params)


def _auth_header(init_data: str | None = None) -> dict[str, str]:
    """Build an Authorization header dict."""
    if init_data is None:
        init_data = _build_init_data()
    return {"authorization": f"tg-init-data {init_data}"}


def _make_hunk(
    hunk_id: str = "abc123",
    file_path: str = "src/main.py",
    staged: bool = False,
) -> Hunk:
    return Hunk(
        id=hunk_id,
        file_path=file_path,
        language="python",
        is_new_file=False,
        is_deleted_file=False,
        hunk_header="@@ -1,3 +1,4 @@",
        lines=[
            HunkLine(type="context", old_no=1, new_no=1, content="import os"),
            HunkLine(type="add", old_no=None, new_no=2, content="import json"),
            HunkLine(type="context", old_no=2, new_no=3, content=""),
        ],
        staged=staged,
        is_binary=False,
    )


def _make_hunk_result(hunks: list[Hunk] | None = None) -> HunkResult:
    if hunks is None:
        hunks = [_make_hunk()]
    return HunkResult(total_hunks=len(hunks), offset=0, hunks=hunks)


@pytest.fixture
def config() -> Config:
    return _make_config()


@pytest.fixture
def db_mock() -> AsyncMock:
    db = AsyncMock()
    # get_active_context returns "default"
    cursor_mock = AsyncMock()
    cursor_mock.fetchone = AsyncMock(return_value=("default",))
    db.execute = AsyncMock(return_value=cursor_mock)
    return db


@pytest.fixture
def app(config: Config, db_mock: AsyncMock):
    _hunk_cache.clear()
    return create_review_app(config, db_mock)


@pytest.fixture
def transport(app):
    return ASGITransport(app=app)


# ── GET /api/review/hunks ──


@pytest.mark.asyncio
async def test_get_hunks_success(transport) -> None:
    """Authenticated request returns hunks JSON."""
    hunks = [_make_hunk()]
    result = _make_hunk_result(hunks)

    with patch("open_udang.review.api.get_hunks", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = result
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(
                f"/api/review/hunks?chat_id={CHAT_ID}",
                headers=_auth_header(),
            )

    assert resp.status_code == 200
    data = resp.json()
    assert data["context"] == "default"
    assert data["total_hunks"] == 1
    assert len(data["hunks"]) == 1
    assert data["hunks"][0]["id"] == "abc123"
    assert data["hunks"][0]["file_path"] == "src/main.py"


@pytest.mark.asyncio
async def test_get_hunks_no_auth(transport) -> None:
    """Request without auth header returns 401."""
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(f"/api/review/hunks?chat_id={CHAT_ID}")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_get_hunks_bad_hmac(transport) -> None:
    """Request with tampered HMAC returns 401."""
    init_data = _build_init_data(tamper_hash=True)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            f"/api/review/hunks?chat_id={CHAT_ID}",
            headers=_auth_header(init_data),
        )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_get_hunks_wrong_user(transport) -> None:
    """Request from non-allowed user returns 403."""
    init_data = _build_init_data(user_id=999999999)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            f"/api/review/hunks?chat_id={CHAT_ID}",
            headers=_auth_header(init_data),
        )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_get_hunks_missing_chat_id(transport) -> None:
    """Request without chat_id returns 400."""
    with patch("open_udang.review.api.get_hunks", new_callable=AsyncMock):
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(
                "/api/review/hunks",
                headers=_auth_header(),
            )
    assert resp.status_code == 400
    assert "chat_id" in resp.json()["error"]


@pytest.mark.asyncio
async def test_get_hunks_pagination(transport) -> None:
    """Pagination via offset/limit works correctly."""
    hunks = [_make_hunk(hunk_id=f"hunk{i}") for i in range(5)]
    result = _make_hunk_result(hunks)

    with patch("open_udang.review.api.get_hunks", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = result
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(
                f"/api/review/hunks?chat_id={CHAT_ID}&offset=2&limit=2",
                headers=_auth_header(),
            )

    assert resp.status_code == 200
    data = resp.json()
    assert data["total_hunks"] == 5
    assert data["offset"] == 2
    assert len(data["hunks"]) == 2
    assert data["hunks"][0]["id"] == "hunk2"
    assert data["hunks"][1]["id"] == "hunk3"


# ── POST /api/review/stage ──


@pytest.mark.asyncio
async def test_stage_hunk_success(transport) -> None:
    """Staging a cached hunk returns success."""
    hunk = _make_hunk()
    _hunk_cache[(CHAT_ID, "default", 0)] = [hunk]

    with (
        patch("open_udang.review.api.stage_hunk", new_callable=AsyncMock) as mock_stage,
    ):
        mock_stage.return_value = StageResult(ok=True)

        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/review/stage",
                json={"hunk_id": "abc123", "chat_id": CHAT_ID},
                headers=_auth_header(),
            )

    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True


@pytest.mark.asyncio
async def test_stage_hunk_not_found(transport) -> None:
    """Staging a hunk that's not in cache returns 409."""
    _hunk_cache.clear()

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/review/stage",
            json={"hunk_id": "nonexistent", "chat_id": CHAT_ID},
            headers=_auth_header(),
        )

    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_stage_hunk_stale(transport) -> None:
    """Staging a stale hunk returns 409 and invalidates cache."""
    hunk = _make_hunk()
    _hunk_cache[(CHAT_ID, "default", 0)] = [hunk]

    with patch("open_udang.review.api.stage_hunk", new_callable=AsyncMock) as mock_stage:
        mock_stage.return_value = StageResult(ok=False, error="Hunk is stale", stale=True)

        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/review/stage",
                json={"hunk_id": "abc123", "chat_id": CHAT_ID},
                headers=_auth_header(),
            )

    assert resp.status_code == 409
    # Cache should be invalidated.
    assert (CHAT_ID, "default", 0) not in _hunk_cache


@pytest.mark.asyncio
async def test_stage_hunk_no_auth(transport) -> None:
    """Staging without auth returns 401."""
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/review/stage",
            json={"hunk_id": "abc123"},
        )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_stage_hunk_missing_hunk_id(transport) -> None:
    """Staging without hunk_id returns 400."""
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/review/stage",
            json={},
            headers=_auth_header(),
        )
    assert resp.status_code == 400
    assert "hunk_id" in resp.json()["error"]


# ── POST /api/review/unstage ──


@pytest.mark.asyncio
async def test_unstage_hunk_success(transport) -> None:
    """Unstaging a cached hunk returns success."""
    hunk = _make_hunk(staged=True)
    _hunk_cache[(CHAT_ID, "default", 0)] = [hunk]

    with (
        patch("open_udang.review.api.unstage_hunk", new_callable=AsyncMock) as mock_unstage,
    ):
        mock_unstage.return_value = StageResult(ok=True)

        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/review/unstage",
                json={"hunk_id": "abc123", "chat_id": CHAT_ID},
                headers=_auth_header(),
            )

    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True


@pytest.mark.asyncio
async def test_unstage_hunk_stale(transport) -> None:
    """Unstaging a stale hunk returns 409."""
    hunk = _make_hunk(staged=True)
    _hunk_cache[(CHAT_ID, "default", 0)] = [hunk]

    with patch("open_udang.review.api.unstage_hunk", new_callable=AsyncMock) as mock_unstage:
        mock_unstage.return_value = StageResult(ok=False, error="Hunk is stale", stale=True)

        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/review/unstage",
                json={"hunk_id": "abc123", "chat_id": CHAT_ID},
                headers=_auth_header(),
            )

    assert resp.status_code == 409
    assert (CHAT_ID, "default", 0) not in _hunk_cache


@pytest.mark.asyncio
async def test_unstage_hunk_not_found(transport) -> None:
    """Unstaging a non-existent hunk returns 409."""
    _hunk_cache.clear()

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/review/unstage",
            json={"hunk_id": "nonexistent", "chat_id": CHAT_ID},
            headers=_auth_header(),
        )

    assert resp.status_code == 409


# ── Hunk cache ──


@pytest.mark.asyncio
async def test_hunks_endpoint_populates_cache(transport) -> None:
    """GET /api/review/hunks should populate the hunk cache."""
    _hunk_cache.clear()
    hunks = [_make_hunk()]
    result = _make_hunk_result(hunks)

    with patch("open_udang.review.api.get_hunks", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = result
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            await client.get(
                f"/api/review/hunks?chat_id={CHAT_ID}",
                headers=_auth_header(),
            )

    assert (CHAT_ID, "default", 0) in _hunk_cache
    assert len(_hunk_cache[(CHAT_ID, "default", 0)]) == 1


@pytest.mark.asyncio
async def test_stage_without_chat_id_searches_all_cache(transport) -> None:
    """Stage request without chat_id searches entire cache."""
    hunk = _make_hunk()
    _hunk_cache[(CHAT_ID, "default", 0)] = [hunk]

    updated_result = _make_hunk_result([_make_hunk(staged=True)])

    with (
        patch("open_udang.review.api.stage_hunk", new_callable=AsyncMock) as mock_stage,
        patch("open_udang.review.api.get_hunks", new_callable=AsyncMock) as mock_get,
    ):
        mock_stage.return_value = StageResult(ok=True)
        mock_get.return_value = updated_result

        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/review/stage",
                json={"hunk_id": "abc123"},
                headers=_auth_header(),
            )

    assert resp.status_code == 200
    assert resp.json()["ok"] is True
