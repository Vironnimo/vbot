"""One durable record per Model attempt, including work without a Session.

Callers start immediately before dispatch and finish before validating or
persisting the result. Cumulative reports replace counters; enrichment and
Session-history imports preserve call identity. Nothing follows Session
archive/deletion, and no prompts or generated content enter this database.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from weakref import WeakSet

from core.database import Database, open_database
from core.models.pricing import TokenPricing, nonnegative_amount, price_usage, project_cost
from core.usage._schema import usage_database_spec
from core.utils.ids import new_id
from core.utils.timestamps import utc_now_timestamp

_COLUMNS = (
    "id,revision,timestamp,model,kind,status,usage_json,agent_id,project_id,"
    "session_id,run_id,connection_id,session_title,owner_name,group_id"
)
_COUNTERS = (
    "input_tokens",
    "output_tokens",
    "reasoning_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
)
_FLAGS = ("input_tokens_estimated", "output_tokens_estimated", "estimated")
_IMPORT_NAME = "session-history-v1"


class UsageHistorySource(Protocol):
    @property
    def database(self) -> Database: ...

    def usage_history(
        self, after_entry_key: int = 0, *, limit: int = 1000
    ) -> tuple[dict[str, Any], ...]: ...


@dataclass(frozen=True)
class UsageRecord:
    id: str
    revision: int
    timestamp: str
    model: str
    kind: str
    status: str
    usage: dict[str, Any]
    agent_id: str | None = None
    project_id: str | None = None
    session_id: str | None = None
    run_id: str | None = None
    connection_id: str | None = None
    session_title: str | None = None
    owner_name: str | None = None
    group_id: str | None = None


def _revision(connection: sqlite3.Connection) -> int:
    row = connection.execute(
        "INSERT INTO usage_revision (singleton,revision) VALUES (1,1) "
        "ON CONFLICT (singleton) DO UPDATE SET revision=revision+1 RETURNING revision"
    ).fetchone()
    return int(row[0])


def _import_cursor(connection: sqlite3.Connection, source_id: str, cursor: int) -> None:
    connection.execute(
        "INSERT INTO usage_imports (name,source_id,cursor) VALUES (?,?,?) "
        "ON CONFLICT (name) DO UPDATE SET source_id=excluded.source_id,cursor=excluded.cursor",
        (_IMPORT_NAME, source_id, cursor),
    )


def _usage_fields(value: Mapping[str, Any] | None) -> dict[str, Any]:
    """Only normalized accounting metadata crosses this persistence boundary."""
    source = value or {}
    result = {key: source[key] for key in (*_COUNTERS, *_FLAGS) if key in source}
    # Canonical producers already validate these fields. Reject non-JSON numeric
    # values before they can poison a durable source or a later report.
    for key in _COUNTERS:
        if key in result and (
            isinstance(result[key], bool) or not isinstance(result[key], int) or result[key] < 0
        ):
            result.pop(key)
    for key in _FLAGS:
        if key in result and not isinstance(result[key], bool):
            result.pop(key)
    reported = nonnegative_amount(source.get("reported_cost_usd"))
    if reported is not None:
        result["reported_cost_usd"] = reported
    cost = project_cost(source.get("cost"))
    if cost is not None:
        result["cost"] = cost
    return result


def _import_usage(previous: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    """Import evidence without replacing known measurements with older snapshots."""
    merged = dict(previous)
    for counter in _COUNTERS:
        flag = f"{counter}_estimated"
        if counter not in incoming or (
            counter in previous
            and not (previous.get(flag) is True and incoming.get(flag) is not True)
        ):
            continue
        merged[counter] = incoming[counter]
        if flag in _FLAGS:
            if flag in incoming:
                merged[flag] = incoming[flag]
            else:
                merged.pop(flag, None)
    if any(merged.get(key) is True for key in _FLAGS[:2]):
        merged["estimated"] = True
    else:
        merged.pop("estimated", None)

    old_cost = project_cost(previous.get("cost"))
    new_cost = project_cost(incoming.get("cost"))
    if "reported_cost_usd" in previous:
        merged["cost"] = price_usage(previous, None)
    elif old_cost is not None and old_cost["source"] == "provider":
        merged["cost"] = old_cost
    elif "reported_cost_usd" in incoming:
        merged["reported_cost_usd"] = incoming["reported_cost_usd"]
        merged["cost"] = price_usage(incoming, None)
    elif new_cost is not None and new_cost["source"] == "provider":
        merged["cost"] = new_cost
    else:
        evidence = (*_COUNTERS, *_FLAGS[:2])
        unchanged = all(merged.get(key) == previous.get(key) for key in evidence)
        pricing = TokenPricing.from_dict((old_cost or {}).get("pricing"))
        pricing = pricing or TokenPricing.from_dict((new_cost or {}).get("pricing"))
        if old_cost is not None and old_cost["source"] == "catalog" and unchanged:
            merged["cost"] = old_cost
        elif pricing is not None:
            # Revalue changed counters at a retained historical rate, never at
            # today's catalog price, when combining two partial snapshots.
            merged["cost"] = price_usage(merged, pricing)
        elif new_cost is not None and all(merged.get(key) == incoming.get(key) for key in evidence):
            merged["cost"] = new_cost
        elif old_cost is not None and not unchanged:
            merged["cost"] = price_usage(merged, None)
    return merged


class UsageRecorder:
    """Canonical accounting owner; the Statistics database remains disposable."""

    def __init__(
        self, path: Path, *, pricing_lookup: Callable[[str], TokenPricing | None] | None = None
    ) -> None:
        self.database = open_database(usage_database_spec(path))
        self._pricing_lookup = pricing_lookup
        self._projection_epoch = new_id("epoch")
        self._import_lock = threading.Lock()
        self._imported_sources: WeakSet[Database] = WeakSet()
        try:
            self.database.write(self._interrupt_unfinished)
        except BaseException:
            self.database.close()
            raise

    @property
    def projection_epoch(self) -> str:
        """Opaque continuity token, fresh for every recorder initialization."""
        return self._projection_epoch

    @staticmethod
    def _interrupt_unfinished(connection: sqlite3.Connection) -> None:
        pending = connection.execute("SELECT id FROM usage_calls WHERE status='started'").fetchall()
        for row in pending:
            connection.execute(
                "UPDATE usage_calls SET status='interrupted',revision=? WHERE id=?",
                (_revision(connection), row["id"]),
            )

    def close(self) -> None:
        self.database.close()

    async def aclose(self) -> None:
        self.close()

    async def start(
        self,
        *,
        model: str,
        kind: str,
        agent_id: str | None = None,
        project_id: str | None = None,
        session_id: str | None = None,
        run_id: str | None = None,
        connection_id: str | None = None,
        session_title: str | None = None,
        owner_name: str | None = None,
        group_id: str | None = None,
    ) -> str:
        if not model or not kind:
            raise ValueError("Usage requires a Model and request kind")
        identifier = new_id("use")
        stamp = utc_now_timestamp()

        def operation(connection: sqlite3.Connection) -> str:
            connection.execute(
                f"INSERT INTO usage_calls ({_COLUMNS}) VALUES ({','.join('?' for _ in range(15))})",
                (
                    identifier,
                    _revision(connection),
                    stamp,
                    model.split("::", 1)[0],
                    kind,
                    "started",
                    "{}",
                    agent_id,
                    project_id,
                    session_id,
                    run_id,
                    connection_id,
                    session_title,
                    owner_name,
                    group_id,
                ),
            )
            return identifier

        return await self.database.write_async(operation)

    async def finish(
        self, call_id: str, usage: Mapping[str, Any] | None = None, *, status: str = "completed"
    ) -> dict[str, Any]:
        return await self._settle_save(call_id, usage, status)

    async def update(self, call_id: str, usage: Mapping[str, Any] | None) -> dict[str, Any]:
        return await self._settle_save(call_id, usage, None)

    async def _settle_save(
        self, call_id: str, usage: Mapping[str, Any] | None, status: str | None
    ) -> dict[str, Any]:
        # Once a response has supplied Usage, cancellation must not discard it
        # while its persistence is waiting for worker admission.
        pending = asyncio.create_task(self.database.run_async(self._save, call_id, usage, status))
        cancelled = None
        while not pending.done():
            try:
                await asyncio.shield(pending)
            except asyncio.CancelledError as error:
                cancelled = error
        result = pending.result()
        if cancelled is not None:
            raise cancelled
        return result

    def _save(
        self, call_id: str, usage: Mapping[str, Any] | None, status: str | None
    ) -> dict[str, Any]:
        incoming = _usage_fields(usage)

        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            row = connection.execute(
                "SELECT model,status,usage_json FROM usage_calls WHERE id=?", (call_id,)
            ).fetchone()
            if row is None:
                raise ValueError(f"Unknown Usage call: {call_id}")
            merged: dict[str, Any] = json.loads(row["usage_json"])
            for counter in ("input_tokens", "output_tokens"):
                if counter in incoming and f"{counter}_estimated" not in incoming:
                    merged.pop(f"{counter}_estimated", None)
            merged.update(incoming)
            merged["usage_call_id"] = call_id
            if any(merged.get(key) is True for key in _FLAGS[:2]):
                merged["estimated"] = True
            else:
                merged.pop("estimated", None)
            if "reported_cost_usd" in merged:
                merged["cost"] = price_usage(merged, None)
            elif "cost" not in incoming and (incoming or "cost" not in merged):
                pricing = self._pricing_lookup(row["model"]) if self._pricing_lookup else None
                merged["cost"] = price_usage(merged, pricing)
            encoded = json.dumps(merged, ensure_ascii=False, allow_nan=False)
            new_status = status or row["status"]
            if encoded != row["usage_json"] or new_status != row["status"]:
                connection.execute(
                    "UPDATE usage_calls SET revision=?,status=?,usage_json=? WHERE id=?",
                    (_revision(connection), new_status, encoded, call_id),
                )
            # The producer still owns its full Usage contract (for example
            # Chat's context_usage). Return its metadata unchanged without
            # persisting it in the accounting database.
            result = {
                key: value
                for key, value in (usage or {}).items()
                if key not in (*_COUNTERS, *_FLAGS, "cost", "reported_cost_usd", "usage_call_id")
            }
            return {**result, **merged}

        return self.database.write(operation)

    def read_since(self, revision: int = 0) -> tuple[int, tuple[UsageRecord, ...]]:
        """Current versions changed since a watermark, from one read snapshot."""
        with self.database.read() as connection:
            high = connection.execute(
                "SELECT COALESCE(MAX(revision),0) FROM usage_revision"
            ).fetchone()[0]
            records = connection.execute(
                f"SELECT {_COLUMNS} FROM usage_calls WHERE revision>? ORDER BY revision",
                (revision,),
            ).fetchall()
        decoded = []
        for row in records:
            fields = dict(row)
            fields["usage"] = json.loads(fields.pop("usage_json"))
            decoded.append(UsageRecord(**fields))
        return int(high), tuple(decoded)

    def import_session_history(self, sessions: UsageHistorySource) -> None:
        """Resumable named import of retained own-audit Usage, including archives.

        The cursor and imported records commit together. Call ids embedded by
        current producers enrich live recording; older rows have deterministic
        ids scoped to their source database and entry identity. A new source
        handle replays history because restore can rewind both keys and cursor.
        """
        with self._import_lock:
            self._import_history(sessions)

    def _import_history(self, sessions: UsageHistorySource) -> None:
        source = sessions.database
        source_id = source.database_id
        with self.database.read() as connection:
            row = connection.execute(
                "SELECT source_id,cursor FROM usage_imports WHERE name=?", (_IMPORT_NAME,)
            ).fetchone()
        cursor = (
            int(row["cursor"])
            if source in self._imported_sources and row and row["source_id"] == source_id
            else 0
        )
        while True:
            batch = sessions.usage_history(cursor, limit=1000)
            if not batch:
                break

            def operation(
                connection: sqlite3.Connection, batch: tuple[dict[str, Any], ...] = batch
            ) -> None:
                for record in batch:
                    usage = _usage_fields(record["usage"])
                    existing_id = (record["usage"] or {}).get("usage_call_id")
                    identity = (
                        f"{source_id}/{record['generation_id']}/"
                        f"{record['entry_key']}/{record['message_id']}"
                    )
                    identifier = (
                        existing_id
                        if isinstance(existing_id, str) and existing_id
                        else ("legacy_" + hashlib.sha256(identity.encode()).hexdigest())
                    )
                    existing = connection.execute(
                        "SELECT status,usage_json FROM usage_calls WHERE id=?", (identifier,)
                    ).fetchone()
                    status = "interrupted" if record.get("interrupted") else "completed"
                    previous = json.loads(existing["usage_json"]) if existing else {}
                    usage = _import_usage(previous, usage)
                    usage["usage_call_id"] = identifier
                    encoded = json.dumps(usage, ensure_ascii=False, allow_nan=False)
                    if existing is not None:
                        if existing["status"] not in {"started", "interrupted"}:
                            status = existing["status"]
                        if encoded != existing["usage_json"] or status != existing["status"]:
                            connection.execute(
                                "UPDATE usage_calls SET revision=?,status=?,usage_json=? "
                                "WHERE id=?",
                                (_revision(connection), status, encoded, identifier),
                            )
                        continue
                    connection.execute(
                        f"INSERT INTO usage_calls ({_COLUMNS}) VALUES "
                        f"({','.join('?' for _ in range(15))})",
                        (
                            identifier,
                            _revision(connection),
                            record["timestamp"],
                            (record["model"] or "unknown").split("::", 1)[0],
                            record["kind"],
                            status,
                            encoded,
                            record["agent_id"],
                            record["project_id"],
                            record["session_id"],
                            record["run_id"],
                            None,
                            record["session_title"],
                            record["owner_name"],
                            record["group_id"],
                        ),
                    )
                _import_cursor(connection, source_id, batch[-1]["entry_key"])

            self.database.write(operation)
            cursor = batch[-1]["entry_key"]
        if cursor == 0:
            # A restored empty source must reset an older persisted cursor before
            # this handle switches to incremental imports of newly written rows.
            self.database.write(lambda connection: _import_cursor(connection, source_id, 0))
        self._imported_sources.add(source)
