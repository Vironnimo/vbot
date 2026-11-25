"""Shared native record persistence and structural validation.

Actor authority is established by the service; this owner validates references,
revisions, execution epochs and the lifecycle facts that must commit together.
"""

from __future__ import annotations

import json
import sqlite3
from copy import deepcopy
from typing import Any

from core.extensions.databases import Database
from core.utils.ids import new_id
from core.utils.timestamps import utc_now_timestamp

type Json = dict[str, Any]

TASK_STATES = frozenset(
    {"backlog", "ready", "working", "blocked", "paused", "review", "done", "cancelled"}
)
FINAL_TASK_STATES = frozenset({"done", "cancelled"})
_TABLES = {
    "factory": ("native_factories", ("project_id", "state")),
    "member": ("native_members", ("factory_id", "agent_id", "state")),
    "task": (
        "native_tasks",
        ("factory_id", "parent_id", "assignee_id", "state", "priority", "epoch"),
    ),
    "topic": ("native_topics", ("factory_id", "task_id", "state")),
    "execution": (
        "native_executions",
        (
            "factory_id",
            "kind",
            "scope_id",
            "member_id",
            "epoch",
            "state",
            "group_id",
            "participant_id",
            "input_id",
        ),
    ),
    "delivery": ("native_deliveries", ("factory_id", "execution_id", "state", "receipt")),
}


def _new_id(connection: sqlite3.Connection, prefix: str) -> str:
    """Allocate under the caller's immediate write transaction."""
    table, column = {
        "fac": ("native_factories", "id"),
        "fmb": ("native_members", "id"),
        "ftk": ("native_tasks", "id"),
        "ftp": ("native_topics", "id"),
        "fpt": ("native_posts", "id"),
        "fex": ("native_executions", "id"),
        "fdl": ("native_deliveries", "id"),
        "fag": ("native_identity_reservations", "agent_id"),
    }[prefix]
    return new_id(
        prefix,
        claim=lambda candidate: (
            connection.execute(f"SELECT 1 FROM {table} WHERE {column} = ?", (candidate,)).fetchone()
            is None
        ),
    )


def _error(code: str, message: str) -> Any:
    # Store owns the public error; import lazily to keep its composition acyclic.
    from .store import FactoryError

    return FactoryError(code, message)


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _merge(base: Json, patch: Json) -> Json:
    """Patch known fields without erasing newer nested JSON members."""
    result = deepcopy(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


def _columns(kind: str) -> str:
    return ", ".join(("id", *_TABLES[kind][1], "revision", "created_at", "updated_at", "body_json"))


def _decode(row: sqlite3.Row) -> Json:
    record = dict(row)
    body = json.loads(record.pop("body_json"))
    return {**body, **record}


def _get(connection: sqlite3.Connection, kind: str, record_id: str) -> Json:
    table = _TABLES[kind][0]
    row = connection.execute(
        f"SELECT {_columns(kind)} FROM {table} WHERE id = ?", (record_id,)
    ).fetchone()
    if row is None:
        raise _error(f"{kind}_not_found", f"This {kind} no longer exists. Refresh the Factory.")
    record = _decode(row)
    if kind == "task":
        record["dependencies"] = [
            item[0]
            for item in connection.execute(
                "SELECT dependency_id FROM native_task_dependencies WHERE task_id = ? "
                "ORDER BY dependency_id",
                (record_id,),
            )
        ]
    elif kind == "topic":
        record["participants"] = [
            item[0]
            for item in connection.execute(
                "SELECT member_id FROM native_topic_participants WHERE topic_id = ? ORDER "
                "BY member_id",
                (record_id,),
            )
        ]
    return record


def _event(
    connection: sqlite3.Connection,
    kind: str,
    record: Json,
    actor: str,
    event: str,
    detail: Json | None = None,
) -> None:
    connection.execute(
        "INSERT INTO native_events (factory_id, kind, scope_id, actor, event, "
        "created_at, detail_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            record["id"] if kind == "factory" else record["factory_id"],
            kind,
            record["id"],
            actor,
            event,
            utc_now_timestamp(),
            _dump(detail or {}),
        ),
    )


def _insert(connection: sqlite3.Connection, kind: str, record: Json, actor: str) -> Json:
    table, fields = _TABLES[kind]
    now = utc_now_timestamp()
    value = {**record, "revision": 1, "created_at": now, "updated_at": now}
    columns = ("id", *fields, "revision", "created_at", "updated_at", "body_json")
    body = {
        key: item
        for key, item in value.items()
        if key not in columns and key not in {"dependencies", "participants"}
    }
    connection.execute(
        f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
        (*[value[key] for key in columns[:-1]], _dump(body)),
    )
    _event(connection, kind, value, actor, "created")
    return value


def _revision(
    record: Json, expected_revision: int | None, expected_epoch: int | None = None
) -> None:
    if expected_revision is not None and expected_revision != record["revision"]:
        raise _error("revision_conflict", "The record changed elsewhere. Refresh before saving.")
    if expected_epoch is not None and expected_epoch != record.get("epoch"):
        raise _error(
            "stale_execution", "This execution no longer owns the task. Refresh its assignment."
        )


def _save(
    connection: sqlite3.Connection, kind: str, previous: Json, patch: Json, actor: str, event: str
) -> Json:
    table, fields = _TABLES[kind]
    if any(
        key in patch and patch[key] != previous[key]
        for key in ("id", "created_at", "factory_id")
        if key in previous
    ):
        raise _error("immutable_field", "Record identity and Factory cannot be changed.")
    value = _merge(previous, patch)
    value.update(revision=previous["revision"] + 1, updated_at=utc_now_timestamp())
    columns = (*fields, "revision", "updated_at", "body_json")
    body = {
        key: item
        for key, item in value.items()
        if key not in {"id", "created_at", *columns, "dependencies", "participants"}
    }
    connection.execute(
        f"UPDATE {table} SET {', '.join(f'{key} = ?' for key in columns)} WHERE id = ?",
        (*[value[key] for key in columns[:-1]], _dump(body), previous["id"]),
    )
    _event(connection, kind, value, actor, event, {"revision": value["revision"]})
    return value


def _text(value: Any, field: str, maximum: int = 20000, *, required: bool = False) -> str:
    if not isinstance(value, str) or len(value) > maximum or (required and not value.strip()):
        raise _error(
            "invalid_field",
            f"{field} must be {'nonempty ' if required else ''}text "
            f"of at most {maximum} characters.",
        )
    return value.strip()


def _capacity(value: Any) -> int:
    if type(value) is not int or not 1 <= value <= 10000:
        raise _error("invalid_capacity", "Capacity must be an integer between 1 and 10000.")
    return value


def _member(
    connection: sqlite3.Connection, factory_id: str, member_id: str, *, active: bool = True
) -> Json:
    member = _get(connection, "member", member_id)
    if member["factory_id"] != factory_id:
        raise _error("factory_mismatch", "Choose a member of this Factory.")
    if active and member["state"] != "active":
        raise _error("member_retired", "This member is retired. Choose an active member.")
    return member


def _ids(value: Any, field: str, maximum: int = 100) -> list[str]:
    if (
        not isinstance(value, (list, tuple))
        or len(value) > maximum
        or any(not isinstance(item, str) or not item for item in value)
    ):
        raise _error("invalid_field", f"{field} must contain at most {maximum} exact ids.")
    return sorted(set(value))


def _task_links(connection: sqlite3.Connection, record: Json, previous: Json | None = None) -> None:
    """Validate the proposed hierarchy and dependency graph before writing."""
    task_id, factory_id = record["id"], record["factory_id"]
    parent_id = record["parent_id"]
    visited = {task_id}
    while parent_id:
        if parent_id in visited:
            raise _error("task_cycle", "A task cannot be its own ancestor.")
        visited.add(parent_id)
        parent = _get(connection, "task", parent_id)
        if parent["factory_id"] != factory_id:
            raise _error("factory_mismatch", "Parent tasks must belong to the same Factory.")
        if (
            parent["id"] == record["parent_id"]
            and parent["state"] in {"done", "review"}
            and (previous is None or previous["parent_id"] != record["parent_id"])
        ):
            raise _error(
                "parent_finished", "Reopen the parent before adding unfinished child work."
            )
        parent_id = parent["parent_id"]
    dependencies = _ids(record["dependencies"], "dependencies")
    for dependency_id in dependencies:
        dependency = _get(connection, "task", dependency_id)
        if dependency["factory_id"] != factory_id:
            raise _error("factory_mismatch", "Dependencies must belong to the same Factory.")
        if dependency_id == task_id:
            raise _error("task_cycle", "A task cannot depend on itself.")
        cycle = connection.execute(
            "WITH RECURSIVE reachable(id) AS (SELECT ? UNION SELECT d.dependency_id "
            "FROM native_task_dependencies d JOIN reachable r ON d.task_id = r.id) "
            "SELECT 1 FROM reachable WHERE id = ? LIMIT 1",
            (dependency_id, task_id),
        ).fetchone()
        if cycle:
            raise _error("task_cycle", "Task dependencies must not form a cycle.")
    if record["assignee_id"]:
        _member(
            connection,
            factory_id,
            record["assignee_id"],
            active=previous is None or previous["assignee_id"] != record["assignee_id"],
        )
    if record.get("reviewer_id"):
        _member(
            connection,
            factory_id,
            record["reviewer_id"],
            active=previous is None or previous.get("reviewer_id") != record["reviewer_id"],
        )
    # Completion waits both for explicit prerequisites and child tasks. Check
    # their combined graph: an individually acyclic hierarchy/dependency graph
    # can otherwise deadlock when a child depends on its parent.
    added_edges = [(task_id, dependency) for dependency in dependencies]
    if record["parent_id"]:
        added_edges.append((record["parent_id"], task_id))
    additions = "".join(" UNION SELECT ?, ?" for _ in added_edges)
    parameters: list[Any] = [task_id, task_id]
    for source, target in added_edges:
        parameters.extend((source, target))
    parameters.extend((task_id, task_id))
    cycle = connection.execute(
        "WITH RECURSIVE edges(source, target) AS ("
        "SELECT task_id, dependency_id FROM native_task_dependencies WHERE task_id != ? "
        "UNION SELECT parent_id, id FROM native_tasks WHERE parent_id != '' AND id != ?"
        + additions
        + "), reachable(id) AS (SELECT target FROM edges WHERE source = ? "
        "UNION SELECT e.target FROM edges e JOIN reachable r ON e.source = r.id) "
        "SELECT 1 FROM reachable WHERE id = ? LIMIT 1",
        parameters,
    ).fetchone()
    if cycle:
        raise _error("task_cycle", "Task prerequisites and child completion must not form a cycle.")


def _write_dependencies(connection: sqlite3.Connection, record: Json) -> None:
    connection.execute("DELETE FROM native_task_dependencies WHERE task_id = ?", (record["id"],))
    connection.executemany(
        "INSERT INTO native_task_dependencies (task_id, dependency_id) VALUES (?, ?)",
        [(record["id"], item) for item in record["dependencies"]],
    )


def _completion_ready(connection: sqlite3.Connection, task: Json) -> None:
    if not task["assignee_id"]:
        raise _error("unassigned_task", "Assign a responsible member before completing this task.")
    unfinished = connection.execute(
        "SELECT id FROM native_tasks WHERE parent_id = ? AND state != 'done' LIMIT 1", (task["id"],)
    ).fetchone()
    if unfinished:
        raise _error(
            "children_unfinished",
            "Complete the required child tasks before completing their parent.",
        )
    unfinished = connection.execute(
        "SELECT d.dependency_id FROM native_task_dependencies d JOIN native_tasks t "
        "ON t.id = d.dependency_id WHERE d.task_id = ? AND t.state != 'done' LIMIT 1",
        (task["id"],),
    ).fetchone()
    if unfinished:
        raise _error(
            "dependencies_unfinished", "Complete the prerequisites before completing this task."
        )


class NativeRecordStore:
    """Common record I/O inside the Factory persistence owner."""

    def __init__(self, database: Database) -> None:
        self.database = database

    async def _get(self, kind: str, record_id: str) -> Json:
        return await self.database.read_async(lambda connection: _get(connection, kind, record_id))

    async def _list(self, kind: str, filters: Json, after: str, limit: int) -> list[Json]:
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise _error("invalid_limit", "Choose a page size between 1 and 1000.")
        table, fields = _TABLES[kind]
        if set(filters) - set(fields):
            raise ValueError("Unknown native list filter")
        clauses = ["id > ?"]
        parameters: list[Any] = [after]
        for key, value in filters.items():
            if value is not None:
                clauses.append(f"{key} = ?")
                parameters.append(value)
        parameters.append(limit)

        def read(connection: sqlite3.Connection) -> list[Json]:
            return [
                _get(connection, kind, row[0])
                for row in connection.execute(
                    f"SELECT id FROM {table} WHERE {' AND '.join(clauses)} ORDER BY id LIMIT ?",
                    parameters,
                ).fetchall()
            ]

        return await self.database.read_async(read)

    async def events(
        self, kind: str, scope_id: str, *, after: int = 0, limit: int = 100
    ) -> list[Json]:
        if type(limit) is not int or not 1 <= limit <= 1000 or type(after) is not int or after < 0:
            raise _error("invalid_limit", "Choose a valid event page.")

        def read(connection: sqlite3.Connection) -> list[Json]:
            _get(connection, kind, scope_id)
            result = []
            for row in connection.execute(
                "SELECT id, factory_id, kind, scope_id, actor, event, created_at, "
                "detail_json FROM native_events WHERE kind = ? AND scope_id = ? AND id > ? "
                "ORDER BY id LIMIT ?",
                (kind, scope_id, after, limit),
            ):
                record = dict(row)
                record["detail"] = json.loads(record.pop("detail_json"))
                result.append(record)
            return result

        return await self.database.read_async(read)
