"""Tests for path-scoped tool approval and session-approved-dirs in hooks.py.

Covers the "Claude-Code-style" UX:
- Out-of-scope path-tool calls always prompt; blanket "Approve all <Tool>"
  rules cannot bypass the directory boundary.
- Paths within session-approved dirs auto-approve for any tool, including
  the mutating ones (Edit/Write).
- The suggested directory passed to ``request_approval`` matches Claude
  Code's parent-of-file granularity.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from open_shrimp.opencode_client.events import (
    PermissionResultAllow,
    PermissionResultDeny,
    ToolPermissionContext,
)

from open_shrimp.hooks import (
    ApprovalRule,
    _suggested_session_dir,
    make_can_use_tool,
    matches_approval_rule,
    parse_apply_patch_files,
)


def _ctx(tool_use_id: str = "tu_1") -> ToolPermissionContext:
    return ToolPermissionContext(tool_use_id=tool_use_id)


# ---------------------------------------------------------------------------
# _suggested_session_dir
# ---------------------------------------------------------------------------


class TestSuggestedSessionDir:
    def test_read_returns_parent_of_file(self) -> None:
        assert _suggested_session_dir(
            "Read", {"filePath": "/etc/passwd"},
        ) == "/etc"

    def test_edit_returns_parent_of_file(self) -> None:
        assert _suggested_session_dir(
            "Edit", {"filePath": "/var/log/syslog"},
        ) == "/var/log"

    def test_write_returns_parent_of_file(self) -> None:
        assert _suggested_session_dir(
            "Write", {"filePath": "/tmp/out.txt"},
        ) == "/tmp"

    def test_glob_returns_path_itself(self) -> None:
        assert _suggested_session_dir("Glob", {"path": "/etc"}) == "/etc"

    def test_grep_returns_path_itself(self) -> None:
        assert _suggested_session_dir("Grep", {"path": "/var/log"}) == "/var/log"

    def test_glob_without_path_returns_none(self) -> None:
        assert _suggested_session_dir("Glob", {}) is None

    def test_read_without_file_path_returns_none(self) -> None:
        assert _suggested_session_dir("Read", {}) is None

    def test_non_path_tool_returns_none(self) -> None:
        assert _suggested_session_dir("Bash", {"command": "ls"}) is None
        assert _suggested_session_dir("WebFetch", {"url": "x"}) is None


# ---------------------------------------------------------------------------
# make_can_use_tool: in-scope vs out-of-scope path checks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestMakeCanUseToolPathScope:
    async def test_in_cwd_read_auto_approves(self, tmp_path: Path) -> None:
        request_approval = AsyncMock(return_value=False)
        can_use = make_can_use_tool(
            request_approval=request_approval,
            cwd=str(tmp_path),
        )
        target = tmp_path / "f.txt"
        target.write_text("x")
        result = await can_use("Read", {"filePath": str(target)}, _ctx())
        assert isinstance(result, PermissionResultAllow)
        request_approval.assert_not_awaited()

    async def test_additional_dir_read_auto_approves(self, tmp_path: Path) -> None:
        cwd = tmp_path / "cwd"
        extra = tmp_path / "extra"
        cwd.mkdir(); extra.mkdir()
        target = extra / "f.txt"
        target.write_text("x")
        request_approval = AsyncMock(return_value=False)
        can_use = make_can_use_tool(
            request_approval=request_approval,
            cwd=str(cwd),
            additional_directories=[str(extra)],
        )
        result = await can_use("Read", {"filePath": str(target)}, _ctx())
        assert isinstance(result, PermissionResultAllow)
        request_approval.assert_not_awaited()

    async def test_out_of_scope_read_prompts(self, tmp_path: Path) -> None:
        cwd = tmp_path / "cwd"; cwd.mkdir()
        outside = tmp_path / "outside"; outside.mkdir()
        target = outside / "f.txt"; target.write_text("x")
        request_approval = AsyncMock(return_value=True)
        can_use = make_can_use_tool(
            request_approval=request_approval,
            cwd=str(cwd),
        )
        result = await can_use("Read", {"filePath": str(target)}, _ctx())
        assert isinstance(result, PermissionResultAllow)
        request_approval.assert_awaited_once()
        # Suggested dir is the parent of the file (Claude Code parity).
        args = request_approval.await_args.args
        assert args[0] == "Read"
        assert args[2] == "tu_1"
        assert args[3] == str(outside)

    async def test_out_of_scope_glob_uses_path_as_suggested_dir(
        self, tmp_path: Path,
    ) -> None:
        cwd = tmp_path / "cwd"; cwd.mkdir()
        outside = tmp_path / "outside"; outside.mkdir()
        request_approval = AsyncMock(return_value=True)
        can_use = make_can_use_tool(
            request_approval=request_approval,
            cwd=str(cwd),
        )
        await can_use("Glob", {"path": str(outside), "pattern": "*"}, _ctx())
        request_approval.assert_awaited_once()
        assert request_approval.await_args.args[3] == str(outside)

    async def test_in_scope_does_not_pass_suggested_dir(
        self, tmp_path: Path,
    ) -> None:
        # When the path falls inside scope, Read auto-approves and never
        # reaches the prompt — so no suggested_dir is computed at all.
        request_approval = AsyncMock(return_value=True)
        can_use = make_can_use_tool(
            request_approval=request_approval,
            cwd=str(tmp_path),
        )
        target = tmp_path / "f.txt"; target.write_text("x")
        await can_use("Read", {"filePath": str(target)}, _ctx())
        request_approval.assert_not_awaited()


# ---------------------------------------------------------------------------
# Blanket "Approve all <Tool>" rule does NOT bypass directory boundary
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestPathToolGateBlocksBlanketRule:
    async def test_approve_all_read_does_not_bypass_out_of_scope(
        self, tmp_path: Path,
    ) -> None:
        cwd = tmp_path / "cwd"; cwd.mkdir()
        outside = tmp_path / "outside"; outside.mkdir()
        target = outside / "secret.txt"; target.write_text("s")

        # User has previously clicked some hypothetical "Approve all Read" —
        # we still expect the prompt to fire for an out-of-scope path.
        request_approval = AsyncMock(return_value=False)
        can_use = make_can_use_tool(
            request_approval=request_approval,
            cwd=str(cwd),
            is_tool_auto_approved=lambda tn, ti: tn == "Read",
        )
        result = await can_use("Read", {"filePath": str(target)}, _ctx())
        assert isinstance(result, PermissionResultDeny)
        request_approval.assert_awaited_once()

    async def test_blanket_rule_still_works_for_in_scope_tool(
        self, tmp_path: Path,
    ) -> None:
        # WebFetch isn't path-scoped — the blanket rule should auto-approve
        # without ever prompting (this preserves existing behavior).
        request_approval = AsyncMock(return_value=False)
        can_use = make_can_use_tool(
            request_approval=request_approval,
            cwd=str(tmp_path),
            is_tool_auto_approved=lambda tn, ti: tn == "WebFetch",
        )
        result = await can_use("WebFetch", {"url": "https://x"}, _ctx())
        assert isinstance(result, PermissionResultAllow)
        request_approval.assert_not_awaited()


# ---------------------------------------------------------------------------
# Session-approved dirs grant full access (read AND write)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestSessionApprovedDirs:
    async def test_session_dir_auto_approves_read(
        self, tmp_path: Path,
    ) -> None:
        cwd = tmp_path / "cwd"; cwd.mkdir()
        approved = tmp_path / "approved"; approved.mkdir()
        target = approved / "f.txt"; target.write_text("x")
        request_approval = AsyncMock(return_value=False)
        can_use = make_can_use_tool(
            request_approval=request_approval,
            cwd=str(cwd),
            get_session_approved_dirs=lambda: [str(approved)],
        )
        result = await can_use("Read", {"filePath": str(target)}, _ctx())
        assert isinstance(result, PermissionResultAllow)
        request_approval.assert_not_awaited()

    async def test_session_dir_auto_approves_edit_without_accept_all_edits(
        self, tmp_path: Path,
    ) -> None:
        # Edits in session-approved dirs bypass the accept-all-edits gate.
        cwd = tmp_path / "cwd"; cwd.mkdir()
        approved = tmp_path / "approved"; approved.mkdir()
        target = approved / "f.txt"; target.write_text("x")
        notify = AsyncMock()
        request_approval = AsyncMock(return_value=False)
        can_use = make_can_use_tool(
            request_approval=request_approval,
            cwd=str(cwd),
            is_edit_auto_approved=lambda: False,
            notify_auto_approved_edit=notify,
            get_session_approved_dirs=lambda: [str(approved)],
        )
        result = await can_use(
            "Edit",
            {"filePath": str(target), "oldString": "x", "newString": "y"},
            _ctx(),
        )
        assert isinstance(result, PermissionResultAllow)
        request_approval.assert_not_awaited()
        # User still gets to see the diff.
        notify.assert_awaited_once()

    async def test_in_cwd_edit_still_requires_accept_all_edits(
        self, tmp_path: Path,
    ) -> None:
        # Within static cwd (not session-approved), Edit still prompts when
        # accept-all-edits is off — session-dirs don't change cwd semantics.
        target = tmp_path / "f.txt"; target.write_text("x")
        request_approval = AsyncMock(return_value=False)
        can_use = make_can_use_tool(
            request_approval=request_approval,
            cwd=str(tmp_path),
            is_edit_auto_approved=lambda: False,
        )
        await can_use(
            "Edit",
            {"filePath": str(target), "oldString": "x", "newString": "y"},
            _ctx(),
        )
        request_approval.assert_awaited_once()

    async def test_session_dirs_recomputed_per_call(
        self, tmp_path: Path,
    ) -> None:
        # The callback is consulted fresh on each invocation so newly-added
        # session dirs take effect immediately for the next tool call.
        cwd = tmp_path / "cwd"; cwd.mkdir()
        approved = tmp_path / "approved"; approved.mkdir()
        target = approved / "f.txt"; target.write_text("x")
        dirs: list[str] = []
        request_approval = AsyncMock(return_value=False)
        can_use = make_can_use_tool(
            request_approval=request_approval,
            cwd=str(cwd),
            get_session_approved_dirs=lambda: list(dirs),
        )
        # First call: dirs empty -> prompt.
        result1 = await can_use("Read", {"filePath": str(target)}, _ctx("a"))
        assert isinstance(result1, PermissionResultDeny)
        # User clicks the dir button — simulate by mutating dirs.
        dirs.append(str(approved))
        # Second call: now in scope -> auto-approve.
        result2 = await can_use("Read", {"filePath": str(target)}, _ctx("b"))
        assert isinstance(result2, PermissionResultAllow)


# ---------------------------------------------------------------------------
# Existing behavior preserved: matches_approval_rule sanity
# ---------------------------------------------------------------------------


def test_matches_approval_rule_blanket() -> None:
    rule = ApprovalRule(tool_name="WebFetch", pattern=None)
    assert matches_approval_rule(rule, "WebFetch", {"url": "x"}) is True
    assert matches_approval_rule(rule, "WebSearch", {"query": "x"}) is False


# ---------------------------------------------------------------------------
# ApplyPatch (OpenCode): patchText path extraction + auto-approve paths
# ---------------------------------------------------------------------------


class TestParseApplyPatchFiles:
    def test_extracts_add_update_delete_headers(self) -> None:
        patch = (
            "*** Begin Patch\n"
            "*** Add File: src/new.py\n"
            "+hello\n"
            "*** Update File: src/app.py\n"
            "@@ def f():\n"
            "-1\n"
            "+2\n"
            "*** Delete File: old.txt\n"
            "*** End Patch\n"
        )
        assert parse_apply_patch_files(patch) == [
            ("add", "src/new.py"),
            ("update", "src/app.py"),
            ("delete", "old.txt"),
        ]

    def test_extracts_move_target(self) -> None:
        patch = (
            "*** Update File: src/a.py\n"
            "*** Move to: src/b.py\n"
            "@@\n-x\n+y\n"
        )
        assert parse_apply_patch_files(patch) == [
            ("update", "src/a.py"),
            ("move", "src/b.py"),
        ]

    def test_absolute_paths_kept_as_is(self) -> None:
        assert parse_apply_patch_files(
            "*** Add File: /etc/passwd\n+root::0\n"
        ) == [("add", "/etc/passwd")]

    def test_empty_envelope_returns_empty_list(self) -> None:
        assert parse_apply_patch_files("") == []
        assert parse_apply_patch_files(
            "*** Begin Patch\n*** End Patch\n"
        ) == []


@pytest.mark.asyncio
class TestApplyPatchApproval:
    async def test_in_cwd_with_accept_all_edits_auto_approves(
        self, tmp_path: Path,
    ) -> None:
        notify = AsyncMock()
        request_approval = AsyncMock(return_value=False)
        patch = (
            "*** Begin Patch\n"
            "*** Update File: a.py\n@@\n-x\n+y\n"
            "*** End Patch\n"
        )
        can_use = make_can_use_tool(
            request_approval=request_approval,
            cwd=str(tmp_path),
            is_edit_auto_approved=lambda: True,
            notify_auto_approved_edit=notify,
        )
        result = await can_use(
            "ApplyPatch", {"patchText": patch}, _ctx(),
        )
        assert isinstance(result, PermissionResultAllow)
        request_approval.assert_not_awaited()
        notify.assert_awaited_once()

    async def test_out_of_scope_path_does_not_auto_approve(
        self, tmp_path: Path,
    ) -> None:
        # Even with accept-all-edits on, an absolute path outside cwd must
        # still prompt — the directory boundary holds.
        cwd = tmp_path / "cwd"; cwd.mkdir()
        outside = tmp_path / "outside"; outside.mkdir()
        patch = (
            f"*** Update File: {outside / 'x.py'}\n@@\n-a\n+b\n"
        )
        request_approval = AsyncMock(return_value=False)
        can_use = make_can_use_tool(
            request_approval=request_approval,
            cwd=str(cwd),
            is_edit_auto_approved=lambda: True,
        )
        result = await can_use(
            "ApplyPatch", {"patchText": patch}, _ctx(),
        )
        assert isinstance(result, PermissionResultDeny)
        request_approval.assert_awaited_once()

    async def test_without_accept_all_edits_prompts(
        self, tmp_path: Path,
    ) -> None:
        patch = "*** Update File: a.py\n@@\n-x\n+y\n"
        request_approval = AsyncMock(return_value=False)
        can_use = make_can_use_tool(
            request_approval=request_approval,
            cwd=str(tmp_path),
            is_edit_auto_approved=lambda: False,
        )
        result = await can_use(
            "ApplyPatch", {"patchText": patch}, _ctx(),
        )
        assert isinstance(result, PermissionResultDeny)
        request_approval.assert_awaited_once()
