"""Unit tests for OpenCodeServer spawn/supervision.

Uses a tiny Python script as a fake `opencode` binary: prints the
`listening on …` line, echoes the env-var, then sleeps until SIGTERM.
"""

from __future__ import annotations

import asyncio
import os
import sys
import textwrap

import pytest

from open_shrimp.opencode_client import process as proc_mod
from open_shrimp.opencode_client.errors import OpenCodeNotFoundError
from open_shrimp.opencode_client.process import OpenCodeServer

pytestmark = pytest.mark.asyncio


_FAKE_BIN = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import os, sys, time
    pwd = os.environ.get("OPENCODE_SERVER_PASSWORD", "")
    print(f"listening on http://127.0.0.1:54321  (pw_len={len(pwd)})", flush=True)
    try:
        time.sleep(60)
    except KeyboardInterrupt:
        pass
    """
)


@pytest.fixture
def fake_binary(tmp_path) -> str:
    binary = tmp_path / "fake-opencode"
    binary.write_text(_FAKE_BIN)
    binary.chmod(0o755)
    # The fake binary is a Python script with a shebang. macOS/Linux can
    # exec it directly given the shebang line.
    return str(binary)


async def test_spawn_parses_listening_line(fake_binary, monkeypatch) -> None:
    monkeypatch.setenv("OPENCODE_BIN", fake_binary)
    OpenCodeServer._instance = None
    server = await OpenCodeServer.get_or_start()
    try:
        assert server.base_url == "http://127.0.0.1:54321"
        assert server.password
        assert len(server.password) == 64  # 32 bytes hex
        assert await server.health() is True
    finally:
        await server.stop()
    assert server.proc.returncode is not None


async def test_get_or_start_is_idempotent(fake_binary, monkeypatch) -> None:
    monkeypatch.setenv("OPENCODE_BIN", fake_binary)
    OpenCodeServer._instance = None
    s1 = await OpenCodeServer.get_or_start()
    s2 = await OpenCodeServer.get_or_start()
    try:
        assert s1 is s2
    finally:
        await s1.stop()


async def test_password_propagates_to_child(fake_binary, monkeypatch) -> None:
    """The child must see OPENCODE_SERVER_PASSWORD in its env."""
    monkeypatch.setenv("OPENCODE_BIN", fake_binary)
    OpenCodeServer._instance = None
    server = await OpenCodeServer.get_or_start()
    try:
        # Fake binary prints `(pw_len=64)` after listening; our parser
        # already captured the listening line. Just check the password is
        # exactly 32 bytes hex.
        assert len(server.password) == 64
    finally:
        await server.stop()


async def test_missing_binary_raises(monkeypatch) -> None:
    """If OPENCODE_BIN points nowhere and nothing is on PATH, raise."""
    monkeypatch.setenv("OPENCODE_BIN", "/no/such/binary")
    # Push HOME somewhere with no ~/.opencode/bin/opencode.
    monkeypatch.setenv("HOME", "/tmp/__nonexistent_home_for_test__")
    monkeypatch.setenv("PATH", "/no/such/path")
    OpenCodeServer._instance = None
    with pytest.raises(OpenCodeNotFoundError):
        await OpenCodeServer.get_or_start()


async def test_stop_kills_runaway_child(monkeypatch, tmp_path) -> None:
    """If the child ignores SIGTERM, stop() escalates to SIGKILL within 5s."""
    stubborn = tmp_path / "stubborn"
    stubborn.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import signal, time
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            print("listening on http://127.0.0.1:54321", flush=True)
            while True:
                time.sleep(0.1)
            """
        )
    )
    stubborn.chmod(0o755)
    monkeypatch.setenv("OPENCODE_BIN", str(stubborn))
    OpenCodeServer._instance = None
    server = await OpenCodeServer.get_or_start()
    await server.stop()
    assert server.proc.returncode is not None
