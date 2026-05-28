"""Unit tests for the GET /session wrapper."""

from __future__ import annotations

import pytest

from open_shrimp.opencode_client import SessionInfo, list_sessions

from tests.opencode_client.mock_server import MockOpenCode

pytestmark = pytest.mark.asyncio


async def test_list_sessions_translates_rows(
    mock_server: MockOpenCode, wired_server, tmp_path,
) -> None:
    canonical = str(tmp_path.resolve())
    mock_server.session_rows = [
        {
            "id": "ses_abc",
            "title": "first session",
            "directory": canonical,
            "time": {"created": 1779970000000, "updated": 1779970500000},
        },
        {
            "id": "ses_def",
            "title": "second session",
            "directory": canonical,
            "time": {"created": 1779970100000, "updated": 1779970600000},
        },
    ]

    out = await list_sessions(tmp_path)

    assert [type(s) for s in out] == [SessionInfo, SessionInfo]
    assert out[0].session_id == "ses_abc"
    assert out[0].summary == "first session"
    assert out[0].last_modified == 1779970500000
    assert out[0].created_at == 1779970000000
    assert out[1].session_id == "ses_def"


async def test_list_sessions_filters_by_canonical_directory(
    mock_server: MockOpenCode, wired_server, tmp_path,
) -> None:
    canonical = str(tmp_path.resolve())
    other = "/some/other/path"
    mock_server.session_rows = [
        {
            "id": "ses_match",
            "title": "ours",
            "directory": canonical,
            "time": {"updated": 1, "created": 0},
        },
        {
            "id": "ses_skip",
            "title": "theirs",
            "directory": other,
            "time": {"updated": 2, "created": 0},
        },
    ]

    out = await list_sessions(tmp_path)

    assert [s.session_id for s in out] == ["ses_match"]


async def test_list_sessions_empty_directory_raises(
    mock_server: MockOpenCode, wired_server,
) -> None:
    with pytest.raises(ValueError):
        await list_sessions("")


async def test_list_sessions_returns_empty_when_nothing_matches(
    mock_server: MockOpenCode, wired_server, tmp_path,
) -> None:
    mock_server.session_rows = []
    out = await list_sessions(tmp_path)
    assert out == []


async def test_list_sessions_handles_missing_title(
    mock_server: MockOpenCode, wired_server, tmp_path,
) -> None:
    canonical = str(tmp_path.resolve())
    mock_server.session_rows = [
        {
            "id": "ses_x",
            "directory": canonical,
            "time": {"updated": 1, "created": 0},
        }
    ]
    out = await list_sessions(tmp_path)
    assert out[0].summary == "(no title)"
