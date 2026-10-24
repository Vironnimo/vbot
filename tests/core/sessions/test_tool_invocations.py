"""Own canonical Tool admissions, Result linkage and old outcome evidence."""

from __future__ import annotations

import json

import pytest

from core.chat import ChatMessage
from core.chat.errors import ChatSessionError
from core.chat.messages import ToolCall
from core.sessions import SessionAddress, ToolResultFacts, _store_tool_invocations
from core.sessions._types import SessionRunCompletion
from core.sessions.errors import SessionNotFoundError

_TIME = "2026-10-25T12:00:00.000000Z"
_SUCCESS = json.dumps({"ok": True, "error": None, "data": {}, "artifacts": []})


def _finish(writer, status="completed"):
    writer._store.execution.finish_run(
        writer.address,
        SessionRunCompletion(
            run_id=writer.run_id,
            status=status,
            timing={"started_at": _TIME, "completed_at": _TIME, "duration_ms": 0},
            iteration_count=1,
        ),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("termination", ["cancelled", "restart"])
async def test_started_calls_count_without_results_and_declarations_do_not(manager, termination):
    session = manager.create("coder", session_id="source")
    writer = session.start_run("run-one")
    writer.append_many(
        [
            ChatMessage.user("go"),
            ChatMessage.assistant(
                model="test",
                content=None,
                tool_calls=[
                    ToolCall(id="one", name="probe", arguments={}),
                    ToolCall(id="two", name="probe", arguments={}),
                    ToolCall(id="declared-only", name="probe", arguments={}),
                ],
            ),
        ]
    )
    before = session.load_since()
    first = await writer.start_tool_invocation_async(call_id="one", name="probe", started_at=_TIME)
    second = await writer.start_tool_invocation_async(call_id="two", name="probe", started_at=_TIME)
    started = session.tool_invocations_since()
    assert [item.invocation_key for item in started.invocations] == [first, second]
    assert all(item.status == "running" and item.result_ok is None for item in started.invocations)
    delta = session.load_since(before.cursor)
    assert delta.messages == () and delta.cursor.next_seq == before.cursor.next_seq

    # Even an unbound Result append keeps the matched call's admitted Run.
    session.append_many(
        [ChatMessage.tool(tool_call_id="one", name="probe", content=_SUCCESS)],
        tool_results={"one": ToolResultFacts("completed", ok=True, invocation_key=first)},
    )
    changed = session.tool_invocations_since(started.revision)
    assert [(item.invocation_key, item.result_ok) for item in changed.invocations] == [
        (first, True)
    ]
    assert changed.invocations[0].run_id == "run-one"
    if termination == "restart":
        manager._store.execution.recover_interrupted_runs()
    else:
        _finish(writer, "cancelled")
    final = session.tool_invocations_since()
    assert len(final.invocations) == 2
    assert [(item.status, item.result_ok) for item in final.invocations] == [
        ("completed", True),
        ("interrupted" if termination == "restart" else "cancelled", None),
    ]
    assert session.tool_invocations_since(final.revision).invocations == ()


@pytest.mark.asyncio
async def test_invocation_result_link_is_exact_and_transactional(manager):
    session = manager.create("coder", session_id="source")
    writer = session.start_run("run-one")
    writer.append(
        ChatMessage.assistant(
            model="test", content=None, tool_calls=[ToolCall(id="one", name="probe", arguments={})]
        )
    )
    key = await writer.start_tool_invocation_async(call_id="one", name="probe", started_at=_TIME)
    before = session.load_since()
    result = ChatMessage.tool(tool_call_id="one", name="probe", content=_SUCCESS)
    with pytest.raises(ChatSessionError):
        writer.append_many(
            [result],
            tool_results={"one": ToolResultFacts("completed", ok=True, invocation_key=key + 10)},
        )
    after = session.load_since()
    assert after.cursor == before.cursor
    [invocation] = session.tool_invocations_since().invocations
    assert invocation.status == "running" and invocation.result_ok is None
    writer.append_many(
        [result], tool_results={"one": ToolResultFacts("completed", ok=True, invocation_key=key)}
    )
    assert len(session.tool_invocations_since().invocations) == 1


@pytest.mark.asyncio
async def test_invocations_follow_own_generation_without_fork_inheritance(manager):
    session = manager.create("coder", session_id="source")
    writer = session.start_run("run-one")
    user = ChatMessage.user("go")
    writer.append(user)
    first = await writer.start_tool_invocation_async(call_id="one", name="probe", started_at=_TIME)
    _finish(writer)
    writer = session.start_run("run-two")
    second = await writer.start_tool_invocation_async(call_id="one", name="probe", started_at=_TIME)
    _finish(writer)
    assert first != second
    session.apply_edit(user.id, [ChatMessage.user("replacement")])
    assert len(session.tool_invocations_since().invocations) == 2
    session = await manager.move(session.address, SessionAddress(None, "coder", "moved"))
    assert len(session.tool_invocations_since().invocations) == 2
    generation = session.load_since().cursor.generation_id
    fork = await manager.fork(session.address)
    assert fork.tool_invocations_since().invocations == ()
    await manager.archive(session.address)
    recreated = manager.create("coder", session_id="moved")
    assert recreated.tool_invocations_since().invocations == ()
    with pytest.raises(SessionNotFoundError):
        recreated.tool_invocations_since(expected_generation_id=generation)


@pytest.mark.asyncio
async def test_result_evidence_from_materialized_inherited_calls_never_becomes_own_work(manager):
    source = manager.create("coder", session_id="source")
    source.append(
        ChatMessage.assistant(
            model="test",
            content=None,
            tool_calls=[ToolCall(id="inherited", name="probe", arguments={})],
        )
    )
    fork = await manager.fork(source.address)
    manager.delete(source.address)
    fork.append(ChatMessage.tool(tool_call_id="inherited", name="probe", content=_SUCCESS))
    assert fork.tool_invocations_since().invocations == ()
    manager._store.database.write(_store_tool_invocations.backfill)
    assert fork.tool_invocations_since().invocations == ()


def test_backfill_retains_only_outcome_evidence_and_is_idempotent(manager):
    session = manager.create("coder", session_id="source")
    writer = session.start_run("run-one")
    # Cross the migration's fetch boundary; one Session still gets one revision.
    proven_ids = [f"returned-{index}" for index in range(257)]
    writer.append(
        ChatMessage.assistant(
            model="test",
            content=None,
            tool_calls=[
                *(ToolCall(id=call_id, name="probe", arguments={}) for call_id in proven_ids),
                ToolCall(id="never-returned", name="probe", arguments={}),
                ToolCall(id="synthetic-stop", name="probe", arguments={}),
            ],
        )
    )
    session.append_many(
        [
            ChatMessage.tool(tool_call_id=call_id, name="probe", content=_SUCCESS)
            for call_id in proven_ids
        ]
    )
    assert all(item.run_id == "run-one" for item in session.tool_invocations_since().invocations)
    writer.append(
        ChatMessage.tool(
            tool_call_id="synthetic-stop",
            name="probe",
            content=json.dumps(
                {
                    "ok": False,
                    "data": None,
                    "artifacts": [],
                    "error": {"code": "tool_stopped", "message": "Stopped", "retryable": False},
                }
            ),
        )
    )
    _finish(writer, "interrupted")

    def old_data(connection):
        connection.execute("DELETE FROM tool_invocations")
        _store_tool_invocations.backfill(connection)
        _store_tool_invocations.backfill(connection)

    manager._store.database.write(old_data)
    batch = session.tool_invocations_since()
    assert len(batch.invocations) == len(proven_ids)
    assert all(item.name == "probe" and item.result_ok is True for item in batch.invocations)
    revision = batch.revision
    assert {item.source_revision for item in batch.invocations} == {revision}
    manager._store.database.write(_store_tool_invocations.backfill)
    assert session.tool_invocations_since().revision == revision
