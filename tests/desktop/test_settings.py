"""Tests for the per-user Desktop settings store."""

from __future__ import annotations

import json
import threading
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
    wakeword = {
        "enabled": True,
        "model_sensitivities": {"builtin/okay_nabu": 0.7},
    }
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


# -- Window size -------------------------------------------------------------


def test_read_window_size_returns_none_when_unset(tmp_path: Path) -> None:
    assert desktop_settings.read_window_size(tmp_path / "settings.json") is None


def test_read_window_size_returns_persisted_dimensions(tmp_path: Path) -> None:
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(
        json.dumps({"window": {"width": 1420, "height": 910}}),
        encoding="utf-8",
    )

    assert desktop_settings.read_window_size(settings_file) == (1420, 910)


@pytest.mark.parametrize(
    "window",
    [
        None,
        [],
        {"width": 0, "height": 800},
        {"width": True, "height": 800},
        {"width": "1280", "height": 800},
        {"width": 1280},
    ],
)
def test_read_window_size_returns_none_for_malformed_dimensions(
    tmp_path: Path,
    window: object,
) -> None:
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(json.dumps({"window": window}), encoding="utf-8")

    assert desktop_settings.read_window_size(settings_file) is None


def test_write_window_size_preserves_other_keys(tmp_path: Path) -> None:
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(
        json.dumps(
            {
                "servers": [{"host": "pi.lan", "port": 9000}],
                "wakeword": {"enabled": True},
            }
        ),
        encoding="utf-8",
    )

    desktop_settings.write_window_size(1360, 880, settings_file)

    stored = json.loads(settings_file.read_text(encoding="utf-8"))
    assert stored["window"] == {"width": 1360, "height": 880}
    assert stored["servers"] == [{"host": "pi.lan", "port": 9000}]
    assert stored["wakeword"] == {"enabled": True}


@pytest.mark.parametrize(("width", "height"), [(0, 800), (1280, 0), (True, 800)])
def test_write_window_size_rejects_invalid_dimensions(
    tmp_path: Path,
    width: object,
    height: object,
) -> None:
    with pytest.raises(ValueError, match="positive integers"):
        desktop_settings.write_window_size(width, height, tmp_path / "settings.json")  # type: ignore[arg-type]


# -- Wakeword block ----------------------------------------------------------


def test_read_wakeword_settings_returns_defaults_when_file_missing(tmp_path: Path) -> None:
    config = desktop_settings.read_wakeword_settings(tmp_path / "settings.json")

    assert config == desktop_settings.DEFAULT_WAKEWORD_SETTINGS


def test_wakeword_defaults_return_independent_server_profile_maps(tmp_path: Path) -> None:
    first = desktop_settings.read_wakeword_settings(tmp_path / "missing.json")
    second = desktop_settings.read_wakeword_settings(tmp_path / "missing.json")

    first["server_profiles"]["http://a.lan:8420"] = {"target_agent_id": "main"}

    assert second["server_profiles"] == {}


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
    assert config["active_model_ids"] == list(desktop_settings.DEFAULT_WAKEWORD_MODEL_IDS)
    assert config["model_sensitivities"] == {}


@pytest.mark.parametrize(
    "active_model_ids",
    [None, [], ["one", "one"], ["one", "two", "three"], [1]],
)
def test_read_wakeword_settings_normalizes_invalid_active_models(
    tmp_path: Path, active_model_ids: object
) -> None:
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(
        json.dumps(
            {
                "wakeword": {
                    "model_id": "builtin/legacy",
                    "active_model_ids": active_model_ids,
                    "model_sensitivities": [],
                }
            }
        ),
        encoding="utf-8",
    )

    config = desktop_settings.read_wakeword_settings(settings_file)

    assert config["active_model_ids"] == list(desktop_settings.DEFAULT_WAKEWORD_MODEL_IDS)
    assert config["model_sensitivities"] == {}
    assert "model_id" not in config


def test_read_wakeword_settings_trims_active_model_ids(tmp_path: Path) -> None:
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(
        json.dumps({"wakeword": {"active_model_ids": [" builtin/okay_nabu "]}}),
        encoding="utf-8",
    )

    config = desktop_settings.read_wakeword_settings(settings_file)

    assert config["active_model_ids"] == ["builtin/okay_nabu"]


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
        "microphone": None,
        "active_model_ids": ["builtin/okay_nabu", "builtin/hey_nabu"],
        "model_sensitivities": {"builtin/hey_nabu": 0.8},
        "server_profiles": {
            "http://127.0.0.1:8420": {
                "target_agent_id": "test-agent",
                "session_behavior": "new",
            }
        },
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

    desktop_settings.write_wakeword_settings(
        {
            "enabled": False,
            "model_sensitivities": {"builtin/okay_nabu": 0.3},
        },
        settings_file,
    )

    stored = json.loads(settings_file.read_text(encoding="utf-8"))
    assert stored["wakeword"]["enabled"] is False
    assert stored["wakeword"]["model_sensitivities"] == {"builtin/okay_nabu": 0.3}


def test_parallel_section_writes_share_one_transaction_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(
        json.dumps(
            {
                "servers": [{"host": "old.lan", "port": 8420}],
                "wakeword": {"enabled": False},
            }
        ),
        encoding="utf-8",
    )
    original_read = desktop_settings._read_settings_unlocked
    server_read_started = threading.Event()
    release_server_write = threading.Event()
    wakeword_write_finished = threading.Event()

    def controlled_read(path: Path) -> dict[str, object]:
        settings = original_read(path)
        if threading.current_thread().name == "server-settings-writer":
            server_read_started.set()
            assert release_server_write.wait(timeout=2)
        return settings

    monkeypatch.setattr(desktop_settings, "_read_settings_unlocked", controlled_read)

    server_thread = threading.Thread(
        name="server-settings-writer",
        target=desktop_settings.write_servers,
        args=([{"host": "new.lan", "port": 9000}], settings_file),
    )

    def write_wakeword() -> None:
        desktop_settings.write_wakeword_settings({"enabled": True}, settings_file)
        wakeword_write_finished.set()

    wakeword_thread = threading.Thread(target=write_wakeword)
    server_thread.start()
    assert server_read_started.wait(timeout=2)
    wakeword_thread.start()

    try:
        assert not wakeword_write_finished.wait(timeout=0.1)
    finally:
        release_server_write.set()
        server_thread.join(timeout=2)
        wakeword_thread.join(timeout=2)

    assert not server_thread.is_alive()
    assert not wakeword_thread.is_alive()
    stored = json.loads(settings_file.read_text(encoding="utf-8"))
    assert stored["servers"] == [{"host": "new.lan", "port": 9000}]
    assert stored["wakeword"] == {"enabled": True}
