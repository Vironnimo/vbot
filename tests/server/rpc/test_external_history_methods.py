"""Import orchestration uses Session and Memory ownership boundaries."""

import hashlib
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
    original = b"\xef\xbb\xbf# Original\r\n\r\nA long source document.\r\n\r\nOther facts."
    path.write_bytes(original)
    params = {
        "source": "claude-code",
        "path": str(path),
        "agent_id": "coder",
        "scope": "agent",
        "entries": ["Keep this fact.", "Keep this fact."],
    }
    preview = await rpc_result(state, "external.memories", source=params["source"], path=str(path))
    digest = preview["items"][0]["digest"]
    assert digest == hashlib.sha256(original).hexdigest()
    changed = original.replace(b"\r\n", b"\n")
    path.write_bytes(changed)
    before = await rpc_result(state, "memory.list", agent_id="coder")
    error = await rpc_error(state, "memory.import", **params, expected_digest=digest)
    assert error["code"] == "external_memory_changed"
    assert await rpc_result(state, "memory.list", agent_id="coder") == before
    assert not (tmp_path / "workspace" / "memory-sources").exists()
    path.write_bytes(original)
    first = await rpc_result(state, "memory.import", **params, expected_digest=digest)
    again = await rpc_result(state, "memory.import", **params)
    assert len(first["entries"]) == len(again["entries"]) == 1
    copied = Path(first["saved_source_path"])
    assert copied.stem == hashlib.sha256(original).hexdigest()
    assert copied.read_bytes() == path.read_bytes() == original
    implicitly_selected = await rpc_result(
        state, "memory.import", **{**params, "scope": "user", "entries": None}
    )
    assert [entry["content"] for entry in implicitly_selected["entries"]] == [
        "# Original",
        "A long source document.",
        "Other facts.",
    ]
    before = await rpc_result(state, "memory.list", agent_id="coder")
    error = await rpc_error(
        state, "memory.import", **{**params, "entries": ["a" * 1900, "b" * 1900, "c" * 1900]}
    )
    assert error["code"]
    assert await rpc_result(state, "memory.list", agent_id="coder") == before


@pytest.mark.asyncio
async def test_memory_import_preserves_facts_when_the_source_copy_cannot_be_saved(
    tmp_path: Path,
) -> None:
    state = make_state(tmp_path, StubAdapter())
    workspace = tmp_path / "workspace"
    state.runtime.agents.update("coder", workspace=str(workspace))
    workspace.mkdir(exist_ok=True)
    blocked = workspace / "memory-sources"
    blocked.write_text("existing file", encoding="utf-8")
    path = tmp_path / "source.md"
    path.write_text("Original source.", encoding="utf-8")
    before = await rpc_result(state, "memory.list", agent_id="coder")

    error = await rpc_error(
        state,
        "memory.import",
        source="claude-code",
        path=str(path),
        agent_id="coder",
        scope="agent",
        entries=["New fact."],
    )

    assert error["code"] == "domain_error"
    assert await rpc_result(state, "memory.list", agent_id="coder") == before
    assert blocked.read_text(encoding="utf-8") == "existing file"
