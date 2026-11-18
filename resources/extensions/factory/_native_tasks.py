"""Native task agreements, hierarchy, completion and review."""

from __future__ import annotations

import math
import sqlite3
from copy import deepcopy
from typing import Any

from core.utils.timestamps import utc_now_timestamp

from ._native_dispatch import NativeDispatchStore
from ._native_records import (
    TASK_STATES,
    Json,
    _completion_ready,
    _decode,
    _dump,
    _error,
    _get,
    _ids,
    _insert,
    _merge,
    _new_id,
    _revision,
    _save,
    _task_links,
    _text,
    _write_dependencies,
)


class NativeTaskStore(NativeDispatchStore):
    async def create_task(
        self,
        factory_id: str,
        title: str,
        objective: str,
        *,
        assignee_id: str = "",
        parent_id: str = "",
        dependencies: Any = (),
        conditions: str = "",
        priority: float = 0,
        actor: str = "user",
        conversation_id: str = "",
        **fields: Any,
    ) -> Json:
        title, objective, conditions = (
            _text(title, "title", 200, required=True),
            _text(objective, "objective", required=True),
            _text(conditions, "conditions"),
        )
        if (
            isinstance(priority, bool)
            or not isinstance(priority, (int, float))
            or not math.isfinite(priority)
        ):
            raise _error("invalid_priority", "Priority must be a finite number.")

        def write(connection: sqlite3.Connection) -> Json:
            factory = _get(connection, "factory", factory_id)
            if factory["state"] == "archived":
                raise _error("factory_archived", "This Factory is archived.")
            topic = _get(connection, "topic", conversation_id) if conversation_id else None
            if topic:
                if topic["factory_id"] != factory_id or topic["state"] != "open":
                    raise _error("outside_factory", "Use an open conversation in this Factory.")
                if topic["task_id"]:
                    existing = _get(connection, "task", topic["task_id"])
                    requested = {
                        "title": title,
                        "objective": objective,
                        "conditions": conditions,
                        "priority": priority,
                        "assignee_id": assignee_id,
                        "parent_id": parent_id,
                        "dependencies": _ids(dependencies, "dependencies"),
                        **fields,
                    }
                    if all(existing.get(key) == value for key, value in requested.items()):
                        return existing
                    raise _error(
                        "task_exists",
                        "This conversation already has a task. Read it with factory_tasks get.",
                    )
            record = {
                "conditions": conditions,
                "reason": "",
                "runtime": {},
                "limits": {},
                "consumption": {},
                "workflow_id": "",
                "workflow_card_id": "",
                "reviewer_id": "",
                "user_approval": False,
                "result": {},
                **fields,
                "id": _new_id(connection, "ftk"),
                "factory_id": factory_id,
                "title": title,
                "objective": objective,
                "assignee_id": assignee_id,
                "parent_id": parent_id,
                "dependencies": _ids(dependencies, "dependencies"),
                "priority": priority,
                "epoch": 1,
                "state": fields.get("state", "ready" if assignee_id else "backlog"),
            }
            if record["state"] not in {"ready", "backlog"}:
                raise _error("invalid_state", "New tasks start in backlog or ready.")
            _task_links(connection, record)
            if topic:
                record["conversation_id"] = topic["id"]
                last = (
                    connection.execute(
                        "SELECT MAX(sequence) FROM native_posts WHERE topic_id = ?", (topic["id"],)
                    ).fetchone()[0]
                    or 0
                )
                record["runtime"] = {"topic_sequences": {topic["id"]: last}}
                _save(connection, "topic", topic, {"task_id": record["id"]}, actor, "task_started")
                if assignee_id and assignee_id not in topic["participants"]:
                    connection.execute(
                        "INSERT INTO native_topic_participants(topic_id, member_id, joined_at) "
                        "VALUES (?, ?, ?)",
                        (topic["id"], assignee_id, utc_now_timestamp()),
                    )
            _insert(connection, "task", record, actor)
            _write_dependencies(connection, record)
            return _get(connection, "task", record["id"])

        return await self.database.write_async(write)

    async def get_task(self, task_id: str) -> Json:
        return await self._get("task", task_id)

    async def list_tasks(
        self,
        factory_id: str | None = None,
        *,
        state: str | None = None,
        assignee_id: str | None = None,
        parent_id: str | None = None,
        after: str = "",
        limit: int = 100,
    ) -> list[Json]:
        return await self._list(
            "task",
            {
                "factory_id": factory_id,
                "state": state,
                "assignee_id": assignee_id,
                "parent_id": parent_id,
            },
            after,
            limit,
        )

    async def change_task(
        self,
        task_id: str,
        patch: Json,
        *,
        actor: str = "factory",
        event: str = "updated",
        expected_revision: int | None = None,
        expected_epoch: int | None = None,
        result: Json | None = None,
        intent: Json | None = None,
    ) -> Json:
        def write(connection: sqlite3.Connection) -> Json:
            return self._change_task(
                connection,
                task_id,
                patch,
                actor,
                event,
                expected_revision,
                expected_epoch,
                result,
                intent,
            )

        return await self.database.write_async(write)

    def _change_task(
        self,
        connection: sqlite3.Connection,
        task_id: str,
        patch: Json,
        actor: str,
        event: str,
        expected_revision: int | None,
        expected_epoch: int | None,
        result: Json | None,
        intent: Json | None,
    ) -> Json:
        previous = _get(connection, "task", task_id)
        _revision(previous, expected_revision, expected_epoch)
        proposed = _merge(previous, patch)
        if proposed["state"] not in TASK_STATES:
            raise _error("invalid_state", "Choose a supported task state.")
        if type(proposed["epoch"]) is not int or proposed["epoch"] < previous["epoch"]:
            raise _error("stale_execution", "Task execution epochs cannot move backwards.")
        for field, maximum in (("title", 200), ("objective", 20000), ("conditions", 20000)):
            proposed[field] = _text(proposed[field], field, maximum, required=field != "conditions")
        if (
            isinstance(proposed["priority"], bool)
            or not isinstance(proposed["priority"], (int, float))
            or not math.isfinite(proposed["priority"])
        ):
            raise _error("invalid_priority", "Priority must be a finite number.")
        proposed["dependencies"] = _ids(proposed["dependencies"], "dependencies")
        # Scope edits/reassignment fence previous executions even when a caller
        # forgot to advance the epoch. Consumption and previous evidence stay.
        changed_scope = any(
            proposed.get(key) != previous.get(key)
            for key in (
                "assignee_id",
                "objective",
                "conditions",
                "parent_id",
                "dependencies",
                "reviewer_id",
                "user_approval",
                "workflow_id",
            )
        )
        if changed_scope and proposed["epoch"] == previous["epoch"]:
            proposed["epoch"] += 1
        if changed_scope:
            proposed["submission"] = None
            proposed["workflow_card_id"] = ""
            if previous["state"] in {"done", "review"} and proposed["state"] == previous["state"]:
                proposed["state"] = "ready" if proposed["assignee_id"] else "backlog"
                proposed["reason"] = "agreement_changed"
        if previous["state"] in {"done", "cancelled"} and proposed["state"] not in {
            "done",
            "cancelled",
        }:
            proposed["submission"] = None
            if proposed["epoch"] == previous["epoch"]:
                proposed["epoch"] += 1
        if (
            proposed["state"] in {"paused", "cancelled"}
            and proposed["state"] != previous["state"]
            and proposed["epoch"] == previous["epoch"]
        ):
            proposed["epoch"] += 1
        _task_links(connection, proposed, previous)
        if proposed["state"] in {"done", "review"} and proposed["state"] != previous["state"]:
            _completion_ready(connection, proposed)
            evidence = (
                result if result and result.get("kind") != "review" else proposed.get("result")
            )
            if (
                not isinstance(evidence, dict)
                or not evidence.get("outcome")
                or not evidence.get("evidence")
            ):
                raise _error(
                    "completion_evidence_required",
                    "Provide an outcome and evidence for the completion conditions.",
                )
            if proposed["state"] == "done" and (
                proposed.get("reviewer_id") or proposed.get("user_approval")
            ):
                submission = proposed.get("submission") or {}
                if (
                    submission.get("epoch") != proposed["epoch"]
                    or not submission.get("reviewer_approved")
                    or not submission.get("user_approved")
                ):
                    raise _error(
                        "review_required",
                        "Complete the configured review and approval before finishing this task.",
                    )
        if result is not None and result.get("kind") != "review":
            proposed["result"] = deepcopy(result)
        saved = _save(connection, "task", previous, proposed, actor, event)
        _write_dependencies(connection, saved)
        if result is not None:
            connection.execute(
                "INSERT INTO native_results (task_id, actor, epoch, task_revision, "
                "created_at, body_json) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    task_id,
                    actor,
                    saved["epoch"],
                    saved["revision"],
                    utc_now_timestamp(),
                    _dump(result),
                ),
            )
        if intent is not None:
            self._create_execution(
                connection,
                saved["factory_id"],
                "task",
                task_id,
                saved["assignee_id"],
                saved["epoch"],
                actor=actor,
                **intent,
            )
        if previous["state"] == "done" and saved["state"] != "done":
            # Reopening a prerequisite fences unfinished dependent claims.
            rows = connection.execute(
                "WITH RECURSIVE affected(id) AS (SELECT task_id FROM "
                "native_task_dependencies WHERE dependency_id = ? UNION SELECT d.task_id "
                "FROM native_task_dependencies d JOIN affected a ON d.dependency_id = "
                "a.id) SELECT t.id FROM native_tasks t JOIN affected a ON t.id = a.id "
                "WHERE t.state NOT IN ('done', 'cancelled')",
                (task_id,),
            ).fetchall()
            for row in rows:
                dependent = _get(connection, "task", row[0])
                _save(
                    connection,
                    "task",
                    dependent,
                    {
                        "state": "paused" if dependent["state"] == "paused" else "blocked",
                        "reason": dependent["reason"]
                        if dependent["state"] == "paused"
                        else "dependencies",
                        "epoch": dependent["epoch"] + 1,
                        "submission": None,
                    },
                    actor,
                    "dependency_reopened",
                )
        return saved

    async def assign_task(
        self,
        task_id: str,
        assignee_id: str,
        *,
        actor: str = "user",
        expected_revision: int | None = None,
    ) -> Json:
        return await self.change_task(
            task_id,
            {
                "assignee_id": assignee_id,
                "state": "ready" if assignee_id else "backlog",
                "reason": "",
            },
            actor=actor,
            event="assigned",
            expected_revision=expected_revision,
        )

    async def submit_task(
        self,
        task_id: str,
        outcome: str,
        evidence: Any,
        *,
        actor: str,
        expected_revision: int | None = None,
        expected_epoch: int | None = None,
    ) -> Json:
        outcome = _text(outcome, "outcome", required=True)
        if not evidence or len(_dump(evidence)) > 100000:
            raise _error(
                "completion_evidence_required",
                "Provide bounded evidence addressing the completion conditions.",
            )

        def write(connection: sqlite3.Connection) -> Json:
            task = _get(connection, "task", task_id)
            _revision(task, expected_revision, expected_epoch)
            if task["state"] not in {"working", "ready", "blocked"}:
                raise _error("wrong_state", "Resume this task before submitting its result.")
            review = bool(task.get("reviewer_id") or task.get("user_approval"))
            return self._change_task(
                connection,
                task_id,
                {
                    "state": "review" if review else "done",
                    "reason": "review" if review else "",
                    "submission": {
                        "epoch": task["epoch"],
                        "agreement_revision": task["revision"],
                        "reviewer_approved": not bool(task.get("reviewer_id")),
                        "user_approved": not bool(task.get("user_approval")),
                    },
                },
                actor,
                "submitted",
                expected_revision,
                expected_epoch,
                {"outcome": outcome, "evidence": evidence},
                None,
            )

        return await self.database.write_async(write)

    async def review_task(
        self,
        task_id: str,
        approved: bool,
        *,
        actor: str,
        comment: str = "",
        expected_revision: int | None = None,
        expected_epoch: int | None = None,
        reviewer_id: str = "",
        user: bool = False,
    ) -> Json:
        def write(connection: sqlite3.Connection) -> Json:
            task = _get(connection, "task", task_id)
            _revision(task, expected_revision, expected_epoch)
            submission = task.get("submission")
            if (
                task["state"] != "review"
                or not isinstance(submission, dict)
                or submission.get("epoch") != task["epoch"]
            ):
                raise _error("stale_review", "There is no current submitted result to review.")
            if not user and reviewer_id != task.get("reviewer_id"):
                raise _error(
                    "reviewer_required", "Only the configured reviewer can review this submission."
                )
            if not approved:
                if not comment.strip():
                    raise _error("missing_text", "Describe the changes required.")
                patch = {
                    "state": "ready",
                    "reason": "changes_requested",
                    "epoch": task["epoch"] + 1,
                    "submission": None,
                }
            else:
                submission["user_approved" if user else "reviewer_approved"] = True
                done = submission.get("user_approved") and submission.get("reviewer_approved")
                patch = {
                    "submission": submission,
                    "state": "done" if done else "review",
                    "reason": "" if done else "review",
                }
            return self._change_task(
                connection,
                task_id,
                patch,
                actor,
                "reviewed",
                expected_revision,
                expected_epoch,
                {"kind": "review", "approved": approved, "comment": comment},
                None,
            )

        return await self.database.write_async(write)

    async def results(self, task_id: str, *, after: int = 0, limit: int = 100) -> list[Json]:
        if type(limit) is not int or not 1 <= limit <= 1000 or type(after) is not int or after < 0:
            raise _error("invalid_limit", "Choose a valid result page.")

        def read(connection: sqlite3.Connection) -> list[Json]:
            _get(connection, "task", task_id)
            return [
                _decode(row)
                for row in connection.execute(
                    "SELECT id, task_id, actor, epoch, task_revision, created_at, body_json "
                    "FROM native_results WHERE task_id = ? AND id > ? ORDER BY id LIMIT ?",
                    (task_id, after, limit),
                )
            ]

        return await self.database.read_async(read)
