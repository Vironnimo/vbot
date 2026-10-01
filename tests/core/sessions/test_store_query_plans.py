"""Planner guards: every Session read reaches canonical rows through an index.

A read of one Session's history walks its view ranges (own entries plus
inherited lineage segments) by ``(session_key, seq)``; search pushes its
eligible Sessions into both the own and the inherited branch. These tests
capture the statements a read issues and fail on a plan that scans a canonical
table or builds an automatic index.
"""

from __future__ import annotations

import asyncio
import itertools
import re
import shutil
import sqlite3
from collections.abc import Callable, Iterator
from functools import partial
from pathlib import Path
from typing import Any, cast

import pytest

from core.chat.messages import ChatMessage, ToolCall
from core.runs import RunExecutionOwner
from core.sessions import (
    ArchiveEntryCursor,
    ArchiveEntryFilter,
    ArchiveScope,
    ArchiveTree,
    ChatSessionManager,
    _store_archive,
    _store_archive_backfill,
    _store_history,
    _store_owned,
    _store_queries,
    _store_search,
    _store_timeline,
    _store_values,
)
from core.sessions._types import (
    SessionAddress,
    SessionListFilters,
    SessionReadCursor,
    SessionRunAdmission,
)
from core.sessions.errors import SessionNotFoundError
from core.utils.timestamps import utc_now_timestamp
from tests.core.sessions.history_fixtures import complete_run

_ALIAS = re.compile(r"\b(?:FROM|JOIN)\s+([A-Za-z_]\w*)(?:\s+AS\s+([A-Za-z_]\w*))?", re.IGNORECASE)


class _RecordingConnection:
    """Delegate to one connection and keep each statement for EXPLAIN QUERY PLAN."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self.statements: list[tuple[str, tuple[Any, ...]]] = []

    def execute(self, sql: str, params: Any = ()) -> sqlite3.Cursor:
        self.statements.append((sql, tuple(params)))
        return self._connection.execute(sql, params)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._connection, name)


_Statements = list[tuple[str, tuple[Any, ...]]]


def _recording(connection: sqlite3.Connection) -> tuple[sqlite3.Connection, _Statements]:
    """Return a connection that records each statement, and its record."""
    recorder = _RecordingConnection(connection)
    return cast(sqlite3.Connection, recorder), recorder.statements


def _plan(connection: sqlite3.Connection, sql: str, params: tuple[Any, ...]) -> list[str]:
    return [str(row[3]) for row in connection.execute("EXPLAIN QUERY PLAN " + sql, params)]


def _violations(connection: sqlite3.Connection, sql: str, params: tuple[Any, ...]) -> list[str]:
    """Plan steps that scan a canonical table (by name or alias) or build an index."""
    tables = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND sql NOT LIKE '%VIRTUAL%'"
        )
    }
    scanned_names = set(tables)
    for table, alias in _ALIAS.findall(sql):
        if table in tables and alias:
            scanned_names.add(alias)
    found = []
    for detail in _plan(connection, sql, params):
        match = re.match(r"SCAN (\w+)", detail)
        if "AUTOMATIC" in detail or (
            match and match.group(1) in scanned_names and "VIRTUAL TABLE" not in detail
        ):
            found.append(detail)
    return found


def _assert_indexed(
    connection: sqlite3.Connection, statements: _Statements, case: object = None
) -> None:
    assert statements, case
    for sql, params in statements:
        assert _violations(connection, sql, params) == [], (case, sql)


def _plans(connection: sqlite3.Connection, statements: _Statements) -> list[str]:
    return [detail for sql, params in statements for detail in _plan(connection, sql, params)]


@pytest.fixture(scope="module")
def history(
    tmp_path_factory: pytest.TempPathFactory, current_session_store_template: Path
) -> Iterator[tuple[SessionAddress, str, sqlite3.Connection]]:
    """Two Sessions and a fork whose history fills every view range kind.

    Each Session has a completed Run with a Tool call, a Compaction checkpoint
    and an edited draft; the fork of "two" inherits that view and adds its own
    entry. Returns the fork's address, the id of an inherited answer and a
    query-only connection: the tests of this module share the history.
    """
    directory = tmp_path_factory.mktemp("query-plans")
    for name in ("data-store.json", "sessions.db"):
        shutil.copy2(current_session_store_template / name, directory)
    manager = ChatSessionManager(directory)
    anchor = ""
    try:
        for session_id in ("one", "two"):
            session = manager.create("agent", session_id=session_id, project_id="project")
            run = session.start_run(f"run-{session_id}")
            question = ChatMessage.user("needle question")
            assistant = ChatMessage.assistant(
                model="test",
                content=None,
                tool_calls=[ToolCall(id="call", name="read", arguments={"path": "x"})],
            )
            run.append_many([question, assistant])
            run.assistant_message_id = assistant.id
            run.append(ChatMessage.tool(tool_call_id="call", name="read", content="needle result"))
            answer = ChatMessage.assistant(model="test", content="needle answer")
            run.append(answer)
            complete_run(
                run,
                ChatMessage.run_summary(
                    run_id=f"run-{session_id}",
                    status="completed",
                    iteration_count=1,
                    timing={
                        "started_at": "2026-09-19T10:00:00Z",
                        "completed_at": "2026-09-19T10:00:01Z",
                        "duration_ms": 1000,
                    },
                ),
            )
            edited = ChatMessage.user("draft")
            session.append_many(
                [
                    ChatMessage.compaction_checkpoint(
                        summary="needle summary", projection=[], compacted_token_count=1
                    ),
                    edited,
                ]
            )
            session.apply_edit(edited.id, [ChatMessage.user("needle follow-up")])
            anchor = answer.id
        fork = asyncio.run(
            manager.fork(
                SessionAddress("project", "agent", "two"),
                target_project_id="project",
                title="needle fork",
            )
        )
        fork.append(ChatMessage.user("needle fork question"))
    finally:
        manager.close()
    connection = sqlite3.connect(directory / "sessions.db")
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    try:
        yield fork.address, anchor, connection
    finally:
        connection.close()


_SEARCH_FILTERS: tuple[dict[str, Any], ...] = (
    {},
    {"roles": ("user",)},
    {"roles": ("user",), "include_subagents": True},
    {"roles": ("user", "assistant"), "since": "2026-01-01T00:00:00Z"},
    {"roles": ("tool",), "until": "2100-01-01T00:00:00Z", "excluded_session_ids": ("one",)},
)


@pytest.mark.parametrize("order", ["relevance", "oldest", "newest"])
@pytest.mark.parametrize("use_fts", [True, False], ids=["fts", "scan"])
def test_scoped_search_pushes_the_eligible_sessions_into_every_branch(
    history, use_fts, order
) -> None:
    address, _anchor, connection = history
    scopes = (
        {"agent_id": "agent", "session_id": address.session_id},
        {"agent_id": "agent"},
        {"agent_id": None},
    )
    # Whole terms use the trigram index, a short term the standard one, and a
    # miss of a Tool-inclusive search falls back to a scan.
    for scope, filters, query in itertools.product(
        scopes, _SEARCH_FILTERS, ("needle", "ne", "absent")
    ):
        recorder, statements = _recording(connection)
        result = _store_search.search(
            recorder,
            query,
            project_id="project",
            use_fts=use_fts,
            order=order,
            **scope,
            **filters,
        )
        if query == "absent":
            assert result.hits == ()
        _assert_indexed(connection, statements, (scope, filters, query))


def test_inherited_search_hits_are_reported_once_for_the_eligible_fork(history) -> None:
    address, anchor, connection = history
    scoped = _store_search.search(
        connection,
        "needle answer",
        project_id="project",
        agent_id="agent",
        session_id=address.session_id,
        match_mode="phrase",
    )
    assert [(hit.message_id, hit.address) for hit in scoped.hits] == [(anchor, address)]
    everywhere = _store_search.search(
        connection, "needle answer", project_id="project", agent_id="agent", match_mode="phrase"
    )
    # The origin still shows its own entry, so the fork never duplicates it.
    assert sorted(hit.address.session_id for hit in everywhere.hits) == ["one", "two"]


def _session_reads(address: SessionAddress, anchor: str) -> dict[str, Callable[[Any], Any]]:
    def snapshot(recorder: Any, *, complete: bool) -> Any:
        return _store_timeline.chat_history_snapshot(
            recorder,
            address,
            limit=3,
            before_message_id=None,
            before_sequence=None,
            expected_generation_id=None,
            excluded_roles=("note", "history_edit"),
            complete_run_segment=complete,
            background_tool_names=("read",),
            background_note_marker="needle",
        )()

    return {
        "recall_context": lambda r: _store_history.recall_context(r, address, anchor),
        "rows_since_start": lambda r: _store_history.message_rows_since(r, address, None),
        "snapshot_complete": lambda r: snapshot(r, complete=True),
        "snapshot_bounded": lambda r: snapshot(r, complete=False),
        "status": lambda r: _store_history.status_snapshot(r, address)(),
        "active_user_count": lambda r: _store_history.active_user_message_count(
            r, address, limit=2
        ),
        "tool_result_payload": lambda r: _store_history.tool_result_payload(
            r, address, "res_x", "mcp"
        ),
        "latest_note": lambda r: _store_history.latest_note(r, address, content_prefix="x"),
        "skill_activations": lambda r: _store_history.current_skill_activation_messages(r, address),
        "history_snapshot": lambda r: _store_history.history_snapshot(r, address),
        "run_result": lambda r: _store_history.run_result(r, address, run_id="run-two")(),
        "run_messages": lambda r: _store_history.run_messages(r, address, "run-two"),
        "descriptor_sources": lambda r: _store_queries.descriptor_sources(r, [address])(),
    }


@pytest.mark.parametrize("read", sorted(_session_reads(SessionAddress(None, "a", "b"), "")))
def test_every_session_read_over_a_fork_is_indexed(history, read) -> None:
    address, anchor, connection = history
    recorder, statements = _recording(connection)
    _session_reads(address, anchor)[read](recorder)
    _assert_indexed(connection, statements)


def test_fork_reads_see_the_inherited_view(history) -> None:
    address, anchor, connection = history
    context = _store_history.recall_context(connection, address, anchor)
    assert [item["role"] for item in context] == ["user"]
    snapshot = _store_timeline.chat_history_snapshot(
        connection,
        address,
        limit=None,
        before_message_id=None,
        before_sequence=None,
        expected_generation_id=None,
        excluded_roles=("note", "history_edit"),
        complete_run_segment=True,
    )()
    assert [(run["run_id"], run["complete"]) for run in snapshot.runs] == [("run-two", True)]
    run = _store_history.run_result(connection, address, run_id="run-two")()
    assert run is not None and run[0] is not None and run[0].id == anchor


def test_tool_result_probe_reads_one_call_by_its_public_id(history) -> None:
    _address, _anchor, connection = history
    address = SessionAddress("project", "agent", "two")
    recorder, statements = _recording(connection)
    assert _store_history.tool_result_persisted(recorder, address, "call") is True
    assert _store_history.tool_result_persisted(recorder, address, "missing") is False
    _assert_indexed(connection, statements)
    assert any("tool_calls_by_call_id" in detail for detail in _plans(connection, statements))


def test_existing_addresses_probe_the_live_address_index_in_one_statement(history) -> None:
    address, _anchor, connection = history
    other = SessionAddress("project", "agent", "one")
    missing = SessionAddress(None, "agent", "two")
    recorder, statements = _recording(connection)
    found = _store_queries.existing_addresses(recorder, [address, other, missing, address])
    assert found == {address, other}
    assert len(statements) == 1
    _assert_indexed(connection, statements)
    assert any("sessions_one_live_address" in detail for detail in _plans(connection, statements))


def test_session_point_reads_probe_the_live_address_index(history) -> None:
    address, _anchor, connection = history
    missing = SessionAddress("project", "agent", "missing")
    recorder, statements = _recording(connection)
    assert _store_queries.exists(recorder, address) is True
    assert _store_queries.exists(recorder, missing) is False
    assert _store_values._require_live(recorder, address)["session_id"] == address.session_id
    with pytest.raises(SessionNotFoundError):
        _store_values._require_live(recorder, missing)
    details = _plans(connection, statements)
    assert len(details) == len(statements) == 4, details
    assert all(
        re.fullmatch(
            r"SEARCH (sessions|s) USING (COVERING )?INDEX sessions_one_live_address "
            r"\(project_id=\? AND agent_id=\? AND session_id=\?\)",
            detail,
        )
        for detail in details
    ), details


def test_session_owning_agents_read_distinct_ids_from_the_live_address_index(history) -> None:
    _address, _anchor, connection = history
    recorder, statements = _recording(connection)
    agent_ids = _store_queries.list_agent_ids(recorder, "project", exclude_owner_managed=True)
    assert agent_ids == ["agent"]
    details = _plans(connection, statements)
    assert not [detail for detail in details if detail.startswith("SCAN sessions")], details
    assert not [detail for detail in details if "TEMP B-TREE" in detail], details


def test_delta_read_reads_only_the_entries_after_the_cursor(history) -> None:
    address, _anchor, connection = history
    full = _store_history.message_rows_since(connection, address, None)
    assert full is not None
    last = full.view_rows[-1]
    previous = full.view_rows[-2]
    cursor = SessionReadCursor(
        full.cursor.generation_id,
        full.cursor.history_revision,
        int(last["seq"]),
        str(previous["entry_id"]),
    )
    recorder, statements = _recording(connection)
    delta = _store_history.message_rows_since(recorder, address, cursor)
    assert delta is not None
    assert [int(row["seq"]) for row in delta.view_rows] == [int(last["seq"])]
    assert [int(row["seq"]) for row in delta.audit_rows] == [int(last["seq"])]
    assert delta.inherited_count == 0
    _assert_indexed(connection, statements)


def test_current_delta_cursor_reads_only_the_session_row(history) -> None:
    address, _anchor, connection = history
    full = _store_history.message_rows_since(connection, address, None)
    assert full is not None
    recorder, statements = _recording(connection)
    current = _store_history.message_rows_since(recorder, address, full.cursor)
    assert current is not None
    assert (current.audit_rows, current.view_rows, current.cursor) == ([], [], full.cursor)
    assert not [sql for sql, _params in statements if "entries" in sql]


def test_unchanged_history_cursor_reads_only_the_session_row(history) -> None:
    address, _anchor, connection = history

    def snapshot(recorder: Any, **kwargs: Any) -> Any:
        return _store_timeline.chat_history_snapshot(
            recorder,
            address,
            limit=6,
            before_message_id=None,
            before_sequence=None,
            expected_generation_id=None,
            excluded_roles=("note", "history_edit"),
            complete_run_segment=True,
            **kwargs,
        )()

    full = snapshot(connection)
    state = _store_values._require_live(connection, address)
    recorder, statements = _recording(connection)
    unchanged = snapshot(
        recorder,
        background_tool_names=("read",),
        background_note_marker="needle",
        after=(full.generation_id, int(state["next_seq"])),
        skip_unchanged=True,
    )
    assert unchanged.unchanged and unchanged.incremental
    assert unchanged.page.messages == ()
    assert unchanged.after_cursor == full.after_cursor
    assert unchanged.background_records == ()
    assert not [sql for sql, _params in statements if "entries" in sql]


def test_owned_run_point_lookups_probe_indexes_not_group_history(manager) -> None:
    binding = manager.create_bound_temporary_session(
        SessionAddress(None, "temporary", "participant"),
        owner_name="owner",
        group_id="group",
        participant_id="peer",
        config={},
    )
    owner = RunExecutionOwner("owner", "group", "peer", binding.generation_id, "epoch")
    for number in range(3):
        manager._store.admit_run(
            binding.address,
            SessionRunAdmission(
                run_id=f"run{number}",
                run_kind="system",
                started_at=utc_now_timestamp(),
                owner=owner,
                input_id=f"input{number}",
            ),
        )
    connection = sqlite3.connect(manager._store.path)
    connection.row_factory = sqlite3.Row
    try:
        recorder, statements = _recording(connection)
        records = _store_owned.owned_runs_by_id(
            recorder, owner_name="owner", group_id="group", run_ids=["run2", "run0"]
        )
        assert sorted(records) == ["run0", "run2"]
        record = _store_owned.owned_run_by_input(recorder, binding.address, "input1")
        assert record is not None and record.run_id == "run1"
        _assert_indexed(connection, statements)
        details = _plans(connection, statements)
        assert any("run_execution_owners_group_run" in detail for detail in details), details
    finally:
        connection.close()


def test_completion_activity_searches_each_scope_by_live_address_index(history) -> None:
    _address, _anchor, connection = history
    recorder, statements = _recording(connection)
    _store_queries.list_completion_activity(recorder, [("project", "agent"), (None, "other")])
    plans = _plans(connection, statements)
    assert any(
        re.match(r"SEARCH s USING INDEX \w+ \(project_id=\? AND agent_id=\?", p) for p in plans
    )
    assert not any(re.match(r"SCAN s\b", plan) for plan in plans)


def test_session_list_reads_follow_the_declared_order_indexes(history) -> None:
    """One scope walks its order index and several the global one; none sorts."""
    _address, _anchor, connection = history

    def plans(read: Callable[[Any], Any]) -> list[str]:
        recorder, statements = _recording(connection)
        read(recorder)
        return [" ".join(_plan(connection, sql, params)) for sql, params in statements]

    def page(scopes: list[tuple[str | None, str]]) -> Callable[[Any], Any]:
        return lambda recorder: _store_queries.list_summaries_page(
            recorder,
            scopes,
            limit=20,
            cursor=None,
            filters=SessionListFilters(include_cron=False),
            required_address=None,
        )()

    (scoped,) = plans(
        lambda recorder: _store_queries.list_summaries(recorder, "project", "agent")()
    )
    scoped_count, scoped_page = plans(page([("project", "agent")]))
    global_count, global_page = plans(page([("project", "agent"), (None, "other")]))

    assert "sessions_live_scope_order" in scoped
    assert "sessions_live_scope_order" in scoped_page
    assert "sessions_live_global_order" in global_page
    assert "sessions_live_scope_visibility" in scoped_count
    assert "sessions_live_scope_visibility" in global_count
    assert not [plan for plan in (scoped, scoped_page, global_page) if "USE TEMP B-TREE" in plan]


def test_payload_reads_start_from_the_payload_id(history) -> None:
    address, _anchor, connection = history
    recorder, statements = _recording(connection)

    _store_history.tool_result_payload(recorder, address, "res_x", "mcp")

    # The view only filters the candidates the id finds; it never drives the read.
    plans = [
        detail for detail in _plans(connection, statements) if "tool_result_payloads" in detail
    ]
    assert plans == ["SEARCH p USING INDEX tool_result_payloads_by_id (payload_id=?)"]


def test_archive_entry_reads_and_transitions_use_their_indexes(manager) -> None:
    """Each archive statement probes the index that names it as its reader."""
    ledger = manager.archive_ledger
    for session_id in ("one", "two"):
        manager.create("agent", session_id=session_id, project_id="project")
    manager.create("agent", session_id="identity")
    entry = ledger.begin(
        "project",
        subject_id="project",
        project_id="project",
        trees=lambda entry_id: [ArchiveTree(f"archive/entries/{entry_id}/project", "project")],
    )
    ledger.commit_scope(entry.entry_key, ArchiveScope(project_id="project"), cleanup_pending=True)
    with sqlite3.connect(manager._store.path) as connection:
        connection.execute(
            "UPDATE sessions SET state = 'archived', archived_at = ? WHERE session_id = 'identity'",
            (utc_now_timestamp(),),
        )
    connection = sqlite3.connect(manager._store.path)
    connection.row_factory = sqlite3.Row
    try:

        def plans(action: Callable[[sqlite3.Connection], Any], *, scans: str = "") -> list[str]:
            """Run ``action`` rolled back; only a walk of the partial index ``scans`` may scan."""
            connection.execute("SAVEPOINT plan")
            recorder, statements = _recording(connection)
            try:
                action(recorder)
            finally:
                connection.execute("ROLLBACK TO plan")
                connection.execute("RELEASE plan")
            assert statements
            for sql, params in statements:
                violations = _violations(connection, sql, params)
                assert [v for v in violations if not scans or f"INDEX {scans}" not in v] == [], sql
            return _plans(connection, statements)

        filters = {
            "archive_entries_by_archived": ArchiveEntryFilter(),
            "archive_entries_by_scope": ArchiveEntryFilter(project_id="project"),
        }
        cursor = ArchiveEntryCursor(utc_now_timestamp(), "arc_zzzz")
        for index, page_filter in filters.items():
            details = plans(
                partial(_store_archive.page, filters=page_filter, cursor=cursor, limit=10)
            )
            assert any(index in detail for detail in details), details
            assert any("archive_entry_trees_by_entry" in detail for detail in details), details
            assert any("archive_entry_sessions_by_entry" in detail for detail in details), details
        assert any(
            "archive_entries_by_subject" in detail
            for detail in plans(
                lambda recorder: _store_archive.newest_entry_id(recorder, "project", "project")
            )
        )
        assert any(
            "archive_entries_due" in detail
            for detail in plans(lambda recorder: _store_archive.due(recorder, "9999", 10))
        )
        assert any(
            "archive_entries_unsettled" in detail
            for detail in plans(_store_archive.unsettled, scans="archive_entries_unsettled")
        )
        assert any(
            "archive_entry_trees" in detail
            for detail in plans(
                lambda recorder: _store_archive.is_recorded(
                    recorder, f"archive/entries/{entry.entry_id}"
                )
            )
        )
        purge = plans(
            lambda recorder: (
                _store_archive.begin_purge(recorder, entry.entry_id),
                _store_archive.purge_next_session(recorder, entry.entry_key),
            )
        )
        assert any("archive_entry_sessions_by_entry" in detail for detail in purge), purge
        # Startup repair reads archived rows through the archived-address index only.
        repair = plans(
            _store_archive_backfill.adopt_unrecorded_archived_rows,
            scans="sessions_archived_address",
        )
        assert any("sessions_archived_address" in detail for detail in repair), repair
        # Id allocation still probes archived generations by address.
        allocation = plans(
            lambda recorder: _store_values._allocate_address(
                recorder, SessionAddress("project", "agent", "")
            )
        )
        assert any("sessions_archived_address" in detail for detail in allocation), allocation
    finally:
        connection.close()
