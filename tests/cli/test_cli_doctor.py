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
    assert result.message.splitlines()[-1].startswith("[OK]")
    assert result.message.splitlines()[:-1] == [
        "doctor settings: ok",
        f"data_dir: {tmp_path.resolve()}",
        f"file: {tmp_path.resolve() / 'settings.json'}",
        "status: missing (defaults will be used)",
    ]


def test_doctor_settings_reports_valid_file(tmp_path: Path) -> None:
    (tmp_path / "settings.json").write_text(
        json.dumps({"format_version": 1, "server_port": 8500}), encoding="utf-8"
    )

    result = doctor_management.doctor_settings(tmp_path)

    assert result.ok is True
    assert result.message.splitlines()[-1].startswith("[OK]")
    assert result.message.splitlines()[:-1] == [
        "doctor settings: ok",
        f"data_dir: {tmp_path.resolve()}",
        f"file: {tmp_path.resolve() / 'settings.json'}",
        "status: valid",
    ]


def test_doctor_warning_is_visible_even_when_configuration_is_usable(tmp_path):
    (tmp_path / "settings.json").write_text('{"format_version": 1, "typo": true}', encoding="utf-8")
    result = doctor_management.doctor_settings(tmp_path)
    assert result.ok
    assert result.message.splitlines()[-1].startswith("[WARN]")
    assert "$.typo" in result.message


def test_doctor_settings_reports_errors_and_warnings(tmp_path: Path) -> None:
    (tmp_path / "settings.json").write_text(
        json.dumps({"format_version": 1, "server_port": 0, "typo": True}), encoding="utf-8"
    )

    result = doctor_management.doctor_settings(tmp_path)

    assert result.ok is False
    assert result.message.splitlines()[-1].startswith("[ERROR]")
    assert f"data_dir: {tmp_path.resolve()}" in result.message
    assert f"file: {tmp_path.resolve() / 'settings.json'}" in result.message
    assert "errors: 1" in result.message
    assert "warnings: 1" in result.message
    assert "$.typo" in result.message
    assert "$.server_port" in result.message


def test_doctor_config_reports_all_config_files(tmp_path: Path) -> None:
    (tmp_path / "settings.json").write_text(
        json.dumps({"format_version": 1, "server_port": 8500}), encoding="utf-8"
    )
    agent_dir = tmp_path / "agents" / "broken"
    agent_dir.mkdir(parents=True)
    agent_dir.joinpath("agent.json").write_text(
        json.dumps(
            {
                "format_version": 1,
                "id": "broken",
                "name": "Broken Agent",
                "model": "",
                "fallback_models": [],
                "temperature": 9,
                "thinking_effort": None,
                "allowed_tools": ["read_file"],
                "allowed_skills": ["*"],
                "custom_system_prompt_enabled": False,
                "created_at": "2026-05-03T12:00:00Z",
                "updated_at": "2026-05-03T12:00:00Z",
            }
        ),
        encoding="utf-8",
    )

    result = doctor_management.doctor_config(tmp_path)

    assert result.ok is False
    assert f"data_dir: {tmp_path.resolve()}" in result.message
    assert "files_checked: 2" in result.message
    assert "errors: 1" in result.message
    assert "warnings: 1" in result.message
    assert "settings.json" in result.message
    assert "agents/broken/agent.json" in result.message
    assert "$.temperature" in result.message
    # A retired field is an unknown field: reported, kept on disk, not an error.
    assert "$.allowed_tools" in result.message


def test_doctor_config_summarizes_a_directory_of_many_valid_documents(tmp_path: Path) -> None:
    (tmp_path / "settings.json").write_text(json.dumps({"format_version": 1}), encoding="utf-8")
    attachments = tmp_path / "artifacts" / "attachments"
    attachments.mkdir(parents=True)
    for index in range(1, 7):
        attachment_id = f"att_00000000000{index}"
        sidecar = {
            "format_version": 1,
            "id": attachment_id,
            "filename": "notes.txt",
            "media_type": "text/plain",
            "size_bytes": 5,
            "stored_at": "2026-06-18T10:00:00+00:00",
        }
        if index == 6:
            sidecar["size_bytes"] = -1
        (attachments / f"{attachment_id}.json").write_text(json.dumps(sidecar), encoding="utf-8")

    result = doctor_management.doctor_config(tmp_path)

    lines = result.message.splitlines()
    assert result.ok is False
    assert "files_checked: 7" in lines
    assert "settings.json: valid" in lines
    assert "artifacts/attachments/: 5 documents valid" in lines
    assert "artifacts/attachments/att_000000000006.json:" in lines
    assert not any(line.endswith("att_000000000001.json: valid") for line in lines)


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
