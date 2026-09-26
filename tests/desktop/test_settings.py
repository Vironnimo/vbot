"""Tests for the per-user Desktop settings store."""

from __future__ import annotations

import json
import logging
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


def test_read_settings_returns_defaults_for_invalid_utf8(tmp_path: Path) -> None:
    settings_file = tmp_path / "settings.json"
    settings_file.write_bytes(b'{"wakeword": "\xff"}')

    assert desktop_settings.read_settings(settings_file) == {}
    assert desktop_settings.read_section(desktop_settings.WAKEWORD_KEY, settings_file) == {}


@pytest.mark.parametrize("settings_text", ["[]", '"not an object"', "42"])
def test_read_settings_returns_empty_for_non_object_json(
    tmp_path: Path,
    settings_text: str,
) -> None:
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(settings_text, encoding="utf-8")

    assert desktop_settings.read_settings(settings_file) == {}


def test_write_servers_creates_config_dir_and_round_trips(tmp_path: Path) -> None:
    settings_file = tmp_path / "missing-dir" / "settings.json"

    desktop_settings.write_servers([], settings_file)

    assert settings_file.exists()
    assert json.loads(settings_file.read_text(encoding="utf-8")) == {"servers": []}


def test_write_servers_retries_transient_replace_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings_file = tmp_path / "settings.json"
    original_replace = Path.replace
    replace_attempts = 0
    retry_delays: list[float] = []

    def flaky_replace(path: Path, target: Path) -> Path:
        nonlocal replace_attempts
        replace_attempts += 1
        if replace_attempts < desktop_settings._IO_RETRY_ATTEMPTS:
            raise PermissionError("settings file is temporarily locked")
        return original_replace(path, target)

    monkeypatch.setattr(Path, "replace", flaky_replace)
    monkeypatch.setattr(desktop_settings.time, "sleep", retry_delays.append)

    desktop_settings.write_servers([], settings_file)

    assert replace_attempts == desktop_settings._IO_RETRY_ATTEMPTS
    assert retry_delays == pytest.approx([0.05, 0.1])
    assert json.loads(settings_file.read_text(encoding="utf-8")) == {"servers": []}
    assert list(tmp_path.glob(".settings.json.*.tmp")) == []


@pytest.mark.parametrize("error_type", [PermissionError, OSError])
def test_write_servers_raises_and_logs_persistent_write_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    error_type: type[OSError],
) -> None:
    settings_file = tmp_path / "settings.json"
    replace_attempts = 0

    def failing_replace(path: Path, target: Path) -> Path:
        del path, target
        nonlocal replace_attempts
        replace_attempts += 1
        raise error_type("settings file remains locked")

    monkeypatch.setattr(Path, "replace", failing_replace)
    monkeypatch.setattr(desktop_settings.time, "sleep", lambda _delay: None)

    with (
        caplog.at_level(logging.ERROR, logger="vbot.desktop.settings"),
        pytest.raises(error_type, match="settings file remains locked"),
    ):
        desktop_settings.write_servers([], settings_file)

    assert replace_attempts == desktop_settings._IO_RETRY_ATTEMPTS
    error_records = [
        record
        for record in caplog.records
        if record.name == "vbot.desktop.settings" and record.levelno == logging.ERROR
    ]
    assert len(error_records) == 1
    assert not settings_file.exists()
    assert list(tmp_path.glob(".settings.json.*.tmp")) == []


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


@pytest.mark.parametrize("error_type", [PermissionError, OSError])
def test_section_write_preserves_unreadable_existing_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error_type: type[OSError]
) -> None:
    settings_file = tmp_path / "settings.json"
    original = b'{"servers": [{"host": "pi.lan", "port": 9000}]}'
    settings_file.write_bytes(original)

    def fail_read(_path: Path, **_kwargs: object) -> str:
        raise error_type("test-owned read failure")

    monkeypatch.setattr(Path, "read_text", fail_read)
    monkeypatch.setattr(desktop_settings.time, "sleep", lambda _delay: None)

    # Startup can still fall back, but a later resize/settings save must not
    # mistake an unreadable document for a new one and discard other sections.
    assert desktop_settings.read_settings(settings_file) == {}
    with pytest.raises(error_type):
        desktop_settings.write_window_size(1280, 800, settings_file)

    assert settings_file.read_bytes() == original


@pytest.mark.parametrize("original", [b"not json", b'{"label": "\xff"}', b"[]", b"null"])
def test_section_write_preserves_malformed_existing_settings(
    tmp_path: Path, original: bytes
) -> None:
    settings_file = tmp_path / "settings.json"
    settings_file.write_bytes(original)

    assert desktop_settings.read_settings(settings_file) == {}
    with pytest.raises(ValueError):
        desktop_settings.write_window_size(1280, 800, settings_file)

    assert settings_file.read_bytes() == original


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
    with pytest.raises(ValueError):
        desktop_settings.write_window_size(width, height, tmp_path / "settings.json")  # type: ignore[arg-type]


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
        desktop_settings.update_section(
            desktop_settings.WAKEWORD_KEY,
            lambda section: {**section, "enabled": True},
            settings_file,
        )
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


# -- Live voice hotkey ---------------------------------------------------------


def test_read_live_hotkey_settings_defaults_to_disabled_ctrl_alt_space(tmp_path: Path) -> None:
    settings = desktop_settings.read_live_hotkey_settings(tmp_path / "missing.json")

    assert settings == {
        "enabled": False,
        "ctrl": True,
        "alt": True,
        "shift": False,
        "win": False,
        "key": "Space",
    }


def test_read_live_hotkey_settings_falls_back_per_malformed_field(tmp_path: Path) -> None:
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(
        json.dumps(
            {
                "live_voice": {
                    "hotkey": {
                        "enabled": True,
                        "ctrl": "yes",
                        "alt": False,
                        "shift": True,
                        "key": " KeyL ",
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    settings = desktop_settings.read_live_hotkey_settings(settings_file)

    assert settings == {
        "enabled": True,
        "ctrl": True,
        "alt": False,
        "shift": True,
        "win": False,
        "key": "KeyL",
    }


@pytest.mark.parametrize("live_voice", [None, [], {"hotkey": "Ctrl+Alt+Space"}])
def test_read_live_hotkey_settings_defaults_malformed_sections(
    tmp_path: Path, live_voice: object
) -> None:
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(json.dumps({"live_voice": live_voice}), encoding="utf-8")

    settings = desktop_settings.read_live_hotkey_settings(settings_file)

    assert settings == desktop_settings.DEFAULT_LIVE_HOTKEY_SETTINGS


def test_write_live_hotkey_settings_preserves_other_sections(tmp_path: Path) -> None:
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(
        json.dumps(
            {
                "servers": [{"host": "a.lan", "port": 8420}],
                "live_voice": {"future": {"kept": True}},
            }
        ),
        encoding="utf-8",
    )
    hotkey = {**desktop_settings.DEFAULT_LIVE_HOTKEY_SETTINGS, "enabled": True, "key": "F13"}

    desktop_settings.write_live_hotkey_settings(hotkey, settings_file)

    stored = json.loads(settings_file.read_text(encoding="utf-8"))
    assert stored["servers"] == [{"host": "a.lan", "port": 8420}]
    assert stored["live_voice"] == {"future": {"kept": True}, "hotkey": hotkey}


# -- Generic section API -------------------------------------------------------


@pytest.mark.parametrize("stored", [None, [], "text", 3])
def test_read_section_returns_empty_for_missing_or_non_object_sections(
    tmp_path: Path, stored: object
) -> None:
    settings_file = tmp_path / "settings.json"
    content: dict[str, object] = {"servers": []}
    if stored is not None:
        content["wakeword"] = stored
    settings_file.write_text(json.dumps(content), encoding="utf-8")

    assert desktop_settings.read_section("wakeword", settings_file) == {}


def test_read_section_returns_an_isolated_copy(tmp_path: Path) -> None:
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(
        json.dumps({"wakeword": {"model_sensitivities": {"builtin/hey_nabu": 0.4}}}),
        encoding="utf-8",
    )

    section = desktop_settings.read_section("wakeword", settings_file)
    section["model_sensitivities"]["builtin/hey_nabu"] = 0.9

    assert desktop_settings.read_section("wakeword", settings_file) == {
        "model_sensitivities": {"builtin/hey_nabu": 0.4}
    }


def test_read_section_returns_empty_for_an_unreadable_file(tmp_path: Path) -> None:
    settings_file = tmp_path / "settings.json"
    settings_file.write_bytes(b"not json")

    assert desktop_settings.read_section("wakeword", settings_file) == {}


def test_update_section_mutates_one_section_and_preserves_everything_else(
    tmp_path: Path,
) -> None:
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(
        json.dumps(
            {
                "servers": [{"host": "a.lan", "port": 8420}],
                "future": {"kept": True},
                "wakeword": {"enabled": False, "unknown": [1, 2]},
            }
        ),
        encoding="utf-8",
    )
    seen: list[dict[str, object]] = []

    def enable(section: dict[str, object]) -> dict[str, object]:
        seen.append(dict(section))
        return {**section, "enabled": True}

    result = desktop_settings.update_section("wakeword", enable, settings_file)

    assert seen == [{"enabled": False, "unknown": [1, 2]}]
    assert result == {"enabled": True, "unknown": [1, 2]}
    stored = json.loads(settings_file.read_text(encoding="utf-8"))
    assert stored == {
        "servers": [{"host": "a.lan", "port": 8420}],
        "future": {"kept": True},
        "wakeword": {"enabled": True, "unknown": [1, 2]},
    }


@pytest.mark.parametrize("stored", [None, ["not", "an", "object"]])
def test_update_section_starts_from_empty_for_missing_or_non_object_sections(
    tmp_path: Path, stored: object
) -> None:
    settings_file = tmp_path / "settings.json"
    content: dict[str, object] = {"servers": []}
    if stored is not None:
        content["wakeword"] = stored
    settings_file.write_text(json.dumps(content), encoding="utf-8")
    seen: list[dict[str, object]] = []

    def enable(section: dict[str, object]) -> dict[str, object]:
        seen.append(dict(section))
        return {"enabled": True}

    desktop_settings.update_section("wakeword", enable, settings_file)

    assert seen == [{}]
    stored_settings = json.loads(settings_file.read_text(encoding="utf-8"))
    assert stored_settings == {"servers": [], "wakeword": {"enabled": True}}


def test_update_section_creates_a_missing_settings_file(tmp_path: Path) -> None:
    settings_file = tmp_path / "config" / "settings.json"

    desktop_settings.update_section("wakeword", lambda _section: {"enabled": True}, settings_file)

    assert json.loads(settings_file.read_text(encoding="utf-8")) == {"wakeword": {"enabled": True}}


def test_update_section_does_not_rewrite_an_unchanged_section(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(json.dumps({"wakeword": {"enabled": True}}), encoding="utf-8")
    writes: list[object] = []
    monkeypatch.setattr(
        desktop_settings,
        "_write_settings_unlocked",
        lambda settings, _path: writes.append(settings),
    )

    result = desktop_settings.update_section("wakeword", lambda section: section, settings_file)

    assert result == {"enabled": True}
    assert writes == []


def test_update_section_writes_nothing_when_the_mutation_raises(tmp_path: Path) -> None:
    settings_file = tmp_path / "settings.json"
    original = b'{"wakeword": {"enabled": false}}'
    settings_file.write_bytes(original)

    def reject(section: dict[str, object]) -> dict[str, object]:
        section["enabled"] = True  # the stored document must not see this edit
        raise ValueError("invalid change")

    with pytest.raises(ValueError, match="invalid change"):
        desktop_settings.update_section("wakeword", reject, settings_file)

    assert settings_file.read_bytes() == original


def test_update_section_rejects_a_non_object_result(tmp_path: Path) -> None:
    settings_file = tmp_path / "settings.json"
    original = b'{"wakeword": {"enabled": false}}'
    settings_file.write_bytes(original)

    with pytest.raises(TypeError):
        desktop_settings.update_section(
            "wakeword",
            lambda _section: ["enabled"],  # type: ignore[arg-type,return-value]
            settings_file,
        )

    assert settings_file.read_bytes() == original


@pytest.mark.parametrize("original", [b"not json", b"[]"])
def test_update_section_refuses_to_replace_an_unreadable_document(
    tmp_path: Path, original: bytes
) -> None:
    settings_file = tmp_path / "settings.json"
    settings_file.write_bytes(original)
    calls: list[dict[str, object]] = []

    def enable(section: dict[str, object]) -> dict[str, object]:
        calls.append(section)
        return {"enabled": True}

    with pytest.raises(ValueError):
        desktop_settings.update_section("wakeword", enable, settings_file)

    assert calls == []
    assert settings_file.read_bytes() == original


def test_update_section_serializes_with_other_section_writers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(json.dumps({"wakeword": {"enabled": False}}), encoding="utf-8")
    mutation_started = threading.Event()
    release_mutation = threading.Event()
    server_write_finished = threading.Event()

    def slow_enable(section: dict[str, object]) -> dict[str, object]:
        mutation_started.set()
        assert release_mutation.wait(timeout=2)
        return {**section, "enabled": True}

    voice_thread = threading.Thread(
        target=desktop_settings.update_section,
        args=("wakeword", slow_enable, settings_file),
    )

    def write_servers() -> None:
        desktop_settings.write_servers([{"host": "new.lan", "port": 9000}], settings_file)
        server_write_finished.set()

    server_thread = threading.Thread(target=write_servers)
    voice_thread.start()
    assert mutation_started.wait(timeout=2)
    server_thread.start()

    try:
        assert not server_write_finished.wait(timeout=0.1)
    finally:
        release_mutation.set()
        voice_thread.join(timeout=2)
        server_thread.join(timeout=2)

    assert not voice_thread.is_alive()
    assert not server_thread.is_alive()
    stored = json.loads(settings_file.read_text(encoding="utf-8"))
    assert stored == {
        "servers": [{"host": "new.lan", "port": 9000}],
        "wakeword": {"enabled": True},
    }
