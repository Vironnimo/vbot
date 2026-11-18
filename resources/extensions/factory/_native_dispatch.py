"""Exact native execution and delivery intentions."""

from __future__ import annotations

import sqlite3
from typing import Any

from ._native_records import (
    Json,
    NativeRecordStore,
    _error,
    _get,
    _insert,
    _member,
    _new_id,
    _revision,
    _save,
    _text,
)


class NativeDispatchStore(NativeRecordStore):
    def _create_execution(
        self,
        connection: sqlite3.Connection,
        factory_id: str,
        kind: str,
        scope_id: str,
        member_id: str,
        epoch: int,
        *,
        group_id: str,
        participant_id: str,
        input_id: str = "",
        runtime: Json | None = None,
        actor: str = "factory",
        **fields: Any,
    ) -> Json:
        _member(connection, factory_id, member_id)
        if kind not in {"task", "topic", "review"}:
            raise _error("invalid_execution", "Execution kind must be task, review or topic.")
        scope = _get(connection, "task" if kind == "review" else kind, scope_id)
        if scope["factory_id"] != factory_id:
            raise _error("factory_mismatch", "Execution scope must belong to the same Factory.")
        if kind == "task" and (scope["epoch"] != epoch or scope["assignee_id"] != member_id):
            raise _error("stale_execution", "Task assignment changed before execution admission.")
        if kind == "review" and (scope["epoch"] != epoch or scope.get("reviewer_id") != member_id):
            raise _error("stale_execution", "Task review changed before execution admission.")
        if kind == "topic" and member_id not in scope["participants"]:
            raise _error(
                "participant_required", "Invite this member before starting a topic execution."
            )
        row = connection.execute(
            "SELECT id FROM native_executions WHERE kind = ? AND scope_id = ? AND "
            "member_id = ? AND epoch = ?",
            (kind, scope_id, member_id, epoch),
        ).fetchone()
        if row:
            existing = _get(connection, "execution", row[0])
            if (existing["group_id"], existing["participant_id"], existing["input_id"]) != (
                group_id or existing["group_id"],
                participant_id,
                input_id,
            ):
                raise _error(
                    "receipt_conflict",
                    "This execution epoch already has a different canonical input.",
                )
            return existing
        execution_id = _new_id(connection, "fex")
        return _insert(
            connection,
            "execution",
            {
                "runtime": runtime or {},
                "run_id": "",
                "session": {},
                **fields,
                "id": execution_id,
                "factory_id": factory_id,
                "kind": kind,
                "scope_id": scope_id,
                "member_id": member_id,
                "epoch": epoch,
                "state": fields.get("state", "pending"),
                "group_id": group_id or f"native.{execution_id}",
                "participant_id": participant_id,
                "input_id": input_id,
            },
            actor,
        )

    async def create_execution(
        self,
        factory_id: str,
        kind: str,
        scope_id: str,
        member_id: str,
        epoch: int,
        *,
        group_id: str,
        participant_id: str,
        input_id: str = "",
        runtime: Json | None = None,
        actor: str = "factory",
        **fields: Any,
    ) -> Json:
        return await self.database.write_async(
            lambda connection: self._create_execution(
                connection,
                factory_id,
                kind,
                scope_id,
                member_id,
                epoch,
                group_id=group_id,
                participant_id=participant_id,
                input_id=input_id,
                runtime=runtime,
                actor=actor,
                **fields,
            )
        )

    async def get_execution(self, execution_id: str) -> Json:
        return await self._get("execution", execution_id)

    async def latest_execution(self, kind: str, scope_id: str, member_id: str) -> Json | None:
        def read(connection: sqlite3.Connection) -> Json | None:
            row = connection.execute(
                "SELECT id FROM native_executions WHERE kind = ? AND scope_id = ? "
                "AND member_id = ? ORDER BY epoch DESC LIMIT 1",
                (kind, scope_id, member_id),
            ).fetchone()
            return _get(connection, "execution", row[0]) if row else None

        return await self.database.read_async(read)

    async def list_executions(
        self,
        factory_id: str | None = None,
        *,
        kind: str | None = None,
        scope_id: str | None = None,
        member_id: str | None = None,
        state: str | None = None,
        after: str = "",
        limit: int = 100,
    ) -> list[Json]:
        return await self._list(
            "execution",
            {
                "factory_id": factory_id,
                "kind": kind,
                "scope_id": scope_id,
                "member_id": member_id,
                "state": state,
            },
            after,
            limit,
        )

    async def change_execution(
        self,
        execution_id: str,
        patch: Json,
        *,
        actor: str = "factory",
        expected_revision: int | None = None,
    ) -> Json:
        def write(connection: sqlite3.Connection) -> Json:
            execution = _get(connection, "execution", execution_id)
            _revision(execution, expected_revision)
            if any(
                key in patch and patch[key] != execution[key]
                for key in ("kind", "scope_id", "member_id", "epoch", "participant_id")
            ):
                raise _error("immutable_field", "Execution ownership cannot change after creation.")
            if (
                "group_id" in patch
                and execution["group_id"]
                and patch["group_id"] != execution["group_id"]
            ):
                raise _error("immutable_field", "An execution cannot change its canonical group.")
            return _save(connection, "execution", execution, patch, actor, "updated")

        return await self.database.write_async(write)

    async def add_delivery(
        self,
        execution_id: str,
        receipt: str,
        *,
        payload: Json | None = None,
        actor: str = "factory",
        **fields: Any,
    ) -> Json:
        receipt = _text(receipt, "receipt", 500, required=True)

        def write(connection: sqlite3.Connection) -> Json:
            execution = _get(connection, "execution", execution_id)
            row = connection.execute(
                "SELECT id FROM native_deliveries WHERE receipt = ?", (receipt,)
            ).fetchone()
            if row:
                prior = _get(connection, "delivery", row[0])
                if prior["execution_id"] != execution_id or prior.get("payload") != (payload or {}):
                    raise _error(
                        "receipt_conflict",
                        "This delivery receipt already identifies different content.",
                    )
                return prior
            return _insert(
                connection,
                "delivery",
                {
                    **fields,
                    "id": _new_id(connection, "fdl"),
                    "factory_id": execution["factory_id"],
                    "execution_id": execution_id,
                    "receipt": receipt,
                    "state": "pending",
                    "payload": payload or {},
                },
                actor,
            )

        return await self.database.write_async(write)

    async def get_delivery(self, delivery_id: str) -> Json:
        return await self._get("delivery", delivery_id)

    async def list_deliveries(
        self,
        factory_id: str | None = None,
        *,
        execution_id: str | None = None,
        state: str | None = None,
        after: str = "",
        limit: int = 100,
    ) -> list[Json]:
        return await self._list(
            "delivery",
            {"factory_id": factory_id, "execution_id": execution_id, "state": state},
            after,
            limit,
        )

    async def change_delivery(
        self,
        delivery_id: str,
        patch: Json,
        *,
        actor: str = "factory",
        expected_revision: int | None = None,
    ) -> Json:
        def write(connection: sqlite3.Connection) -> Json:
            delivery = _get(connection, "delivery", delivery_id)
            _revision(delivery, expected_revision)
            if any(
                key in patch and patch[key] != delivery[key]
                for key in ("execution_id", "receipt", "payload")
            ):
                raise _error("receipt_conflict", "Delivery identity and content cannot change.")
            return _save(connection, "delivery", delivery, patch, actor, "updated")

        return await self.database.write_async(write)

    async def acknowledge_delivery(
        self, delivery_id: str, *, actor: str = "factory", **acknowledgment: Any
    ) -> Json:
        return await self.change_delivery(
            delivery_id, {"state": "acknowledged", "acknowledgment": acknowledgment}, actor=actor
        )
