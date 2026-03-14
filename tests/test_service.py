"""Tests for the service install/uninstall module."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from open_udang.config import DEFAULT_CONFIG_PATH
from open_udang.service import (
    _detect_executable,
    _detect_platform,
    _generate_launchd_plist,
    _generate_systemd_unit,
    install_service,
    uninstall_service,
)


class TestDetectPlatform:
    def test_linux(self) -> None:
        with patch("open_udang.service.sys") as mock_sys:
            mock_sys.platform = "linux"
            assert _detect_platform() == "linux"

    def test_macos(self) -> None:
        with patch("open_udang.service.sys") as mock_sys:
            mock_sys.platform = "darwin"
            assert _detect_platform() == "macos"

    def test_unsupported(self) -> None:
        with patch("open_udang.service.sys") as mock_sys:
            mock_sys.platform = "win32"
            with pytest.raises(RuntimeError, match="Unsupported platform"):
                _detect_platform()


class TestDetectExecutable:
    def test_found_on_path(self, tmp_path: Path) -> None:
        exe = tmp_path / "openudang"
        exe.touch()
        with patch("open_udang.service.shutil.which", return_value=str(exe)):
            result = _detect_executable()
        assert result == str(exe.resolve())

    def test_found_next_to_python(self, tmp_path: Path) -> None:
        fake_python = tmp_path / "python"
        fake_python.touch()
        exe = tmp_path / "openudang"
        exe.touch()
        with (
            patch("open_udang.service.shutil.which", return_value=None),
            patch("open_udang.service.sys") as mock_sys,
        ):
            mock_sys.executable = str(fake_python)
            result = _detect_executable()
        assert result == str(exe.resolve())

    def test_fallback_to_module(self, tmp_path: Path) -> None:
        fake_python = tmp_path / "python"
        fake_python.touch()
        with (
            patch("open_udang.service.shutil.which", return_value=None),
            patch("open_udang.service.sys") as mock_sys,
        ):
            mock_sys.executable = str(fake_python)
            result = _detect_executable()
        assert result == f"{fake_python} -m open_udang"


class TestGenerateSystemdUnit:
    def test_basic(self) -> None:
        unit = _generate_systemd_unit("/usr/bin/openudang", "/etc/config.yaml")
        assert "ExecStart=/usr/bin/openudang --config /etc/config.yaml" in unit
        assert "WantedBy=default.target" in unit
        assert "Restart=on-failure" in unit
        assert "ANTHROPIC_API_KEY" not in unit


class TestGenerateLaunchdPlist:
    def test_basic(self) -> None:
        plist = _generate_launchd_plist("/usr/bin/openudang", "/etc/config.yaml")
        assert "<string>/usr/bin/openudang</string>" in plist
        assert "<string>--config</string>" in plist
        assert "<string>/etc/config.yaml</string>" in plist
        assert "com.openudang.bot" in plist
        assert "<key>KeepAlive</key>" in plist
        assert "openudang.stderr.log" in plist
        assert "ANTHROPIC_API_KEY" not in plist

    def test_module_fallback_args(self) -> None:
        plist = _generate_launchd_plist(
            "/usr/bin/python -m open_udang", "/etc/config.yaml"
        )
        assert "<string>/usr/bin/python</string>" in plist
        assert "<string>-m</string>" in plist
        assert "<string>open_udang</string>" in plist


class TestInstallService:
    @patch("open_udang.service._run")
    @patch("open_udang.service._detect_executable", return_value="/usr/bin/openudang")
    @patch("open_udang.service._detect_platform", return_value="linux")
    def test_install_linux(
        self,
        _plat: MagicMock,
        _exe: MagicMock,
        mock_run: MagicMock,
        tmp_path: Path,
    ) -> None:
        config = tmp_path / "config.yaml"
        config.write_text("test: true")
        svc_path = tmp_path / "open-udang.service"

        with patch("open_udang.service._SYSTEMD_UNIT_PATH", svc_path):
            install_service(str(config))

        assert svc_path.exists()
        content = svc_path.read_text()
        assert "ExecStart=/usr/bin/openudang" in content

        # Verify systemctl calls
        calls = mock_run.call_args_list
        cmd_lists = [c[0][0] for c in calls]
        assert ["systemctl", "--user", "daemon-reload"] in cmd_lists
        assert ["systemctl", "--user", "enable", "open-udang.service"] in cmd_lists
        assert ["systemctl", "--user", "start", "open-udang.service"] in cmd_lists

    @patch("open_udang.service._run")
    @patch("open_udang.service._detect_executable", return_value="/usr/bin/openudang")
    @patch("open_udang.service._detect_platform", return_value="macos")
    def test_install_macos(
        self,
        _plat: MagicMock,
        _exe: MagicMock,
        mock_run: MagicMock,
        tmp_path: Path,
    ) -> None:
        config = tmp_path / "config.yaml"
        config.write_text("test: true")
        svc_path = tmp_path / "com.openudang.bot.plist"
        log_dir = tmp_path / "logs"

        with (
            patch("open_udang.service._LAUNCHD_PLIST_PATH", svc_path),
            patch("open_udang.service._LAUNCHD_LOG_DIR", log_dir),
        ):
            install_service(str(config))

        assert svc_path.exists()
        content = svc_path.read_text()
        assert "<string>/usr/bin/openudang</string>" in content
        assert log_dir.exists()

    @patch("open_udang.service._detect_platform", return_value="linux")
    def test_install_existing_declines(
        self,
        _plat: MagicMock,
        tmp_path: Path,
    ) -> None:
        svc_path = tmp_path / "open-udang.service"
        svc_path.write_text("existing")

        with (
            patch("open_udang.service._SYSTEMD_UNIT_PATH", svc_path),
            patch("open_udang.service.sys") as mock_sys,
            patch("builtins.input", return_value="n"),
        ):
            mock_sys.stdin.isatty.return_value = True
            mock_sys.platform = "linux"
            install_service(str(DEFAULT_CONFIG_PATH))

        # Should not have been overwritten
        assert svc_path.read_text() == "existing"


class TestUninstallService:
    @patch("open_udang.service._run")
    @patch("open_udang.service._detect_platform", return_value="linux")
    def test_uninstall_linux(
        self,
        _plat: MagicMock,
        mock_run: MagicMock,
        tmp_path: Path,
    ) -> None:
        svc_path = tmp_path / "open-udang.service"
        svc_path.write_text("[Unit]\nDescription=test")

        with patch("open_udang.service._SYSTEMD_UNIT_PATH", svc_path):
            uninstall_service()

        assert not svc_path.exists()
        calls = mock_run.call_args_list
        cmd_lists = [c[0][0] for c in calls]
        assert ["systemctl", "--user", "stop", "open-udang.service"] in cmd_lists
        assert ["systemctl", "--user", "disable", "open-udang.service"] in cmd_lists
        assert ["systemctl", "--user", "daemon-reload"] in cmd_lists

    @patch("open_udang.service._run")
    @patch("open_udang.service._detect_platform", return_value="macos")
    def test_uninstall_macos(
        self,
        _plat: MagicMock,
        mock_run: MagicMock,
        tmp_path: Path,
    ) -> None:
        svc_path = tmp_path / "com.openudang.bot.plist"
        svc_path.write_text("<plist>test</plist>")

        with patch("open_udang.service._LAUNCHD_PLIST_PATH", svc_path):
            uninstall_service()

        assert not svc_path.exists()

    @patch("open_udang.service._detect_platform", return_value="linux")
    def test_uninstall_not_installed(
        self,
        _plat: MagicMock,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        svc_path = tmp_path / "open-udang.service"

        with patch("open_udang.service._SYSTEMD_UNIT_PATH", svc_path):
            uninstall_service()

        captured = capsys.readouterr()
        assert "not installed" in captured.out
