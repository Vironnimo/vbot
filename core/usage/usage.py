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
import math
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NamedTuple, Protocol

from core.database import Database, open_database
from core.models.pricing import TokenPricing, nonnegative_amount, price_usage, project_cost
from core.sessions import SessionAddress
from core.usage._calibration import InputEstimateCalibration, LearnedSample
from core.usage._failures import classify_failure
from core.usage._schema import usage_database_spec
from core.utils.errors import FailureClass
from core.utils.ids import new_id
from core.utils.timestamps import utc_now_timestamp

_COLUMNS = (
    "id,revision,timestamp,model,kind,status,usage_json,agent_id,project_id,"
    "session_id,run_id,connection_id,session_title,owner_name,group_id,session_generation_id,"
    "duration_ms,failure_class,http_status"
)
_PLACEHOLDERS = ",".join("?" for _ in _COLUMNS.split(","))
_COUNTERS = (
    "input_tokens",
    "output_tokens",
    "reasoning_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
)
_FLAGS = ("input_tokens_estimated", "output_tokens_estimated", "estimated")
_IMPORT_NAME = "session-history-v2"
READ_PAGE_SIZE = 1000


class UsageHistorySource(Protocol):
    @property
    def database(self) -> Database: ...

    def usage_history(
        self, after_entry_key: int = 0, *, limit: int = 1000
    ) -> tuple[dict[str, Any], ...]: ...


class UsageSessionSource(Protocol):
    """Resolve a request's immutable Session generation at dispatch."""

    def list_history_versions(
        self, addresses: list[SessionAddress]
    ) -> dict[SessionAddress, tuple[str, int]]: ...

    async def run_async[Result](
        self, function: Callable[..., Result], *arguments: Any, **keyword_arguments: Any
    ) -> Result: ...


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
    session_generation_id: str | None = None
    duration_ms: int | None = None
    failure_class: str | None = None
    http_status: int | None = None


class _Settlement(NamedTuple):
    """What a settling ``finish`` measured besides Usage."""

    duration_ms: int | None
    failure_class: FailureClass | None
    http_status: int | None


class UsagePage(NamedTuple):
    """One bounded read of changed calls and the ledger's revision watermark at that read."""

    revision: int
    records: tuple[UsageRecord, ...]


def _revision(connection: sqlite3.Connection) -> int:
    row = connection.execute(
        "INSERT INTO usage_revision (singleton,revision) VALUES (1,1) "
        "ON CONFLICT (singleton) DO UPDATE SET revision=revision+1 RETURNING revision"
    ).fetchone()
    return int(row[0])


def _import_cursor(
    connection: sqlite3.Connection, source_id: str, source_restore_id: str | None, cursor: int
) -> None:
    connection.execute(
        "INSERT INTO usage_imports (name,source_id,source_restore_id,cursor) VALUES (?,?,?,?) "
        "ON CONFLICT (name) DO UPDATE SET source_id=excluded.source_id,"
        "source_restore_id=excluded.source_restore_id,cursor=excluded.cursor",
        (_IMPORT_NAME, source_id, source_restore_id, cursor),
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
    speed = _speed_fields(source.get("speed"))
    if speed:
        result["speed"] = speed
    return result


_SPEED_COUNTS = ("first_token_ms", "first_answer_ms", "duration_ms", "tokens", "window_ms")
_SPEED_BASES = ("output", "answer", "provider")


def _speed_fields(value: Any) -> dict[str, Any]:
    """Chat's measured output speed (``chat/usage.md`` -> Output speed), validated."""
    if not isinstance(value, Mapping):
        return {}
    speed: dict[str, Any] = {
        key: value[key]
        for key in _SPEED_COUNTS
        if isinstance(value.get(key), int)
        and not isinstance(value.get(key), bool)
        and value[key] >= 0
    }
    rate = value.get("output_tokens_per_second")
    if (
        isinstance(rate, int | float)
        and not isinstance(rate, bool)
        and math.isfinite(rate)
        and rate > 0
    ):
        speed["output_tokens_per_second"] = float(rate)
    if value.get("basis") in _SPEED_BASES:
        speed["basis"] = value["basis"]
    if value.get("estimated") is True:
        speed["estimated"] = True
    return speed


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
    if "speed" not in merged and "speed" in incoming:
        merged["speed"] = incoming["speed"]

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
        self,
        path: Path,
        *,
        pricing_lookup: Callable[[str], TokenPricing | None] | None = None,
        session_source: UsageSessionSource | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.database = open_database(usage_database_spec(path))
        self._pricing_lookup = pricing_lookup
        self._session_source = session_source
        # Monotonic dispatch instants of unsettled calls: only a call this
        # process started has a duration.
        self._clock = clock
        self._dispatched: dict[str, float] = {}
        self._import_lock = threading.Lock()
        try:
            self.database.write(self._interrupt_unfinished)
            self._calibration = InputEstimateCalibration(self.database)
        except BaseException:
            self.database.close()
            raise

    @property
    def ledger_id(self) -> str:
        """Opaque continuity token of this ledger for projections that follow it.

        Stable across restarts. A data snapshot restore keeps the database
        identity but can bring back older calls whose revisions new work then
        catches up with, so the token also names the ledger's latest restore
        (``Database.restore_id``): a projection that sees another token
        rebuilds instead of continuing from its revision.
        """
        return f"{self.database.database_id}:{self.database.restore_id or ''}"

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
        session_generation_id: str | None = None,
    ) -> str:
        """Durably record a call immediately before its dispatch; returns its id.

        ``session_generation_id`` is the Session generation the caller already
        knows it works in (a Run's admitted Session); without it, the generation
        live at the Session address is looked up.
        """
        if not model or not kind:
            raise ValueError("Usage requires a Model and request kind")
        generation_id = session_generation_id if agent_id and session_id else None
        if generation_id is None and self._session_source is not None and agent_id and session_id:
            address = SessionAddress(project_id, agent_id, session_id)
            versions = await self._session_source.run_async(
                self._session_source.list_history_versions, [address]
            )
            if version := versions.get(address):
                generation_id = version[0]
        identifier = new_id("use")
        stamp = utc_now_timestamp()
        dispatched = self._clock()

        def operation(connection: sqlite3.Connection) -> str:
            connection.execute(
                f"INSERT INTO usage_calls ({_COLUMNS}) VALUES ({_PLACEHOLDERS})",
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
                    generation_id,
                    None,
                    None,
                    None,
                ),
            )
            return identifier

        # A cancel cannot stop an admitted insert; the call it recorded is
        # settled as cancelled instead of staying started until a restart.
        identifier, cancelled = await _settled(
            asyncio.create_task(self.database.write_async(operation))
        )
        self._dispatched[identifier] = dispatched
        if cancelled is not None:
            await self.finish(identifier, status="cancelled")
            raise cancelled
        return identifier

    async def finish(
        self,
        call_id: str,
        usage: Mapping[str, Any] | None = None,
        *,
        status: str = "completed",
        failure: BaseException | FailureClass | None = None,
    ) -> dict[str, Any]:
        """Settle a call with its outcome; a ``failed`` one with what failed it.

        ``failure`` is the error that ended the attempt, or its class where no
        error exists; only its class and HTTP status are kept. The first
        settlement also records the duration since ``start``.
        """
        dispatched = self._dispatched.pop(call_id, None)
        duration_ms = (
            max(0, round((self._clock() - dispatched) * 1000)) if dispatched is not None else None
        )
        failure_class, http_status = (
            classify_failure(failure) if status == "failed" else (None, None)
        )
        return await self._settle_save(
            call_id, usage, status, _Settlement(duration_ms, failure_class, http_status)
        )

    async def update(
        self,
        call_id: str,
        usage: Mapping[str, Any] | None,
        *,
        input_estimate: tuple[str, int] | None = None,
    ) -> dict[str, Any]:
        """Enrich a call's Usage without changing its outcome.

        ``input_estimate`` is the request's Model and the uncorrected local
        estimate of exactly that request's input. When the call's Usage then
        holds a Provider-measured input, the pair teaches the input estimate
        calibration in the same transaction (see :meth:`record_input_estimate`).
        """
        return await self._settle_save(call_id, usage, None, None, input_estimate)

    def input_estimate_factor(self, model: str) -> float:
        """Correction for local input estimates of ``model``; 1.0 without evidence.

        Multiply an estimate of a request to ``model`` (``provider/model``, an
        optional ``::`` Connection scope is ignored) by it. Cheap and lock-free.
        """
        return self._calibration.factor(model)

    def record_input_estimate(self, model: str, *, measured: int, estimated: int) -> None:
        """Learn from one request: its Provider-measured input and local estimate.

        ``estimated`` must be the uncorrected local estimate of exactly the
        measured request. Tiny requests and implausible ratios are ignored.
        Blocking (one small database write): call it off the Event Loop.
        """
        self._calibration.record(model, measured, estimated)

    async def _settle_save(
        self,
        call_id: str,
        usage: Mapping[str, Any] | None,
        status: str | None,
        settlement: _Settlement | None,
        input_estimate: tuple[str, int] | None = None,
    ) -> dict[str, Any]:
        # Once a response has supplied Usage, cancellation must not discard it
        # while its persistence is waiting for worker admission.
        result, cancelled = await _settled(
            asyncio.create_task(
                self.database.run_async(
                    self._save, call_id, usage, status, settlement, input_estimate
                )
            )
        )
        if cancelled is not None:
            raise cancelled
        return result

    def _save(
        self,
        call_id: str,
        usage: Mapping[str, Any] | None,
        status: str | None,
        settlement: _Settlement | None,
        input_estimate: tuple[str, int] | None,
    ) -> dict[str, Any]:
        incoming = _usage_fields(usage)

        def operation(
            connection: sqlite3.Connection,
        ) -> tuple[dict[str, Any], LearnedSample | None]:
            row = connection.execute(
                "SELECT model,status,usage_json,duration_ms,failure_class,http_status "
                "FROM usage_calls WHERE id=?",
                (call_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"Unknown Usage call: {call_id}")
            merged: dict[str, Any] = json.loads(row["usage_json"])
            previous_cost = project_cost(merged.get("cost"))
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
            elif (
                previous_cost is not None
                and previous_cost["source"] == "provider"
                and incoming.get("cost", {}).get("source") != "provider"
            ):
                merged["cost"] = previous_cost
            elif "cost" not in incoming and (incoming or "cost" not in merged):
                pricing = TokenPricing.from_dict((previous_cost or {}).get("pricing"))
                if pricing is None and self._pricing_lookup is not None:
                    pricing = self._pricing_lookup(row["model"])
                merged["cost"] = price_usage(merged, pricing)
            encoded = json.dumps(merged, ensure_ascii=False, allow_nan=False)
            new_status = status or row["status"]
            stored = (row["duration_ms"], row["failure_class"], row["http_status"])
            outcome = stored
            if settlement is not None:
                # A repeated settlement keeps the first duration; the latest
                # outcome decides the failure fields.
                outcome = (
                    stored[0] if stored[0] is not None else settlement.duration_ms,
                    settlement.failure_class,
                    settlement.http_status,
                )
            if encoded != row["usage_json"] or new_status != row["status"] or outcome != stored:
                connection.execute(
                    "UPDATE usage_calls SET revision=?,status=?,usage_json=?,"
                    "duration_ms=?,failure_class=?,http_status=? WHERE id=?",
                    (_revision(connection), new_status, encoded, *outcome, call_id),
                )
            # The producer still owns its full Usage contract (for example
            # Chat's context_usage). Return its metadata unchanged without
            # persisting it in the accounting database.
            result = {
                key: value
                for key, value in (usage or {}).items()
                if key
                not in (*_COUNTERS, *_FLAGS, "cost", "reported_cost_usd", "speed", "usage_call_id")
            }
            learned = None
            measured = merged.get("input_tokens")
            if (
                input_estimate is not None
                and isinstance(measured, int)
                and merged.get("input_tokens_estimated") is not True
            ):
                model, estimated = input_estimate
                learned = self._calibration.write(connection, model, measured, estimated)
            return {**result, **merged}, learned

        saved, learned = self.database.write(operation)
        self._calibration.adopt(learned)
        return saved

    def read_since(
        self, revision: int = 0, *, page_size: int = READ_PAGE_SIZE
    ) -> Iterator[UsagePage]:
        """Current versions of the calls changed after ``revision``, in bounded pages.

        Each page is one short read transaction: at most ``page_size`` calls
        with the next higher revisions, plus the ledger's revision watermark at
        that read. The first page arrives even when nothing changed. A call
        that changes while pages are read reappears later with its newer
        revision, so once the iterator is exhausted the last page's watermark
        covers every change: reading after it continues exactly there.
        """
        if page_size < 1:
            raise ValueError("Usage pages need at least one call")
        position = revision
        while True:
            with self.database.read() as connection:
                high = connection.execute(
                    "SELECT COALESCE(MAX(revision),0) FROM usage_revision"
                ).fetchone()[0]
                rows = connection.execute(
                    f"SELECT {_COLUMNS} FROM usage_calls WHERE revision>? ORDER BY revision "
                    "LIMIT ?",
                    (position, page_size),
                ).fetchall()
            records = []
            for row in rows:
                fields = dict(row)
                fields["usage"] = json.loads(fields.pop("usage_json"))
                records.append(UsageRecord(**fields))
            yield UsagePage(int(high), tuple(records))
            if len(records) < page_size:
                return
            position = records[-1].revision

    def import_session_history(self, sessions: UsageHistorySource) -> None:
        """Resumable named import of retained own-audit Usage, including archives.

        The cursor and imported records commit together, with the identity and
        latest restore of the Session database they were read from. Call ids
        embedded by current producers enrich live recording; older rows have
        deterministic ids scoped to their source database and entry identity.
        The import resumes from the cursor while that source is unchanged; a
        restored or another source replays history from the start, because a
        restore can rewind the entry keys the cursor counts.
        """
        with self._import_lock:
            self._import_history(sessions)

    def _import_history(self, sessions: UsageHistorySource) -> None:
        source = sessions.database
        source_id, source_restore_id = source.database_id, source.restore_id
        with self.database.read() as connection:
            row = connection.execute(
                "SELECT source_id,source_restore_id,cursor FROM usage_imports WHERE name=?",
                (_IMPORT_NAME,),
            ).fetchone()
        resumed = (
            row is not None
            and row["source_id"] == source_id
            and row["source_restore_id"] == source_restore_id
        )
        cursor = int(row["cursor"]) if resumed else 0
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
                    if not (isinstance(existing_id, str) and existing_id) and not any(
                        key in usage for key in (*_COUNTERS, "cost", "reported_cost_usd")
                    ):
                        continue
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
                        "SELECT status,usage_json,session_generation_id "
                        "FROM usage_calls WHERE id=?",
                        (identifier,),
                    ).fetchone()
                    status = "interrupted" if record.get("interrupted") else "completed"
                    previous = json.loads(existing["usage_json"]) if existing else {}
                    usage = _import_usage(previous, usage)
                    usage["usage_call_id"] = identifier
                    encoded = json.dumps(usage, ensure_ascii=False, allow_nan=False)
                    if existing is not None:
                        if existing["status"] not in {"started", "interrupted"}:
                            status = existing["status"]
                        generation_id = existing["session_generation_id"] or record["generation_id"]
                        if (
                            encoded != existing["usage_json"]
                            or status != existing["status"]
                            or generation_id != existing["session_generation_id"]
                        ):
                            connection.execute(
                                "UPDATE usage_calls SET revision=?,status=?,usage_json=?,"
                                "session_generation_id=? "
                                "WHERE id=?",
                                (_revision(connection), status, encoded, generation_id, identifier),
                            )
                        continue
                    connection.execute(
                        f"INSERT INTO usage_calls ({_COLUMNS}) VALUES ({_PLACEHOLDERS})",
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
                            record["generation_id"],
                            None,
                            None,
                            None,
                        ),
                    )
                _import_cursor(connection, source_id, source_restore_id, batch[-1]["entry_key"])

            self.database.write(operation)
            cursor = batch[-1]["entry_key"]
        if not resumed and cursor == 0:
            # A new or restored source without billable history still records
            # its identity, so later imports resume instead of replaying.
            self.database.write(
                lambda connection: _import_cursor(connection, source_id, source_restore_id, 0)
            )


async def _settled[Result](
    pending: asyncio.Task[Result],
) -> tuple[Result, asyncio.CancelledError | None]:
    """Wait for *pending* through any cancellation; return its result and that cancellation."""
    cancelled = None
    while not pending.done():
        try:
            await asyncio.shield(pending)
        except asyncio.CancelledError as error:
            cancelled = error
    return pending.result(), cancelled
