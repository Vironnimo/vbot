"""Tests for configuration and dotenv helpers."""

import json
import os
from pathlib import Path

import pytest

from core.utils.config import (
    Config,
    _read_worktree_data_dir,
    _resolve_default_data_dir,
    parse_env_lines,
)


def test_parse_env_lines_keeps_values_conservative() -> None:
    """Dotenv parsing keeps only simple key-value behavior."""
    lines = [
        "# comment",
        "",
        "IGNORED",
        "OPENROUTER_API_KEY=sk-or-test=value",
        "QUOTED='quoted value'",
    ]

    values = parse_env_lines(lines)

    assert values == {
        "OPENROUTER_API_KEY": "sk-or-test=value",
        "QUOTED": "quoted value",
    }


def test_default_data_dir_is_home_vbot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No env var, no worktree file -> ~/.vbot."""
    monkeypatch.delenv("VBOT_DATA_DIR", raising=False)
    monkeypatch.setattr("core.utils.config._WORKTREE_FILE", tmp_path / ".vbot-worktree")
    # file does not exist -> falls to default
    assert os.environ.get("VBOT_DATA_DIR") is None
    assert _resolve_default_data_dir() == Path.home() / ".vbot"
    assert Config().data_dir == Path.home() / ".vbot"


def test_vbot_data_dir_env_var_sets_data_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """VBOT_DATA_DIR env var -> uses that path."""
    monkeypatch.setenv("VBOT_DATA_DIR", str(tmp_path / "custom"))
    monkeypatch.setattr("core.utils.config._WORKTREE_FILE", tmp_path / ".vbot-worktree")
    assert Config().data_dir == tmp_path / "custom"


def test_worktree_file_sets_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Valid .vbot-worktree file -> its data_dir is used."""
    monkeypatch.delenv("VBOT_DATA_DIR", raising=False)
    worktree_file = tmp_path / ".vbot-worktree"
    worktree_file.write_text(json.dumps({"data_dir": str(tmp_path / "wt-data")}), encoding="utf-8")
    monkeypatch.setattr("core.utils.config._WORKTREE_FILE", worktree_file)
    assert Config().data_dir == tmp_path / "wt-data"


def test_explicit_data_dir_arg_wins_over_worktree_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Explicit data_dir constructor arg beats the worktree file."""
    monkeypatch.delenv("VBOT_DATA_DIR", raising=False)
    worktree_file = tmp_path / ".vbot-worktree"
    worktree_file.write_text(json.dumps({"data_dir": str(tmp_path / "wt-data")}), encoding="utf-8")
    monkeypatch.setattr("core.utils.config._WORKTREE_FILE", worktree_file)
    explicit = tmp_path / "explicit"
    assert Config(data_dir=explicit).data_dir == explicit


def test_env_var_wins_over_worktree_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """VBOT_DATA_DIR env var takes priority over the worktree file."""
    env_dir = tmp_path / "env-data"
    monkeypatch.setenv("VBOT_DATA_DIR", str(env_dir))
    worktree_file = tmp_path / ".vbot-worktree"
    worktree_file.write_text(json.dumps({"data_dir": str(tmp_path / "wt-data")}), encoding="utf-8")
    monkeypatch.setattr("core.utils.config._WORKTREE_FILE", worktree_file)
    assert Config().data_dir == env_dir


def test_malformed_json_in_worktree_file_falls_to_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Malformed JSON in .vbot-worktree -> silently falls through to default."""
    monkeypatch.delenv("VBOT_DATA_DIR", raising=False)
    worktree_file = tmp_path / ".vbot-worktree"
    worktree_file.write_text("not valid json", encoding="utf-8")
    result = _read_worktree_data_dir(worktree_file)
    assert result is None


def test_non_object_json_in_worktree_file_falls_to_default(tmp_path: Path) -> None:
    """Valid non-object JSON in .vbot-worktree -> treated as absent marker data."""
    worktree_file = tmp_path / ".vbot-worktree"
    worktree_file.write_text(json.dumps(["not", "an", "object"]), encoding="utf-8")

    result = _read_worktree_data_dir(worktree_file)

    assert result is None


def test_missing_data_dir_key_in_worktree_file_falls_to_default(tmp_path: Path) -> None:
    """Missing data_dir key -> _read_worktree_data_dir returns None."""
    worktree_file = tmp_path / ".vbot-worktree"
    worktree_file.write_text(json.dumps({"other_key": "value"}), encoding="utf-8")
    result = _read_worktree_data_dir(worktree_file)
    assert result is None
