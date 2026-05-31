"""Tests for DesktopBridge API shape and state management."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from desktop.wakeword.bridge import DesktopBridge


def _write_settings(path: Path, wakeword_config: dict | None = None) -> None:
    data = {"host": "127.0.0.1", "port": 8420}
    if wakeword_config is not None:
        data["wakeword"] = wakeword_config
    path.write_text(json.dumps(data), encoding="utf-8")


def test_get_desktop_capabilities(tmp_path: Path) -> None:
    _write_settings(tmp_path / "settings.json")

    bridge = DesktopBridge(settings_path=tmp_path / "settings.json")

    capabilities = bridge.getDesktopCapabilities()

    assert capabilities == {"wakeword": True}


def test_get_wakeword_status_shape(tmp_path: Path) -> None:
    _write_settings(tmp_path / "settings.json", {"enabled": True, "sensitivity": 0.7})

    bridge = DesktopBridge(settings_path=tmp_path / "settings.json")
    status = bridge.getWakewordStatus()

    assert status["enabled"] is True
    assert status["sensitivity"] == 0.7
    assert status["state"] == "off"
    assert "engine" in status
    assert "microphone" in status
    assert "target_agent_id" in status
    assert "session_behavior" in status
    assert "wake_phrase" in status


def test_set_wakeword_enabled_toggles_and_persists(tmp_path: Path) -> None:
    settings_file = tmp_path / "settings.json"
    _write_settings(settings_file)

    bridge = DesktopBridge(settings_path=settings_file)

    bridge.setWakewordEnabled(True)
    status = bridge.getWakewordStatus()
    assert status["enabled"] is True

    bridge.setWakewordEnabled(False)
    status = bridge.getWakewordStatus()
    assert status["enabled"] is False


def test_set_wakeword_config_partial_update(tmp_path: Path) -> None:
    settings_file = tmp_path / "settings.json"
    _write_settings(settings_file)

    bridge = DesktopBridge(settings_path=settings_file)
    bridge.setWakewordConfig({"sensitivity": 0.9, "target_agent_id": "agent-1"})

    status = bridge.getWakewordStatus()
    assert status["sensitivity"] == 0.9
    assert status["target_agent_id"] == "agent-1"


def test_set_wakeword_config_rejects_non_dict(tmp_path: Path) -> None:
    settings_file = tmp_path / "settings.json"
    _write_settings(settings_file)

    bridge = DesktopBridge(settings_path=settings_file)
    # Non-dict input should be silently ignored
    bridge.setWakewordConfig({"not": "applicable"})

    status = bridge.getWakewordStatus()
    assert status["sensitivity"] == 0.5  # Default unchanged


def test_publish_state_updates_state(tmp_path: Path) -> None:
    _write_settings(tmp_path / "settings.json")

    bridge = DesktopBridge(settings_path=tmp_path / "settings.json")

    bridge.publish_state("listening")
    assert bridge.getWakewordStatus()["state"] == "listening"

    bridge.publish_state("recording")
    assert bridge.getWakewordStatus()["state"] == "recording"

    bridge.publish_state("error")
    assert bridge.getWakewordStatus()["state"] == "error"


def test_publish_state_rejects_invalid_state(tmp_path: Path) -> None:
    _write_settings(tmp_path / "settings.json")

    bridge = DesktopBridge(settings_path=tmp_path / "settings.json")

    with pytest.raises(ValueError, match="Invalid wakeword state"):
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
                bridge.setWakewordConfig({"sensitivity": 0.5 + (i % 10) * 0.05})
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
