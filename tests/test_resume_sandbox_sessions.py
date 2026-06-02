from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from open_shrimp.config import ContextConfig, SandboxConfig
from open_shrimp.handlers import commands
from open_shrimp.opencode_client import SessionInfo


pytestmark = pytest.mark.asyncio


@dataclass(frozen=True)
class FakeServer:
    base_url: str = "http://127.0.0.1:4096"
    auth_header: str = "Bearer sandbox"


class FakeSandbox:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def ensure_environment(self) -> None:
        self.calls.append("ensure_environment")

    def ensure_running(self) -> None:
        self.calls.append("ensure_running")

    def provision_workspace(self) -> None:
        self.calls.append("provision_workspace")

    def ensure_opencode_server(self) -> FakeServer:
        self.calls.append("ensure_opencode_server")
        return FakeServer()


class FakeManager:
    def __init__(self, opencode_home: Path, sandbox: FakeSandbox) -> None:
        self._opencode_home = opencode_home
        self.sandbox = sandbox
        self.created = False

    def opencode_home_dir(self, context_name: str) -> Path:
        return self._opencode_home / context_name

    def create_sandbox(self, context_name: str, context: ContextConfig) -> FakeSandbox:
        self.created = True
        return self.sandbox


def _sandbox_context(directory: Path) -> ContextConfig:
    return ContextConfig(
        directory=str(directory),
        description="sandbox",
        allowed_tools=[],
        sandbox=SandboxConfig(backend="docker"),
    )


async def test_sandbox_resume_listing_uses_sandbox_opencode_server(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    ctx = _sandbox_context(tmp_path / "repo")
    home_base = tmp_path / "state"
    (home_base / "sandboxed").mkdir(parents=True)
    sandbox = FakeSandbox()
    manager = FakeManager(home_base, sandbox)
    seen: dict[str, Any] = {}

    async def fake_list_sessions(
        directory: str,
        *,
        limit: int = 500,
        base_url: str | None = None,
        auth_header: str | None = None,
    ) -> list[SessionInfo]:
        seen.update(
            directory=directory,
            limit=limit,
            base_url=base_url,
            auth_header=auth_header,
        )
        return [SessionInfo("ses_1", "sandbox session", 10)]

    monkeypatch.setattr(commands, "list_sessions", fake_list_sessions)

    sessions = await commands._list_sessions_for_context(
        "sandboxed",
        ctx,
        sandbox_manager=manager,
        limit=1,
    )

    assert [s.session_id for s in sessions] == ["ses_1"]
    assert manager.created is True
    assert sandbox.calls == [
        "ensure_environment",
        "ensure_running",
        "provision_workspace",
        "ensure_opencode_server",
    ]
    assert seen == {
        "directory": str(tmp_path / "repo"),
        "limit": 1,
        "base_url": "http://127.0.0.1:4096",
        "auth_header": "Bearer sandbox",
    }


async def test_sandbox_resume_listing_skips_uninitialized_sandbox_state(
    tmp_path: Path,
) -> None:
    ctx = _sandbox_context(tmp_path / "repo")
    sandbox = FakeSandbox()
    manager = FakeManager(tmp_path / "missing-state", sandbox)

    sessions = await commands._list_sessions_for_context(
        "sandboxed",
        ctx,
        sandbox_manager=manager,
    )

    assert sessions == []
    assert manager.created is False
    assert sandbox.calls == []
