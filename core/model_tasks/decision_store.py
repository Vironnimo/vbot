"""Durable experiment drafts and immutable evaluation snapshots in ``decisions.db``.

The database is a canonical member of the data store and opens through the
shared kernel (``core/database``): identity, format generation, additive schema
evolution, snapshots and recovery come from there. This module owns the DDL,
the queries and the startup recovery that marks unfinished evaluations
interrupted.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from core.database import (
    APPLICATION_IDS,
    CANONICAL,
    Database,
    DatabaseSpec,
    SnapshotFacts,
    open_database,
)
from core.database.marker import utc_now
from core.model_tasks.decision_types import DecisionError, text, validate_draft
from core.utils.ids import new_id
from core.utils.logging import get_logger

_LOGGER = get_logger("decisions")

DATABASE_NAME = "decisions"
FORMAT_GENERATION = 1

# Additive only within a format generation (``database.md``). Status values are
# validated in code, never by a CHECK constraint, so a new status stays additive.
SCHEMA_SQL = """
CREATE TABLE experiments (
  id         TEXT PRIMARY KEY,
  title      TEXT NOT NULL,
  revision   INTEGER NOT NULL,
  draft      TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
) STRICT;

CREATE TABLE evaluations (
  sequence      INTEGER PRIMARY KEY,
  id            TEXT UNIQUE NOT NULL,
  experiment_id TEXT NOT NULL REFERENCES experiments (id) ON DELETE CASCADE,
  request_id    TEXT UNIQUE NOT NULL,
  status        TEXT NOT NULL,
  snapshot      TEXT NOT NULL,
  result        TEXT,
  error         TEXT,
  created_at    TEXT NOT NULL,
  completed_at  TEXT
) STRICT;

-- Reader: DecisionStore.history pages one experiment's evaluations by sequence.
CREATE INDEX evaluation_history ON evaluations (experiment_id, sequence DESC);
"""

_SNAPSHOT_FACTS = SnapshotFacts(
    {
        "experiment_count": "SELECT COUNT(*) FROM experiments",
        "evaluation_count": "SELECT COUNT(*) FROM evaluations",
    }
)

RUNNING = "running"
TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled", "interrupted"})

# Reads name their columns and never use ``SELECT *``: an older vBot must not
# pick up columns a newer one adds.
_EXPERIMENT_COLUMNS = "id,title,revision,draft,created_at,updated_at"
_EVALUATION_COLUMNS = (
    "sequence,id,experiment_id,request_id,status,snapshot,result,error,created_at,completed_at"
)


def now() -> str:
    """Canonical fixed-width UTC timestamp for stored values."""
    return utc_now()


def decision_database_spec(path: Path) -> DatabaseSpec:
    """Declare the canonical decisions database at ``path`` (``<data-dir>/decisions.db``)."""
    return DatabaseSpec(
        name=DATABASE_NAME,
        path=Path(path),
        profile=CANONICAL,
        application_id=APPLICATION_IDS[DATABASE_NAME],
        format_generation=FORMAT_GENERATION,
        schema_sql=SCHEMA_SQL,
        snapshot_facts=_SNAPSHOT_FACTS,
    )


class DecisionStore:
    """Blocking experiment and evaluation persistence on one kernel database."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.database: Database = open_database(decision_database_spec(self.path))
        try:
            self._interrupt_unfinished()
        except BaseException:
            self.database.close()
            raise

    def close(self) -> None:
        self.database.close()

    def _interrupt_unfinished(self) -> None:
        """Startup recovery: an evaluation still running belongs to a stopped process."""

        def operation(connection: sqlite3.Connection) -> int:
            return connection.execute(
                "UPDATE evaluations SET status='interrupted', completed_at=? WHERE status=?",
                (now(), RUNNING),
            ).rowcount

        interrupted = self.database.write(operation)
        if interrupted:
            _LOGGER.info("Decision evaluations interrupted by restart (count=%s)", interrupted)

    def list(self) -> list[dict[str, Any]]:
        with self.database.read() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT id,title,revision,created_at,updated_at FROM "
                    "experiments ORDER BY updated_at DESC,id"
                )
            ]

    def get(self, identifier: str) -> dict[str, Any]:
        with self.database.read() as db:
            row = db.execute(
                f"SELECT {_EXPERIMENT_COLUMNS} FROM experiments WHERE id=?", (identifier,)
            ).fetchone()
        if row is None:
            raise DecisionError(
                "Experiment no longer exists. Refresh the experiment list.", code="not_found"
            )
        return {**dict(row), "draft": json.loads(row["draft"])}

    def save(
        self, draft: Any, identifier: str | None = None, revision: int | None = None
    ) -> dict[str, Any]:
        draft = validate_draft(draft)
        title = draft["title"]
        encoded = json.dumps(draft, ensure_ascii=False, allow_nan=False)
        if identifier is not None and (isinstance(revision, bool) or not isinstance(revision, int)):
            raise DecisionError("Supply the experiment revision before saving.")

        def operation(db: sqlite3.Connection) -> sqlite3.Row:
            stamp = now()
            saved_id = identifier
            if saved_id is None:

                def claim(candidate: str) -> bool:
                    return (
                        db.execute(
                            "INSERT OR IGNORE INTO experiments "
                            "(id,title,revision,draft,created_at,updated_at) "
                            "VALUES (?,?,1,?,?,?)",
                            (candidate, title, encoded, stamp, stamp),
                        ).rowcount
                        == 1
                    )

                saved_id = new_id("exp", claim=claim)
            else:
                changed = db.execute(
                    (
                        "UPDATE experiments SET "
                        "title=?,draft=?,revision=revision+1,updated_at=? "
                        "WHERE id=? AND revision=?"
                    ),
                    (title, encoded, stamp, saved_id, revision),
                )
                if changed.rowcount != 1:
                    raise DecisionError(
                        "This experiment changed elsewhere. Reload it before saving your changes.",
                        code="conflict",
                    )
            row: sqlite3.Row = db.execute(
                f"SELECT {_EXPERIMENT_COLUMNS} FROM experiments WHERE id=?", (saved_id,)
            ).fetchone()
            return row

        row = self.database.write(operation)
        _LOGGER.info("Decision experiment saved (id=%s)", row["id"])
        return {**dict(row), "draft": json.loads(row["draft"])}

    def delete(self, identifier: str, revision: int) -> None:
        def operation(db: sqlite3.Connection) -> None:
            if db.execute(
                "SELECT 1 FROM evaluations WHERE experiment_id=? AND status=?",
                (identifier, RUNNING),
            ).fetchone():
                raise DecisionError(
                    "Cancel the active evaluation before deleting this experiment.", code="conflict"
                )
            if (
                db.execute(
                    "DELETE FROM experiments WHERE id=? AND revision=?", (identifier, revision)
                ).rowcount
                != 1
            ):
                raise DecisionError(
                    "This experiment changed elsewhere. Reload before deleting it.", code="conflict"
                )

        self.database.write(operation)
        _LOGGER.info("Decision experiment deleted (id=%s)", identifier)

    def history(self, identifier: str, before: int | None = None) -> dict[str, Any]:
        self.get(identifier)
        with self.database.read() as db:
            rows = db.execute(
                (
                    "SELECT sequence,id,status,created_at,completed_at FROM "
                    "evaluations WHERE experiment_id=? AND (? IS NULL OR "
                    "sequence < ?) ORDER BY sequence DESC LIMIT 51"
                ),
                (identifier, before, before),
            ).fetchall()
        return {
            "evaluations": [dict(row) for row in rows[:50]],
            "next_before": rows[49]["sequence"] if len(rows) > 50 else None,
        }

    def evaluation(self, identifier: str) -> dict[str, Any]:
        with self.database.read() as db:
            row = db.execute(
                f"SELECT {_EVALUATION_COLUMNS} FROM evaluations WHERE id=?", (identifier,)
            ).fetchone()
        if row is None:
            raise DecisionError("Evaluation no longer exists.", code="not_found")
        value = dict(row)
        for key in ("snapshot", "result", "error"):
            value[key] = json.loads(value[key]) if value[key] is not None else None
        return value

    def request(self, request_id: str) -> dict[str, Any] | None:
        with self.database.read() as db:
            row = db.execute(
                "SELECT id FROM evaluations WHERE request_id=?", (request_id,)
            ).fetchone()
        return self.evaluation(row["id"]) if row else None

    def begin(
        self, identifier: str, revision: int, request_id: str, snapshot: dict[str, Any]
    ) -> dict[str, Any]:
        text(request_id, "Request id", maximum=128)
        encoded = json.dumps(snapshot, ensure_ascii=False, allow_nan=False)

        def operation(db: sqlite3.Connection) -> str:
            if db.execute("SELECT 1 FROM evaluations WHERE request_id=?", (request_id,)).fetchone():
                raise DecisionError("Request id already belongs to an evaluation.", code="conflict")
            row = db.execute(
                "SELECT revision FROM experiments WHERE id=?", (identifier,)
            ).fetchone()
            if row is None or row["revision"] != revision:
                raise DecisionError(
                    "The experiment changed. Save or reload it before evaluating.", code="conflict"
                )
            if db.execute(
                "SELECT 1 FROM evaluations WHERE experiment_id=? AND status=?",
                (identifier, RUNNING),
            ).fetchone():
                raise DecisionError(
                    "This experiment already has an active evaluation.", code="conflict"
                )

            def claim(candidate: str) -> bool:
                return (
                    db.execute(
                        (
                            "INSERT OR IGNORE INTO evaluations "
                            "(id,experiment_id,request_id,status,snapshot,created_at) "
                            "VALUES (?,?,?,?,?,?)"
                        ),
                        (candidate, identifier, request_id, RUNNING, encoded, now()),
                    ).rowcount
                    == 1
                )

            return new_id("evl", claim=claim)

        evaluation_id = self.database.write(operation)
        _LOGGER.info("Decision evaluation started (id=%s experiment=%s)", evaluation_id, identifier)
        return self.evaluation(evaluation_id)

    def finish(
        self, identifier: str, status: str, *, result: Any = None, error: Any = None
    ) -> None:
        if status not in TERMINAL_STATUSES:
            raise ValueError(f"unknown terminal evaluation status: {status!r}")
        encoded_result = json.dumps(result) if result is not None else None
        encoded_error = json.dumps(error) if error is not None else None

        def operation(db: sqlite3.Connection) -> int:
            return db.execute(
                (
                    "UPDATE evaluations SET "
                    "status=?,result=COALESCE(?,result),error=?,completed_at=? "
                    "WHERE id=? AND status=?"
                ),
                (status, encoded_result, encoded_error, now(), identifier, RUNNING),
            ).rowcount

        if self.database.write(operation):
            _LOGGER.info("Decision evaluation finished (id=%s status=%s)", identifier, status)

    def progress(self, identifier: str, result: dict[str, Any]) -> None:
        # Keep detailed live history bounded by bytes as well as step count.
        encoded = json.dumps(result, allow_nan=False)
        while len(encoded.encode("utf-8")) > 1_000_000 and len(result["steps"]) > 1:
            result["steps"].pop(0)
            encoded = json.dumps(result, allow_nan=False)

        def operation(db: sqlite3.Connection) -> None:
            db.execute(
                "UPDATE evaluations SET result=? WHERE id=? AND status=?",
                (encoded, identifier, RUNNING),
            )

        self.database.write(operation)
