"""Smoke tests of the Tool lab CLI (``python -m scripts.tool_lab``) commands that run offline."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.database import write_bootstrap_marker
from core.sessions.sessions import ChatSessionManager
from scripts.tool_lab.__main__ import main


def test_probe_runs_a_case_file_through_the_production_tool_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    case_file = tmp_path / "cases.json"
    case_file.write_text(
        json.dumps(
            {
                "files": {"a.txt": "one\ntwo\n"},
                "cases": [
                    {
                        "name": "read",
                        "tool": "read",
                        "arguments": {"path": "a.txt"},
                        "expect": {"ok": True, "contains": ["two"]},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    assert main(["probe", str(case_file)]) == 0
    assert "1/1 checked cases pass" in capsys.readouterr().out


def test_sessions_reads_a_copy_and_leaves_the_source_untouched(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    live = tmp_path / "live"
    live.mkdir()
    write_bootstrap_marker(live)
    manager = ChatSessionManager(live)
    manager.create("main", session_id="s1")
    manager.close()
    before = (live / "sessions.db").read_bytes()

    assert main(["sessions", str(live)]) == 0

    assert (live / "sessions.db").read_bytes() == before
    assert capsys.readouterr().out.strip()
