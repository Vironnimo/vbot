"""Tests for Desktop wakeword settings read/write/merge."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from desktop import main as desktop_main


def test_read_wakeword_settings_returns_defaults_when_file_missing(tmp_path: Path) -> None:
    settings_file = tmp_path / "settings.json"

    config = desktop_main.read_wakeword_settings(settings_file)

    assert config == desktop_main.DEFAULT_WAKEWORD_SETTINGS


def test_read_wakeword_settings_merges_with_defaults(tmp_path: Path) -> None:
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(
        json.dumps({"host": "127.0.0.1", "port": 8420, "wakeword": {"enabled": True}}),
        encoding="utf-8",
    )

    config = desktop_main.read_wakeword_settings(settings_file)

    assert config["enabled"] is True
    assert config["engine"] == desktop_main.DEFAULT_WAKEWORD_SETTINGS["engine"]
    assert config["wake_phrase"] == desktop_main.DEFAULT_WAKEWORD_SETTINGS["wake_phrase"]


def test_read_wakeword_settings_falls_back_for_missing_wakeword_key(tmp_path: Path) -> None:
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(
        json.dumps({"host": "127.0.0.1", "port": 8420}),
        encoding="utf-8",
    )

    config = desktop_main.read_wakeword_settings(settings_file)

    assert config == desktop_main.DEFAULT_WAKEWORD_SETTINGS


def test_read_wakeword_settings_falls_back_for_non_dict_wakeword(tmp_path: Path) -> None:
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(
        json.dumps({"host": "127.0.0.1", "port": 8420, "wakeword": "invalid"}),
        encoding="utf-8",
    )

    config = desktop_main.read_wakeword_settings(settings_file)

    assert config == desktop_main.DEFAULT_WAKEWORD_SETTINGS


def test_write_wakeword_settings_persists_merged_with_host_port(tmp_path: Path) -> None:
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(
        json.dumps({"host": "10.0.0.1", "port": 9000}),
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
    desktop_main.write_wakeword_settings(wakeword_config, settings_file)

    stored = json.loads(settings_file.read_text(encoding="utf-8"))
    assert stored["host"] == "10.0.0.1"
    assert stored["port"] == 9000
    assert stored["wakeword"] == wakeword_config


def test_write_wakeword_settings_overwrites_existing_wakeword(tmp_path: Path) -> None:
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(
        json.dumps({"host": "127.0.0.1", "port": 8420, "wakeword": {"enabled": True}}),
        encoding="utf-8",
    )

    desktop_main.write_wakeword_settings({"enabled": False, "sensitivity": 0.3}, settings_file)

    stored = json.loads(settings_file.read_text(encoding="utf-8"))
    assert stored["wakeword"]["enabled"] is False
    assert stored["wakeword"]["sensitivity"] == 0.3


def test_read_wakeword_settings_handles_corrupt_file(tmp_path: Path) -> None:
    settings_file = tmp_path / "settings.json"
    settings_file.write_text("not valid json", encoding="utf-8")

    config = desktop_main.read_wakeword_settings(settings_file)

    assert config == desktop_main.DEFAULT_WAKEWORD_SETTINGS


@pytest.mark.parametrize("flag", [True, False])
def test_parse_args_mock_wakeword_flag(flag: bool) -> None:
    argv = ["--mock-wakeword"] if flag else []

    args = desktop_main.parse_args(argv)

    assert args.mock_wakeword is flag


def test_parse_args_mock_wakeword_defaults_to_false() -> None:
    args = desktop_main.parse_args(["--host", "127.0.0.1"])

    assert args.mock_wakeword is False


def test_append_accessor_param_appends_to_root_url() -> None:
    result = desktop_main._append_accessor_param("http://127.0.0.1:8420/")

    assert result == "http://127.0.0.1:8420/?accessor=desktop"


def test_append_accessor_param_preserves_existing_params() -> None:
    result = desktop_main._append_accessor_param("http://127.0.0.1:8420/?foo=bar")

    assert result == "http://127.0.0.1:8420/?foo=bar&accessor=desktop"
