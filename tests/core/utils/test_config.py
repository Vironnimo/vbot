"""Tests for configuration loading."""

import json
import os
import re
from pathlib import Path
from typing import Any

import pytest

from core.utils.config import (
    DEFAULT_PORT,
    VBOT_ROOT,
    Config,
    _find_worktree_file_from_cwd,
    _resolve_default_data_dir,
    resolve_port,
    resolve_server_bind,
)
from core.utils.errors import ConfigError

_PORT_SETTINGS = {"format_version": 1, "server_port": 8500}

# The parts that cannot import DEFAULT_PORT repeat it; each pattern captures that copy.
_DEFAULT_PORT_COPIES = {
    "desktop/main.py": r"^DEFAULT_PORT = (\d+)$",
    "scripts/install.ps1": r"\[int\]\$Port = (\d+),",
    "scripts/install.sh": r'^PORT="(\d+)"$',
    "scripts/windows/vbot.iss": r"\{param:VBOTPORT\|(\d+)\}",
    "webui/src/components/settings/DesktopConnectionSettings.svelte": (
        r"const DEFAULT_SERVER_PORT = '(\d+)';"
    ),
}


@pytest.mark.parametrize("path", sorted(_DEFAULT_PORT_COPIES))
def test_parts_that_cannot_import_the_default_port_repeat_it(path: str) -> None:
    source = (VBOT_ROOT / path).read_text(encoding="utf-8")
    copies = re.findall(_DEFAULT_PORT_COPIES[path], source, flags=re.MULTILINE)

    assert copies == [str(DEFAULT_PORT)]


def test_config_layers_overrides_below_the_process_environment_and_ignores_env_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A plaintext .env is no configuration source: credentials live in the Secret Store.
    (tmp_path / ".env").write_text("VBOT_TEST_FROM_FILE=file\n", encoding="utf-8")
    monkeypatch.setenv("VBOT_TEST_OVERRIDDEN", "environment")

    config = Config(
        data_dir=tmp_path,
        overrides={"VBOT_TEST_FROM_OVERRIDE": "20", "VBOT_TEST_OVERRIDDEN": "override"},
    )

    # Values from either source are coerced alike.
    assert config.get("VBOT_TEST_FROM_OVERRIDE") == 20
    assert config.get("VBOT_TEST_OVERRIDDEN") == "environment"
    assert config.get("VBOT_TEST_FROM_FILE") is None


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


@pytest.mark.parametrize(
    ("settings", "environment", "explicit_port", "port", "source"),
    [
        (_PORT_SETTINGS, {"VBOT_SERVER_PORT": "8600"}, 8700, 8700, "cli"),
        (_PORT_SETTINGS, {"VBOT_SERVER_PORT": "8600"}, None, 8600, "VBOT_SERVER_PORT"),
        (_PORT_SETTINGS, {}, None, 8500, "settings.server_port"),
        (
            {"format_version": 1, "SERVER_PORT": 8700},
            {"PORT": "8600", "SERVER_PORT": "8800"},
            None,
            8700,
            "settings.SERVER_PORT",
        ),
        (
            {**_PORT_SETTINGS, "debug": {"enabled": "yes"}},
            {},
            None,
            8500,
            "settings.server_port",
        ),
        (None, {"PORT": "8600", "SERVER_PORT": "8700"}, None, DEFAULT_PORT, "default"),
        ("{", {}, None, None, None),
        ({"server_port": 8421}, {}, None, None, None),
        ({"format_version": 1, "server_port": "invalid"}, {}, None, None, None),
        ({**_PORT_SETTINGS, "PORT": "invalid"}, {}, None, 8500, "settings.server_port"),
        ({"format_version": 1, "server_port": "invalid", "PORT": 8500}, {}, None, None, None),
        ("{", {}, 8421, 8421, "cli"),
        ("{", {"VBOT_SERVER_PORT": "8421"}, None, 8421, "VBOT_SERVER_PORT"),
    ],
    ids=[
        "cli-first",
        "environment-before-settings",
        "settings",
        "settings-uppercase-key-ignores-ambient-environment",
        "valid-port-next-to-invalid-setting",
        "default-ignores-ambient-environment",
        "malformed-settings-refused",
        "unversioned-settings-refused",
        "invalid-port-refused",
        "higher-priority-port-kept",
        "invalid-higher-priority-port-refused",
        "explicit-port-bypasses-malformed-settings",
        "environment-port-bypasses-malformed-settings",
    ],
)
def test_server_bind_resolves_cli_then_environment_then_settings_then_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    settings: dict[str, Any] | str | None,
    environment: dict[str, str],
    explicit_port: int | None,
    port: int | None,
    source: str | None,
) -> None:
    monkeypatch.delenv("VBOT_SERVER_PORT", raising=False)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    if settings is not None:
        text = settings if isinstance(settings, str) else json.dumps(settings)
        (tmp_path / "settings.json").write_text(text, encoding="utf-8")
    config = Config(data_dir=tmp_path)

    if port is None:
        with pytest.raises(ConfigError):
            resolve_server_bind(config, host="0.0.0.0", explicit_port=explicit_port)
        with pytest.raises(ConfigError):
            resolve_port(config, explicit_port)
        return

    assert resolve_server_bind(config, host="0.0.0.0", explicit_port=explicit_port) == {
        "listen_host": "0.0.0.0",
        "listen_port": port,
        "port_source": source,
    }
    assert resolve_port(config, explicit_port) == port
