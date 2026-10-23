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
        pytest.param(None, 0, "[OK]", ["missing (defaults will be used)"], id="missing"),
        pytest.param(
            {"format_version": 1, "server_port": 8500}, 0, "[OK]", ["valid_files: 1"], id="valid"
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
    assert (
        lines[0]
        == f"doctor settings: {'failed' if code else 'warning' if verdict == '[WARN]' else 'ok'}"
    )
    assert not capsys.readouterr().err
    if settings is not None:
        assert json.loads((tmp_path / "settings.json").read_text()) == settings


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
        "valid_files: 1",
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
    assert "valid_files: 6" in lines
    assert "artifacts/attachments/att_000000000006.json:" in lines
    assert not any(line.endswith("att_000000000001.json: valid") for line in lines)


@pytest.mark.parametrize("command", ["settings", "config"])
@pytest.mark.parametrize("kind", ["missing", "file"])
def test_doctor_rejects_invalid_data_directory_without_creating_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], command: str, kind: str
) -> None:
    target = tmp_path / "data"
    if kind == "file":
        target.write_text("keep me", encoding="utf-8")
    code = cli_main.run(["doctor", command, "--data-dir", str(target), "--json"])
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert code == 1
    assert result["status"] == "failed"
    assert result["read_only"] is True
    assert result["findings"][0]["code"] == (
        "missing_data_dir" if kind == "missing" else "wrong_file_type"
    )
    assert not captured.err
    assert target.read_text() == "keep me" if kind == "file" else not target.exists()


@pytest.mark.parametrize(
    ("content", "expected_code"),
    [(b'{"format_version":1,', "invalid_json"), (b"\xffsecret-content", "invalid_encoding")],
)
def test_doctor_json_reports_parse_failures_without_exposing_contents(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], content: bytes, expected_code: str
) -> None:
    path = tmp_path / "settings.json"
    path.write_bytes(content)
    code = cli_main.run(["doctor", "settings", "--data-dir", str(tmp_path), "--json"])
    result = json.loads(capsys.readouterr().out)
    assert code == 1
    assert result["findings"][0]["code"] == expected_code
    assert result["findings"][0]["recommendation"]
    assert "secret-content" not in str(result)
    assert path.read_bytes() == content


def test_doctor_pages_findings_with_global_verdict_and_exact_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "settings.json"
    content = json.dumps(
        {
            "format_version": 1,
            "server_port": 0,
            **{f"unknown_{index:03}": "secret-value" for index in range(75)},
        }
    )
    path.write_text(content, encoding="utf-8")
    args = ["doctor", "settings", "--data-dir", str(tmp_path), "--limit", "100"]
    assert cli_main.run(args) == 1
    plain = capsys.readouterr().out
    assert len(plain) < 10000
    assert "Next:" in plain
    assert plain.index("$.server_port") < plain.index("$.unknown_")
    found = []
    offset = 0
    while True:
        assert cli_main.run([*args[:-2], "--limit", "20", "--json", "--offset", str(offset)]) == 1
        result = json.loads(capsys.readouterr().out)
        assert result["errors"] == 1 and result["warnings"] == 75
        assert result["total_findings"] == 76
        found.extend(result["findings"])
        if result["next_offset"] is None:
            break
        offset = result["next_offset"]
        assert str(tmp_path) in result["next_command"]
    assert len(found) == len({finding["path"] for finding in found}) == 76
    assert all(finding["code"] == "unknown_field" for finding in found[1:])
    assert all(finding["recommendation"] for finding in found)
    assert "secret-value" not in str(found)
    assert path.read_text() == content


def test_doctor_escapes_terminal_control_characters_but_json_keeps_exact_paths(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    key = "unknown\nNext: injected\x1b[31m"
    (tmp_path / "settings.json").write_text(json.dumps({"format_version": 1, key: "secret"}))
    args = ["doctor", "settings", "--data-dir", str(tmp_path)]
    assert cli_main.run(args) == 0
    plain = capsys.readouterr().out
    assert "\nNext: injected" not in plain
    assert "\x1b" not in plain
    assert cli_main.run([*args, "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert key in result["findings"][0]["message"]


@pytest.mark.parametrize(
    "arguments", [["--limit", "0"], ["--limit", "101"], ["--offset", "-1"], ["--host", "localhost"]]
)
def test_doctor_rejects_invalid_options(arguments: list[str]) -> None:
    with pytest.raises(SystemExit) as stopped:
        cli_main.run(["doctor", "config", *arguments])
    assert stopped.value.code == 2
