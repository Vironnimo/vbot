"""Durable experiment drafts and immutable evaluation snapshots."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.model_tasks.decision_types import DecisionError, text, validate_draft
from core.utils.ids import new_id
from core.utils.logging import get_logger

_LOGGER = get_logger("decisions")


# Reads name their columns and never use ``SELECT *``: an older vBot must not
# pick up columns a newer one adds.
_EXPERIMENT_COLUMNS = "id,title,revision,draft,created_at,updated_at"
_EVALUATION_COLUMNS = (
    "sequence,id,experiment_id,request_id,status,snapshot,result,error,created_at,completed_at"
)


def now() -> str:
    return datetime.now(UTC).isoformat()


class DecisionStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS experiments (
                    id TEXT PRIMARY KEY, title TEXT NOT NULL, revision INTEGER NOT NULL,
                    draft TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                ) STRICT;
                CREATE TABLE IF NOT EXISTS evaluations (
                    sequence INTEGER PRIMARY KEY, id TEXT UNIQUE NOT NULL,
                    experiment_id TEXT NOT NULL REFERENCES experiments(id) ON DELETE CASCADE,
                    request_id TEXT UNIQUE NOT NULL, status TEXT NOT NULL,
                    snapshot TEXT NOT NULL, result TEXT, error TEXT,
                    created_at TEXT NOT NULL, completed_at TEXT
                ) STRICT;
                CREATE INDEX IF NOT EXISTS evaluation_history
                    ON evaluations(experiment_id, sequence DESC);
            """)
            db.execute(
                (
                    "UPDATE evaluations SET status='interrupted', "
                    "completed_at=? WHERE status='running'"
                ),
                (now(),),
            )

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA synchronous=FULL")
        try:
            with db:
                yield db
        finally:
            db.close()

    def list(self) -> list[dict[str, Any]]:
        with self.connection() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT id,title,revision,created_at,updated_at FROM "
                    "experiments ORDER BY updated_at DESC,id"
                )
            ]

    def get(self, identifier: str) -> dict[str, Any]:
        with self.connection() as db:
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
        stamp = now()
        with self.connection() as db:
            if identifier is None:

                def claim(candidate: str) -> bool:
                    return (
                        db.execute(
                            "INSERT OR IGNORE INTO experiments VALUES (?,?,1,?,?,?)",
                            (candidate, title, encoded, stamp, stamp),
                        ).rowcount
                        == 1
                    )

                identifier = new_id("exp", claim=claim)
            else:
                if isinstance(revision, bool) or not isinstance(revision, int):
                    raise DecisionError("Supply the experiment revision before saving.")
                changed = db.execute(
                    (
                        "UPDATE experiments SET "
                        "title=?,draft=?,revision=revision+1,updated_at=? "
                        "WHERE id=? AND revision=?"
                    ),
                    (title, encoded, stamp, identifier, revision),
                )
                if changed.rowcount != 1:
                    raise DecisionError(
                        "This experiment changed elsewhere. Reload it before saving your changes.",
                        code="conflict",
                    )
            row = db.execute(
                f"SELECT {_EXPERIMENT_COLUMNS} FROM experiments WHERE id=?", (identifier,)
            ).fetchone()
        _LOGGER.info("Decision experiment saved (id=%s)", identifier)
        return {**dict(row), "draft": json.loads(row["draft"])}

    def delete(self, identifier: str, revision: int) -> None:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute(
                "SELECT 1 FROM evaluations WHERE experiment_id=? AND status='running'",
                (identifier,),
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
        _LOGGER.info("Decision experiment deleted (id=%s)", identifier)

    def history(self, identifier: str, before: int | None = None) -> dict[str, Any]:
        self.get(identifier)
        with self.connection() as db:
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
        with self.connection() as db:
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
        with self.connection() as db:
            row = db.execute(
                "SELECT id FROM evaluations WHERE request_id=?", (request_id,)
            ).fetchone()
        return self.evaluation(row["id"]) if row else None

    def begin(
        self, identifier: str, revision: int, request_id: str, snapshot: dict[str, Any]
    ) -> dict[str, Any]:
        text(request_id, "Request id", maximum=128)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
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
                "SELECT 1 FROM evaluations WHERE experiment_id=? AND status='running'",
                (identifier,),
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
                            "VALUES (?,?,?,'running',?,?)"
                        ),
                        (
                            candidate,
                            identifier,
                            request_id,
                            json.dumps(snapshot, ensure_ascii=False, allow_nan=False),
                            now(),
                        ),
                    ).rowcount
                    == 1
                )

            evaluation_id = new_id("evl", claim=claim)
        _LOGGER.info("Decision evaluation started (id=%s experiment=%s)", evaluation_id, identifier)
        return self.evaluation(evaluation_id)

    def finish(
        self, identifier: str, status: str, *, result: Any = None, error: Any = None
    ) -> None:
        with self.connection() as db:
            changed = db.execute(
                (
                    "UPDATE evaluations SET "
                    "status=?,result=COALESCE(?,result),error=?,completed_at=? "
                    "WHERE id=? AND status='running'"
                ),
                (
                    status,
                    json.dumps(result) if result is not None else None,
                    json.dumps(error) if error is not None else None,
                    now(),
                    identifier,
                ),
            )
        if changed.rowcount:
            _LOGGER.info("Decision evaluation finished (id=%s status=%s)", identifier, status)

    def progress(self, identifier: str, result: dict[str, Any]) -> None:
        # Keep detailed live history bounded by bytes as well as step count.
        encoded = json.dumps(result, allow_nan=False)
        while len(encoded.encode("utf-8")) > 1_000_000 and len(result["steps"]) > 1:
            result["steps"].pop(0)
            encoded = json.dumps(result, allow_nan=False)
        with self.connection() as db:
            db.execute(
                "UPDATE evaluations SET result=? WHERE id=? AND status='running'",
                (encoded, identifier),
            )
