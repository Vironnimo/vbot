"""Tests for the Generation 1 conversion of ``sessions.db``: forks and their lineage."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any

from core.sessions import ChatSessionManager, SessionAddress
from core.sessions.store import SessionStore
from scripts.converters.persistence_generation_1._context import ConversionContext
from scripts.converters.persistence_generation_1.sessions import AREA, convert
from tests.scripts.converters.persistence_generation_1.legacy_sessions_support import (
    LegacySessionStore,
    canonical,
)

BASE = SessionAddress(project_id=None, agent_id="main", session_id="base")
BRANCH = SessionAddress(project_id=None, agent_id="main", session_id="branch")
TWIG = SessionAddress(project_id=None, agent_id="main", session_id="twig")


def _context(tmp_path: Path) -> ConversionContext:
    source = tmp_path / "data"
    source.mkdir(exist_ok=True)
    return ConversionContext(source=source, staging=tmp_path / "staging")


@contextmanager
def _opened(context: ConversionContext) -> Iterator[ChatSessionManager]:
    store = SessionStore(context.staging / "sessions.db", _offline=True)
    manager = ChatSessionManager(context.staging, store=store)
    try:
        yield manager
    finally:
        manager.close()
        store.close()


def _rows(context: ConversionContext, sql: str) -> list[tuple[Any, ...]]:
    with closing(sqlite3.connect(context.staging / "sessions.db")) as connection:
        return [tuple(row) for row in connection.execute(sql)]


def _skips(context: ConversionContext) -> list[tuple[str, str]]:
    return [(skip.item, skip.reason) for skip in context.report.skipped if skip.area == AREA]


def _ids(manager: ChatSessionManager, address: SessionAddress) -> list[str]:
    return [message.id for message in manager.get(address).load_active()]


def _base(legacy: LegacySessionStore) -> tuple[int, dict[str, str]]:
    """A Session with one settled Run: four history records."""
    key = legacy.session("base", minute=0)
    legacy.start_run(key, "run_1", minute=1)
    ids = {
        "question": legacy.user(key, "zebra question", minute=1, run_id="run_1"),
        "call": legacy.assistant(
            key,
            None,
            minute=2,
            run_id="run_1",
            tool_calls=[{"id": "call_1", "name": "read_file", "arguments": {"path": "a.txt"}}],
        ),
        "result": legacy.tool_result(key, "call_1", '{"ok": true, "data": "a"}', minute=3),
    }
    ids["summary"] = legacy.finish_run(key, "run_1", minute=4)
    return key, ids


def _family(legacy: LegacySessionStore) -> tuple[dict[str, int], dict[str, str]]:
    """``base``, its fork ``branch`` and the fork of that fork ``twig``."""
    base, ids = _base(legacy)
    branch = legacy.fork(base, "branch", minute=10)
    ids["branch_question"] = legacy.user(branch, "branch question", minute=11)
    ids["branch_answer"] = legacy.assistant(branch, "branch answer", minute=12)
    ids["base_more"] = legacy.user(base, "base continues", minute=13)
    twig = legacy.fork(branch, "twig", minute=20)
    ids["twig_question"] = legacy.user(twig, "twig question", minute=21)
    return {"base": base, "branch": branch, "twig": twig}, ids


def test_matching_forks_share_their_source_history(tmp_path: Path) -> None:
    context = _context(tmp_path)
    with LegacySessionStore(context.source / "sessions.db") as legacy:
        keys, ids = _family(legacy)

    convert(context)

    prefix = [ids["question"], ids["call"], ids["result"], ids["summary"]]
    branch_view = [*prefix, ids["branch_question"], ids["branch_answer"]]
    with _opened(context) as manager:
        assert _ids(manager, BASE) == [*prefix, ids["base_more"]]
        assert _ids(manager, BRANCH) == branch_view
        assert _ids(manager, TWIG) == [*branch_view, ids["twig_question"]]
        result = manager.get(TWIG).load_active()[2]
        assert (result.tool_call_id, result.content) == ("call_1", '{"ok": true, "data": "a"}')
        assert manager.get_metadata(TWIG)["fork_source"] == {
            "agent_id": "main",
            "session_id": "branch",
            "project_id": None,
            "forked_at": canonical(20),
        }
        search = manager.search_messages("zebra", project_id=None, agent_id="main")
        assert [(hit.address, hit.message_id) for hit in search.hits] == [(BASE, ids["question"])]
    base, branch, twig = keys["base"], keys["branch"], keys["twig"]
    assert _rows(
        context,
        "SELECT session_key, from_seq, ancestor_key, upto_seq, as_of_seq FROM session_lineage "
        "ORDER BY session_key, from_seq",
    ) == [(branch, 0, base, 4, 4), (twig, 0, base, 4, 4), (twig, 4, branch, 6, 6)]
    assert _rows(
        context,
        "SELECT session_key, fork_parent_key, forked_at, fork_point_seq, next_seq FROM sessions "
        "ORDER BY session_key",
    ) == [
        (base, None, None, None, 5),
        (branch, base, canonical(10), 4, 6),
        (twig, branch, canonical(20), 6, 7),
    ]
    assert _rows(
        context, "SELECT session_key, MIN(seq), COUNT(*) FROM entries GROUP BY session_key"
    ) == [(base, 0, 5), (branch, 4, 2), (twig, 6, 1)]
    assert _rows(context, "SELECT session_key, run_id FROM runs") == [(base, "run_1")]
    counts = context.report.counts[AREA]
    assert {
        key: counts[key]
        for key in (
            "forks_shared",
            "forks_self_contained",
            "fork_copies_dropped",
            "fork_runs_dropped",
        )
    } == {
        "forks_shared": 2,
        "forks_self_contained": 0,
        "fork_copies_dropped": 10,
        "fork_runs_dropped": 2,
    }
    assert _rows(context, "PRAGMA foreign_key_check") == []
    assert _skips(context) == []


def test_deleting_a_shared_source_keeps_its_forks_history(tmp_path: Path) -> None:
    context = _context(tmp_path)
    with LegacySessionStore(context.source / "sessions.db") as legacy:
        keys, _ids_by_name = _family(legacy)
    convert(context)

    with _opened(context) as manager:
        before = {address: _ids(manager, address) for address in (BRANCH, TWIG)}
        manager.delete(BASE)
        assert {address: _ids(manager, address) for address in (BRANCH, TWIG)} == before
        assert manager.fts_health().state == "healthy"
    assert keys["base"] not in {
        row[0] for row in _rows(context, "SELECT ancestor_key FROM session_lineage")
    }
    assert _rows(context, "PRAGMA foreign_key_check") == []


def test_source_edits_after_the_fork_leave_the_fork_unchanged(tmp_path: Path) -> None:
    context = _context(tmp_path)
    with LegacySessionStore(context.source / "sessions.db") as legacy:
        base, ids = _base(legacy)
        branch = legacy.fork(base, "branch", minute=10)
        branch_question = legacy.user(branch, "branch question", minute=11)
        legacy.edit(base, ids["question"], minute=12)
        rewritten = legacy.user(base, "rewritten question", minute=13)

    convert(context)

    prefix = [ids["question"], ids["call"], ids["result"], ids["summary"]]
    with _opened(context) as manager:
        assert _ids(manager, BASE) == [rewritten]
        assert _ids(manager, BRANCH) == [*prefix, branch_question]
        # The source replaced the Message, so the fork that still shows it reports it.
        search = manager.search_messages("zebra", project_id=None, agent_id="main")
        assert [(hit.address, hit.message_id) for hit in search.hits] == [(BRANCH, ids["question"])]
    assert _rows(
        context, "SELECT session_key, ancestor_key, upto_seq, as_of_seq FROM session_lineage"
    ) == [(branch, base, 4, 4)]
    assert context.report.counts[AREA]["forks_shared"] == 1


def test_a_fork_edit_of_shared_history_truncates_its_lineage(tmp_path: Path) -> None:
    context = _context(tmp_path)
    with LegacySessionStore(context.source / "sessions.db") as legacy:
        base, ids = _base(legacy)
        branch = legacy.fork(base, "branch", minute=10)
        legacy.edit(branch, ids["question"], minute=11)
        restart = legacy.user(branch, "a different start", minute=12)

    convert(context)

    with _opened(context) as manager:
        assert _ids(manager, BRANCH) == [restart]
        assert _ids(manager, BASE) == [ids["question"], ids["call"], ids["result"], ids["summary"]]
    assert _rows(context, "SELECT COUNT(*) FROM session_lineage") == [(0,)]
    assert _rows(
        context, f"SELECT seq, role, superseded_at_seq FROM entries WHERE session_key = {branch}"
    ) == [(4, "history_edit", 4), (5, "user", None)]
    assert _rows(
        context, f"SELECT cursor_floor_seq FROM sessions WHERE session_key = {branch}"
    ) == [(5,)]
    assert context.report.counts[AREA]["forks_shared"] == 1
    assert _skips(context) == []


def test_a_fork_made_while_a_run_was_running_keeps_its_copies(tmp_path: Path) -> None:
    context = _context(tmp_path)
    with LegacySessionStore(context.source / "sessions.db") as legacy:
        base = legacy.session("base", minute=0)
        legacy.start_run(base, "run_1", minute=1)
        question = legacy.user(base, "go", minute=1, run_id="run_1")
        call = legacy.assistant(
            base,
            None,
            minute=2,
            run_id="run_1",
            tool_calls=[{"id": "call_1", "name": "shell", "arguments": {}}],
        )
        branch = legacy.fork(base, "branch", minute=5)
        branch_question = legacy.user(branch, "branch question", minute=6)
        twig = legacy.fork(branch, "twig", minute=7)
        legacy.tool_result(base, "call_1", '{"ok": true}', minute=8)
        legacy.finish_run(base, "run_1", minute=9)
        branch_label = f"session -/main/branch ({legacy.generation(branch)})"

    convert(context)

    with _opened(context) as manager:
        assert _ids(manager, BRANCH) == [question, call, branch_question]
        assert _ids(manager, TWIG) == [question, call, branch_question]
        copied_call = manager.get(BRANCH).load_active()[1].tool_calls or []
        assert [tool_call.id for tool_call in copied_call] == ["call_1"]
        manager.recover_interrupted_runs()
        manager.delete(BASE)
        assert _ids(manager, BRANCH) == [question, call, branch_question]
        assert _ids(manager, TWIG) == [question, call, branch_question]
    assert _rows(
        context,
        "SELECT run_id, status, completion_reason, completed_at, inherited, "
        f"contributes_to_activity FROM runs WHERE session_key = {branch}",
    ) == [("run_1", "interrupted", None, canonical(5), 1, 0)]
    assert _rows(
        context,
        "SELECT c.status, c.completed_at FROM tool_calls AS c JOIN entries AS e "
        f"ON e.entry_key = c.entry_key WHERE e.session_key = {branch}",
    ) == [("interrupted", canonical(5))]
    assert _rows(
        context,
        "SELECT session_key, from_seq, ancestor_key, upto_seq, as_of_seq FROM session_lineage",
    ) == [(twig, 0, branch, 3, 3)]
    assert _rows(
        context,
        f"SELECT fork_parent_key, fork_point_seq FROM sessions WHERE session_key = {branch}",
    ) == [(None, 2)]
    counts = context.report.counts[AREA]
    assert (
        counts["forks_shared"],
        counts["forks_self_contained"],
        counts["runs_inherited"],
        # Only the branch writes its copy; the twig shares the branch's.
        counts["fork_snapshot_reasons_dropped"],
    ) == (1, 1, 1, 1)
    assert _skips(context) == [
        (
            branch_label,
            "fork keeps its copied history: Run run_1 was running when the Session was forked",
        ),
        (
            branch_label,
            "Run run_1 retired completion reason fork_snapshot dropped: the copy of a Run "
            "that was running when the Session was forked stays interrupted",
        ),
    ]


def test_a_fork_whose_copies_differ_from_its_source_keeps_them(tmp_path: Path) -> None:
    context = _context(tmp_path)
    with LegacySessionStore(context.source / "sessions.db") as legacy:
        base, ids = _base(legacy)
        branch = legacy.fork(base, "branch", minute=10)
        legacy.execute(
            "UPDATE messages SET message_id = 'msg_other' WHERE session_key = ? AND seq = 0",
            (branch,),
        )
        branch_label = f"session -/main/branch ({legacy.generation(branch)})"

    convert(context)

    with _opened(context) as manager:
        assert _ids(manager, BRANCH) == ["msg_other", ids["call"], ids["result"], ids["summary"]]
    assert _rows(context, "SELECT COUNT(*) FROM session_lineage") == [(0,)]
    assert _rows(
        context,
        f"SELECT fork_parent_key, fork_point_seq FROM sessions WHERE session_key = {branch}",
    ) == [(base, 4)]
    assert _rows(
        context,
        f"SELECT run_id, inherited, contributes_to_activity FROM runs WHERE session_key = {branch}",
    ) == [("run_1", 1, 0)]
    assert _skips(context) == [
        (branch_label, "fork keeps its copied history: seq 0 holds another Message")
    ]


def test_a_fork_whose_source_is_gone_keeps_its_copies(tmp_path: Path) -> None:
    context = _context(tmp_path)
    with LegacySessionStore(context.source / "sessions.db") as legacy:
        base, ids = _base(legacy)
        branch = legacy.fork(base, "branch", minute=10)
        own = legacy.user(branch, "branch question", minute=11)
        legacy.delete(base)
        branch_label = f"session -/main/branch ({legacy.generation(branch)})"

    convert(context)

    with _opened(context) as manager:
        assert _ids(manager, BRANCH) == [
            ids["question"],
            ids["call"],
            ids["result"],
            ids["summary"],
            own,
        ]
        assert "fork_source" not in manager.get_metadata(BRANCH)
        summary = manager.get(BRANCH).find_run_summary(run_id="run_1")
        assert summary is not None and summary.id == ids["summary"]
    assert _rows(
        context,
        "SELECT fork_parent_key, forked_at, fork_point_seq FROM sessions "
        f"WHERE session_key = {branch}",
    ) == [(None, canonical(10), 4)]
    assert context.report.counts[AREA]["forks_self_contained"] == 1
    assert _skips(context) == [
        (
            branch_label,
            "fork source no longer exists; the fork keeps its copied history without a fork source",
        )
    ]


def test_a_fork_follows_its_source_whatever_the_session_keys(tmp_path: Path) -> None:
    context = _context(tmp_path)
    with LegacySessionStore(context.source / "sessions.db") as legacy:
        base, ids = _base(legacy)
        branch = legacy.fork(base, "branch", minute=10)
        # Imported Sessions do not number their keys in creation order.
        legacy.execute("PRAGMA foreign_keys = OFF")
        for table in ("sessions", "runs", "messages", "compaction_checkpoints", "history_edits"):
            legacy.execute(f"UPDATE {table} SET session_key = 99 WHERE session_key = ?", (base,))
        legacy.execute("PRAGMA foreign_keys = ON")

    convert(context)

    with _opened(context) as manager:
        assert _ids(manager, BRANCH) == [
            ids["question"],
            ids["call"],
            ids["result"],
            ids["summary"],
        ]
    assert _rows(context, "SELECT session_key, ancestor_key FROM session_lineage") == [(branch, 99)]
    assert _rows(context, "SELECT session_key FROM entries ORDER BY entry_key LIMIT 1") == [(99,)]
    assert _skips(context) == []
