"""Smoke tests of the Tool lab CLI (``python -m scripts.tool_lab``) commands that run offline."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from core.chat import ChatMessage
from core.chat.messages import ToolCall
from core.database import write_bootstrap_marker
from core.sessions.sessions import ChatSessionManager
from scripts.tool_lab import replay
from scripts.tool_lab.__main__ import main
from scripts.tool_lab.sessions import Filters
from tests.core.sessions.history_fixtures import seed_history


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


def _patch_call(number: int, patch: str, result: dict[str, Any]) -> list[ChatMessage]:
    call = ToolCall(id=f"call-{number}", name="apply_patch", arguments={"patch": patch})
    return [
        ChatMessage.assistant(model="model", content=None, tool_calls=[call]),
        ChatMessage.tool(tool_call_id=call.id, name="apply_patch", content=json.dumps(result)),
    ]


def _patch(*lines: str) -> str:
    return "\n".join(("*** Begin Patch", *lines, "*** End Patch"))


def _update(old: str) -> str:
    return _patch("*** Update File: src/app.py", "@@", f"-{old}", "+    value = 2")


@pytest.mark.asyncio
async def test_replay_exports_session_edits_and_classifies_their_replay(tmp_path: Path) -> None:
    live = tmp_path / "live"
    live.mkdir()
    write_bootstrap_marker(live)
    manager = ChatSessionManager(live)
    add = _patch("*** Add File: src/app.py", "+def main():", "+    value = 1", "+    return value")
    created = {"status": "applied", "content": "Created src/app.py (3 lines)."}
    seed_history(
        manager.create("main", session_id="s1"),
        [
            ChatMessage.user("change the value"),
            *_patch_call(1, add, {"ok": True, "data": created}),
            # Recorded as failed; the current engine tolerates the trailing blanks.
            *_patch_call(
                2,
                _update("    value = 1   "),
                {"ok": False, "error": {"code": "text_not_found", "message": "not found"}},
            ),
            *_patch_call(
                3,
                _update("    value = 1"),
                {
                    "ok": True,
                    "data": {
                        "status": "applied",
                        "content": "Updated src/app.py:\n1| def main():\n2|     value = 2",
                    },
                },
            ),
            ChatMessage.run_summary(
                run_id="run-1",
                status="completed",
                iteration_count=3,
                timing={
                    "started_at": "2026-09-30T10:00:00Z",
                    "completed_at": "2026-09-30T10:00:05Z",
                    "duration_ms": 5000,
                },
            ),
        ],
    )
    manager.close()
    corpus = tmp_path / "work" / replay.CORPUS_NAME

    async with replay.replay_dispatcher() as dispatch:
        stats, sessions = await replay.export_corpus(
            live / "sessions.db", corpus, dispatch=dispatch, admit=Filters()
        )
        results = [
            result
            async for result in replay.replay_cases(
                replay.read_corpus(corpus), dispatch=dispatch, tool="apply_patch"
            )
        ]

    assert (sessions, stats.calls, stats.cases) == (1, 3, 3)
    assert [result.case["target"]["label"] for result in results] == ["self", "eventual", "self"]
    assert [result.label for result in results] == ["same", "fixed", "same"]
