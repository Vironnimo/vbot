"""Tests for local doctor CLI commands."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from cli import doctor_management
from cli import main as cli_main
from cli.server_management import CommandResult, ServerInstance
from core.utils.logging import resolve_daily_log_path


def make_instance(tmp_path: Path, *, port: int = 0) -> ServerInstance:
    data_dir = tmp_path / "data"
    return ServerInstance(
        host="127.0.0.1",
        port=port,
        data_dir=data_dir,
        url="local",
        log_path=resolve_daily_log_path(data_dir),
    )


def test_parse_args_supports_doctor_settings() -> None:
    args = cli_main.parse_args(["doctor", "settings", "--data-dir", "dev-data"])

    assert args.area == "doctor"
    assert args.command == "settings"
    assert args.data_dir == "dev-data"


def test_parse_args_supports_doctor_config() -> None:
    args = cli_main.parse_args(["doctor", "config", "--data-dir", "dev-data"])

    assert args.area == "doctor"
    assert args.command == "config"
    assert args.data_dir == "dev-data"


def test_doctor_settings_reports_missing_file_as_ok(tmp_path: Path) -> None:
    result = doctor_management.doctor_settings(tmp_path)

    assert result.ok is True
    assert result.message.splitlines() == [
        "doctor settings: ok",
        f"data_dir: {tmp_path.resolve()}",
        f"file: {tmp_path.resolve() / 'settings.json'}",
        "status: missing (defaults will be used)",
    ]


def test_doctor_settings_reports_valid_file(tmp_path: Path) -> None:
    (tmp_path / "settings.json").write_text(json.dumps({"server_port": 8500}), encoding="utf-8")

    result = doctor_management.doctor_settings(tmp_path)

    assert result.ok is True
    assert result.message.splitlines() == [
        "doctor settings: ok",
        f"data_dir: {tmp_path.resolve()}",
        f"file: {tmp_path.resolve() / 'settings.json'}",
        "status: valid",
    ]


def test_doctor_settings_reports_errors_and_warnings(tmp_path: Path) -> None:
    (tmp_path / "settings.json").write_text(
        json.dumps({"server_port": 0, "typo": True}), encoding="utf-8"
    )

    result = doctor_management.doctor_settings(tmp_path)

    assert result.ok is False
    assert result.message.splitlines() == [
        "doctor settings: failed",
        f"data_dir: {tmp_path.resolve()}",
        f"file: {tmp_path.resolve() / 'settings.json'}",
        "errors: 1",
        "warnings: 1",
        "- warning $.typo: unknown settings key: typo",
        "- error $.server_port: must be between 1 and 65535",
    ]


def test_doctor_config_reports_all_config_files(tmp_path: Path) -> None:
    (tmp_path / "settings.json").write_text(json.dumps({"server_port": 8500}), encoding="utf-8")
    agent_dir = tmp_path / "agents" / "broken"
    agent_dir.mkdir(parents=True)
    agent_dir.joinpath("agent.json").write_text(
        json.dumps(
            {
                "id": "broken",
                "name": "Broken Agent",
                "model": "",
                "fallback_model": "",
                "temperature": None,
                "thinking_effort": None,
                "allowed_tools": "read_file",
                "allowed_skills": ["*"],
                "created_at": "2026-05-03T12:00:00Z",
                "updated_at": "2026-05-03T12:00:00Z",
            }
        ),
        encoding="utf-8",
    )

    result = doctor_management.doctor_config(tmp_path)

    assert result.ok is False
    assert result.message.splitlines() == [
        "doctor config: failed",
        f"data_dir: {tmp_path.resolve()}",
        "files_checked: 2",
        "errors: 1",
        "settings.json: valid",
        "agents/broken/agent.json:",
        "- error $.allowed_tools: must be a list of strings",
    ]


def test_run_dispatches_doctor_settings(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    instance = make_instance(tmp_path)
    calls: list[Any] = []

    def fake_doctor_settings(data_dir: str | Path | None) -> CommandResult:
        calls.append(data_dir)
        return CommandResult(ok=True, message="doctor settings: ok", instance=instance)

    exit_code = cli_main.run(
        ["doctor", "settings", "--data-dir", str(tmp_path / "data")],
        doctor_settings_fn=fake_doctor_settings,
    )

    assert exit_code == 0
    assert calls == [str(tmp_path / "data")]
    assert capsys.readouterr().out.splitlines() == ["doctor settings: ok"]


def test_run_dispatches_doctor_config(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    instance = make_instance(tmp_path)
    calls: list[Any] = []

    def fake_doctor_config(data_dir: str | Path | None) -> CommandResult:
        calls.append(data_dir)
        return CommandResult(ok=True, message="doctor config: ok", instance=instance)

    exit_code = cli_main.run(
        ["doctor", "config", "--data-dir", str(tmp_path / "data")],
        doctor_config_fn=fake_doctor_config,
    )

    assert exit_code == 0
    assert calls == [str(tmp_path / "data")]
    assert capsys.readouterr().out.splitlines() == ["doctor config: ok"]
