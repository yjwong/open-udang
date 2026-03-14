"""Tests for the cloudflared tunnel module."""

from __future__ import annotations

import asyncio
import os
import platform
import stat
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from open_udang.tunnel import (
    _BIN_DIR,
    _find_cloudflared,
    _get_binary_name,
    _parse_tunnel_url,
    ensure_cloudflared,
    start_tunnel,
    stop_tunnel,
)


class TestGetBinaryName:
    """Tests for _get_binary_name()."""

    def test_linux_x86_64(self) -> None:
        with patch("open_udang.tunnel.platform") as mock_platform:
            mock_platform.system.return_value = "Linux"
            mock_platform.machine.return_value = "x86_64"
            assert _get_binary_name() == "cloudflared-linux-amd64"

    def test_linux_aarch64(self) -> None:
        with patch("open_udang.tunnel.platform") as mock_platform:
            mock_platform.system.return_value = "Linux"
            mock_platform.machine.return_value = "aarch64"
            assert _get_binary_name() == "cloudflared-linux-arm64"

    def test_darwin_arm64(self) -> None:
        with patch("open_udang.tunnel.platform") as mock_platform:
            mock_platform.system.return_value = "Darwin"
            mock_platform.machine.return_value = "arm64"
            assert _get_binary_name() == "cloudflared-darwin-amd64.tgz"

    def test_unsupported_platform(self) -> None:
        with patch("open_udang.tunnel.platform") as mock_platform:
            mock_platform.system.return_value = "FreeBSD"
            mock_platform.machine.return_value = "x86_64"
            assert _get_binary_name() is None


class TestFindCloudflared:
    """Tests for _find_cloudflared()."""

    def test_finds_in_bin_dir(self, tmp_path: Path) -> None:
        """Should find cloudflared in our managed bin directory."""
        fake_bin = tmp_path / "cloudflared"
        fake_bin.write_text("#!/bin/sh\n")
        fake_bin.chmod(fake_bin.stat().st_mode | stat.S_IXUSR)

        with patch("open_udang.tunnel._BIN_DIR", tmp_path):
            result = _find_cloudflared()
            assert result == str(fake_bin)

    def test_finds_in_path(self) -> None:
        """Should fall back to $PATH lookup."""
        with (
            patch("open_udang.tunnel._BIN_DIR", Path("/nonexistent")),
            patch("shutil.which", return_value="/usr/bin/cloudflared"),
        ):
            result = _find_cloudflared()
            assert result == "/usr/bin/cloudflared"

    def test_not_found(self) -> None:
        """Should return None if not found anywhere."""
        with (
            patch("open_udang.tunnel._BIN_DIR", Path("/nonexistent")),
            patch("shutil.which", return_value=None),
        ):
            result = _find_cloudflared()
            assert result is None


class TestParseTunnelUrl:
    """Tests for _parse_tunnel_url()."""

    @pytest.mark.asyncio
    async def test_parses_url_from_stderr(self) -> None:
        """Should extract the trycloudflare.com URL from stderr output."""
        mock_proc = MagicMock()
        mock_proc.stderr = AsyncMock()

        lines = [
            b"2024-01-01 INFO Starting tunnel\n",
            b"2024-01-01 INFO +----------------------------+\n",
            b"2024-01-01 INFO | https://foo-bar-baz.trycloudflare.com |\n",
            b"2024-01-01 INFO +----------------------------+\n",
        ]
        mock_proc.stderr.readline = AsyncMock(side_effect=lines)

        url = await _parse_tunnel_url(mock_proc, timeout=5.0)
        assert url == "https://foo-bar-baz.trycloudflare.com"

    @pytest.mark.asyncio
    async def test_process_exits_before_url(self) -> None:
        """Should raise RuntimeError if process exits without printing URL."""
        mock_proc = MagicMock()
        mock_proc.stderr = AsyncMock()
        mock_proc.stderr.readline = AsyncMock(return_value=b"")
        mock_proc.wait = AsyncMock(return_value=1)

        with pytest.raises(RuntimeError, match="exited with code 1"):
            await _parse_tunnel_url(mock_proc, timeout=5.0)

    @pytest.mark.asyncio
    async def test_timeout(self) -> None:
        """Should raise RuntimeError on timeout."""
        mock_proc = MagicMock()
        mock_proc.stderr = AsyncMock()
        mock_proc.terminate = MagicMock()

        # readline never returns a URL, just keeps returning non-matching lines.
        async def slow_readline() -> bytes:
            await asyncio.sleep(10)
            return b"no url here\n"

        mock_proc.stderr.readline = slow_readline

        with pytest.raises(RuntimeError, match="Timed out"):
            await _parse_tunnel_url(mock_proc, timeout=0.1)


class TestEnsureCloudflared:
    """Tests for ensure_cloudflared()."""

    @pytest.mark.asyncio
    async def test_already_installed(self) -> None:
        """Should return existing path if found."""
        with patch(
            "open_udang.tunnel._find_cloudflared",
            return_value="/usr/bin/cloudflared",
        ):
            result = await ensure_cloudflared()
            assert result == "/usr/bin/cloudflared"

    @pytest.mark.asyncio
    async def test_downloads_if_not_found(self) -> None:
        """Should attempt download if not found."""
        with (
            patch("open_udang.tunnel._find_cloudflared", return_value=None),
            patch(
                "open_udang.tunnel._download_cloudflared",
                return_value="/home/user/.config/openudang/bin/cloudflared",
            ) as mock_download,
        ):
            result = await ensure_cloudflared()
            assert result == "/home/user/.config/openudang/bin/cloudflared"
            mock_download.assert_called_once()


class TestStartTunnel:
    """Tests for start_tunnel()."""

    @pytest.mark.asyncio
    async def test_starts_and_returns_url(self) -> None:
        """Should start cloudflared and return the tunnel URL."""
        mock_proc = MagicMock()
        mock_proc.stderr = AsyncMock()
        mock_proc.stdout = AsyncMock()

        lines = [
            b"INFO Starting tunnel\n",
            b"INFO https://test-tunnel-abc.trycloudflare.com\n",
        ]
        mock_proc.stderr.readline = AsyncMock(side_effect=lines)

        with (
            patch(
                "open_udang.tunnel.ensure_cloudflared",
                return_value="/usr/bin/cloudflared",
            ),
            patch(
                "asyncio.create_subprocess_exec",
                return_value=mock_proc,
            ) as mock_exec,
        ):
            proc, url = await start_tunnel(8080)
            assert url == "https://test-tunnel-abc.trycloudflare.com"
            assert proc is mock_proc

            # Verify the subprocess was called correctly.
            mock_exec.assert_called_once_with(
                "/usr/bin/cloudflared",
                "tunnel",
                "--url",
                "http://localhost:8080",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )


class TestStopTunnel:
    """Tests for stop_tunnel()."""

    @pytest.mark.asyncio
    async def test_terminates_running_process(self) -> None:
        """Should terminate a running tunnel process."""
        mock_proc = MagicMock()
        mock_proc.returncode = None  # Still running.
        mock_proc.terminate = MagicMock()
        mock_proc.kill = MagicMock()
        mock_proc.wait = AsyncMock(return_value=0)

        await stop_tunnel(mock_proc)
        mock_proc.terminate.assert_called_once()

    @pytest.mark.asyncio
    async def test_skips_already_exited(self) -> None:
        """Should not terminate a process that already exited."""
        mock_proc = MagicMock()
        mock_proc.returncode = 0  # Already exited.
        mock_proc.terminate = MagicMock()

        await stop_tunnel(mock_proc)
        mock_proc.terminate.assert_not_called()
