"""The shared Desktop microphone: stored settings, migration, change listeners, devices."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import pytest

from desktop import settings as desktop_settings
from desktop.speech.capture import CAPTURE_CAPTURING
from desktop.speech.microphone import (
    MicrophoneConfigError,
    MicrophoneSelection,
    MicrophoneService,
    MicrophoneSettings,
    apply_microphone_changes,
    migrate_wakeword_microphone,
    parse_microphone_settings,
)
from tests.desktop.speech.speech_test_support import FakeSoundDevice, wait_until

USB = {"index": 2, "name": "USB", "host_api": "MME"}


def _write(path: Path, document: dict[str, Any]) -> None:
    path.write_text(json.dumps(document), encoding="utf-8")


def _read(path: Path) -> dict[str, Any]:
    document: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return document


# -- Stored settings -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, MicrophoneSettings()),
        (
            {"device": {"index": 4, "name": " Studio mic ", "host_api": "Windows WASAPI"}},
            MicrophoneSettings(device=MicrophoneSelection(4, "Studio mic", "Windows WASAPI")),
        ),
        (
            {"device": USB, "echo_cancellation": False},
            MicrophoneSettings(
                device=MicrophoneSelection(2, "USB", "MME"), echo_cancellation=False
            ),
        ),
        # Each malformed field falls back on its own.
        (
            {"device": {"index": -1, "name": "Mic", "host_api": "MME"}, "echo_cancellation": "off"},
            MicrophoneSettings(),
        ),
        ({"device": {"index": True, "name": "Mic", "host_api": "MME"}}, MicrophoneSettings()),
    ],
    ids=["missing", "device", "complete", "malformed", "bool-index"],
)
def test_parse_reads_the_section_tolerantly(raw: object, expected: MicrophoneSettings) -> None:
    assert parse_microphone_settings(raw) == expected


def test_apply_changes_writes_valid_fields_and_keeps_unknown_keys() -> None:
    raw = {"device": USB, "unknown": {"kept": True}}

    section = apply_microphone_changes(raw, {"device": None, "echo_cancellation": False})

    assert section == {"device": None, "echo_cancellation": False, "unknown": {"kept": True}}
    assert raw == {"device": USB, "unknown": {"kept": True}}


@pytest.mark.parametrize(
    ("changes", "field"),
    [
        ({"device": {"index": "1", "name": "Mic", "host_api": "MME"}}, "device"),
        ({"echo_cancellation": "false"}, "echo_cancellation"),
        ({"microphone": None}, "microphone"),
        ({"echo_cancellation": False, "device": "default"}, "device"),
        (["device"], None),
    ],
)
def test_apply_changes_rejects_invalid_input_with_the_offending_field(
    changes: object, field: str | None
) -> None:
    with pytest.raises(MicrophoneConfigError) as raised:
        apply_microphone_changes({}, changes)

    assert raised.value.field == field
    assert raised.value.error_code == "microphone_config_invalid"


# -- Migration from the wakeword section -----------------------------------------------


def test_migration_moves_the_choice_out_of_the_wakeword_section_once(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    _write(
        path,
        {
            "wakeword": {"enabled": True, "microphone": USB, "echo_cancellation": False},
            "servers": [],
        },
    )

    assert migrate_wakeword_microphone(path) is True
    migrated = _read(path)
    assert migrate_wakeword_microphone(path) is False

    assert migrated == {
        "wakeword": {"enabled": True},
        "servers": [],
        "microphone": {"device": USB, "echo_cancellation": False},
    }
    assert _read(path) == migrated


def test_migration_keeps_an_existing_microphone_section(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    _write(
        path,
        {
            "microphone": {"device": None},
            "wakeword": {"microphone": USB, "echo_cancellation": False},
        },
    )

    migrate_wakeword_microphone(path)

    assert _read(path) == {"microphone": {"device": None}, "wakeword": {}}


def test_migration_leaves_an_unreadable_settings_file_alone(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text("{not json", encoding="utf-8")

    assert migrate_wakeword_microphone(path) is False
    assert path.read_text(encoding="utf-8") == "{not json"


# -- Service ---------------------------------------------------------------------------


def test_the_service_migrates_on_construction(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    _write(path, {"wakeword": {"microphone": USB}})

    service = MicrophoneService(settings_path=path)

    assert service.status() == {"device": USB, "echo_cancellation": True}
    assert "microphone" not in _read(path)["wakeword"]


def test_an_update_persists_and_notifies_listeners_only_on_an_effective_change(
    tmp_path: Path,
) -> None:
    path = tmp_path / "settings.json"
    service = MicrophoneService(settings_path=path)
    heard: list[MicrophoneSettings] = []
    service.add_listener(heard.append)

    changed = service.update({"device": USB})
    unchanged = service.update({"device": USB})
    with pytest.raises(MicrophoneConfigError):
        service.update({"device": "USB"})

    assert changed == unchanged == {"device": USB, "echo_cancellation": True}
    assert heard == [MicrophoneSettings(device=MicrophoneSelection(2, "USB", "MME"))]
    assert desktop_settings.read_section("microphone", path) == {"device": USB}
    assert MicrophoneService(settings_path=path).settings == heard[0]


def test_a_failing_listener_does_not_stop_the_others(tmp_path: Path) -> None:
    service = MicrophoneService(settings_path=tmp_path / "settings.json")
    heard: list[bool] = []

    def fail(_settings: MicrophoneSettings) -> None:
        raise RuntimeError("listener broke")

    service.add_listener(fail)
    service.add_listener(lambda settings: heard.append(settings.echo_cancellation))

    service.update({"echo_cancellation": False})

    assert heard == [False]


def test_listing_devices_refreshes_them_unless_a_capture_holds_a_stream(tmp_path: Path) -> None:
    sd = FakeSoundDevice(pace=0.0025)
    service = MicrophoneService(settings_path=tmp_path / "settings.json", audio_backend=sd)

    idle = service.list_devices()
    stop = threading.Event()
    statuses: list[str] = []
    capture = service.create_capture(
        on_status=lambda status: statuses.append(status.state), stop_event=stop
    )
    capture.start()
    try:
        wait_until(lambda: CAPTURE_CAPTURING in statuses)
        capturing = service.list_devices()
    finally:
        stop.set()
        capture.join(5)

    assert [device["name"] for device in idle] == ["Mic"]
    assert capturing == idle
    assert sd.events[:3] == ["terminate", "initialize", "stream.open"]
    assert sd.events.count("terminate") == 1
