"""Tests for the explicit Session Run-Summary Model-step converter."""

from __future__ import annotations

import json
import subprocess
import sys
from importlib import import_module
from pathlib import Path
from typing import Any

import pytest

_CONVERTER = import_module("scripts.converters.session_model_step_counts")
SessionModelStepConversionError = _CONVERTER.SessionModelStepConversionError
apply_session_model_step_conversion = _CONVERTER.apply_session_model_step_conversion
plan_session_model_step_conversion = _CONVERTER.plan_session_model_step_conversion

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def _write_session(
    data_dir: Path,
    relative_path: str,
    records: list[dict[str, Any]],
    *,
    line_ending: str = "\n",
) -> Path:
    path = data_dir / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    content = line_ending.join(
        json.dumps(record, ensure_ascii=False, separators=(",", ":")) for record in records
    )
    path.write_bytes((content + line_ending).encode("utf-8"))
    return path


def _records(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_dry_run_reports_counts_without_changing_sessions(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    session = _write_session(
        data_dir,
        "agents/main/sessions/one.jsonl",
        [
            {"role": "assistant", "content": "done"},
            {"role": "run_summary", "run_id": "run-one"},
        ],
    )
    before = session.read_bytes()

    result = subprocess.run(
        [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "converters" / "session_model_step_counts.py"),
            str(data_dir),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "sessions=1 files=1 summaries=1" in result.stdout
    assert "dry-run only" in result.stdout
    assert session.read_bytes() == before


def test_apply_converts_live_project_and_archived_sessions(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    identity = _write_session(
        data_dir,
        "agents/main/sessions/identity.jsonl",
        [
            {"role": "user", "content": "work"},
            {"role": "assistant", "tool_calls": [{"id": "one"}]},
            {"role": "tool", "tool_call_id": "one"},
            {"role": "assistant", "tool_calls": [{"id": "two"}]},
            {"role": "tool", "tool_call_id": "two"},
            {"role": "assistant", "content": "done"},
            {"role": "run_summary", "run_id": "run-three"},
            {"role": "user", "content": "failed request"},
            {"role": "error", "content": "provider failed"},
            {"role": "run_summary", "run_id": "run-zero"},
            {"role": "assistant", "content": "open run"},
        ],
        line_ending="\r\n",
    )
    project = _write_session(
        data_dir,
        "projects/vbot/agents/builder/sessions/project.jsonl",
        [
            {"role": "assistant", "content": "project result"},
            {"role": "run_summary", "run_id": "project-run"},
        ],
    )
    archived = _write_session(
        data_dir,
        "archive/sessions/agents/main/archived.jsonl",
        [
            {"role": "assistant", "content": "archived result"},
            {"role": "run_summary", "run_id": "archived-run"},
        ],
    )

    result = apply_session_model_step_conversion(data_dir)

    assert result.converted_files == 3
    assert result.converted_summaries == 4
    identity_summaries = [
        record for record in _records(identity) if record["role"] == "run_summary"
    ]
    assert [record["model_step_count"] for record in identity_summaries] == [3, 0]
    assert _records(project)[-1]["model_step_count"] == 1
    assert _records(archived)[-1]["model_step_count"] == 1
    assert identity.read_bytes().count(b"\r\n") == 11


def test_apply_is_noop_for_current_summaries_and_summary_free_sessions(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    current = _write_session(
        data_dir,
        "agents/main/sessions/current.jsonl",
        [
            {"role": "assistant", "content": "done"},
            {"role": "run_summary", "run_id": "current-run", "model_step_count": 1},
        ],
    )
    open_session = _write_session(
        data_dir,
        "agents/main/sessions/open.jsonl",
        [{"role": "assistant", "content": "still running"}],
    )
    before = {path: path.read_bytes() for path in (current, open_session)}

    plan = plan_session_model_step_conversion(data_dir)
    result = apply_session_model_step_conversion(data_dir)

    assert plan.session_files == 2
    assert plan.current_summaries == 1
    assert plan.conversions == ()
    assert result.converted_files == 0
    assert result.converted_summaries == 0
    assert {path: path.read_bytes() for path in before} == before


def test_preflight_rejects_invalid_session_before_writing_any_file(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    convertible = _write_session(
        data_dir,
        "agents/main/sessions/one.jsonl",
        [
            {"role": "assistant", "content": "done"},
            {"role": "run_summary", "run_id": "run-one"},
        ],
    )
    invalid = data_dir / "agents" / "main" / "sessions" / "two.jsonl"
    invalid.write_bytes(b'{"role":"assistant"}\n{"role":')
    before = convertible.read_bytes()

    with pytest.raises(SessionModelStepConversionError, match="incomplete trailing record"):
        apply_session_model_step_conversion(data_dir)

    assert convertible.read_bytes() == before


def test_preflight_rejects_an_existing_count_that_disagrees_with_history(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    _write_session(
        data_dir,
        "agents/main/sessions/mismatch.jsonl",
        [
            {"role": "assistant", "content": "done"},
            {"role": "run_summary", "run_id": "run-one", "model_step_count": 2},
        ],
    )

    with pytest.raises(SessionModelStepConversionError, match="stored=2 derived=1"):
        plan_session_model_step_conversion(data_dir)
