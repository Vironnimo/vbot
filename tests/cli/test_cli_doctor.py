"""Tests for the local ``vbot doctor`` commands that validate config files in a data-dir."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cli import main as cli_main


def _doctor(capsys: pytest.CaptureFixture[str], command: str, data_dir: Path) -> tuple[int, str]:
    code = cli_main.run(["doctor", command, "--data-dir", str(data_dir)])
    return code, capsys.readouterr().out


@pytest.mark.parametrize(
    ("settings", "code", "verdict", "details"),
    [
        pytest.param(None, 0, "[OK]", ["status: missing (defaults will be used)"], id="missing"),
        pytest.param(
            {"format_version": 1, "server_port": 8500}, 0, "[OK]", ["status: valid"], id="valid"
        ),
        pytest.param(
            {"format_version": 1, "typo": True}, 0, "[WARN]", ["$.typo"], id="usable-with-warning"
        ),
        pytest.param(
            {"format_version": 1, "server_port": 0, "typo": True},
            1,
            "[ERROR]",
            ["errors: 1", "warnings: 1", "$.typo", "$.server_port"],
            id="errors-and-warnings",
        ),
    ],
)
def test_doctor_settings_reports_the_settings_file_state(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    settings: dict[str, object] | None,
    code: int,
    verdict: str,
    details: list[str],
) -> None:
    if settings is not None:
        (tmp_path / "settings.json").write_text(json.dumps(settings), encoding="utf-8")

    exit_code, out = _doctor(capsys, "settings", tmp_path)

    lines = out.splitlines()
    assert exit_code == code
    assert lines[1:3] == [
        f"data_dir: {tmp_path.resolve()}",
        f"file: {tmp_path.resolve() / 'settings.json'}",
    ]
    assert lines[-1].startswith(verdict)
    for detail in details:
        assert detail in out
    if code == 0:
        assert lines[0] == "doctor settings: ok"


def test_doctor_config_reports_every_config_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
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

    code, out = _doctor(capsys, "config", tmp_path)

    assert code == 1
    for text in (
        f"data_dir: {tmp_path.resolve()}",
        "files_checked: 2",
        "errors: 1",
        "warnings: 1",
        "settings.json",
        "agents/broken/agent.json",
        "$.temperature",
        # A retired field is an unknown field: reported, kept on disk, not an error.
        "$.allowed_tools",
    ):
        assert text in out


def test_doctor_config_summarizes_a_directory_of_many_valid_documents(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
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
            "size_bytes": 5 if index < 6 else -1,
            "stored_at": "2026-06-18T10:00:00+00:00",
        }
        (attachments / f"{attachment_id}.json").write_text(json.dumps(sidecar), encoding="utf-8")

    code, out = _doctor(capsys, "config", tmp_path)

    lines = out.splitlines()
    assert code == 1
    assert "files_checked: 7" in lines
    assert "settings.json: valid" in lines
    assert "artifacts/attachments/: 5 documents valid" in lines
    assert "artifacts/attachments/att_000000000006.json:" in lines
    assert not any(line.endswith("att_000000000001.json: valid") for line in lines)
