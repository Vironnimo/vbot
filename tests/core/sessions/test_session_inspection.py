"""Compact activity and bounded transcript contracts at the Sessions boundary."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from core.chat import ChatMessage, ChatSessionError
from core.chat.messages import ToolCall
from core.runs import RunKind
from core.sessions import SessionListFilters, ToolResultFacts
from core.subagents.links import link_session


def test_inspection_keeps_tool_outcomes_and_exact_text_without_large_payloads(manager):
    session = manager.create("coder", session_id="inspect")
    session.append(ChatMessage.user("Implement the requested change"))
    assistant = ChatMessage.assistant(
        model="test",
        content="Changing files",
        reasoning="PRIVATE REASONING",
        tool_calls=[
            ToolCall(id="patch", name="apply_patch", arguments={"patch": "diff" * 8000}),
            ToolCall(id="pending", name="bash", arguments={"command": "check"}),
            ToolCall(id="background", name="shell", arguments={"command": "validate"}),
        ],
    )
    session.append(assistant)
    session.assistant_message_id = assistant.id
    content = json.dumps(
        {
            "ok": False,
            "error": {"code": "test_error", "message": "write refused"},
            "data": None,
            "padding": "x" * 20000,
        }
    )
    result = ChatMessage.tool(tool_call_id="patch", name="apply_patch", content=content)
    result = replace(
        result,
        tool_display={
            "summary": "src/one.py",
            "details": [
                {
                    "type": "file_changes",
                    "files": [
                        {"path": "src/one.py", "change": "updated", "added": 2, "removed": 1}
                    ],
                }
            ],
        },
    )
    session.append_many(
        [result], tool_results={"patch": ToolResultFacts("failed", False, error_code="test_error")}
    )
    background = ChatMessage.tool(
        tool_call_id="background",
        name="shell",
        content=json.dumps(
            {"ok": True, "data": {"status": "running", "command_id": "cmd-still-running"}}
        ),
    )
    session.append_many(
        [background], tool_results={"background": ToolResultFacts("completed", True)}
    )
    session.append(ChatMessage.note("PRIVATE NOTE"))
    session.append(ChatMessage.assistant(model="test", content="The change failed"))

    page = manager.inspection_page(session.address, summary=True)
    text = json.dumps(page)
    assert "PRIVATE" not in text and "padding" not in text and "diffdiff" not in text
    assert page["task"]["text"] == "Implement the requested change"
    assert page["latest_answer"]["text"] == "The change failed"
    tool = next(entry for entry in page["entries"] if entry.get("tool"))
    assert tool["status"] == "failed" and tool["error"] == "write refused"
    background_entry = next(entry for entry in page["entries"] if entry.get("tool") == "shell")
    assert background_entry["status"] == "completed"
    assert background_entry["result_status"] == "running"
    assert background_entry["command_id"] == "cmd-still-running"
    assert tool["file_changes"] == [
        {
            "path": "src/one.py",
            "path_truncated": False,
            "change": "updated",
            "added": 2,
            "removed": 1,
        }
    ]
    pending = next(entry for entry in page["entries"] if entry.get("pending_calls"))
    assert pending["pending_calls"][0]["name"] == "bash"
    assert pending["pending_calls"][0]["status"] == "pending"
    pieces = []
    offset = 0
    while True:
        detail = manager.inspection_page(session.address, entry=tool["seq"], offset=offset)
        pieces.append(detail["entries"][0]["text"])
        if "next_offset" not in detail:
            break
        assert detail["next_offset"] > offset
        offset = detail["next_offset"]
    assert "".join(pieces) == content


@pytest.mark.asyncio
@pytest.mark.parametrize("summary", [False, True])
async def test_inspection_pages_current_lineage_and_rejects_changed_cursors(manager, summary):
    source = manager.create("coder", session_id="source")
    first = ChatMessage.user("Inherited task")
    source.append(first)
    source.append(ChatMessage.assistant(model="test", content="Inherited answer"))
    fork = manager.get((await manager.fork(source.address)).address)
    question = ChatMessage.user("Own task")
    fork.append(question)
    fork.append(ChatMessage.assistant(model="test", content="Own answer"))
    page = manager.inspection_page(fork.address, summary=summary, limit=2)
    older = manager.inspection_page(
        fork.address, summary=summary, limit=2, cursor=page["next_cursor"]
    )
    assert [item["text"] for item in older["entries"]] == ["Inherited task", "Inherited answer"]
    assert older["next_cursor"] is None
    assert [item["text"] for item in page["entries"]] == ["Own task", "Own answer"]
    fork.apply_edit(question.id, [ChatMessage.user("Replacement")])
    with pytest.raises(ChatSessionError):
        manager.inspection_page(fork.address, summary=summary, cursor=page["next_cursor"])
    source.apply_edit(first.id, [ChatMessage.user("Changed source")])
    assert manager.inspection_page(fork.address)["entries"][0]["text"] == "Inherited task"
    manager.delete(fork.address)
    replacement = manager.create(fork.address.agent_id, session_id=fork.id)
    with pytest.raises(ChatSessionError):
        manager.inspection_page(replacement.address, summary=summary, cursor=page["next_cursor"])


def test_global_session_discovery_and_parent_filter_preserve_exact_owners(manager):
    root = manager.create("coder", session_id="same")
    other = manager.create("reviewer", session_id="same", project_id="repo")
    child = manager.create("helper", session_id="child", metadata={"is_subagent_session": True})
    manager.create("helper", session_id="cron", run_kind=RunKind.CRON)
    manager.create("helper", session_id="hidden", metadata={"ephemeral": True})
    link_session(
        manager,
        child.address,
        subagent_id="child-ref",
        parent=root.address,
        run_id="parent-run",
        tool_call_id="parent-call",
        tool_call_index=0,
    )
    assert set(manager.addresses_by_id("same")) == {root.address, other.address}
    cursor = None
    seen = []
    while True:
        page = manager.list_summaries_page(None, limit=1, cursor=cursor)
        seen.extend((row["id"], row["agent_id"], row["project_id"]) for row in page.sessions)
        assert page.total_count == 4
        cursor = page.next_cursor
        if cursor is None:
            break
    assert len(seen) == len(set(seen)) == 4
    children = manager.list_summaries_page(
        None, limit=1, filters=SessionListFilters(parent=root.address)
    )
    assert [row["id"] for row in children.sessions] == [child.id]
    assert children.total_count == 1


def test_inspection_run_selection_keeps_task_and_answer_in_scope(manager):
    session = manager.create("coder", session_id="runs")
    for run_id in ("first-run", "second-run"):
        writer = session.start_run(run_id)
        writer.append_many(
            [
                ChatMessage.user(f"Task for {run_id}"),
                ChatMessage.assistant(model="test", content=f"Answer for {run_id}"),
            ]
        )
    page = manager.inspection_page(session.address, summary=True, run_id="first-run", limit=1)
    assert page["task"]["text"] == "Task for first-run"
    assert page["latest_answer"]["text"] == "Answer for first-run"
    assert all(item["run_id"] == "first-run" for item in page["entries"])
    with pytest.raises(ChatSessionError):
        manager.inspection_page(
            session.address, summary=True, run_id="second-run", cursor=page["next_cursor"]
        )


@pytest.mark.asyncio
async def test_fork_keeps_call_when_parent_result_is_outside_its_history(manager):
    source = manager.create("coder", session_id="source")
    source.append(ChatMessage.user("Run validation"))
    declaration = ChatMessage.assistant(
        model="test",
        content=None,
        tool_calls=[ToolCall(id="check", name="shell", arguments={"command": "validate"})],
    )
    source.append(declaration)
    fork = (await manager.fork(source.address)).address
    before = manager.inspection_page(fork, summary=True)["entries"][-1]["pending_calls"]
    assert before[0]["status"] == "unresolved"
    source.append_many(
        [ChatMessage.tool(tool_call_id="check", name="shell", content='{"ok": true}')],
        assistant_message_id=declaration.id,
        tool_results={"check": ToolResultFacts("completed", True)},
    )
    for selection in ({"summary": True}, {}, {"entry": 1}):
        page = manager.inspection_page(fork, **selection)
        calls = [call for item in page["entries"] for call in item.get("pending_calls", [])]
        assert len(calls) == 1
        assert calls[0]["name"] == "shell" and calls[0]["status"] == "unresolved"
        assert calls == before
    assert not any(
        item.get("pending_calls") for item in manager.inspection_page(source.address)["entries"]
    )
    manager.delete(source.address)
    assert manager.inspection_page(fork, summary=True)["entries"][-1]["pending_calls"] == before
