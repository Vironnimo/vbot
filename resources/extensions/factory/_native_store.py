"""Native Factory persistence facade using the existing host Database."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from copy import deepcopy
from typing import Any

from core.utils.timestamps import utc_now_timestamp

from ._native_records import (
    Json,
    _capacity,
    _dump,
    _error,
    _get,
    _insert,
    _member,
    _merge,
    _new_id,
    _revision,
    _save,
    _text,
)
from ._native_tasks import NativeTaskStore
from ._native_topics import NativeTopicStore


class NativeFactoryStore(NativeTaskStore, NativeTopicStore):
    """Permanent Factory records; actor authority belongs to the service."""

    async def create_factory(
        self,
        project_id: str,
        *,
        leader_agent_id: str,
        leader_agent_proof: Any = "",
        name: str = "",
        remit: str = "",
        actor: str = "user",
        **settings: Any,
    ) -> Json:
        project_id = _text(project_id, "project_id", 200, required=True)
        leader_agent_id = _text(leader_agent_id, "leader_agent_id", 200, required=True)
        name, remit = _text(name, "name", 200), _text(remit, "remit")
        capacity = _capacity(settings.pop("capacity", 2))
        leader_created_by_factory = bool(settings.pop("leader_created_by_factory", False))
        leader_name = _text(settings.pop("leader_name", leader_agent_id), "leader_name", 200)

        def write(connection: sqlite3.Connection) -> Json:
            if connection.execute(
                "SELECT 1 FROM native_factories WHERE project_id = ?", (project_id,)
            ).fetchone():
                raise _error("factory_exists", "This Project already has a Factory.")
            factory_id, member_id = _new_id(connection, "fac"), _new_id(connection, "fmb")
            factory = _insert(
                connection,
                "factory",
                {
                    **settings,
                    "id": factory_id,
                    "project_id": project_id,
                    "state": "active",
                    "name": name or project_id,
                    "remit": remit,
                    "leader_member_id": member_id,
                    "capacity": capacity,
                    "config": settings.get("config", {}),
                    "limits": settings.get("limits", {}),
                },
                actor,
            )
            _insert(
                connection,
                "member",
                {
                    "id": member_id,
                    "factory_id": factory_id,
                    "agent_id": leader_agent_id,
                    "agent_proof": leader_agent_proof,
                    "name": leader_name,
                    "state": "active",
                    "role": "Leader",
                    "instructions": "",
                    "config": {},
                    "capacity": 1,
                    "created_by_factory": leader_created_by_factory,
                },
                actor,
            )
            return factory

        return await self.database.write_async(write)

    async def get_factory(self, factory_id: str) -> Json:
        return await self._get("factory", factory_id)

    async def list_factories(
        self,
        *,
        project_id: str | None = None,
        state: str | None = None,
        after: str = "",
        limit: int = 100,
    ) -> list[Json]:
        return await self._list("factory", {"project_id": project_id, "state": state}, after, limit)

    async def update_factory(
        self,
        factory_id: str,
        patch: Json,
        *,
        actor: str = "user",
        expected_revision: int | None = None,
        clear_config_fields: Iterable[str] = (),
    ) -> Json:
        if isinstance(clear_config_fields, (str, bytes)):
            raise _error("invalid_field", "Choose Factory configuration fields to clear.")
        clear_fields = tuple(clear_config_fields)
        allowed = {
            "allowed_models",
            "allowed_skills",
            "allowed_tools",
            "denied_tools",
            "permission_ceiling",
        }
        if any(not isinstance(field, str) or field not in allowed for field in clear_fields):
            raise _error(
                "invalid_field", "Only known Factory configuration overrides can be cleared."
            )

        def write(connection: sqlite3.Connection) -> Json:
            previous = _get(connection, "factory", factory_id)
            _revision(previous, expected_revision)
            for field in clear_fields:
                previous["config"].pop(field, None)
            proposed = _merge(previous, patch)
            if proposed["state"] not in {"active", "paused", "archived"}:
                raise _error("invalid_state", "Factory state must be active, paused or archived.")
            if proposed["project_id"] != previous["project_id"]:
                raise _error("immutable_field", "A Factory cannot move to another Project.")
            _capacity(proposed["capacity"])
            _member(connection, factory_id, proposed["leader_member_id"])
            saved = _save(connection, "factory", previous, patch, actor, "updated")
            if saved["state"] == "archived":
                for row in connection.execute(
                    "SELECT id FROM native_tasks WHERE factory_id = ? AND state NOT IN "
                    "('done', 'cancelled')",
                    (factory_id,),
                ).fetchall():
                    task = _get(connection, "task", row[0])
                    _save(
                        connection,
                        "task",
                        task,
                        {
                            "state": "paused",
                            "reason": "factory_archived",
                            "epoch": task["epoch"] + 1,
                        },
                        actor,
                        "factory_archived",
                    )
            return saved

        return await self.database.write_async(write)

    async def add_member(
        self,
        factory_id: str,
        agent_id: str,
        *,
        agent_proof: Any = "",
        role: str = "",
        instructions: str = "",
        config: Json | None = None,
        capacity: int = 1,
        created_by_factory: bool = False,
        actor: str = "user",
        **fields: Any,
    ) -> Json:
        agent_id = _text(agent_id, "agent_id", 200, required=True)
        role, instructions = _text(role, "role", 200), _text(instructions, "instructions")
        _capacity(capacity)

        def write(connection: sqlite3.Connection) -> Json:
            factory = _get(connection, "factory", factory_id)
            if factory["state"] == "archived":
                raise _error("factory_archived", "This Factory is archived.")
            if connection.execute(
                "SELECT 1 FROM native_members WHERE factory_id = ? AND agent_id = ? AND "
                "state = 'active' AND (? = '' OR json_extract(body_json, '$.agent_proof') = ?)",
                (factory_id, agent_id, agent_proof, agent_proof),
            ).fetchone():
                raise _error(
                    "member_exists", "This Agent is already an active member of the Factory."
                )
            return _insert(
                connection,
                "member",
                {
                    **fields,
                    "id": _new_id(connection, "fmb"),
                    "factory_id": factory_id,
                    "agent_id": agent_id,
                    "agent_proof": agent_proof,
                    "state": "active",
                    "role": role,
                    "instructions": instructions,
                    "config": config or {},
                    "capacity": capacity,
                    "created_by_factory": created_by_factory,
                },
                actor,
            )

        return await self.database.write_async(write)

    async def get_member(self, member_id: str) -> Json:
        return await self._get("member", member_id)

    async def identity_creation_member(self, agent_id: str, proof: str) -> Json | None:
        """The membership created with this incarnation, even after retirement or archive."""

        def read(connection: sqlite3.Connection) -> Json | None:
            rows = connection.execute(
                "SELECT id FROM native_members WHERE agent_id = ? "
                "AND json_extract(body_json, '$.agent_proof') = ? "
                "AND json_extract(body_json, '$.created_by_factory') = 1 LIMIT 2",
                (agent_id, proof),
            ).fetchall()
            if len(rows) > 1:
                raise _error("identity_conflict", "The Agent has conflicting creation records.")
            return _get(connection, "member", rows[0][0]) if rows else None

        return await self.database.read_async(read)

    async def workflow_profile_projects(self) -> dict[tuple[str, str], str]:
        """Index active Factory profiles by exact identity for Workflow Project pickers."""

        def read(connection: sqlite3.Connection) -> dict[tuple[str, str], str]:
            rows = connection.execute(
                """SELECT origin.agent_id, json_extract(origin.body_json, '$.agent_proof') AS proof,
                          MAX(f.project_id)
                   FROM native_members AS origin
                   JOIN native_factories AS f ON f.id = origin.factory_id
                   WHERE json_extract(origin.body_json, '$.created_by_factory') = 1
                     AND f.state != 'archived'
                     AND EXISTS (
                         SELECT 1 FROM native_members AS member
                         WHERE member.agent_id = origin.agent_id AND member.state = 'active'
                           AND member.factory_id = origin.factory_id
                           AND json_extract(member.body_json, '$.agent_proof') =
                               json_extract(origin.body_json, '$.agent_proof')
                     )
                   GROUP BY origin.agent_id, proof
                   HAVING COUNT(*) = 1"""
            )
            return {(agent, proof): project for agent, proof, project in rows}

        return await self.database.read_async(read)

    async def list_members(
        self,
        factory_id: str | None = None,
        *,
        state: str | None = None,
        after: str = "",
        limit: int = 100,
    ) -> list[Json]:
        return await self._list("member", {"factory_id": factory_id, "state": state}, after, limit)

    async def update_member(
        self,
        member_id: str,
        patch: Json,
        *,
        actor: str = "user",
        expected_revision: int | None = None,
        clear_config_fields: Iterable[str] = (),
    ) -> Json:
        if isinstance(clear_config_fields, (str, bytes)):
            raise _error("invalid_field", "Choose local configuration fields to clear.")
        clear_fields = tuple(clear_config_fields)
        allowed = {"allowed_skills", "allowed_tools", "denied_tools"}
        if any(not isinstance(field, str) or field not in allowed for field in clear_fields):
            raise _error(
                "invalid_field", "Only known local configuration overrides can be cleared."
            )

        def write(connection: sqlite3.Connection) -> Json:
            previous = _get(connection, "member", member_id)
            _revision(previous, expected_revision)
            if any(
                key in patch and patch[key] != previous.get(key)
                for key in ("agent_id", "agent_proof", "factory_id", "created_by_factory")
            ):
                raise _error(
                    "immutable_field",
                    "A membership cannot be retargeted to another Agent incarnation.",
                )
            for field in clear_fields:
                previous["config"].pop(field, None)
            proposed = _merge(previous, patch)
            if proposed["state"] not in {"active", "retired"}:
                raise _error("invalid_state", "Member state must be active or retired.")
            _capacity(proposed["capacity"])
            factory = _get(connection, "factory", previous["factory_id"])
            if (
                proposed["state"] == "retired"
                and factory["leader_member_id"] == member_id
                and factory["state"] != "archived"
            ):
                raise _error(
                    "leader_required", "Choose another Leader before retiring this member."
                )
            saved = _save(connection, "member", previous, patch, actor, "updated")
            if saved["state"] == "retired":
                for row in connection.execute(
                    "SELECT id FROM native_tasks WHERE assignee_id = ? AND state NOT IN "
                    "('done', 'cancelled')",
                    (member_id,),
                ).fetchall():
                    task = _get(connection, "task", row[0])
                    _save(
                        connection,
                        "task",
                        task,
                        {
                            "state": "blocked",
                            "reason": "member_retired",
                            "epoch": task["epoch"] + 1,
                        },
                        actor,
                        "member_retired",
                    )
            return saved

        return await self.database.write_async(write)

    async def identity_reservation(self, request_id: str) -> Json | None:
        def read(connection: sqlite3.Connection) -> Json | None:
            row = connection.execute(
                "SELECT request_id, agent_id, state, payload_json, detail_json, "
                "created_at, updated_at FROM native_identity_reservations WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            return self._reservation(row) if row else None

        return await self.database.read_async(read)

    async def pending_identities(self, *, agent_id: str | None = None) -> list[Json]:
        """Creation receipts still needing a membership commit or its acknowledgement."""

        def read(connection: sqlite3.Connection) -> list[Json]:
            query = "SELECT * FROM native_identity_reservations WHERE state != 'complete'"
            if agent_id is not None:
                query += " AND agent_id = ?"
            return [
                self._reservation(row)
                for row in connection.execute(query, (agent_id,) if agent_id is not None else ())
            ]

        return await self.database.read_async(read)

    async def reserve_identity(self, request_id: str, payload: Json) -> Json:
        """Persist the exact cross-owner creation intention before creating an Agent."""
        request_id = _text(request_id, "request_id", 500, required=True)
        if not isinstance(payload, dict) or len(_dump(payload)) > 100000:
            raise _error("invalid_field", "Identity creation needs a bounded object payload.")

        def write(connection: sqlite3.Connection) -> Json:
            prior = connection.execute(
                "SELECT request_id, agent_id, state, payload_json, detail_json, "
                "created_at, updated_at FROM native_identity_reservations WHERE request_id "
                "= ?",
                (request_id,),
            ).fetchone()
            if prior:
                record = self._reservation(prior)
                if record["payload"] != payload:
                    raise _error(
                        "receipt_conflict",
                        "This request id already describes another identity creation.",
                    )
                return record
            now = utc_now_timestamp()
            agent_id = _new_id(connection, "fag")
            connection.execute(
                "INSERT INTO native_identity_reservations (request_id, agent_id, state, "
                "payload_json, detail_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, "
                "?, ?)",
                (request_id, agent_id, "reserved", _dump(payload), "{}", now, now),
            )
            return {
                "request_id": request_id,
                "agent_id": agent_id,
                "state": "reserved",
                "payload": deepcopy(payload),
                "created_at": now,
                "updated_at": now,
            }

        return await self.database.write_async(write)

    @staticmethod
    def _reservation(row: sqlite3.Row) -> Json:
        record = dict(row)
        detail = json.loads(record.pop("detail_json"))
        record["payload"] = json.loads(record.pop("payload_json"))
        return {**detail, **record}

    async def save_identity_reservation(self, request_id: str, patch: Json) -> Json:
        def write(connection: sqlite3.Connection) -> Json:
            prior = connection.execute(
                "SELECT request_id, agent_id, state, payload_json, detail_json, "
                "created_at, updated_at FROM native_identity_reservations WHERE request_id "
                "= ?",
                (request_id,),
            ).fetchone()
            if prior is None:
                raise _error(
                    "identity_reservation_not_found",
                    "Reserve this identity request before creating the Agent.",
                )
            record = self._reservation(prior)
            if any(
                key in patch and patch[key] != record[key]
                for key in ("request_id", "agent_id", "payload", "created_at")
            ):
                raise _error(
                    "receipt_conflict", "Identity creation request and Agent id cannot change."
                )
            if any(
                key in patch and key in record and patch[key] != record[key]
                for key in ("proof", "resource_id")
            ):
                raise _error(
                    "receipt_conflict",
                    "An identity reservation cannot change its committed identity or resource.",
                )
            if record["state"] == "complete" and patch.get("state", "complete") != "complete":
                raise _error(
                    "receipt_conflict", "A completed identity request cannot be restarted."
                )
            updated = _merge(record, patch)
            updated["updated_at"] = utc_now_timestamp()
            detail = {
                key: value
                for key, value in updated.items()
                if key
                not in {"request_id", "agent_id", "state", "payload", "created_at", "updated_at"}
            }
            connection.execute(
                "UPDATE native_identity_reservations SET state = ?, detail_json = ?, "
                "updated_at = ? WHERE request_id = ?",
                (updated["state"], _dump(detail), updated["updated_at"], request_id),
            )
            return updated

        return await self.database.write_async(write)

    async def complete_identity_reservation(self, request_id: str, resource_id: str) -> Json:
        return await self.save_identity_reservation(
            request_id, {"state": "complete", "resource_id": resource_id}
        )
