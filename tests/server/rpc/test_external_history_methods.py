"""Import orchestration uses Session and Memory ownership boundaries."""

import json
from pathlib import Path

import pytest

from core.sessions import SessionAddress
from tests.server.rpc_test_support import StubAdapter, make_state, rpc_error, rpc_result


@pytest.mark.asyncio
async def test_external_session_import_is_idempotent_and_readable(tmp_path: Path):
    state = make_state(tmp_path, StubAdapter())
    path = tmp_path / "source.json"
    path.write_text(
        json.dumps(
            {
                "session": {"id": "s", "title": "Previous work"},
                "messages": [
                    {"role": "user", "content": "hello"},
                    {"role": "assistant", "content": "prior answer"},
                ],
            }
        ),
        encoding="utf-8",
    )
    params = {"source": "hermes", "path": str(path), "source_session_id": "s", "agent_id": "coder"}
    result = await rpc_result(state, "external.import_session", **params)
    again = await rpc_result(state, "external.import_session", **params)
    assert again["session_id"] == result["session_id"]
    assert result["entry_count"] == 2
    saved = state.runtime.chat_sessions.get(
        SessionAddress(None, "coder", result["session_id"])
    ).load()
    assert [m.content for m in saved] == ["hello", "prior answer"]
    assert all(m.external_history["source"] == "hermes" for m in saved)
    error = await rpc_error(
        state, "external.import_session", **{**params, "source_session_id": "missing"}
    )
    assert error["code"] == "external_history_error"


@pytest.mark.asyncio
async def test_memory_import_preserves_original_deduplicates_and_refuses_over_budget(
    tmp_path: Path,
):
    state = make_state(tmp_path, StubAdapter())
    state.runtime.agents.update("coder", workspace=str(tmp_path / "workspace"))
    path = tmp_path / "source.md"
    original = "# Original\n\nA long source document.\n\nOther facts."
    path.write_text(original, encoding="utf-8")
    params = {
        "source": "claude-code",
        "path": str(path),
        "agent_id": "coder",
        "scope": "agent",
        "entries": ["Keep this fact.", "Keep this fact."],
    }
    first = await rpc_result(state, "memory.import", **params)
    again = await rpc_result(state, "memory.import", **params)
    assert len(first["entries"]) == len(again["entries"]) == 1
    assert Path(first["saved_source_path"]).read_text(encoding="utf-8") == original
    assert path.read_text(encoding="utf-8") == original
    before = await rpc_result(state, "memory.list", agent_id="coder")
    error = await rpc_error(
        state, "memory.import", **{**params, "entries": ["a" * 1900, "b" * 1900, "c" * 1900]}
    )
    assert error["code"]
    assert await rpc_result(state, "memory.list", agent_id="coder") == before
