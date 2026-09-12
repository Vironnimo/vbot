"""Bridge: status behavior."""

from __future__ import annotations

import logging
import threading
from pathlib import Path

import pytest

from desktop.system_actions import DesktopSystemActions
from desktop.wakeword.bridge import DesktopBridge
from desktop.wakeword.engine import (
    DEFAULT_WAKEWORD_MODEL_IDS,
)
from tests.desktop.bridge_helpers import (
    _write_settings,
)


def test_get_desktop_capabilities(tmp_path: Path) -> None:
    _write_settings(tmp_path / "settings.json")

    bridge = DesktopBridge(settings_path=tmp_path / "settings.json")

    capabilities = bridge.getDesktopCapabilities()

    assert capabilities == {
        "wakeword": True,
        "serverSelection": True,
        "contextMenu": True,
    }


def test_desktop_system_actions_validate_and_delegate(tmp_path: Path) -> None:
    _write_settings(tmp_path / "settings.json")
    copied: list[str] = []
    opened: list[str] = []

    def open_url(url: str) -> bool:
        opened.append(url)
        return True

    bridge = DesktopBridge(
        settings_path=tmp_path / "settings.json",
        system_actions=DesktopSystemActions(
            clipboard_writer=copied.append,
            clipboard_reader=lambda: "paste me",
            external_url_opener=open_url,
        ),
    )

    assert bridge.setClipboardText("copy me") == {"copied": True}
    assert bridge.getClipboardText() == "paste me"
    assert bridge.openExternalUrl("https://example.com/path?q=1") == {"opened": True}
    assert copied == ["copy me"]
    assert opened == ["https://example.com/path?q=1"]


def test_get_wakeword_status_shape(tmp_path: Path) -> None:
    _write_settings(
        tmp_path / "settings.json",
        {
            "enabled": True,
            "model_sensitivities": {DEFAULT_WAKEWORD_MODEL_IDS[0]: 0.7},
        },
    )

    bridge = DesktopBridge(settings_path=tmp_path / "settings.json")
    status = bridge.getWakewordStatus()

    assert status["enabled"] is True
    assert status["model_sensitivities"] == {
        DEFAULT_WAKEWORD_MODEL_IDS[0]: 0.7,
        DEFAULT_WAKEWORD_MODEL_IDS[1]: 0.5,
    }
    assert status["state"] == "off"
    assert "engine" in status
    assert "microphone" in status
    assert "target_agent_id" in status
    assert "session_behavior" in status
    assert status["active_model_ids"] == list(DEFAULT_WAKEWORD_MODEL_IDS)
    assert status["engine"] == "pyopen_wakeword"
    assert status["calibration"] == {
        "active": False,
        "phase": None,
        "scores": {},
        "peaks": {},
        "noise_levels": {},
        "sample_counts": {},
        "required_samples": 5,
        "target_model_id": None,
        "recommended_sensitivities": {},
        "noise_seconds_remaining": 0,
        "noise_high": False,
    }


def test_voice_target_profile_is_isolated_per_server(tmp_path: Path) -> None:
    settings_file = tmp_path / "settings.json"
    _write_settings(settings_file)
    bridge = DesktopBridge(
        settings_path=settings_file,
        server_url="http://a.lan:8420",
    )

    bridge.setWakewordConfig({"target_agent_id": "home", "session_behavior": "active"})
    bridge.set_server_url("http://b.lan:8420")
    assert bridge.getWakewordStatus()["target_agent_id"] is None

    bridge.setWakewordConfig({"target_agent_id": "office", "session_behavior": "new"})
    bridge.set_server_url("http://a.lan:8420")
    status = bridge.getWakewordStatus()

    assert status["target_agent_id"] == "home"
    assert status["session_behavior"] == "active"


def test_voice_target_profile_merges_loopback_host_aliases(tmp_path: Path) -> None:
    """localhost and 127.0.0.1 address the same server, so one profile serves both."""

    from desktop.settings import read_wakeword_settings

    settings_file = tmp_path / "settings.json"
    _write_settings(settings_file)
    bridge = DesktopBridge(
        settings_path=settings_file,
        server_url="http://127.0.0.1:8420",
    )
    bridge.setWakewordConfig({"target_agent_id": "nabu"})

    # Reading through the alias spelling finds the profile saved under the
    # canonical loopback key instead of failing as missing_target_agent.
    bridge.set_server_url("http://localhost:8420")
    assert bridge.getWakewordStatus()["target_agent_id"] == "nabu"

    # Saving through the alias spelling updates the canonical key rather than
    # forking a second profile that only matches while localhost is active.
    bridge.setWakewordConfig({"target_agent_id": "nova"})
    stored_profiles = read_wakeword_settings(settings_file)["server_profiles"]

    assert stored_profiles == {"http://127.0.0.1:8420": {"target_agent_id": "nova"}}
    bridge.set_server_url("http://127.0.0.1:8420")
    assert bridge.getWakewordStatus()["target_agent_id"] == "nova"


def test_status_exposes_actionable_error_and_event_history(tmp_path: Path) -> None:
    settings_file = tmp_path / "settings.json"
    _write_settings(settings_file)
    bridge = DesktopBridge(settings_path=settings_file)

    bridge.publish_state("starting")
    bridge.publish_state("error", "microphone_unavailable")

    status = bridge.getWakewordStatus()
    assert status["state"] == "error"
    assert status["error_code"] == "microphone_unavailable"
    assert status["events"][-2:] == [
        {"sequence": 1, "state": "starting", "error_code": None},
        {
            "sequence": 2,
            "state": "error",
            "error_code": "microphone_unavailable",
        },
    ]


def test_status_preserves_microphone_disconnect_as_nonfatal_reason(tmp_path: Path) -> None:
    settings_file = tmp_path / "settings.json"
    _write_settings(settings_file, {"enabled": True})
    bridge = DesktopBridge(settings_path=settings_file)

    bridge.publish_state("listening")
    bridge.publish_state("microphone_disconnected", "microphone_read_failed")

    status = bridge.getWakewordStatus()
    assert status["state"] == "microphone_disconnected"
    assert status["error_code"] == "microphone_read_failed"
    assert status["events"][-1] == {
        "sequence": 2,
        "state": "microphone_disconnected",
        "error_code": "microphone_read_failed",
    }


def test_set_wakeword_config_rejects_non_dict(tmp_path: Path) -> None:
    settings_file = tmp_path / "settings.json"
    _write_settings(settings_file)

    bridge = DesktopBridge(settings_path=settings_file)
    # Non-dict input should be silently ignored
    bridge.setWakewordConfig({"not": "applicable"})

    status = bridge.getWakewordStatus()
    assert status["model_sensitivities"] == {
        DEFAULT_WAKEWORD_MODEL_IDS[0]: 0.5,
        DEFAULT_WAKEWORD_MODEL_IDS[1]: 0.5,
    }


def test_publish_state_updates_state_and_logs_transitions(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _write_settings(tmp_path / "settings.json")

    bridge = DesktopBridge(settings_path=tmp_path / "settings.json")

    with caplog.at_level("INFO", logger="vbot.desktop.wakeword.bridge"):
        bridge.publish_state("listening")
        assert bridge.getWakewordStatus()["state"] == "listening"

        bridge.publish_state("recording")
        assert bridge.getWakewordStatus()["state"] == "recording"

        bridge.publish_state("error", "microphone_unavailable")
        assert bridge.getWakewordStatus()["state"] == "error"

    messages = [
        record.getMessage()
        for record in caplog.records
        if record.name == "vbot.desktop.wakeword.bridge" and record.levelno == logging.INFO
    ]
    assert len(messages) == 3
    assert all(marker in messages[0] for marker in ("sequence=1", "from=off", "to=listening"))
    assert all(marker in messages[1] for marker in ("sequence=2", "from=listening", "to=recording"))
    assert all(
        marker in messages[2]
        for marker in (
            "sequence=3",
            "from=recording",
            "to=error",
            "error_code=microphone_unavailable",
        )
    )


def test_publish_state_does_not_log_unchanged_state(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _write_settings(tmp_path / "settings.json")
    bridge = DesktopBridge(settings_path=tmp_path / "settings.json")

    with caplog.at_level("INFO", logger="vbot.desktop.wakeword.bridge"):
        bridge.publish_state("listening")
        caplog.clear()
        bridge.publish_state("listening")

    assert not [
        record
        for record in caplog.records
        if record.name == "vbot.desktop.wakeword.bridge" and record.levelno == logging.INFO
    ]


def test_publish_state_rejects_invalid_state(tmp_path: Path) -> None:
    _write_settings(tmp_path / "settings.json")

    bridge = DesktopBridge(settings_path=tmp_path / "settings.json")

    with pytest.raises(ValueError):
        bridge.publish_state("nonexistent")


def test_bridge_thread_safety_concurrent_access(tmp_path: Path) -> None:
    _write_settings(tmp_path / "settings.json")

    bridge = DesktopBridge(settings_path=tmp_path / "settings.json")
    errors: list[Exception] = []

    def reader() -> None:
        for _ in range(50):
            try:
                bridge.getWakewordStatus()
            except Exception as exc:
                errors.append(exc)

    def writer() -> None:
        for i in range(50):
            try:
                bridge.publish_state("listening" if i % 2 == 0 else "recording")
                bridge.setWakewordConfig(
                    {"model_sensitivities": {DEFAULT_WAKEWORD_MODEL_IDS[0]: 0.5 + (i % 9) * 0.05}}
                )
            except Exception as exc:
                errors.append(exc)

    threads = [threading.Thread(target=reader) for _ in range(3)] + [
        threading.Thread(target=writer) for _ in range(2)
    ]

    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5.0)

    assert len(errors) == 0
