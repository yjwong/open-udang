"""Tests for the setup wizard."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from open_udang.setup import _path_completer, _validate_directory, run_setup_wizard


def _make_inputs(*responses: str):
    """Create a side_effect iterator for mocking input()."""
    it = iter(responses)
    return lambda prompt="": next(it)


class TestRunSetupWizard:
    """End-to-end tests for run_setup_wizard."""

    def test_creates_valid_config(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.yaml"
        inputs = _make_inputs(
            "111:AAA-bbb",  # token
            "42",  # user ID
            "myproject",  # context name
            "/tmp",  # directory (always exists)
            "My project",  # description
            "1",  # model choice (sonnet)
        )

        with patch("builtins.input", side_effect=inputs):
            run_setup_wizard(config_path)

        assert config_path.exists()
        raw = yaml.safe_load(config_path.read_text())

        assert raw["telegram"]["token"] == "111:AAA-bbb"
        assert raw["allowed_users"] == [42]
        assert "myproject" in raw["contexts"]
        assert raw["default_context"] == "myproject"

        ctx = raw["contexts"]["myproject"]
        assert ctx["description"] == "My project"
        assert ctx["model"] == "claude-sonnet-4-6"
        assert ctx["allowed_tools"] == ["LSP", "AskUserQuestion"]
        review = raw["review"]
        assert review["tunnel"] == "cloudflared"
        assert 49152 <= review["port"] <= 65535

    def test_uses_defaults(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.yaml"
        inputs = _make_inputs(
            "111:AAA-bbb",  # token
            "42",  # user ID
            "",  # context name (default)
            "/tmp",  # directory
            "",  # description (default)
            "",  # model choice (default: 1)
        )

        with patch("builtins.input", side_effect=inputs):
            run_setup_wizard(config_path)

        raw = yaml.safe_load(config_path.read_text())
        assert raw["default_context"] == "default"
        assert raw["contexts"]["default"]["description"] == "Default context"
        assert raw["contexts"]["default"]["model"] == "claude-sonnet-4-6"

    def test_custom_model(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.yaml"
        inputs = _make_inputs(
            "111:AAA-bbb",  # token
            "42",  # user ID
            "default",  # context name
            "/tmp",  # directory
            "test",  # description
            "4",  # model choice (custom)
            "claude-custom-model",  # custom model name
        )

        with patch("builtins.input", side_effect=inputs):
            run_setup_wizard(config_path)

        raw = yaml.safe_load(config_path.read_text())
        assert raw["contexts"]["default"]["model"] == "claude-custom-model"

    def test_ctrl_c_cancels(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.yaml"

        with patch("builtins.input", side_effect=KeyboardInterrupt):
            with pytest.raises(SystemExit) as exc_info:
                run_setup_wizard(config_path)
            assert exc_info.value.code == 0

        assert not config_path.exists()

    def test_eof_cancels(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.yaml"

        with patch("builtins.input", side_effect=EOFError):
            with pytest.raises(SystemExit) as exc_info:
                run_setup_wizard(config_path)
            assert exc_info.value.code == 0

        assert not config_path.exists()

    def test_invalid_token_reprompts(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.yaml"
        inputs = _make_inputs(
            "bad-token",  # invalid (no colon)
            "111:AAA-bbb",  # valid
            "42",  # user ID
            "default",  # context name
            "/tmp",  # directory
            "test",  # description
            "1",  # model
        )

        with patch("builtins.input", side_effect=inputs):
            run_setup_wizard(config_path)

        raw = yaml.safe_load(config_path.read_text())
        assert raw["telegram"]["token"] == "111:AAA-bbb"

    def test_invalid_user_id_reprompts(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.yaml"
        inputs = _make_inputs(
            "111:AAA-bbb",  # token
            "not-a-number",  # invalid
            "-5",  # invalid (negative)
            "42",  # valid
            "default",  # context name
            "/tmp",  # directory
            "test",  # description
            "1",  # model
        )

        with patch("builtins.input", side_effect=inputs):
            run_setup_wizard(config_path)

        raw = yaml.safe_load(config_path.read_text())
        assert raw["allowed_users"] == [42]

    def test_invalid_directory_reprompts(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.yaml"
        inputs = _make_inputs(
            "111:AAA-bbb",  # token
            "42",  # user ID
            "default",  # context name
            "/nonexistent/path/that/does/not/exist",  # invalid
            "/tmp",  # valid
            "test",  # description
            "1",  # model
        )

        with patch("builtins.input", side_effect=inputs):
            run_setup_wizard(config_path)

        raw = yaml.safe_load(config_path.read_text())
        assert raw["contexts"]["default"]["directory"] == "/tmp"

    def test_creates_parent_directories(self, tmp_path: Path) -> None:
        config_path = tmp_path / "deep" / "nested" / "config.yaml"
        inputs = _make_inputs(
            "111:AAA-bbb",
            "42",
            "default",
            "/tmp",
            "test",
            "1",
        )

        with patch("builtins.input", side_effect=inputs):
            run_setup_wizard(config_path)

        assert config_path.exists()

    def test_tilde_directory_accepted(self, tmp_path: Path) -> None:
        """Tilde paths like ~/projects should be accepted and resolved."""
        config_path = tmp_path / "config.yaml"
        inputs = _make_inputs(
            "111:AAA-bbb",  # token
            "42",  # user ID
            "default",  # context name
            "~",  # directory (home dir, always exists)
            "test",  # description
            "1",  # model
        )

        with patch("builtins.input", side_effect=inputs):
            run_setup_wizard(config_path)

        raw = yaml.safe_load(config_path.read_text())
        resolved = raw["contexts"]["default"]["directory"]
        # Should be resolved to an absolute path, not contain ~
        assert "~" not in resolved
        assert Path(resolved).is_absolute()

    def test_config_roundtrips_through_load(self, tmp_path: Path) -> None:
        """The wizard-generated config should pass load_config validation."""
        from open_udang.config import load_config

        config_path = tmp_path / "config.yaml"
        inputs = _make_inputs(
            "111:AAA-bbb",
            "42",
            "default",
            "/tmp",
            "test",
            "1",
        )

        with patch("builtins.input", side_effect=inputs):
            run_setup_wizard(config_path)

        config = load_config(str(config_path))
        assert config.telegram.token == "111:AAA-bbb"
        assert config.allowed_users == [42]
        assert config.default_context == "default"


class TestPathCompleter:
    """Tests for the readline path completer."""

    def test_completes_existing_directory(self, tmp_path: Path) -> None:
        (tmp_path / "subdir").mkdir()
        result = _path_completer(str(tmp_path) + "/sub", 0)
        assert result is not None
        assert "subdir/" in result

    def test_returns_none_for_no_match(self, tmp_path: Path) -> None:
        result = _path_completer(str(tmp_path) + "/nonexistent_xyz", 0)
        assert result is None

    def test_returns_none_past_end(self, tmp_path: Path) -> None:
        (tmp_path / "only_one").mkdir()
        # State 0 should return a match, state 1 should return None
        assert _path_completer(str(tmp_path) + "/only_one", 0) is not None
        assert _path_completer(str(tmp_path) + "/only_one", 1) is None

    def test_tilde_completion(self) -> None:
        """Tilde paths should be expanded for matching but kept in output."""
        result = _path_completer("~/", 0)
        # Home dir always has some contents, so first completion should work
        assert result is not None


class TestValidateDirectory:
    """Tests for _validate_directory with tilde expansion."""

    def test_tilde_is_expanded(self) -> None:
        assert _validate_directory("~") is None

    def test_nonexistent_path_rejected(self) -> None:
        assert _validate_directory("/nonexistent/path/xyz") is not None
