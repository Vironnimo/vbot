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
from core.tools.model_names import model_tool_name
from scripts.tool_lab import definitions, replay
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
    created = {"content": "Created src/app.py (3 lines)."}
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
                {"ok": True, "data": {"content": "Updated src/app.py (line 2)."}},
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
        # A historical context-only hunk is invalid now, but must remain one
        # refused case rather than aborting every later replay in the corpus.
        refused = await dispatch(
            {"patch": _patch("*** Update File: src/app.py", "@@", " context only")},
            {"cwd/src/app.py": "context only\n"},
            {"cwd/src/app.py"},
        )
        assert not refused.ok and refused.error_code == "invalid_arguments"
        results = [
            result
            async for result in replay.replay_cases(
                replay.read_corpus(corpus), dispatch=dispatch, tool="apply_patch"
            )
        ]

    assert (sessions, stats.calls, stats.cases) == (1, 3, 3)
    assert [result.case["target"]["label"] for result in results] == ["self", "eventual", "self"]
    assert [result.label for result in results] == ["same", "fixed", "same"]


@pytest.mark.asyncio
async def test_replay_confines_repaired_calls_and_corpus_files_to_each_case(tmp_path: Path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("before", encoding="utf-8")
    # Corpus export could not decode this JSON text and retained no file snapshots.
    # Current production repair can decode it, including its explicit absolute path.
    encoded = json.dumps(
        [{"path": outside.as_posix(), "old_string": "before", "new_string": 'said "yes"'}]
    ).replace(r"\"yes\"", '"yes"')
    with pytest.raises(json.JSONDecodeError):
        json.loads(encoded)
    async with replay.replay_dispatcher() as dispatch:
        cases: list[tuple[str, Any, dict[str, str | None]]] = [
            ("edit", {"edits": encoded}, {}),
            ("read", {"path": str(outside)}, {}),
            ("write", {"file_path": str(outside), "content": "changed"}, {}),
            ("write", {"path": "../../escaped.txt", "content": "changed"}, {}),
            (
                "apply_patch",
                {"patch": _patch("*** Move File: source.txt -> " + outside.as_posix())},
                {"cwd/source.txt": "before"},
            ),
            (
                "apply_patch",
                {
                    "patch": _patch(
                        "*** Add File: safe.txt",
                        "+safe",
                        "*** Add File: " + outside.as_posix(),
                        "+changed",
                    )
                },
                {},
            ),
            ("read", {"path": "safe.txt"}, {str(outside): "changed"}),
            ("read", {"path": "safe.txt"}, {"../escaped.txt": None}),
            ("bash", {"command": "must never run"}, {}),
        ]
        for tool, arguments, files in cases:
            outcome = await dispatch(arguments, files, set(files), tool=tool)
            assert not outcome.ok and outcome.error_code == "replay_scope", outcome
            assert outcome.files == {}
            assert outside.read_text(encoding="utf-8") == "before"
        # Rewritten absolute and ordinary relative paths within a case still work.
        safe = await dispatch(
            {"path": "{replay-root}/cwd/safe.txt", "content": "after"},
            {"cwd/safe.txt": "before"},
            {"cwd/safe.txt"},
            tool="write",
        )
        assert safe.ok and safe.files == {"cwd/safe.txt": "after"}
        per_item = await dispatch(
            {"edits": [{"path": "safe.txt", "old_string": "before", "new_string": "after"}]},
            {"cwd/safe.txt": "before"},
            {"cwd/safe.txt"},
            tool="edit",
        )
        assert per_item.ok and per_item.files == {"cwd/safe.txt": "after"}


@pytest.mark.asyncio
async def test_replay_refuses_linked_case_directories_without_removing_the_target(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel.txt"
    sentinel.write_text("kept", encoding="utf-8")
    async with replay.replay_dispatcher() as dispatch:
        linked = dispatch._root / "replay"
        try:
            linked.symlink_to(outside, target_is_directory=True)
        except OSError as error:
            pytest.skip(f"directory symlinks unavailable: {error}")
        try:
            outcome = await dispatch(
                {"path": "note.txt", "content": "blocked"}, {}, set(), tool="write"
            )
            assert not outcome.ok and outcome.error_code == "replay_scope", outcome
            assert list(outside.iterdir()) == [sentinel]
            assert sentinel.read_text(encoding="utf-8") == "kept"
        finally:
            linked.unlink()
        # A link at the final file target is rejected before reconstruction too.
        workspace = linked / str(dispatch._count + 1)
        (workspace / "cwd").mkdir(parents=True)
        alias = workspace / "cwd" / "alias.txt"
        alias.symlink_to(sentinel)
        try:
            outcome = await dispatch(
                {"path": "alias.txt", "content": "blocked"}, {}, set(), tool="write"
            )
            assert not outcome.ok and outcome.error_code == "replay_scope", outcome
            assert alias.is_symlink()
            assert sentinel.read_text(encoding="utf-8") == "kept"
        finally:
            alias.unlink()
            alias.parent.rmdir()
            workspace.rmdir()


def test_definitions_summary_counts_on_demand_tools_apart_from_the_tool_list() -> None:
    def tool(name: str, tokens: int, *, offered: bool = True, on_demand: bool = False) -> Any:
        return definitions.ToolDefinition(name, {"name": name}, tokens, offered, on_demand)

    view = definitions.AgentView(
        "main",
        100,
        (
            definitions.PromptBlock("core:soul", "Soul.", 70),
            definitions.PromptBlock("tool:load_tools", "Loaded on demand.", 30),
        ),
        (
            tool("read", 10),
            tool("load_tools", 5),
            tool("web_search", 40, on_demand=True),
            tool("status", 20, offered=False),
        ),
    )

    lines = definitions.summary(view).splitlines()

    assert lines[0] == (
        "Agent main: System Prompt 100 tokens, 2 Tool definitions 15 tokens, "
        "together 115 tokens (estimates)"
    )
    assert lines[1] == (
        "1 On-demand Tools: listed in block tool:load_tools (30 tokens); "
        "their definitions (40 tokens) load with load_tools"
    )
    assert [line.split()[0] for line in lines[-4:]] == [
        "load_tools",
        "read",
        "~web_search",
        "*status",
    ]


def test_definitions_show_the_model_facing_shell_name(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["definitions", "--show", "bash"]) == 0

    output = capsys.readouterr().out
    assert f"== Tool {model_tool_name('bash')} " in output
    definition = json.loads(output.split("-- as sent\n", 1)[1])
    assert definition["name"] == model_tool_name("bash")
