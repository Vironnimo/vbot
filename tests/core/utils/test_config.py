"""Tests for configuration and dotenv helpers."""

import json
import os
from pathlib import Path

import pytest

from core.utils.config import (
    Config,
    _find_worktree_file_from_cwd,
    _resolve_default_data_dir,
    parse_env_lines,
    read_env_file,
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


def test_read_env_file_keeps_existing_quoting_backslashes_and_physical_lines(
    tmp_path: Path,
) -> None:
    env_path = tmp_path / ".env"
    env_path.write_bytes(
        (
            "SINGLE='old value'\r\n"
            'DOUBLE="old value"\r'
            r"BACKSLASH=literal\n\t\path"
            '\nLEADING="literal\nTRAILING=literal"\n'
            "UNICODE=first\u2028SECOND_KEY=second\n"
        ).encode("utf-8")
    )

    assert read_env_file(env_path) == {
        "SINGLE": "old value",
        "DOUBLE": "old value",
        "BACKSLASH": r"literal\n\t\path",
        "LEADING": '"literal',
        "TRAILING": 'literal"',
        "UNICODE": "first\u2028SECOND_KEY=second",
    }


def test_unreadable_utf8_env_file_is_ignored(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    env_path = tmp_path / ".env"
    env_path.write_bytes(b"VALID=value\n\xff")

    with caplog.at_level("WARNING", logger="vbot.config"):
        values = read_env_file(env_path)

    assert values == {}
    assert str(env_path) in caplog.text


def test_config_reads_the_data_directory_env_file_below_the_process_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".env").write_text(
        "VBOT_TEST_FROM_FILE=20\nVBOT_TEST_OVERRIDDEN=file\n", encoding="utf-8"
    )
    monkeypatch.setenv("VBOT_TEST_OVERRIDDEN", "environment")

    config = Config(data_dir=tmp_path)

    # Values from either source are coerced alike.
    assert config.get("VBOT_TEST_FROM_FILE") == 20
    assert config.get("VBOT_TEST_OVERRIDDEN") == "environment"


def test_default_data_dir_is_home_vbot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No env var, no worktree file -> ~/.vbot."""
    monkeypatch.delenv("VBOT_DATA_DIR", raising=False)
    monkeypatch.setattr("core.utils.config._WORKTREE_FILE", tmp_path / ".vbot-worktree")
    monkeypatch.chdir(tmp_path)
    # file does not exist -> falls to default
    assert os.environ.get("VBOT_DATA_DIR") is None
    assert _resolve_default_data_dir() == Path.home() / ".vbot"


def test_worktree_file_sets_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Valid .vbot-worktree file -> its data_dir is used."""
    monkeypatch.delenv("VBOT_DATA_DIR", raising=False)
    worktree_file = tmp_path / ".vbot-worktree"
    worktree_file.write_text(json.dumps({"data_dir": str(tmp_path / "wt-data")}), encoding="utf-8")
    monkeypatch.setattr("core.utils.config._WORKTREE_FILE", worktree_file)
    neutral_cwd = tmp_path / "no-cwd-worktree"
    neutral_cwd.mkdir()
    monkeypatch.chdir(neutral_cwd)
    assert Config().data_dir == tmp_path / "wt-data"


def test_cwd_worktree_file_used_when_module_file_path_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When module marker is missing, CWD worktree marker should be used."""
    monkeypatch.delenv("VBOT_DATA_DIR", raising=False)
    monkeypatch.setattr("core.utils.config._WORKTREE_FILE", tmp_path / "missing/.vbot-worktree")

    wt_root = tmp_path / "worktree-root"
    wt_root.mkdir()
    marker = wt_root / ".vbot-worktree"
    marker.write_text(json.dumps({"data_dir": str(tmp_path / "wt-data")}), encoding="utf-8")

    monkeypatch.chdir(wt_root)

    assert _resolve_default_data_dir() == tmp_path / "wt-data"
    assert _find_worktree_file_from_cwd() == marker


def test_cwd_only_worktree_file_applies_from_its_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("VBOT_DATA_DIR", raising=False)
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    marker = checkout / ".vbot-worktree"
    marker.write_text(
        json.dumps({"data_dir": str(tmp_path / "dev-data"), "cwd_only": True}),
        encoding="utf-8",
    )
    monkeypatch.setattr("core.utils.config._WORKTREE_FILE", marker)
    monkeypatch.chdir(checkout)

    assert _resolve_default_data_dir() == tmp_path / "dev-data"


def test_cwd_only_module_worktree_file_does_not_redirect_installed_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("VBOT_DATA_DIR", raising=False)
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    marker = checkout / ".vbot-worktree"
    marker.write_text(
        json.dumps({"data_dir": str(tmp_path / "dev-data"), "cwd_only": True}),
        encoding="utf-8",
    )
    monkeypatch.setattr("core.utils.config._WORKTREE_FILE", marker)
    outside_checkout = tmp_path / "outside"
    outside_checkout.mkdir()
    monkeypatch.chdir(outside_checkout)

    assert _resolve_default_data_dir() == Path.home() / ".vbot"


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
    """VBOT_DATA_DIR sets the data directory, even when a worktree file exists."""
    env_dir = tmp_path / "env-data"
    monkeypatch.setenv("VBOT_DATA_DIR", str(env_dir))
    worktree_file = tmp_path / ".vbot-worktree"
    worktree_file.write_text(json.dumps({"data_dir": str(tmp_path / "wt-data")}), encoding="utf-8")
    monkeypatch.setattr("core.utils.config._WORKTREE_FILE", worktree_file)
    assert Config().data_dir == env_dir


@pytest.mark.parametrize(
    ("content", "warned"),
    [
        (b"not valid json", True),
        (b"\xff\xfe", True),
        (json.dumps(["not", "an", "object"]).encode(), False),
        (json.dumps({"other_key": "value"}).encode(), False),
    ],
    ids=["malformed-json", "not-utf8", "not-an-object", "no-data-dir"],
)
def test_an_unusable_worktree_file_falls_to_the_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    content: bytes,
    warned: bool,
) -> None:
    monkeypatch.delenv("VBOT_DATA_DIR", raising=False)
    monkeypatch.setattr("core.utils.config._WORKTREE_FILE", tmp_path / "missing/.vbot-worktree")
    marker = tmp_path / ".vbot-worktree"
    marker.write_bytes(content)
    monkeypatch.chdir(tmp_path)

    with caplog.at_level("WARNING", logger="vbot.config"):
        assert _resolve_default_data_dir() == Path.home() / ".vbot"

    assert (str(marker) in caplog.text) is warned
