"""Tests for the per-user Desktop settings store."""

from __future__ import annotations

import json
import logging
import os
import threading
from collections.abc import Callable
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath
from typing import Any

import pytest

from desktop import settings as desktop_settings


def _settings_file(tmp_path: Path, content: Any = None) -> Path:
    settings_file = tmp_path / "settings.json"
    if content is not None:
        settings_file.write_text(json.dumps(content), encoding="utf-8")
    return settings_file


def _stored(settings_file: Path) -> Any:
    return json.loads(settings_file.read_text(encoding="utf-8"))


# -- Location ----------------------------------------------------------------------
#
# resolve_config_dir takes explicit platform inputs so both the Windows and the
# POSIX branch are testable on any host. Mutating the global os.name instead
# would break pathlib's PosixPath/WindowsPath flavor selection on Windows.


@pytest.mark.parametrize(
    ("os_name", "environ", "home", "expected"),
    [
        (
            "nt",
            {"APPDATA": r"D:\Roaming"},
            PureWindowsPath(r"C:\Users\tester"),
            PureWindowsPath(r"D:\Roaming\vbot"),
        ),
        (
            "nt",
            {},
            PureWindowsPath(r"C:\Users\tester"),
            PureWindowsPath(r"C:\Users\tester\AppData\Roaming\vbot"),
        ),
        (
            "posix",
            {"XDG_CONFIG_HOME": "/custom/xdg"},
            PurePosixPath("/home/user"),
            PurePosixPath("/custom/xdg/vbot"),
        ),
        ("posix", {}, PurePosixPath("/home/user"), PurePosixPath("/home/user/.config/vbot")),
    ],
    ids=["appdata", "windows-home", "xdg-config-home", "posix-home"],
)
def test_resolve_config_dir_follows_the_platform_convention(
    os_name: str, environ: dict[str, str], home: PurePath, expected: PurePath
) -> None:
    assert desktop_settings.resolve_config_dir(os_name, environ, home) == expected


def test_settings_path_lives_in_the_resolved_config_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        desktop_settings,
        "resolve_config_dir",
        lambda os_name, environ, home: PureWindowsPath(r"X:\resolved\vbot"),
    )

    expected = Path(PureWindowsPath(r"X:\resolved\vbot")) / "settings.json"
    assert desktop_settings.settings_path() == expected
    assert desktop_settings.settings_path(tmp_path) == tmp_path / "settings.json"


# -- Reading and writing the document ---------------------------------------------


@pytest.mark.parametrize(
    "content",
    [None, b"not valid json", b'{"wakeword": "\xff"}', b"[]", b'"not an object"', b"42"],
    ids=["missing", "corrupt-json", "invalid-utf8", "list", "string", "number"],
)
def test_an_unusable_document_reads_as_empty_settings(
    tmp_path: Path, content: bytes | None
) -> None:
    settings_file = tmp_path / "settings.json"
    if content is not None:
        settings_file.write_bytes(content)

    assert desktop_settings.read_settings(settings_file) == {}
    assert desktop_settings.read_section(desktop_settings.WAKEWORD_KEY, settings_file) == {}


def test_a_write_creates_the_config_dir(tmp_path: Path) -> None:
    settings_file = tmp_path / "missing-dir" / "settings.json"

    desktop_settings.write_servers([], settings_file)

    assert _stored(settings_file) == {"servers": []}


def test_writes_retry_transient_replace_errors(
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
    assert _stored(settings_file) == {"servers": []}
    assert list(tmp_path.glob(".settings.json.*.tmp")) == []


@pytest.mark.parametrize("error_type", [PermissionError, OSError])
def test_writes_raise_and_log_persistent_write_errors(
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


@pytest.mark.parametrize("error_type", [PermissionError, OSError])
def test_a_section_write_preserves_an_unreadable_document(
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
def test_a_section_write_preserves_a_malformed_document(tmp_path: Path, original: bytes) -> None:
    settings_file = tmp_path / "settings.json"
    settings_file.write_bytes(original)
    mutations: list[dict[str, Any]] = []

    def enable(section: dict[str, Any]) -> dict[str, Any]:
        mutations.append(section)
        return {"enabled": True}

    assert desktop_settings.read_settings(settings_file) == {}
    with pytest.raises(ValueError):
        desktop_settings.write_window_size(1280, 800, settings_file)
    with pytest.raises(ValueError):
        desktop_settings.update_section("wakeword", enable, settings_file)

    assert mutations == []
    assert settings_file.read_bytes() == original


@pytest.mark.parametrize(
    ("write", "key", "section"),
    [
        (
            lambda path: desktop_settings.write_servers([{"host": "b.lan", "port": 1}], path),
            "servers",
            [{"host": "b.lan", "port": 1}],
        ),
        (
            lambda path: desktop_settings.write_last_used("b.lan", 1, path),
            "last_used",
            {"host": "b.lan", "port": 1},
        ),
        (
            lambda path: desktop_settings.write_window_size(1360, 880, path),
            "window",
            {"width": 1360, "height": 880},
        ),
        (
            lambda path: desktop_settings.update_section(
                "live_voice", lambda section: {**section, "hotkey": {"key": "F13"}}, path
            ),
            "live_voice",
            {"future": {"kept": True}, "hotkey": {"key": "F13"}},
        ),
    ],
    ids=["servers", "last-used", "window", "section"],
)
def test_each_section_writer_preserves_the_other_sections(
    tmp_path: Path, write: Callable[[Path], None], key: str, section: Any
) -> None:
    before = {
        "servers": [{"host": "a.lan", "port": 8420}],
        "last_used": {"host": "a.lan", "port": 8420},
        "window": {"width": 1000, "height": 700},
        "live_voice": {"future": {"kept": True}},
        "wakeword": {"enabled": True, "model_sensitivities": {"builtin/okay_nabu": 0.7}},
    }
    settings_file = _settings_file(tmp_path, before)

    write(settings_file)

    assert _stored(settings_file) == {**before, key: section}


# -- Typed sections ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("servers", "expected"),
    [
        (None, []),
        ({"host": "x", "port": 1}, []),
        (
            [
                {"host": "127.0.0.1", "port": 8420},
                {"host": "pi.lan", "port": 9000, "label": "Living room Pi"},
            ],
            [
                {"host": "127.0.0.1", "port": 8420},
                {"host": "pi.lan", "port": 9000, "label": "Living room Pi"},
            ],
        ),
        (
            [
                {"host": "good.lan", "port": 8420},
                {"host": "", "port": 8420},
                {"host": "no-port.lan"},
                {"port": 8420},
                {"host": "bool-port.lan", "port": True},
                "not-a-dict",
                {"host": "string-port.lan", "port": "8420"},
                {"host": "pi.lan", "port": 9000, "label": 7},
            ],
            [{"host": "good.lan", "port": 8420}, {"host": "pi.lan", "port": 9000}],
        ),
    ],
    ids=["unset", "not-a-list", "valid", "malformed-entries-and-labels"],
)
def test_read_servers_keeps_only_valid_entries(
    tmp_path: Path, servers: Any, expected: list[dict[str, Any]]
) -> None:
    settings_file = _settings_file(tmp_path, {"last_used": None, "servers": servers})

    assert desktop_settings.read_servers(settings_file) == expected


@pytest.mark.parametrize(
    ("last_used", "expected"),
    [
        ({"host": "pi.lan", "port": 9000, "label": "ignored"}, {"host": "pi.lan", "port": 9000}),
        ({"host": "", "port": 9000}, None),
        ({"host": "pi.lan"}, None),
        ({"host": "pi.lan", "port": "9000"}, None),
        ("pi.lan:9000", None),
        (None, None),
    ],
)
def test_read_last_used_returns_only_a_valid_reference(
    tmp_path: Path, last_used: object, expected: dict[str, Any] | None
) -> None:
    settings_file = _settings_file(tmp_path, {"servers": [], "last_used": last_used})

    assert desktop_settings.read_last_used(settings_file) == expected


@pytest.mark.parametrize(
    ("window", "expected"),
    [
        ({"width": 1420, "height": 910}, (1420, 910)),
        (None, None),
        ([], None),
        ({"width": 0, "height": 800}, None),
        ({"width": True, "height": 800}, None),
        ({"width": "1280", "height": 800}, None),
        ({"width": 1280}, None),
    ],
)
def test_read_window_size_returns_only_valid_dimensions(
    tmp_path: Path, window: object, expected: tuple[int, int] | None
) -> None:
    settings_file = _settings_file(tmp_path, {"window": window})

    assert desktop_settings.read_window_size(settings_file) == expected


@pytest.mark.parametrize(("width", "height"), [(0, 800), (1280, 0), (True, 800)])
def test_write_window_size_rejects_invalid_dimensions(
    tmp_path: Path,
    width: object,
    height: object,
) -> None:
    with pytest.raises(ValueError):
        desktop_settings.write_window_size(width, height, tmp_path / "settings.json")  # type: ignore[arg-type]


# -- Generic section API -------------------------------------------------------------


@pytest.mark.parametrize("stored", [None, [], "text", 3])
def test_read_section_returns_empty_for_missing_or_non_object_sections(
    tmp_path: Path, stored: object
) -> None:
    content: dict[str, object] = {"servers": []}
    if stored is not None:
        content["wakeword"] = stored

    assert desktop_settings.read_section("wakeword", _settings_file(tmp_path, content)) == {}


def test_read_section_returns_an_isolated_copy(tmp_path: Path) -> None:
    settings_file = _settings_file(
        tmp_path, {"wakeword": {"model_sensitivities": {"builtin/hey_nabu": 0.4}}}
    )

    section = desktop_settings.read_section("wakeword", settings_file)
    section["model_sensitivities"]["builtin/hey_nabu"] = 0.9

    assert desktop_settings.read_section("wakeword", settings_file) == {
        "model_sensitivities": {"builtin/hey_nabu": 0.4}
    }


def test_update_section_mutates_one_section_and_preserves_everything_else(
    tmp_path: Path,
) -> None:
    settings_file = _settings_file(
        tmp_path,
        {
            "servers": [{"host": "a.lan", "port": 8420}],
            "future": {"kept": True},
            "wakeword": {"enabled": False, "unknown": [1, 2]},
        },
    )
    seen: list[dict[str, object]] = []

    def enable(section: dict[str, object]) -> dict[str, object]:
        seen.append(dict(section))
        return {**section, "enabled": True}

    result = desktop_settings.update_section("wakeword", enable, settings_file)

    assert seen == [{"enabled": False, "unknown": [1, 2]}]
    assert result == {"enabled": True, "unknown": [1, 2]}
    assert _stored(settings_file) == {
        "servers": [{"host": "a.lan", "port": 8420}],
        "future": {"kept": True},
        "wakeword": {"enabled": True, "unknown": [1, 2]},
    }


@pytest.mark.parametrize("stored", [None, ["not", "an", "object"]])
def test_update_section_starts_from_empty_for_missing_or_non_object_sections(
    tmp_path: Path, stored: object
) -> None:
    content: dict[str, object] = {"servers": []}
    if stored is not None:
        content["wakeword"] = stored
    settings_file = _settings_file(tmp_path, content)
    seen: list[dict[str, object]] = []

    def enable(section: dict[str, object]) -> dict[str, object]:
        seen.append(dict(section))
        return {"enabled": True}

    desktop_settings.update_section("wakeword", enable, settings_file)

    assert seen == [{}]
    assert _stored(settings_file) == {"servers": [], "wakeword": {"enabled": True}}


def test_update_section_does_not_rewrite_an_unchanged_section(tmp_path: Path) -> None:
    settings_file = _settings_file(tmp_path, {"wakeword": {"enabled": True}})
    before = os.stat(settings_file)

    result = desktop_settings.update_section("wakeword", lambda section: section, settings_file)

    after = os.stat(settings_file)
    assert result == {"enabled": True}
    # A write replaces the file atomically, which gives it a new identity.
    assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)


def _edit_then_raise(section: dict[str, object]) -> dict[str, object]:
    section["enabled"] = True  # the stored document must not see this edit
    raise ValueError("invalid change")


@pytest.mark.parametrize(
    ("mutate", "error_type"),
    [(_edit_then_raise, ValueError), (lambda _section: ["enabled"], TypeError)],
    ids=["mutation-raises", "non-object-result"],
)
def test_update_section_writes_nothing_for_a_failed_mutation(
    tmp_path: Path, mutate: Callable[[dict[str, object]], Any], error_type: type[Exception]
) -> None:
    settings_file = tmp_path / "settings.json"
    original = b'{"wakeword": {"enabled": false}}'
    settings_file.write_bytes(original)

    with pytest.raises(error_type):
        desktop_settings.update_section("wakeword", mutate, settings_file)

    assert settings_file.read_bytes() == original


def test_section_writers_share_one_transaction_lock(tmp_path: Path) -> None:
    settings_file = _settings_file(tmp_path, {"wakeword": {"enabled": False}})
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
        # A writer that skipped the lock would finish now and be overwritten below.
        assert not server_write_finished.wait(timeout=0.1)
    finally:
        release_mutation.set()
        voice_thread.join(timeout=2)
        server_thread.join(timeout=2)

    assert not voice_thread.is_alive()
    assert not server_thread.is_alive()
    assert _stored(settings_file) == {
        "servers": [{"host": "new.lan", "port": 9000}],
        "wakeword": {"enabled": True},
    }
