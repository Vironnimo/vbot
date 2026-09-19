"""Execute the shipped SQLite recipes against canonical Session fixtures."""

import json
import re
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from core.chat import ChatMessage
from core.chat.content_blocks import TextBlock
from core.chat.messages import ToolCall
from core.sessions import ChatSessionManager

pytestmark = pytest.mark.usefixtures("current_format_data_directory")


def test_session_sql_recipes_preserve_scope_active_lineage_blocks_and_exact_results(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
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
    session.append_many([obsolete, ChatMessage.history_edit(obsolete.id), user, assistant, result])
    sessions.create("agent-id", session_id="session-id", project_id="other").append(
        ChatMessage.user("wrong Project")
    )
    document = (
        Path(__file__).resolve().parents[3]
        / "resources/skills/vbot-cli/references/session-search.md"
    ).read_text(encoding="utf-8")
    blocks = re.findall(r"```python\n(.*?)\n```", document, re.DOTALL)
    namespace: dict[str, Any] = {}
    setup = blocks[0].replace(
        '"/absolute/data_dir/sessions.db"', repr(str(tmp_path / "sessions.db"))
    )
    exec(setup, namespace)
    db = namespace["db"]
    try:
        with pytest.raises(sqlite3.OperationalError):
            db.execute("DELETE FROM messages")
        exec(blocks[1], namespace)
        transcript = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
        assert [item["message_id"] for item in transcript] == [user.id, assistant.id]
        assert transcript[0]["content"][0]["text"] == "Unicode Grüße"
        exec(blocks[2], namespace)
        hits = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
        assert [item["message_id"] for item in hits] == [result.id]
        assert len(hits[0]["preview"]) == 2000
        assert "exact fragment" in hits[0]["preview"]
        assert hits[0]["generation_id"] == transcript[0]["generation_id"]
        exact = (
            blocks[3]
            .replace("sequence = 123", f"sequence = {hits[0]['seq']}")
            .replace('"generation-id"', repr(transcript[0]["generation_id"]))
        )
        exec(exact, namespace)
        assert json.loads(capsys.readouterr().out)["result_content"] == result.content
    finally:
        db.close()
        sessions.close()
