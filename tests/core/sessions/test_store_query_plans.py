"""Planner guards: Session-scoped reads reach each history_records branch by index.

``history_records`` is a UNION ALL view. SQLite pushes a Session filter into
every branch only when the filter names view columns and constants or
uncorrelated subqueries; a join on ``sessions`` scans each branch whole. These
tests capture the statements a read issues and fail on plans that scan a
branch table, scan the messages branch, or build an automatic index.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from typing import Any, cast

import pytest

from core.chat.messages import ChatMessage, ToolCall
from core.sessions import (
    _store_history,
    _store_queries,
    _store_search,
    _store_values,
)
from core.sessions._types import SessionAddress, SessionReadCursor
from tests.core.sessions.sessions_test_support import manager as manager

_BRANCH_NODES = {"COMPOUND QUERY", "LEFT-MOST SUBQUERY", "UNION ALL"}


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


def _violations(connection: sqlite3.Connection, sql: str, params: tuple[Any, ...]) -> list[str]:
    nodes = {
        int(row[0]): (int(row[1]), str(row[3]))
        for row in connection.execute("EXPLAIN QUERY PLAN " + sql, params)
    }

    def inside_view_branch(node: int) -> bool:
        parent = nodes[node][0]
        while parent in nodes:
            if nodes[parent][1] in _BRANCH_NODES:
                return True
            parent = nodes[parent][0]
        return False

    found = []
    for node, (_parent, detail) in nodes.items():
        if (
            "AUTOMATIC" in detail
            or re.match(r"SCAN (c|e|r|t)\b", detail)
            or (re.match(r"SCAN m\b", detail) and inside_view_branch(node))
        ):
            found.append(detail)
    return found


@pytest.fixture
def history(manager) -> Iterator[tuple[SessionAddress, str, sqlite3.Connection]]:
    """Two Sessions whose history fills every view branch."""
    anchor = ""
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
        run.append(
            ChatMessage.run_summary(
                run_id=f"run-{session_id}",
                status="completed",
                iteration_count=1,
                timing={
                    "started_at": "2026-09-19T10:00:00Z",
                    "completed_at": "2026-09-19T10:00:01Z",
                    "duration_ms": 1000,
                },
            )
        )
        edited = ChatMessage.user("draft")
        session.append_many(
            [
                ChatMessage.compaction_checkpoint(
                    summary="needle summary", projection=[], compacted_token_count=1
                ),
                edited,
                ChatMessage.history_edit(edited.id),
                ChatMessage.user("needle follow-up"),
            ]
        )
        anchor = answer.id
    connection = sqlite3.connect(manager._store.path)
    connection.row_factory = sqlite3.Row
    try:
        yield SessionAddress("project", "agent", "two"), anchor, connection
    finally:
        connection.close()


def _assert_indexed(connection: sqlite3.Connection, statements: _Statements) -> None:
    assert statements
    for sql, params in statements:
        assert _violations(connection, sql, params) == [], sql


@pytest.mark.parametrize(
    "scope",
    [
        {"agent_id": "agent", "session_id": "two"},
        {"agent_id": "agent"},
        {"agent_id": None},
    ],
)
@pytest.mark.parametrize(
    "filters",
    [
        {},
        {"roles": ("user",)},
        {"roles": ("user",), "include_subagents": True},
        {"roles": ("user", "assistant"), "since": "2026-01-01T00:00:00Z"},
        {"roles": ("tool",), "until": "2100-01-01T00:00:00Z", "excluded_session_ids": ("one",)},
    ],
)
@pytest.mark.parametrize("use_fts", [True, False])
@pytest.mark.parametrize("order", ["relevance", "newest", "oldest"])
def test_scoped_search_pushes_the_session_scope_into_every_branch(
    history, scope, filters, use_fts, order
) -> None:
    _address, _anchor, connection = history
    for query in ("needle", "ne", "absent"):
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
        _assert_indexed(connection, statements)


def test_time_filtered_search_reads_messages_through_the_instant_index(history) -> None:
    _address, _anchor, connection = history
    recorder, statements = _recording(connection)
    _store_search.search(
        recorder, "needle", project_id="project", agent_id="agent", since="2026-01-01T00:00:00Z"
    )
    plans = [
        str(row[3])
        for sql, params in statements
        for row in connection.execute("EXPLAIN QUERY PLAN " + sql, params)
    ]
    assert any("messages_by_session_instant (session_key=? AND <expr>>?)" in plan for plan in plans)


def test_recall_context_reads_its_anchor_by_session_index(history) -> None:
    address, anchor, connection = history
    recorder, statements = _recording(connection)
    context = _store_history.recall_context(recorder, address, anchor)
    assert [item["role"] for item in context] == ["user"]
    _assert_indexed(connection, statements)


def test_delta_read_uses_one_indexed_read_from_the_anchor(history) -> None:
    address, _anchor, connection = history
    state = _store_values._require_live(connection, address)
    count = int(state["message_count"])
    full = _store_history.message_rows_since(connection, address, None)
    assert full is not None
    rows = full[0]
    recorder, statements = _recording(connection)
    cursor = SessionReadCursor(
        str(state["generation_id"]), 0, count - 1, count - 1, rows[count - 2]["message_id"]
    )
    delta = _store_history.message_rows_since(recorder, address, cursor)
    assert delta is not None
    assert [int(row["seq"]) for row in delta[0]] == [count - 1]
    assert sum("history_records" in sql for sql, _params in statements) == 1
    _assert_indexed(connection, statements)


def test_current_delta_cursor_reads_only_the_session_row(history) -> None:
    address, _anchor, connection = history
    full = _store_history.message_rows_since(connection, address, None)
    assert full is not None
    current = full[1]
    recorder, statements = _recording(connection)
    assert _store_history.message_rows_since(recorder, address, current) == ([], current)
    assert [sql for sql, _params in statements if "history_records" in sql] == []


def test_page_query_orders_narrow_keys_by_session_index(history) -> None:
    address, _anchor, connection = history
    state = _store_values._require_live(connection, address)
    recorder, statements = _recording(connection)
    rows, has_more, _editable, _floor = _store_history._active_message_page_from_connection(
        recorder,
        state,
        limit=2,
        before_message_id=None,
        before_sequence=None,
        expected_generation_id=None,
        excluded_roles=("note", "history_edit"),
        complete_run_segment=True,
    )
    assert rows and has_more
    _assert_indexed(connection, statements)


@pytest.mark.parametrize("complete_run_segment", [True, False])
def test_chat_history_snapshot_reads_by_session_index(history, complete_run_segment) -> None:
    address, _anchor, connection = history
    recorder, statements = _recording(connection)
    snapshot = _store_history.chat_history_snapshot(
        recorder,
        address,
        limit=6,
        before_message_id=None,
        before_sequence=None,
        expected_generation_id=None,
        excluded_roles=("note", "history_edit"),
        complete_run_segment=complete_run_segment,
        background_roles=("tool",),
        background_tool_names=("read",),
        after=None,
    )()
    assert [(run["run_id"], run["complete"]) for run in snapshot.runs] == [
        ("run-two", complete_run_segment)
    ]
    _assert_indexed(connection, statements)


def test_session_catalog_reads_scope_history_by_session_index(history) -> None:
    address, _anchor, connection = history
    recorder, statements = _recording(connection)
    assert _store_queries.session_ids_with_messages(
        recorder, "project", "agent", ("tool",), None, None
    ) == {"one", "two"}
    sources = _store_queries.descriptor_sources(recorder, [address])()
    assert sources[address][2] is not None
    assert sources[address][3] == "conversation"
    _assert_indexed(connection, statements)
