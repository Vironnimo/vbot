"""Tests for the per-user Desktop settings store."""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath, PureWindowsPath

import pytest

from desktop import settings as desktop_settings

# -- Config-dir resolution ---------------------------------------------------
#
# resolve_config_dir takes explicit platform inputs so both the Windows and the
# POSIX branch are testable on any host. Mutating the global os.name instead
# would break pathlib's PosixPath/WindowsPath flavor selection on Windows.


def test_resolve_config_dir_uses_appdata_on_windows() -> None:
    config_dir = desktop_settings.resolve_config_dir(
        "nt",
        {"APPDATA": r"C:\Users\tester\AppData\Roaming"},
        PureWindowsPath(r"C:\Users\tester"),
    )

    assert config_dir == PureWindowsPath(r"C:\Users\tester\AppData\Roaming\vbot")


def test_resolve_config_dir_falls_back_to_home_appdata_on_windows_without_env() -> None:
    config_dir = desktop_settings.resolve_config_dir(
        "nt",
        {},
        PureWindowsPath(r"C:\Users\tester"),
    )

    assert config_dir == PureWindowsPath(r"C:\Users\tester\AppData\Roaming\vbot")


def test_resolve_config_dir_uses_xdg_config_home_on_posix() -> None:
    config_dir = desktop_settings.resolve_config_dir(
        "posix",
        {"XDG_CONFIG_HOME": "/custom/xdg"},
        PurePosixPath("/home/user"),
    )

    assert config_dir == PurePosixPath("/custom/xdg/vbot")


def test_resolve_config_dir_falls_back_to_dot_config_on_posix_without_env() -> None:
    config_dir = desktop_settings.resolve_config_dir(
        "posix",
        {},
        PurePosixPath("/home/user"),
    )

    assert config_dir == PurePosixPath("/home/user/.config/vbot")


def test_config_dir_binds_resolver_to_current_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        desktop_settings,
        "resolve_config_dir",
        lambda os_name, environ, home: PureWindowsPath(r"X:\resolved\vbot"),
    )

    assert desktop_settings.config_dir() == Path(PureWindowsPath(r"X:\resolved\vbot"))


def test_settings_path_lives_in_config_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(desktop_settings, "config_dir", lambda: Path("/cfg/vbot"))

    assert desktop_settings.settings_path() == Path("/cfg/vbot") / "settings.json"


def test_settings_path_accepts_explicit_base_dir(tmp_path: Path) -> None:
    assert desktop_settings.settings_path(tmp_path) == tmp_path / "settings.json"


# -- read/write round-trip ---------------------------------------------------


def test_read_settings_returns_empty_when_file_missing(tmp_path: Path) -> None:
    assert desktop_settings.read_settings(tmp_path / "settings.json") == {}


def test_read_settings_returns_empty_for_corrupt_json(tmp_path: Path) -> None:
    settings_file = tmp_path / "settings.json"
    settings_file.write_text("not valid json", encoding="utf-8")

    assert desktop_settings.read_settings(settings_file) == {}


@pytest.mark.parametrize("settings_text", ["[]", '"not an object"', "42"])
def test_read_settings_returns_empty_for_non_object_json(
    tmp_path: Path,
    settings_text: str,
) -> None:
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(settings_text, encoding="utf-8")

    assert desktop_settings.read_settings(settings_file) == {}


def test_write_settings_creates_config_dir_and_round_trips(tmp_path: Path) -> None:
    settings_file = tmp_path / "missing-dir" / "settings.json"

    desktop_settings.write_settings({"servers": [], "last_used": None}, settings_file)

    assert settings_file.exists()
    assert json.loads(settings_file.read_text(encoding="utf-8")) == {
        "servers": [],
        "last_used": None,
    }


# -- Remembered servers ------------------------------------------------------


def test_read_servers_returns_empty_when_unset(tmp_path: Path) -> None:
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(json.dumps({"last_used": None}), encoding="utf-8")

    assert desktop_settings.read_servers(settings_file) == []


def test_read_servers_returns_valid_entries(tmp_path: Path) -> None:
    settings_file = tmp_path / "settings.json"
    servers = [
        {"host": "127.0.0.1", "port": 8420},
        {"host": "pi.lan", "port": 9000, "label": "Living room Pi"},
    ]
    settings_file.write_text(json.dumps({"servers": servers}), encoding="utf-8")

    assert desktop_settings.read_servers(settings_file) == servers


def test_read_servers_drops_malformed_entries(tmp_path: Path) -> None:
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(
        json.dumps(
            {
                "servers": [
                    {"host": "good.lan", "port": 8420},
                    {"host": "", "port": 8420},
                    {"host": "no-port.lan"},
                    {"port": 8420},
                    {"host": "bool-port.lan", "port": True},
                    "not-a-dict",
                    {"host": "string-port.lan", "port": "8420"},
                ]
            }
        ),
        encoding="utf-8",
    )

    assert desktop_settings.read_servers(settings_file) == [{"host": "good.lan", "port": 8420}]


def test_read_servers_drops_non_string_label(tmp_path: Path) -> None:
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(
        json.dumps({"servers": [{"host": "pi.lan", "port": 9000, "label": 7}]}),
        encoding="utf-8",
    )

    assert desktop_settings.read_servers(settings_file) == [{"host": "pi.lan", "port": 9000}]


def test_read_servers_returns_empty_for_non_list(tmp_path: Path) -> None:
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(json.dumps({"servers": {"host": "x", "port": 1}}), encoding="utf-8")

    assert desktop_settings.read_servers(settings_file) == []


def test_write_servers_preserves_other_keys(tmp_path: Path) -> None:
    settings_file = tmp_path / "settings.json"
    wakeword = {"enabled": True, "sensitivity": 0.7}
    settings_file.write_text(
        json.dumps({"last_used": {"host": "pi.lan", "port": 9000}, "wakeword": wakeword}),
        encoding="utf-8",
    )

    servers = [{"host": "pi.lan", "port": 9000, "label": "Pi"}]
    desktop_settings.write_servers(servers, settings_file)

    stored = json.loads(settings_file.read_text(encoding="utf-8"))
    assert stored["servers"] == servers
    assert stored["last_used"] == {"host": "pi.lan", "port": 9000}
    assert stored["wakeword"] == wakeword


# -- Last-used target --------------------------------------------------------


def test_read_last_used_returns_none_when_unset(tmp_path: Path) -> None:
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(json.dumps({"servers": []}), encoding="utf-8")

    assert desktop_settings.read_last_used(settings_file) is None


def test_read_last_used_returns_reference(tmp_path: Path) -> None:
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(
        json.dumps({"last_used": {"host": "pi.lan", "port": 9000, "label": "ignored"}}),
        encoding="utf-8",
    )

    assert desktop_settings.read_last_used(settings_file) == {"host": "pi.lan", "port": 9000}


@pytest.mark.parametrize(
    "last_used",
    [
        {"host": "", "port": 9000},
        {"host": "pi.lan"},
        {"host": "pi.lan", "port": "9000"},
        "pi.lan:9000",
        None,
    ],
)
def test_read_last_used_returns_none_for_malformed_reference(
    tmp_path: Path,
    last_used: object,
) -> None:
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(json.dumps({"last_used": last_used}), encoding="utf-8")

    assert desktop_settings.read_last_used(settings_file) is None


def test_write_last_used_preserves_other_keys(tmp_path: Path) -> None:
    settings_file = tmp_path / "settings.json"
    servers = [{"host": "pi.lan", "port": 9000}]
    wakeword = {"enabled": True}
    settings_file.write_text(
        json.dumps({"servers": servers, "wakeword": wakeword}), encoding="utf-8"
    )

    desktop_settings.write_last_used("pi.lan", 9000, settings_file)

    stored = json.loads(settings_file.read_text(encoding="utf-8"))
    assert stored["last_used"] == {"host": "pi.lan", "port": 9000}
    assert stored["servers"] == servers
    assert stored["wakeword"] == wakeword


# -- Wakeword block ----------------------------------------------------------


def test_read_wakeword_settings_returns_defaults_when_file_missing(tmp_path: Path) -> None:
    config = desktop_settings.read_wakeword_settings(tmp_path / "settings.json")

    assert config == desktop_settings.DEFAULT_WAKEWORD_SETTINGS


def test_read_wakeword_settings_merges_with_defaults(tmp_path: Path) -> None:
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(
        json.dumps(
            {
                "servers": [{"host": "127.0.0.1", "port": 8420}],
                "wakeword": {"enabled": True},
            }
        ),
        encoding="utf-8",
    )

    config = desktop_settings.read_wakeword_settings(settings_file)

    assert config["enabled"] is True
    assert config["engine"] == desktop_settings.DEFAULT_WAKEWORD_SETTINGS["engine"]
    assert config["wake_phrase"] == desktop_settings.DEFAULT_WAKEWORD_SETTINGS["wake_phrase"]


def test_read_wakeword_settings_falls_back_for_missing_wakeword_key(tmp_path: Path) -> None:
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(
        json.dumps({"servers": [{"host": "127.0.0.1", "port": 8420}]}),
        encoding="utf-8",
    )

    config = desktop_settings.read_wakeword_settings(settings_file)

    assert config == desktop_settings.DEFAULT_WAKEWORD_SETTINGS


def test_read_wakeword_settings_falls_back_for_non_dict_wakeword(tmp_path: Path) -> None:
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(
        json.dumps({"servers": [], "wakeword": "invalid"}),
        encoding="utf-8",
    )

    config = desktop_settings.read_wakeword_settings(settings_file)

    assert config == desktop_settings.DEFAULT_WAKEWORD_SETTINGS


def test_read_wakeword_settings_handles_corrupt_file(tmp_path: Path) -> None:
    settings_file = tmp_path / "settings.json"
    settings_file.write_text("not valid json", encoding="utf-8")

    config = desktop_settings.read_wakeword_settings(settings_file)

    assert config == desktop_settings.DEFAULT_WAKEWORD_SETTINGS


def test_write_wakeword_settings_preserves_servers_and_last_used(tmp_path: Path) -> None:
    settings_file = tmp_path / "settings.json"
    servers = [{"host": "10.0.0.1", "port": 9000}]
    last_used = {"host": "10.0.0.1", "port": 9000}
    settings_file.write_text(
        json.dumps({"servers": servers, "last_used": last_used}),
        encoding="utf-8",
    )

    wakeword_config = {
        "enabled": True,
        "engine": "openwakeword",
        "microphone": None,
        "sensitivity": 0.8,
        "target_agent_id": "test-agent",
        "session_behavior": "new",
        "wake_phrase": "hey_jarvis",
    }
    desktop_settings.write_wakeword_settings(wakeword_config, settings_file)

    stored = json.loads(settings_file.read_text(encoding="utf-8"))
    assert stored["servers"] == servers
    assert stored["last_used"] == last_used
    assert stored["wakeword"] == wakeword_config


def test_write_wakeword_settings_overwrites_existing_wakeword(tmp_path: Path) -> None:
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(
        json.dumps({"servers": [], "wakeword": {"enabled": True}}),
        encoding="utf-8",
    )

    desktop_settings.write_wakeword_settings({"enabled": False, "sensitivity": 0.3}, settings_file)

    stored = json.loads(settings_file.read_text(encoding="utf-8"))
    assert stored["wakeword"]["enabled"] is False
    assert stored["wakeword"]["sensitivity"] == 0.3
