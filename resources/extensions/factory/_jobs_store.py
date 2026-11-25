"""Persistence for Simplified Software Factory Jobs, Runs, and Trigger Receipts."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from core.extensions.databases import Database
from core.utils.ids import new_id
from core.utils.timestamps import utc_now_timestamp

type Json = dict[str, Any]

JOB_RUN_STATES = frozenset({"queued", "running", "needs_attention", "done", "failed", "cancelled"})
FINAL_JOB_RUN_STATES = frozenset({"done", "failed", "cancelled"})
ACTIVE_JOB_RUN_STATES = frozenset({"queued", "running", "needs_attention"})
JOB_RUN_ORIGINS = frozenset({"manual", "leader", "schedule", "webhook", "public_tool"})
TRIGGER_TYPES = frozenset({"schedule", "webhook"})

_UNSET = object()

_JOB_COLUMNS = (
    "id, factory_id, name, enabled, agent_member_id, revision, created_at, updated_at, body_json"
)
_RUN_COLUMNS = (
    "id, factory_id, job_id, member_id, state, origin, group_id, participant_id, "
    "revision, created_at, updated_at, finished_at, body_json"
)


def _error(code: str, message: str) -> Any:
    from .store import FactoryError

    return FactoryError(code, message)


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _new_job_id(connection: sqlite3.Connection) -> str:
    return new_id(
        "fjob",
        claim=lambda candidate: (
            connection.execute("SELECT 1 FROM factory_jobs WHERE id = ?", (candidate,)).fetchone()
            is None
        ),
    )


def _new_run_id(connection: sqlite3.Connection) -> str:
    return new_id(
        "jrun",
        claim=lambda candidate: (
            connection.execute(
                "SELECT 1 FROM factory_job_runs WHERE id = ?", (candidate,)
            ).fetchone()
            is None
        ),
    )


def _validate_name(value: Any) -> str:
    if not isinstance(value, str):
        raise _error("invalid_job", "Job name must be a non-empty string.")
    stripped = value.strip()
    if not stripped or len(stripped) > 200:
        raise _error("invalid_job", "Job name must be between 1 and 200 characters.")
    return stripped


def _validate_prompt(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _error("invalid_job", "Job prompt must be a non-empty string.")
    return value.strip()


def _validate_description(value: Any) -> str:
    if not isinstance(value, str):
        raise _error("invalid_job", "Job description must be a string.")
    return value


def _validate_triggers(value: Any) -> list[Json]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise _error("invalid_job", "Job triggers must be a list.")
    normalized: list[Json] = []
    seen_ids: set[str] = set()
    for item in value:
        if not isinstance(item, dict):
            raise _error("invalid_job", "Each trigger must be an object.")
        raw_id = item.get("id")
        if raw_id is None or raw_id == "":
            trigger_id = new_id("trg")
            while trigger_id in seen_ids:
                trigger_id = new_id("trg")
        elif isinstance(raw_id, str) and raw_id.strip():
            trigger_id = raw_id.strip()
        else:
            raise _error("invalid_job", "Trigger id must be a non-empty string.")
        if trigger_id in seen_ids:
            raise _error("invalid_job", f"Duplicate trigger id: {trigger_id!r}.")
        seen_ids.add(trigger_id)

        trigger_type = item.get("type")
        if trigger_type not in TRIGGER_TYPES:
            raise _error("invalid_job", "Trigger type must be 'schedule' or 'webhook'.")
        title = item.get("title", "")
        if not isinstance(title, str):
            raise _error("invalid_job", "Trigger title must be a string.")
        schedule = item.get("schedule", "")
        if not isinstance(schedule, str):
            raise _error("invalid_job", "Trigger schedule must be a string.")
        enabled = item.get("enabled", True)
        if not isinstance(enabled, bool):
            raise _error("invalid_job", "Trigger enabled must be a boolean.")

        normalized.append(
            {
                "id": trigger_id,
                "type": trigger_type,
                "title": title,
                "schedule": schedule,
                "enabled": enabled,
            }
        )
    return normalized


def _get_factory(connection: sqlite3.Connection, factory_id: str) -> Json:
    row = connection.execute(
        "SELECT id, project_id, state, revision, created_at, updated_at, body_json "
        "FROM native_factories WHERE id = ?",
        (factory_id,),
    ).fetchone()
    if row is None:
        raise _error("not_found", f"Factory {factory_id!r} does not exist.")
    record = dict(row)
    body = json.loads(record.pop("body_json"))
    return {**body, **record}


def _require_active_member(connection: sqlite3.Connection, factory_id: str, member_id: str) -> Json:
    if not isinstance(member_id, str) or not member_id.strip():
        raise _error("member_unavailable", "Member id must be a non-empty string.")
    row = connection.execute(
        "SELECT id, factory_id, agent_id, state, revision, created_at, updated_at, body_json "
        "FROM native_members WHERE id = ?",
        (member_id,),
    ).fetchone()
    if row is None:
        raise _error("member_unavailable", f"Member {member_id!r} does not exist.")
    record = dict(row)
    body = json.loads(record.pop("body_json"))
    member = {**body, **record}
    if member["factory_id"] != factory_id:
        raise _error("member_unavailable", "Member does not belong to this Factory.")
    if member["state"] != "active":
        raise _error("member_unavailable", "Selected member is not active.")
    return member


def _decode_job(row: sqlite3.Row) -> Json:
    record = dict(row)
    body = json.loads(record.pop("body_json"))
    record["enabled"] = bool(record["enabled"])
    return {
        "prompt": "",
        "description": "",
        "triggers": [],
        **body,
        **record,
    }


def _decode_run(row: sqlite3.Row) -> Json:
    record = dict(row)
    body = json.loads(record.pop("body_json"))
    return {
        "job_name": "",
        "prompt": "",
        "input_note": "",
        "summary": "",
        "question": "",
        "reason": "",
        "trigger_id": "",
        "trigger_event": None,
        "session_address": None,
        **body,
        **record,
    }


def _get_job(connection: sqlite3.Connection, job_id: str) -> Json:
    row = connection.execute(
        f"SELECT {_JOB_COLUMNS} FROM factory_jobs WHERE id = ?",
        (job_id,),
    ).fetchone()
    if row is None:
        raise _error("not_found", f"Job {job_id!r} does not exist.")
    return _decode_job(row)


def _get_run(connection: sqlite3.Connection, run_id: str) -> Json:
    row = connection.execute(
        f"SELECT {_RUN_COLUMNS} FROM factory_job_runs WHERE id = ?",
        (run_id,),
    ).fetchone()
    if row is None:
        raise _error("not_found", f"Job run {run_id!r} does not exist.")
    return _decode_run(row)


class FactoryJobsStore:
    """Persistence for Simplified Software Factory Jobs and Job Runs."""

    def __init__(self, database: Database) -> None:
        self.database = database

    async def create_job(
        self,
        factory_id: str,
        *,
        name: str,
        prompt: str,
        description: str = "",
        enabled: bool = True,
        agent_member_id: str = "",
        triggers: list[Json] | None = None,
    ) -> Json:
        clean_name = _validate_name(name)
        clean_prompt = _validate_prompt(prompt)
        clean_description = _validate_description(description)
        if not isinstance(enabled, bool):
            raise _error("invalid_job", "Job enabled must be a boolean.")
        if not isinstance(agent_member_id, str):
            raise _error("invalid_job", "Job agent_member_id must be a string.")
        clean_member_id = agent_member_id.strip()
        clean_triggers = _validate_triggers(triggers)

        def write(connection: sqlite3.Connection) -> Json:
            factory = _get_factory(connection, factory_id)
            if factory["state"] == "archived":
                raise _error("factory_archived", "This Factory is archived.")
            if clean_member_id:
                _require_active_member(connection, factory_id, clean_member_id)
            job_id = _new_job_id(connection)
            now = utc_now_timestamp()
            body = {
                "prompt": clean_prompt,
                "description": clean_description,
                "triggers": clean_triggers,
            }
            connection.execute(
                "INSERT INTO factory_jobs "
                "(id, factory_id, name, enabled, agent_member_id, revision, created_at, "
                "updated_at, body_json) "
                "VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?)",
                (
                    job_id,
                    factory_id,
                    clean_name,
                    1 if enabled else 0,
                    clean_member_id,
                    now,
                    now,
                    _dump(body),
                ),
            )
            return _get_job(connection, job_id)

        return await self.database.write_async(write)

    async def get_job(self, job_id: str) -> Json:
        return await self.database.read_async(lambda connection: _get_job(connection, job_id))

    async def list_jobs(self, factory_id: str) -> list[Json]:
        def read(connection: sqlite3.Connection) -> list[Json]:
            rows = connection.execute(
                f"SELECT {_JOB_COLUMNS} FROM factory_jobs WHERE factory_id = ? "
                "ORDER BY created_at, id",
                (factory_id,),
            ).fetchall()
            return [_decode_job(row) for row in rows]

        return await self.database.read_async(read)

    async def list_all_jobs(self) -> list[Json]:
        def read(connection: sqlite3.Connection) -> list[Json]:
            rows = connection.execute(
                "SELECT j.id, j.factory_id, j.name, j.enabled, j.agent_member_id, "
                "j.revision, j.created_at, j.updated_at, j.body_json, "
                "f.state AS factory_state "
                "FROM factory_jobs j "
                "JOIN native_factories f ON f.id = j.factory_id "
                "ORDER BY j.created_at, j.id"
            ).fetchall()
            results: list[Json] = []
            for row in rows:
                record = dict(row)
                factory_state = record.pop("factory_state")
                body = json.loads(record.pop("body_json"))
                record["enabled"] = bool(record["enabled"])
                results.append(
                    {
                        "prompt": "",
                        "description": "",
                        "triggers": [],
                        **body,
                        **record,
                        "factory_state": factory_state,
                    }
                )
            return results

        return await self.database.read_async(read)

    async def update_job(
        self,
        job_id: str,
        *,
        name: str | None = None,
        prompt: str | None = None,
        description: str | None = None,
        enabled: bool | None = None,
        agent_member_id: str | None = None,
        triggers: list[Json] | None = None,
        expected_revision: int | None = None,
    ) -> Json:
        clean_name = _validate_name(name) if name is not None else None
        clean_prompt = _validate_prompt(prompt) if prompt is not None else None
        clean_description = _validate_description(description) if description is not None else None
        if enabled is not None and not isinstance(enabled, bool):
            raise _error("invalid_job", "Job enabled must be a boolean.")
        if agent_member_id is not None and not isinstance(agent_member_id, str):
            raise _error("invalid_job", "Job agent_member_id must be a string.")
        clean_member_id = agent_member_id.strip() if agent_member_id is not None else None
        clean_triggers = _validate_triggers(triggers) if triggers is not None else None

        def write(connection: sqlite3.Connection) -> Json:
            previous = _get_job(connection, job_id)
            if expected_revision is not None and expected_revision != previous["revision"]:
                raise _error(
                    "revision_conflict", "The job changed elsewhere. Refresh before saving."
                )
            factory = _get_factory(connection, previous["factory_id"])
            if factory["state"] == "archived":
                raise _error("factory_archived", "This Factory is archived.")
            next_member_id = (
                clean_member_id if clean_member_id is not None else previous["agent_member_id"]
            )
            if next_member_id:
                _require_active_member(connection, previous["factory_id"], next_member_id)

            next_name = clean_name if clean_name is not None else previous["name"]
            next_enabled = enabled if enabled is not None else previous["enabled"]
            known_columns = {
                "id",
                "factory_id",
                "name",
                "enabled",
                "agent_member_id",
                "revision",
                "created_at",
                "updated_at",
            }
            body = {key: val for key, val in previous.items() if key not in known_columns}
            if clean_prompt is not None:
                body["prompt"] = clean_prompt
            if clean_description is not None:
                body["description"] = clean_description
            if clean_triggers is not None:
                body["triggers"] = clean_triggers

            now = utc_now_timestamp()
            next_revision = previous["revision"] + 1
            connection.execute(
                "UPDATE factory_jobs SET name = ?, enabled = ?, agent_member_id = ?, "
                "revision = ?, updated_at = ?, body_json = ? WHERE id = ?",
                (
                    next_name,
                    1 if next_enabled else 0,
                    next_member_id,
                    next_revision,
                    now,
                    _dump(body),
                    job_id,
                ),
            )
            return _get_job(connection, job_id)

        return await self.database.write_async(write)

    async def delete_job(self, job_id: str) -> None:
        def write(connection: sqlite3.Connection) -> None:
            _get_job(connection, job_id)
            connection.execute("DELETE FROM factory_jobs WHERE id = ?", (job_id,))

        await self.database.write_async(write)

    def _create_run_sync(
        self,
        connection: sqlite3.Connection,
        factory_id: str,
        job_id: str,
        *,
        origin: str,
        member_id: str | None = None,
        input_note: str = "",
        trigger_id: str = "",
        trigger_event: Any = None,
        prompt: str | None = None,
        job_name: str | None = None,
    ) -> Json:
        if origin not in JOB_RUN_ORIGINS:
            raise _error("invalid_job", f"Invalid job run origin: {origin!r}.")
        if not isinstance(input_note, str):
            raise _error("invalid_job", "input_note must be a string.")
        if not isinstance(trigger_id, str):
            raise _error("invalid_job", "trigger_id must be a string.")

        factory = _get_factory(connection, factory_id)
        if factory["state"] == "archived":
            raise _error("factory_archived", "This Factory is archived.")
        if factory["state"] != "active":
            raise _error("factory_paused", "This Factory is paused.")

        job: Json | None = None
        if job_id:
            job = _get_job(connection, job_id)
            if job["factory_id"] != factory_id:
                raise _error("not_found", f"Job {job_id!r} does not belong to this Factory.")

        resolved_prompt = (
            _validate_prompt(prompt)
            if prompt is not None
            else (job["prompt"] if job is not None else _validate_prompt(""))
        )
        resolved_job_name = (
            job_name if job_name is not None else (job["name"] if job is not None else "")
        )

        # Resolve member: explicit override -> job's agent_member_id -> factory's leader_member_id.
        if member_id is not None and member_id != "":
            resolved_member_id = member_id
        elif job is not None and job["agent_member_id"]:
            resolved_member_id = job["agent_member_id"]
        else:
            resolved_member_id = str(factory.get("leader_member_id") or "")
        _require_active_member(connection, factory_id, resolved_member_id)

        run_id = _new_run_id(connection)
        group_id = f"fjob.{run_id}"
        participant_id = "worker"
        now = utc_now_timestamp()
        body = {
            "job_name": resolved_job_name,
            "prompt": resolved_prompt,
            "input_note": input_note,
            "summary": "",
            "question": "",
            "reason": "",
            "trigger_id": trigger_id,
            "trigger_event": trigger_event,
            "session_address": None,
        }
        connection.execute(
            "INSERT INTO factory_job_runs "
            "(id, factory_id, job_id, member_id, state, origin, group_id, participant_id, "
            "revision, created_at, updated_at, finished_at, body_json) "
            "VALUES (?, ?, ?, ?, 'queued', ?, ?, ?, 1, ?, ?, NULL, ?)",
            (
                run_id,
                factory_id,
                job_id,
                resolved_member_id,
                origin,
                group_id,
                participant_id,
                now,
                now,
                _dump(body),
            ),
        )
        return _get_run(connection, run_id)

    async def create_run(
        self,
        factory_id: str,
        job_id: str = "",
        *,
        origin: str,
        member_id: str | None = None,
        input_note: str = "",
        trigger_id: str = "",
        trigger_event: Any = None,
        prompt: str | None = None,
        job_name: str | None = None,
    ) -> Json:
        return await self.database.write_async(
            lambda connection: self._create_run_sync(
                connection,
                factory_id,
                job_id,
                origin=origin,
                member_id=member_id,
                input_note=input_note,
                trigger_id=trigger_id,
                trigger_event=trigger_event,
                prompt=prompt,
                job_name=job_name,
            )
        )

    async def create_triggered_run(
        self,
        factory_id: str,
        job_id: str,
        *,
        origin: str,
        trigger_id: str,
        receipt: str,
        trigger_event: Any = None,
        input_note: str = "",
    ) -> Json | None:
        if not isinstance(receipt, str) or not receipt:
            raise _error("invalid_job", "Trigger receipt must be a non-empty string.")

        def write(connection: sqlite3.Connection) -> Json | None:
            now = utc_now_timestamp()
            cursor = connection.execute(
                "INSERT OR IGNORE INTO factory_job_trigger_receipts (receipt, created_at) "
                "VALUES (?, ?)",
                (receipt, now),
            )
            if cursor.rowcount == 0:
                return None
            return self._create_run_sync(
                connection,
                factory_id,
                job_id,
                origin=origin,
                input_note=input_note,
                trigger_id=trigger_id,
                trigger_event=trigger_event,
            )

        return await self.database.write_async(write)

    async def has_trigger_receipt(self, receipt: str) -> bool:
        return await self.database.read_async(
            lambda connection: (
                connection.execute(
                    "SELECT 1 FROM factory_job_trigger_receipts WHERE receipt = ?", (receipt,)
                ).fetchone()
                is not None
            )
        )

    async def get_run(self, run_id: str) -> Json:
        return await self.database.read_async(lambda connection: _get_run(connection, run_id))

    async def get_run_by_group(self, group_id: str) -> Json:
        def read(connection: sqlite3.Connection) -> Json:
            row = connection.execute(
                f"SELECT {_RUN_COLUMNS} FROM factory_job_runs WHERE group_id = ?",
                (group_id,),
            ).fetchone()
            if row is None:
                raise _error("not_found", f"Job run for group {group_id!r} does not exist.")
            return _decode_run(row)

        return await self.database.read_async(read)

    async def list_runs(
        self,
        factory_id: str,
        *,
        state: str | None = None,
        job_id: str | None = None,
        limit: int = 50,
    ) -> list[Json]:
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise _error("invalid_job", "Limit must be an integer between 1 and 1000.")
        if state is not None and state not in JOB_RUN_STATES:
            raise _error("invalid_job", f"Invalid job run state: {state!r}.")

        def read(connection: sqlite3.Connection) -> list[Json]:
            clauses = ["factory_id = ?"]
            params: list[Any] = [factory_id]
            if state is not None:
                clauses.append("state = ?")
                params.append(state)
            if job_id is not None:
                clauses.append("job_id = ?")
                params.append(job_id)
            params.append(limit)
            rows = connection.execute(
                f"SELECT {_RUN_COLUMNS} FROM factory_job_runs "
                f"WHERE {' AND '.join(clauses)} ORDER BY created_at DESC, id LIMIT ?",
                params,
            ).fetchall()
            return [_decode_run(row) for row in rows]

        return await self.database.read_async(read)

    async def list_active_runs(self) -> list[Json]:
        def read(connection: sqlite3.Connection) -> list[Json]:
            rows = connection.execute(
                f"SELECT {_RUN_COLUMNS} FROM factory_job_runs "
                "WHERE state IN ('queued', 'running', 'needs_attention') "
                "ORDER BY created_at, id"
            ).fetchall()
            return [_decode_run(row) for row in rows]

        return await self.database.read_async(read)

    async def update_run(
        self,
        run_id: str,
        *,
        state: str | None = None,
        summary: str | None = None,
        question: str | None = None,
        reason: str | None = None,
        session_address: Any = _UNSET,
        input_note: str | None = None,
        acknowledged: bool | None = None,
        expected_revision: int | None = None,
    ) -> Json:
        if state is not None and state not in JOB_RUN_STATES:
            raise _error("invalid_job", f"Invalid job run state: {state!r}.")

        def write(connection: sqlite3.Connection) -> Json:
            previous = _get_run(connection, run_id)
            if expected_revision is not None and expected_revision != previous["revision"]:
                raise _error(
                    "revision_conflict", "The job run changed elsewhere. Refresh before saving."
                )
            next_state = state if state is not None else previous["state"]
            now = utc_now_timestamp()
            next_finished_at = previous["finished_at"]
            if next_state in FINAL_JOB_RUN_STATES:
                if next_finished_at is None:
                    next_finished_at = now
            else:
                next_finished_at = None

            known_columns = {
                "id",
                "factory_id",
                "job_id",
                "member_id",
                "state",
                "origin",
                "group_id",
                "participant_id",
                "revision",
                "created_at",
                "updated_at",
                "finished_at",
            }
            body = {key: val for key, val in previous.items() if key not in known_columns}
            if summary is not None:
                body["summary"] = summary
            if question is not None:
                body["question"] = question
            if reason is not None:
                body["reason"] = reason
            if session_address is not _UNSET:
                body["session_address"] = session_address
            if input_note is not None:
                body["input_note"] = input_note
            if acknowledged is not None:
                body["acknowledged"] = acknowledged
            elif state is not None:
                body["acknowledged"] = False

            connection.execute(
                "UPDATE factory_job_runs SET state = ?, revision = ?, updated_at = ?, "
                "finished_at = ?, body_json = ? WHERE id = ?",
                (
                    next_state,
                    previous["revision"] + 1,
                    now,
                    next_finished_at,
                    _dump(body),
                    run_id,
                ),
            )
            return _get_run(connection, run_id)

        return await self.database.write_async(write)

    async def transition_run(
        self,
        run_id: str,
        state: str,
        *,
        summary: str | None = None,
        question: str | None = None,
        reason: str | None = None,
        session_address: Any = _UNSET,
        expected_revision: int | None = None,
    ) -> Json:
        return await self.update_run(
            run_id,
            state=state,
            summary=summary,
            question=question,
            reason=reason,
            session_address=session_address,
            expected_revision=expected_revision,
        )

    async def acknowledge_run(self, run_id: str) -> Json:
        run = await self.get_run(run_id)
        if run["state"] != "failed":
            raise _error(
                "wrong_state",
                f"Only failed job runs can be dismissed (current state: {run['state']!r}).",
            )
        if run.get("acknowledged"):
            return run
        return await self.update_run(run_id, acknowledged=True, expected_revision=run["revision"])
