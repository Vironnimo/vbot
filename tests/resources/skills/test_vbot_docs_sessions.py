"""The vbot-docs Session script reads canonical Session history read-only."""

import asyncio
import importlib.util
import json
import sqlite3
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from core.chat import ChatMessage
from core.chat.content_blocks import TextBlock
from core.chat.messages import ToolCall
from core.sessions import ChatSessionManager

pytestmark = pytest.mark.usefixtures("current_format_data_directory")

SCRIPT_PATH = Path(__file__).resolve().parents[3] / "resources/skills/vbot-docs/scripts/sessions.py"


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("vbot_docs_sessions", SCRIPT_PATH)
    if spec is None or spec.loader is None:
        raise AssertionError(f"Unable to load {SCRIPT_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    return module


def test_sessions_script_reads_scope_current_history_forks_blocks_and_exact_results(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("VBOT_RUN_AGENT_ID", "VBOT_RUN_SESSION_ID", "VBOT_RUN_PROJECT_ID"):
        monkeypatch.delenv(name, raising=False)
    sessions = ChatSessionManager(tmp_path)
    session = sessions.create("agent-id", session_id="session-id")
    obsolete = ChatMessage.user("old discarded text")
    user = ChatMessage.user([TextBlock(type="text", text="Unicode Grüße")])
    assistant = ChatMessage.assistant(
        model="test", content="answer", tool_calls=[ToolCall(id="call", name="bash")]
    )
    result = ChatMessage.tool(
        tool_call_id="call", name="bash", content="prefix " * 500 + "exact fragment\n" + "x" * 3000
    )
    session.append(obsolete)
    session.apply_edit(obsolete.id, [user])
    session.append_many([assistant, result])
    sessions.create("agent-id", session_id="session-id", project_id="other").append(
        ChatMessage.user("wrong Project")
    )
    fork = asyncio.run(sessions.fork(session.address))
    fork.append(ChatMessage.assistant(model="test", content="fork only"))
    script = _load_script()

    def run(*arguments: str) -> tuple[str, list[dict[str, Any]]]:
        assert script.main([*arguments, "--data-dir", str(tmp_path)]) == 0
        summary, *lines = capsys.readouterr().out.splitlines()
        return summary, [json.loads(line) for line in lines]

    try:
        database = script._open(tmp_path / "sessions.db")
        with pytest.raises(sqlite3.OperationalError):
            database.execute("DELETE FROM entries")
        database.close()

        _, transcript = run("transcript", "--agent", "agent-id", "--session", "session-id")
        assert [item["message_id"] for item in transcript] == [user.id, assistant.id]
        assert transcript[0]["content"][0]["text"] == "Unicode Grüße"
        summary, first_page = run(
            "transcript", "--agent", "agent-id", "--session", "session-id", "--limit", "1"
        )
        assert f"--after-seq {first_page[0]['seq']}" in summary

        _, hits = run("search", "EXACT fragment", "--agent", "agent-id", "--session", "session-id")
        assert [item["message_id"] for item in hits] == [result.id]
        assert len(hits[0]["preview"]) == 2000
        assert "exact fragment" in hits[0]["preview"]
        assert hits[0]["generation_id"] == transcript[0]["generation_id"]
        _, unicode_hits = run("search", "grüsse", "--agent", "agent-id", "--session", "session-id")
        assert [item["message_id"] for item in unicode_hits] == [user.id]

        _, exact = run(
            "result",
            "--agent",
            "agent-id",
            "--session",
            "session-id",
            "--seq",
            str(hits[0]["seq"]),
            "--generation",
            hits[0]["generation_id"],
        )
        assert exact[0]["content"] == result.content

        # Inside a Run the script defaults to the Run's Session; a fork reads the
        # history it shares with its origin, then its own.
        monkeypatch.setenv("VBOT_RUN_AGENT_ID", "agent-id")
        monkeypatch.setenv("VBOT_RUN_SESSION_ID", fork.id)
        _, forked = run("transcript")
        assert [item["message_id"] for item in forked[:2]] == [user.id, assistant.id]
        assert [item["content"] for item in forked[2:]] == ["fork only"]
        assert {item["generation_id"] for item in forked} != {transcript[0]["generation_id"]}
        _, across = run("search", "exact fragment", "--all-sessions")
        assert {item["session_id"] for item in across} == {"session-id", fork.id}

        assert (
            script.main(
                [
                    "transcript",
                    "--agent",
                    "agent-id@other",
                    "--session",
                    "missing",
                    "--data-dir",
                    str(tmp_path),
                ]
            )
            == 2
        )
        assert "vbot session list agent-id@other --all" in capsys.readouterr().err
    finally:
        sessions.close()
