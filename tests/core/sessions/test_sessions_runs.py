"""Run identity, single-row Tool completion, and durable lifecycle boundaries."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import sqlite3

import pytest

from core.chat import ChatMessage
from core.chat.errors import ChatSessionError
from core.chat.messages import ToolCall
from core.runs import ChatRunManager, Run, RunAdmission, RunInterruptedError, RunKind
from core.sessions import (
    SESSION_RUN_KINDS_META_KEY,
    SessionAddress,
    SessionListFilters,
    SessionRunRecord,
)
from core.sessions._types import SessionRunCompletion
from core.sessions.errors import SessionNotFoundError
from tests.core.sessions.history_fixtures import complete_run


@pytest.mark.asyncio
@pytest.mark.parametrize("policy,expected", [("all", 2), ("errors", 1), ("muted", 0)])
async def test_notification_policy_filters_attention_without_marking_results_read(
    manager, policy, expected
):
    session = manager.create("coder")
    manager.notifications.set_policy(session.address, policy)
    for index, status in enumerate(("completed", "failed")):
        run = Run(run_id=f"result_{index}", agent_id="coder", session_id=session.id)
        await manager.start_run(run)
        result = await manager.finish_run(
            run, status, {"timing": _summary(run.id).timing}, completion_reason=None
        )
        assert result["notification_allowed"] == (
            policy == "all" or policy == "errors" and status == "failed"
        )
    row = manager.summary(session.address)
    assert row["notification_count"] == expected
    assert row["local_unread_count"] == 2
    # Changing the rule also filters already unread results; it never reads them.
    manager.notifications.set_policy(session.address, "all")
    assert manager.summary(session.address)["notification_count"] == 2
    manager.notifications.mark_read(session.address, ["result_1"])
    assert manager.summary(session.address)["notification_count"] == 1
    assert manager.summary(session.address)["unread_run_id"] == "result_0"
    assert manager.summary(session.address)["has_unread_completion"] is True
    manager.notifications.mark_read(session.address, ["result_0"])
    assert manager.summary(session.address)["local_unread_count"] == 0


@pytest.mark.asyncio
async def test_schedule_history_and_read_state_follow_each_runs_source(manager):
    session = manager.create("coder")
    manager.notifications.set_policy(session.address, "muted")
    for index in range(7):
        run = Run(
            run_id=f"scheduled_{index}",
            agent_id="coder",
            session_id=session.id,
            run_kind=RunKind.SYSTEM if index == 6 else RunKind.CRON,
            schedule_id="schedule_a",
            notification_policy=lambda: "all",
        )
        await manager.start_run(run)
        result = await manager.finish_run(
            run, "completed", {"timing": _summary(run.id).timing}, completion_reason=None
        )
        assert result["schedule_id"] == "schedule_a"
        assert result["notification_allowed"] is True
    assert manager.summary(session.address)["notification_count"] == 0
    assert (
        manager.list_summaries_page(
            [(None, "coder")], limit=10, filters=SessionListFilters(include_cron=False)
        ).sessions
        == ()
    )
    page = manager.notifications.schedule_runs("schedule_a")
    assert [run["run_id"] for run in page["runs"]] == [f"scheduled_{i}" for i in range(6, 1, -1)]
    assert all(run["unread"] for run in page["runs"])
    older = manager.notifications.schedule_runs("schedule_a", before=int(page["next_before"]))
    assert [run["run_id"] for run in older["runs"]] == ["scheduled_1", "scheduled_0"]
    assert older["next_before"] is None
    # Reading an older result updates both surfaces, and does not read a newer one.
    await manager.mark_terminal_run_read_async(session.address, "scheduled_1")
    assert manager.notifications.schedule_activity()["schedule_a"]["unread_count"] == 6
    busy = manager.notifications.schedule_activity([session.address])["schedule_a"]
    assert busy["unread_count"] == 6
    assert busy["visible_unread_count"] == 0
    through = manager.notifications.schedule_activity()["schedule_a"]["read_through"]
    later = Run(
        run_id="later",
        agent_id="coder",
        session_id=session.id,
        run_kind=RunKind.CRON,
        schedule_id="schedule_a",
    )
    await manager.start_run(later)
    await manager.finish_run(
        later, "completed", {"timing": _summary(later.id).timing}, completion_reason=None
    )
    assert manager.notifications.mark_schedule_read("schedule_a", through) == 6
    assert manager.notifications.schedule_activity()["schedule_a"]["unread_count"] == 1


def _summary(run_id: str, change_stats: dict[str, object] | None = None) -> ChatMessage:
    return ChatMessage.run_summary(
        run_id=run_id,
        status="completed",
        iteration_count=1,
        change_stats=change_stats,
        timing={
            "started_at": "2026-09-19T10:00:00Z",
            "completed_at": "2026-09-19T10:00:01Z",
            "duration_ms": 1000,
        },
    )


@pytest.mark.asyncio
async def test_completion_commits_entities_before_publishing_terminal_event(manager):
    session = manager.create("coder")
    runs = ChatRunManager(persistence=manager)

    async def execute(run):
        writer = session.for_run(run.id)
        assistant = ChatMessage.assistant(
            model="test",
            content=None,
            tool_calls=[ToolCall(id="call", name="read", arguments={"path": "x"})],
        )
        writer.append_many([ChatMessage.user("read"), assistant])
        writer.assistant_message_id = assistant.id
        writer.append(
            ChatMessage.tool(
                tool_call_id="call", name="read", content='{"ok":true,"data":{"text":"result"}}'
            )
        )
        answer = ChatMessage.assistant(model="test", content="done")
        writer.append(answer)
        return answer

    run = await runs.start(session.address, execute)
    await run.wait()
    assert run.events[-1].payload["history_persisted"] is True
    with sqlite3.connect(manager._store.path) as connection:
        assert connection.execute(
            "SELECT DISTINCT r.run_id FROM entries AS e JOIN runs AS r ON r.run_key = e.run_key"
        ).fetchall() == [(run.id,)]
        assert connection.execute("SELECT status FROM runs").fetchall() == [("completed",)]
        # One Tool call row carries the result's outcome and points at its result entry.
        assert connection.execute(
            "SELECT c.status, t.content FROM tool_calls AS c "
            "JOIN entry_text AS t ON t.entry_key = c.result_entry_key"
        ).fetchall() == [("completed", '{"ok":true,"data":{"text":"result"}}')]
        assert connection.execute("SELECT role FROM entries ORDER BY seq").fetchall() == [
            ("user",),
            ("assistant",),
            ("tool",),
            ("assistant",),
            ("run_summary",),
        ]
        assert connection.execute(
            "SELECT e.role FROM runs AS r JOIN entries AS e ON e.entry_key = r.end_entry_key"
        ).fetchall() == [("run_summary",)]
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name IN ('tool_messages','run_summaries','run_execution_starts')"
            ).fetchall()
            == []
        )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    snapshot = session.read_chat_history_snapshot(limit=50)
    assert snapshot.runs[0]["complete"] is True
    assert session.load_run_result(run_id=run.id).assistant.content == "done"
    await runs.aclose()


@pytest.mark.asyncio
async def test_run_admission_records_its_run_kind_in_the_same_transaction(manager, monkeypatch):
    session = manager.create("coder")
    runs = ChatRunManager(persistence=manager)
    writes = 0
    execute_write = manager._store._execute_write

    def counting_write(*args, **kwargs):
        nonlocal writes
        writes += 1
        return execute_write(*args, **kwargs)

    monkeypatch.setattr(manager._store, "_execute_write", counting_write)

    async def execute(run):
        assert writes == 1
        assert manager.get_metadata(session.address)[SESSION_RUN_KINDS_META_KEY] == ["cron"]
        return None

    run = await runs.start(session.address, execute, admission=RunAdmission(run_kind=RunKind.CRON))
    await run.wait()

    assert manager.get_metadata(session.address)[SESSION_RUN_KINDS_META_KEY] == ["cron"]


@pytest.mark.asyncio
@pytest.mark.parametrize("session_state", ["live", "archived", "recreated", "create_missing"])
async def test_run_admission_requires_the_expected_live_generation(manager, session_state):
    session = manager.create("coder")
    generation_id = manager.get(session.address).generation_id
    if session_state != "live":
        await manager.archive(session.address)
    if session_state == "recreated":
        replacement = manager.create("coder", session_id=session.id)
        replacement.append(ChatMessage.user("Replacement history"))
    runs = ChatRunManager(persistence=manager)
    executed = False

    async def execute(run):
        nonlocal executed
        executed = True

    run = await runs.start(
        session.address,
        execute,
        admission=RunAdmission(
            expected_session_generation_id=(
                None if session_state == "create_missing" else generation_id
            )
        ),
    )
    if session_state in {"live", "create_missing"}:
        await run.wait_admitted()
        await run.wait()
        assert executed
        assert manager.get(session.address).find_run_summary(run_id=run.id).status == "completed"
    else:
        with pytest.raises(ChatSessionError):
            await run.wait_admitted()
        with pytest.raises(ChatSessionError):
            await run.wait()
        assert not executed
        assert run.events[-1].payload["history_persisted"] is False
        if session_state == "recreated":
            assert [message.content for message in replacement.load()] == ["Replacement history"]
        else:
            assert not manager.exists(session.address)
    await runs.aclose()


@pytest.mark.asyncio
async def test_restart_settles_run_streamed_output_and_unfinished_calls_once(
    manager, caplog: pytest.LogCaptureFixture
):
    session = manager.create("coder").start_run("abandoned")
    session.append(
        ChatMessage.assistant(
            model="test", content=None, tool_calls=[ToolCall(id="call", name="write", arguments={})]
        )
    )
    # The next Model step streamed output that no Assistant entry holds yet.
    await session.append_stream_draft_async(
        model="first", reasoning_delta="Plan ", content_delta="Half"
    )
    # A Tool Call started while the step still streamed.
    await session.append_stream_draft_async(
        model="second",
        reasoning_delta="more.",
        content_delta=" an answer",
        tool_calls=[{"id": "started", "name": "read", "arguments": {"path": "a.txt"}}],
    )
    with caplog.at_level(logging.WARNING, logger="vbot.sessions"):
        manager.recover_interrupted_runs()
        first = session.load()
        manager.recover_interrupted_runs()
    assert session.load() == first
    # The settled Run gets its one terminal line, as the Run manager writes for others.
    [line] = [record for record in caplog.records if record.name == "vbot.sessions"]
    assert line.levelno == logging.WARNING
    fields = dict(re.findall(r"(\w+)=([^\s)]+)", line.getMessage()))
    assert {
        "run": "abandoned",
        "agent": "coder",
        "session": session.id,
        "kind": "user",
        "cause": "process_restart",
    }.items() <= fields.items()
    partial = first[-2]
    assert (partial.role, partial.model, partial.content, partial.reasoning) == (
        "assistant",
        "second",
        "Half an answer",
        "Plan more.",
    )
    assert partial.tool_calls == [ToolCall(id="started", name="read", arguments={"path": "a.txt"})]
    assert (partial.interrupted, partial.interruption_cause) == (True, "process_restart")
    summary = session.find_run_summary(run_id="abandoned")
    assert (summary.status, summary.completion_reason) == ("interrupted", "process_restart")
    assert first[-1] == summary
    with sqlite3.connect(manager._store.path) as connection:
        assert connection.execute("SELECT status, result_entry_key FROM tool_calls").fetchall() == [
            ("interrupted", None),
            ("interrupted", None),
        ]
        assert connection.execute("SELECT count(*) FROM run_stream_drafts").fetchone() == (0,)


@pytest.mark.asyncio
async def test_a_stream_draft_lasts_until_the_runs_next_assistant_entry(manager):
    session = manager.create("coder")
    writer = session.start_run("run")

    async def draft(content: str) -> None:
        await writer.append_stream_draft_async(
            model="test", reasoning_delta="", content_delta=content
        )

    def stored() -> list[tuple[str, ...]]:
        with sqlite3.connect(manager._store.path) as connection:
            return connection.execute(
                "SELECT content_delta FROM run_stream_drafts ORDER BY chunk_key"
            ).fetchall()

    await draft("restarted")
    await writer.discard_stream_draft_async()
    assert stored() == []
    await draft("streamed")
    writer.append(ChatMessage.user("steering"))
    assert stored() == [("streamed",)]
    writer.append(ChatMessage.assistant(model="test", content="streamed"))
    assert stored() == []
    await draft("next step")
    await manager.finish_run(
        Run(run_id="run", agent_id="coder", session_id=session.id),
        "cancelled",
        {
            "timing": {
                "started_at": "2026-09-19T10:00:00Z",
                "completed_at": "2026-09-19T10:00:01Z",
                "duration_ms": 1000,
            }
        },
        completion_reason="user",
    )
    assert stored() == []
    with pytest.raises(ChatSessionError, match="running Run"):
        await draft("late")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "status", "reason"),
    [
        ("answer", "completed", None),
        ("cancel", "cancelled", "user"),
        ("interrupt", "interrupted", "network"),
    ],
)
async def test_the_run_summary_names_why_a_run_stopped_early(manager, outcome, status, reason):
    session = manager.create("coder")
    runs = ChatRunManager(persistence=manager)
    stopped = asyncio.Event()

    async def execute(run):
        session.for_run(run.id).append(ChatMessage.user("question"))
        if outcome == "interrupt":
            raise RunInterruptedError("network")
        if outcome == "cancel":
            stopped.set()
            await asyncio.Event().wait()
        return "answer"

    run = await runs.start(session.address, execute)
    if outcome == "cancel":
        await stopped.wait()
        await runs.cancel(run.id, reason="user")
    with contextlib.suppress(Exception, asyncio.CancelledError):
        await run.wait()
    summary = session.find_run_summary(run_id=run.id)
    assert (summary.status, summary.completion_reason) == (status, reason)
    await runs.aclose()


@pytest.mark.asyncio
async def test_failed_terminal_transaction_never_claims_persistence(manager, monkeypatch):
    session = manager.create("coder")
    runs = ChatRunManager(persistence=manager)

    async def fail_finish(*args, **kwargs):
        raise OSError("storage unavailable")

    monkeypatch.setattr(manager, "finish_run", fail_finish)

    async def execute(run):
        session.for_run(run.id).append(ChatMessage.assistant(model="test", content="done"))

    run = await runs.start(session.address, execute)
    with pytest.raises(OSError):
        await run.wait()
    assert run.events[-1].payload["history_persisted"] is False
    assert session.find_run_summary(run_id=run.id) is None
    await runs.aclose()


def test_reused_provider_call_id_requires_exact_assistant_identity(manager):
    session = manager.create("coder").start_run("run")
    for text in ("first", "second"):
        assistant = ChatMessage.assistant(
            model="test",
            content=None,
            tool_calls=[ToolCall(id="reused", name="read", arguments={})],
        )
        session.append(assistant)
        session.assistant_message_id = assistant.id
        session.append(ChatMessage.tool(tool_call_id="reused", name="read", content=text))
    with sqlite3.connect(manager._store.path) as connection:
        assert connection.execute(
            "SELECT t.content FROM tool_calls AS c "
            "JOIN entry_text AS t ON t.entry_key = c.result_entry_key ORDER BY c.call_key"
        ).fetchall() == [("first",), ("second",)]


@pytest.mark.asyncio
async def test_fork_ends_before_a_running_run_and_never_executes_it(manager):
    source = manager.create("coder")
    settled = source.start_run("settled")
    settled.append(ChatMessage.user("question"))
    complete_run(settled, _summary("settled"))
    in_flight = source.start_run("in-flight")
    in_flight.append(ChatMessage.assistant(model="test", content="partial"))

    fork = await manager.fork(source.address)
    inherited = [(message.role, message.content) for message in fork.load_active()]
    assert inherited[0] == ("user", "question")
    assert [role for role, _ in inherited] == ["user", "run_summary"]
    assert fork.load() == []

    manager.recover_interrupted_runs()
    assert source.find_run_summary(run_id="in-flight").status == "interrupted"
    assert fork.find_run_summary(run_id="in-flight") is None

    # Deleting the source gives the fork its own copy; a copied Run never runs again.
    manager.delete(source.address)
    assert [(message.role, message.content) for message in fork.load_active()] == inherited
    with sqlite3.connect(manager._store.path) as connection:
        assert connection.execute(
            "SELECT run_id, status, inherited, contributes_to_activity FROM runs"
        ).fetchall() == [("settled", "completed", 1, 0)]
    with pytest.raises(ChatSessionError, match="inherited Run"):
        fork.start_run("settled")


@pytest.mark.asyncio
async def test_settled_run_rejects_late_output_and_duplicate_completion(manager):
    session = manager.create("coder")
    runs = ChatRunManager(persistence=manager)
    observed = []

    async def execute(run):
        async def completed(status):
            observed.append((str(status), session.find_run_summary(run_id=run.id).status))

        run.add_completion_observer(completed)
        session.for_run(run.id).append(ChatMessage.assistant(model="test", content="done"))

    run = await runs.start(session.address, execute)
    await run.wait()
    assert observed == [("completed", "completed")]
    with pytest.raises(ChatSessionError, match="running Run"):
        session.for_run(run.id).append(ChatMessage.assistant(model="test", content="late"))
    with pytest.raises(ChatSessionError, match="running Run"):
        await manager.finish_run(run, "completed", run.events[-1].payload, completion_reason=None)
    await runs.aclose()


def test_snapshot_lists_page_runs_in_start_order(manager):
    session = manager.create("coder")
    for run_id in ("run-b", "run-a"):
        run = session.start_run(run_id)
        run.append(ChatMessage.user(run_id))
        run.append(ChatMessage.assistant(model="test", content="done"))
        complete_run(run, _summary(run_id))
    snapshot = session.read_chat_history_snapshot(limit=50)
    assert [(run["run_id"], run["complete"]) for run in snapshot.runs] == [
        ("run-b", True),
        ("run-a", True),
    ]


@pytest.mark.asyncio
async def test_run_records_read_the_sessions_own_runs_with_completion_facts(manager):
    source = manager.create("coder")
    source.start_run("done").append(ChatMessage.user("question"))
    source._store.execution.finish_run(
        source.address,
        SessionRunCompletion(
            run_id="done",
            status="completed",
            timing={
                "started_at": "2026-09-19T10:00:01Z",
                "completed_at": "2026-09-19T10:00:03Z",
                "duration_ms": 2000,
            },
            iteration_count=3,
            change_stats={"files": 2, "added": 10, "removed": 3, "paths": ["a.py", "b.py"]},
            completion_reason="answered",
        ),
    )
    source.start_run("open")
    done, running = source.run_records()
    assert done == SessionRunRecord(
        "done",
        "user",
        "completed",
        done.started_at,
        "2026-09-19T10:00:03.000000Z",
        2000,
        "2026-09-19T10:00:01.000000Z",
        "answered",
        3,
        2,
        10,
        3,
    )
    assert (running.run_id, running.status, running.completed_at, running.duration_ms) == (
        "open",
        "running",
        None,
        None,
    )

    # A fork's copies of its source's Runs are never its own, even once materialized.
    fork = await manager.fork(source.address)
    fork.start_run("own")
    generation = fork.load_since().cursor.generation_id
    manager.delete(source.address)
    assert [record.run_id for record in fork.run_records(generation)] == ["own"]
    with pytest.raises(SessionNotFoundError):
        fork.run_records("another-generation")


def _changes(*files: tuple[str, int, int]) -> dict[str, object]:
    return {
        "files": len(files),
        "added": sum(added for _path, added, _removed in files),
        "removed": sum(removed for _path, _added, removed in files),
        "paths": [path for path, _added, _removed in files],
        "file_stats": [
            {"path": path, "added": added, "removed": removed} for path, added, removed in files
        ],
    }


@pytest.mark.asyncio
async def test_change_statistics_persist_while_running_and_sum_per_session(manager):
    session = manager.create("coder")
    interrupted = session.start_run("interrupted")
    await interrupted.record_change_stats_async(
        _changes(("/repo/a.py", 2, 1), ("/repo/b.py", 1, 0))
    )
    # A restart keeps what the Run recorded while running.
    manager.recover_interrupted_runs()
    assert session.find_run_summary(run_id="interrupted").change_stats == _changes(
        ("/repo/a.py", 2, 1), ("/repo/b.py", 1, 0)
    )
    with pytest.raises(ChatSessionError):
        await interrupted.record_change_stats_async(_changes(("/repo/a.py", 1, 0)))

    finished = session.start_run("finished")
    await finished.record_change_stats_async(_changes(("/repo/stale.py", 9, 9)))
    # Final statistics replace those recorded while running; these have no per-file counts.
    legacy = {"files": 1, "added": 4, "removed": 0, "paths": ["/repo/legacy.py"]}
    complete_run(session, _summary("finished", legacy))
    assert session.find_run_summary(run_id="finished").change_stats == legacy
    running = session.start_run("running")
    await running.record_change_stats_async(_changes(("/repo/a.py", 3, 3)))

    totals = {"files": 3, "added": 10, "removed": 4}
    assert manager.change_stats(session.address) == {
        **totals,
        "file_stats": [
            {"path": "/repo/a.py", "added": 5, "removed": 4},
            {"path": "/repo/b.py", "added": 1, "removed": 0},
            {"path": "/repo/legacy.py", "added": None, "removed": None},
        ],
    }
    assert manager.summary(session.address)["change_stats"] == totals
    page = manager.list_summaries_page([(None, "coder")], limit=10)
    assert [summary.get("change_stats") for summary in page.sessions] == [totals]

    # A fork's inherited Runs stay its source's; only its own Runs count.
    fork = await manager.fork(session.address)
    assert manager.change_stats(fork.address) is None
    assert "change_stats" not in manager.summary(fork.address)

    # Merged Sessions' own Runs count as the Session's own, each file once;
    # a merged Session no longer live is skipped.
    helper = manager.create("helper")
    await helper.start_run("helper-run").record_change_stats_async(
        _changes(("/repo/a.py", 1, 1), ("/repo/c.py", 2, 0))
    )
    gone = SessionAddress(None, "helper", "gone")
    assert manager.change_stats(session.address, merged=[helper.address, gone]) == {
        "files": 4,
        "added": 13,
        "removed": 5,
        "file_stats": [
            {"path": "/repo/a.py", "added": 6, "removed": 5},
            {"path": "/repo/b.py", "added": 1, "removed": 0},
            {"path": "/repo/c.py", "added": 2, "removed": 0},
            {"path": "/repo/legacy.py", "added": None, "removed": None},
        ],
    }


@pytest.mark.asyncio
async def test_schedule_results_replaced_by_history_edit_leave_attention_and_history(manager):
    session = manager.create("coder")
    run = Run(
        run_id="scheduled",
        agent_id="coder",
        session_id=session.id,
        run_kind=RunKind.CRON,
        schedule_id="schedule_a",
    )
    await manager.start_run(run)
    question = ChatMessage.user("old question")
    session.for_run(run.id).append_many(
        [question, ChatMessage.assistant(model="test", content="old answer")]
    )
    await manager.finish_run(
        run, "completed", {"timing": _summary(run.id).timing}, completion_reason=None
    )
    assert manager.summary(session.address)["local_unread_count"] == 1
    session.apply_edit(question.id, [ChatMessage.user("replacement")])
    assert manager.summary(session.address)["local_unread_count"] == 0
    assert manager.notifications.schedule_runs("schedule_a")["runs"] == []
    assert manager.notifications.schedule_activity() == {}


@pytest.mark.asyncio
async def test_adopting_known_legacy_schedule_run_preserves_read_state_and_other_results(manager):
    session = manager.create("coder")
    for run_id in ("older", "known_schedule", "newer"):
        run = Run(run_id=run_id, agent_id="coder", session_id=session.id)
        await manager.start_run(run)
        await manager.finish_run(
            run, "completed", {"timing": _summary(run.id).timing}, completion_reason=None
        )
    source = [("schedule_a", "coder", None, "known_schedule", "all")]
    manager.notifications.adopt_schedule_runs(source)
    manager.notifications.adopt_schedule_runs(source)
    assert manager.summary(session.address)["notification_count"] == 2
    assert manager.notifications.schedule_activity()["schedule_a"]["unread_count"] == 1
    manager.notifications.mark_read(session.address, ["known_schedule"])
    manager.notifications.adopt_schedule_runs(source)
    assert manager.notifications.schedule_activity()["schedule_a"]["unread_count"] == 0
    assert [row["run_id"] for row in manager.notifications.schedule_runs("schedule_a")["runs"]] == [
        "known_schedule"
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("reported", [False, True])
async def test_schedule_recovery_preserves_source_and_delivery_eligibility(manager, reported):
    session = manager.create("coder")
    run = Run(
        run_id="abandoned_schedule",
        agent_id="coder",
        session_id=session.id,
        run_kind=RunKind.CRON,
        schedule_id="schedule_a",
        contributes_to_agent_activity=False,
        completion_activity=lambda _status, _result: reported,
    )
    await manager.start_run(run)
    manager.recover_interrupted_runs()
    assert manager.summary(session.address)["notification_count"] == 0
    activity = manager.notifications.schedule_activity()["schedule_a"]
    assert activity["unread_count"] == int(reported)
    assert activity["unread_error_count"] == int(reported)
    assert manager.notifications.schedule_runs("schedule_a")["runs"][0]["status"] == "interrupted"
