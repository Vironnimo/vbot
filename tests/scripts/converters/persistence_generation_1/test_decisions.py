"""Tests for the Generation 1 conversion of ``decisions.db``."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path, PurePosixPath
from typing import Any

import pytest

from core.database import APPLICATION_IDS, open_offline_database, write_bootstrap_marker
from core.model_tasks.decision_store import DecisionStore, decision_database_spec
from scripts.converters.persistence_generation_1._context import (
    ConversionContext,
    ConversionError,
)
from scripts.converters.persistence_generation_1.decisions import AREA, convert
from tests.scripts.converters.persistence_generation_1.legacy_schema_support import (
    LEGACY_DECISIONS_DDL,
    create_legacy_database,
)

_EXPERIMENT = "INSERT INTO experiments VALUES (?,?,?,?,?,?)"
_EVALUATION = "INSERT INTO evaluations VALUES (?,?,?,?,?,?,?,?,?,?)"


def _context(tmp_path: Path) -> ConversionContext:
    source = tmp_path / "data"
    source.mkdir(exist_ok=True)
    return ConversionContext(source=source, staging=tmp_path / "staging")


def _source(context: ConversionContext, *statements: tuple[str, tuple[Any, ...]]) -> Path:
    path = context.source / "decisions.db"
    create_legacy_database(path, LEGACY_DECISIONS_DDL)
    with closing(sqlite3.connect(path)) as connection, connection:
        for sql, parameters in statements:
            connection.execute(sql, parameters)
    return path


def _experiment(identifier: str, *, draft: str = '{"title": "T"}') -> tuple[str, tuple[Any, ...]]:
    return (
        _EXPERIMENT,
        (identifier, "T", 2, draft, "2026-09-01T10:00:00+00:00", "2026-09-01T12:30:00.5+02:00"),
    )


def _evaluation(
    sequence: int, identifier: str, experiment: str, status: str = "completed", **columns: Any
) -> tuple[str, tuple[Any, ...]]:
    values = {
        "snapshot": '{"state": "s"}',
        "result": '{"n": 1}',
        "error": None,
        "created_at": "2026-09-02T08:00:00.123456+00:00",
        "completed_at": "2026-09-02T08:05:00+00:00",
        **columns,
    }
    return (
        _EVALUATION,
        (
            sequence,
            identifier,
            experiment,
            f"request-{identifier}",
            status,
            values["snapshot"],
            values["result"],
            values["error"],
            values["created_at"],
            values["completed_at"],
        ),
    )


def _staged_rows(context: ConversionContext, sql: str) -> list[tuple[Any, ...]]:
    database = open_offline_database(decision_database_spec(context.staging / "decisions.db"))
    try:
        with database.read() as connection:
            return [tuple(row) for row in connection.execute(sql)]
    finally:
        database.close()


def test_copies_experiments_and_evaluations_with_canonical_timestamps(tmp_path: Path) -> None:
    context = _context(tmp_path)
    source = _source(
        context,
        _experiment("exp_a"),
        _evaluation(7, "evl_done", "exp_a"),
        _evaluation(9, "evl_running", "exp_a", "running", result=None, completed_at=None),
    )
    original = source.read_bytes()

    convert(context)

    assert _staged_rows(
        context, "SELECT id,title,revision,draft,created_at,updated_at FROM experiments"
    ) == [
        (
            "exp_a",
            "T",
            2,
            '{"title": "T"}',
            "2026-09-01T10:00:00.000000Z",
            "2026-09-01T10:30:00.500000Z",
        )
    ]
    assert _staged_rows(
        context,
        "SELECT sequence,id,request_id,status,result,created_at,completed_at "
        "FROM evaluations ORDER BY sequence",
    ) == [
        (
            7,
            "evl_done",
            "request-evl_done",
            "completed",
            '{"n": 1}',
            "2026-09-02T08:00:00.123456Z",
            "2026-09-02T08:05:00.000000Z",
        ),
        (
            9,
            "evl_running",
            "request-evl_running",
            "running",
            None,
            "2026-09-02T08:00:00.123456Z",
            None,
        ),
    ]
    assert context.report.counts[AREA] == {"experiments": 1, "evaluations": 2}
    assert context.report.skipped == []
    assert context.retired == []
    assert source.read_bytes() == original


def test_converted_database_opens_in_the_decisions_store(tmp_path: Path) -> None:
    context = _context(tmp_path)
    _source(
        context,
        _experiment("exp_a"),
        _evaluation(3, "evl_running", "exp_a", "running", result=None, completed_at=None),
    )
    convert(context)
    write_bootstrap_marker(context.staging)

    store = DecisionStore(context.staging / "decisions.db")
    try:
        assert [item["id"] for item in store.list()] == ["exp_a"]
        assert store.get("exp_a")["draft"] == {"title": "T"}
        assert store.evaluation("evl_running")["status"] == "interrupted"
        with store.database.read() as connection:
            assert (
                connection.execute("PRAGMA application_id").fetchone()[0]
                == APPLICATION_IDS["decisions"]
            )
    finally:
        store.close()


def test_unreadable_rows_and_orphans_are_dropped_and_reported(tmp_path: Path) -> None:
    context = _context(tmp_path)
    _source(
        context,
        _experiment("exp_a"),
        _experiment("exp_broken", draft="{not json"),
        _evaluation(1, "evl_of_broken", "exp_broken"),
        _evaluation(2, "evl_orphan", "exp_missing"),
        _evaluation(3, "evl_bad_result", "exp_a", result="[1,"),
        _evaluation(4, "evl_odd_time", "exp_a", created_at="yesterday"),
    )

    convert(context)

    assert _staged_rows(context, "SELECT id FROM evaluations") == [("evl_odd_time",)]
    assert _staged_rows(context, "SELECT created_at FROM evaluations") == [("yesterday",)]
    assert context.report.counts[AREA] == {"experiments": 1, "evaluations": 1}
    skipped = {(item.item, item.reason.split(":")[0]) for item in context.report.skipped}
    assert skipped == {
        ("experiments exp_broken", "row dropped"),
        ("evaluations evl_of_broken", "row dropped"),
        ("evaluations evl_orphan", "row dropped"),
        ("evaluations evl_bad_result", "row dropped"),
        ("evaluations evl_odd_time", "created_at 'yesterday' kept"),
    }


def test_a_repeated_run_replaces_its_staged_database(tmp_path: Path) -> None:
    context = _context(tmp_path)
    _source(context, _experiment("exp_a"))

    convert(context)
    convert(ConversionContext(source=context.source, staging=context.staging))

    assert _staged_rows(context, "SELECT id FROM experiments") == [("exp_a",)]


def test_a_missing_source_stages_nothing(tmp_path: Path) -> None:
    context = _context(tmp_path)

    convert(context)

    assert context.report.counts[AREA] == {"source_missing": 1}
    assert not (context.staging / "decisions.db").exists()


def test_an_already_converted_source_is_left_alone(tmp_path: Path) -> None:
    context = _context(tmp_path)
    open_offline_database(decision_database_spec(context.source / "decisions.db")).close()

    convert(context)

    assert context.report.counts[AREA] == {"already_current": 1}
    assert not (context.staging / "decisions.db").exists()


def test_a_foreign_database_is_refused(tmp_path: Path) -> None:
    context = _context(tmp_path)
    path = _source(context)
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("PRAGMA application_id = 1234")

    with pytest.raises(ConversionError, match="not a pre-Generation-1 decisions database"):
        convert(context)


def test_source_journal_files_are_retired(tmp_path: Path) -> None:
    context = _context(tmp_path)
    _source(context, _experiment("exp_a"))
    (context.source / "decisions.db-journal").write_bytes(b"")

    convert(context)

    assert context.retired == [PurePosixPath("decisions.db-journal")]
