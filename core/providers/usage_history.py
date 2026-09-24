"""Durable history of normalized Provider subscription usage.

The Provider domain owns the canonical database ``<data-dir>/provider-usage.db``
as its primary record of upstream observations: one sample per meaningful
automatic collection, the per-Connection snapshots it held, and their usage
windows. Statistics may read the normalized projection but never writes it.
Retention is manual: only an explicit clear deletes samples.

Runtime reads and writes run on the database's own worker pool, never on the
Event Loop. Every stored timestamp is canonical fixed-width UTC text, so text
order is time order.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from math import isfinite
from pathlib import Path
from typing import Any

from core.database import (
    APPLICATION_IDS,
    CANONICAL,
    Database,
    DatabaseSpec,
    SnapshotFacts,
    canonical_database_path,
    open_database,
)
from core.utils.logging import get_logger

JsonObject = dict[str, Any]

DATABASE_NAME = "provider_usage"
FORMAT_GENERATION = 1

_LOGGER = get_logger("providers.usage")

# Every index names its reader. ``usage_samples_by_time`` serves the inclusive
# time-range read of ``ProviderUsageHistoryStore.samples`` and the ``MAX`` of
# ``latest_sampled_at``. Snapshot and window rows are clustered under their
# sample by their primary keys.
SCHEMA_SQL = """
CREATE TABLE usage_samples (
  sample_key INTEGER PRIMARY KEY AUTOINCREMENT,
  sampled_at TEXT NOT NULL
) STRICT;

CREATE INDEX usage_samples_by_time ON usage_samples (sampled_at);

CREATE TABLE usage_snapshots (
  sample_key      INTEGER NOT NULL REFERENCES usage_samples (sample_key) ON DELETE CASCADE,
  ordinal         INTEGER NOT NULL CHECK (ordinal >= 0),
  connection      TEXT NOT NULL,
  account         TEXT NOT NULL,
  display_name    TEXT NOT NULL,
  plan            TEXT,
  credits_enabled INTEGER CHECK (credits_enabled IS NULL OR credits_enabled IN (0, 1)),
  credits_balance REAL,
  error           TEXT,
  PRIMARY KEY (sample_key, ordinal),
  CHECK (credits_enabled IS NOT NULL OR credits_balance IS NULL)
) STRICT, WITHOUT ROWID;

CREATE TABLE usage_windows (
  sample_key       INTEGER NOT NULL,
  snapshot_ordinal INTEGER NOT NULL,
  ordinal          INTEGER NOT NULL CHECK (ordinal >= 0),
  label            TEXT NOT NULL,
  used_percent     REAL NOT NULL CHECK (used_percent >= 0),
  reset_at         TEXT,
  window_seconds   INTEGER CHECK (window_seconds IS NULL OR window_seconds > 0),
  used_units       REAL,
  remaining_units  REAL,
  total_units      REAL,
  unit             TEXT,
  unlimited        INTEGER CHECK (unlimited IS NULL OR unlimited IN (0, 1)),
  PRIMARY KEY (sample_key, snapshot_ordinal, ordinal),
  FOREIGN KEY (sample_key, snapshot_ordinal)
    REFERENCES usage_snapshots (sample_key, ordinal) ON DELETE CASCADE
) STRICT, WITHOUT ROWID;
"""

# Owner facts every data snapshot records for this member and re-verifies.
_SNAPSHOT_FACTS = SnapshotFacts(
    {
        "sample_count": "SELECT COUNT(*) FROM usage_samples",
        "snapshot_count": "SELECT COUNT(*) FROM usage_snapshots",
        "window_count": "SELECT COUNT(*) FROM usage_windows",
    }
)

_SNAPSHOT_KEYS = frozenset(
    {"connection", "account", "display_name", "plan", "windows", "credits", "error"}
)
_WINDOW_KEYS = frozenset(
    {
        "label",
        "used_percent",
        "reset_at",
        "window_seconds",
        "used_units",
        "remaining_units",
        "total_units",
        "unit",
        "unlimited",
    }
)
_CREDITS_KEYS = frozenset({"enabled", "balance"})


def provider_usage_database_spec(path: Path) -> DatabaseSpec:
    """Declare the canonical Provider usage database at ``path``."""
    return DatabaseSpec(
        name=DATABASE_NAME,
        path=Path(path),
        profile=CANONICAL,
        application_id=APPLICATION_IDS[DATABASE_NAME],
        format_generation=FORMAT_GENERATION,
        schema_sql=SCHEMA_SQL,
        snapshot_facts=_SNAPSHOT_FACTS,
    )


class UsageHistoryError(Exception):
    """A usage sample that cannot be stored because it is invalid."""


@dataclass(frozen=True)
class UsageHistorySample:
    """One normalized automatic collection attempt with meaningful targets.

    ``sampled_at`` is canonical UTC text. Each Provider entry is the public
    snapshot projection: ``connection``, ``account``, ``display_name``,
    ``plan``, ``windows``, ``credits`` and ``error``.
    """

    sampled_at: str
    providers: tuple[JsonObject, ...] = ()

    def to_dict(self) -> JsonObject:
        return {"sampled_at": self.sampled_at, "providers": list(self.providers)}


@dataclass(frozen=True)
class UsageHistoryReport:
    """Time-windowed durable Provider-usage samples."""

    generated_at: str
    samples: list[UsageHistorySample] = field(default_factory=list)

    def to_dict(self) -> JsonObject:
        return {
            "generated_at": self.generated_at,
            "samples": [sample.to_dict() for sample in self.samples],
        }


@dataclass(frozen=True)
class UsageHistoryClearResult:
    """Outcome of an explicit full history deletion."""

    deleted_samples: int

    def to_dict(self) -> dict[str, int]:
        return {"deleted_samples": self.deleted_samples}


def usage_history_sample(sampled_at: str, providers: Sequence[Any]) -> UsageHistorySample:
    """Validate and normalize one sample before it is stored.

    Every snapshot, window and credits object must have exactly the public
    projection keys. Numbers must be finite, ``used_percent`` is clamped to
    0-100, and timestamps need an explicit offset and become canonical UTC.
    Raises :class:`UsageHistoryError` for anything else.
    """
    if not isinstance(providers, Sequence) or isinstance(providers, str) or not providers:
        raise UsageHistoryError("providers must be a non-empty list")
    return UsageHistorySample(
        sampled_at=_canonical_timestamp(_required_string(sampled_at, "sampled_at")),
        providers=tuple(_snapshot_from_dict(item) for item in providers),
    )


class ProviderUsageHistoryStore:
    """The canonical Provider usage history, served by the shared database kernel.

    The store owns its database handle and closes it with :meth:`close`. The
    async methods run their SQL on the database's bounded worker pool.
    """

    def __init__(self, database: Database) -> None:
        self._database = database

    @classmethod
    def open(cls, data_dir: str | Path) -> ProviderUsageHistoryStore:
        """Open ``<data_dir>/provider-usage.db`` under the canonical profile."""
        path = canonical_database_path(Path(data_dir), DATABASE_NAME)
        return cls(open_database(provider_usage_database_spec(path)))

    @property
    def database(self) -> Database:
        """The kernel handle, for data snapshots and data-store status."""
        return self._database

    def close(self) -> None:
        self._database.close()

    async def append(self, sampled_at: str, providers: Sequence[Any]) -> bool:
        """Persist one meaningful automatic report.

        Empty reports mean no supported usable Connection exists and are not
        written. Error snapshots are meaningful: they preserve why an expected
        observation is missing. An invalid report raises
        :class:`UsageHistoryError` and writes nothing.
        """
        if not providers:
            return False
        sample = usage_history_sample(sampled_at, providers)
        await self._database.write_async(lambda connection: _insert_sample(connection, sample))
        return True

    def import_samples(self, samples: Iterable[UsageHistorySample]) -> int:
        """Insert normalized samples in one transaction; blocking, for offline conversion."""
        batch = list(samples)

        def insert(connection: sqlite3.Connection) -> int:
            for sample in batch:
                _insert_sample(connection, sample)
            return len(batch)

        return self._database.write(insert)

    async def samples(
        self,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[UsageHistorySample]:
        """Every sample in the inclusive UTC window, oldest first."""
        return await self._database.run_async(self._read_samples, since, until)

    async def latest_sampled_at(self) -> datetime | None:
        """The newest sample timestamp, or ``None`` when the history is empty."""

        def latest(connection: sqlite3.Connection) -> str | None:
            row = connection.execute("SELECT MAX(sampled_at) FROM usage_samples").fetchone()
            return None if row is None or row[0] is None else str(row[0])

        value = await self._database.read_async(latest)
        return None if value is None else _parse_iso_timestamp(value)

    async def clear(self) -> UsageHistoryClearResult:
        """Delete every sample in one transaction after an explicit caller confirmation."""
        deleted_samples = await self._database.write_async(_delete_all)
        _LOGGER.info("Provider usage history cleared (samples=%s)", deleted_samples)
        return UsageHistoryClearResult(deleted_samples=deleted_samples)

    def _read_samples(
        self, since: datetime | None, until: datetime | None
    ) -> list[UsageHistorySample]:
        """Select the window in one read transaction, then assemble it after it ends."""
        where, parameters = _time_window(since, until)
        with self._database.read() as connection:
            sample_rows = connection.execute(
                "SELECT s.sample_key, s.sampled_at FROM usage_samples AS s"
                f"{where} ORDER BY s.sampled_at, s.sample_key",
                parameters,
            ).fetchall()
            snapshot_rows = connection.execute(
                "SELECT n.sample_key, n.ordinal, n.connection, n.account, n.display_name, "
                "n.plan, n.credits_enabled, n.credits_balance, n.error "
                "FROM usage_snapshots AS n JOIN usage_samples AS s ON s.sample_key = n.sample_key"
                f"{where} ORDER BY n.sample_key, n.ordinal",
                parameters,
            ).fetchall()
            window_rows = connection.execute(
                "SELECT w.sample_key, w.snapshot_ordinal, w.label, w.used_percent, w.reset_at, "
                "w.window_seconds, w.used_units, w.remaining_units, w.total_units, w.unit, "
                "w.unlimited "
                "FROM usage_windows AS w JOIN usage_samples AS s ON s.sample_key = w.sample_key"
                f"{where} ORDER BY w.sample_key, w.snapshot_ordinal, w.ordinal",
                parameters,
            ).fetchall()
        return _assemble_samples(sample_rows, snapshot_rows, window_rows)


# ---------------------------------------------------------------------------
# SQL helpers
# ---------------------------------------------------------------------------


def _insert_sample(connection: sqlite3.Connection, sample: UsageHistorySample) -> None:
    cursor = connection.execute(
        "INSERT INTO usage_samples(sampled_at) VALUES (?)", (sample.sampled_at,)
    )
    sample_key = cursor.lastrowid
    snapshot_rows: list[tuple[Any, ...]] = []
    window_rows: list[tuple[Any, ...]] = []
    for snapshot_ordinal, snapshot in enumerate(sample.providers):
        credits = snapshot["credits"]
        snapshot_rows.append(
            (
                sample_key,
                snapshot_ordinal,
                snapshot["connection"],
                snapshot["account"],
                snapshot["display_name"],
                snapshot["plan"],
                None if credits is None else int(credits["enabled"]),
                None if credits is None else credits["balance"],
                snapshot["error"],
            )
        )
        for ordinal, window in enumerate(snapshot["windows"]):
            unlimited = window["unlimited"]
            window_rows.append(
                (
                    sample_key,
                    snapshot_ordinal,
                    ordinal,
                    window["label"],
                    window["used_percent"],
                    window["reset_at"],
                    window["window_seconds"],
                    window["used_units"],
                    window["remaining_units"],
                    window["total_units"],
                    window["unit"],
                    None if unlimited is None else int(unlimited),
                )
            )
    connection.executemany(
        "INSERT INTO usage_snapshots(sample_key, ordinal, connection, account, display_name, "
        "plan, credits_enabled, credits_balance, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        snapshot_rows,
    )
    connection.executemany(
        "INSERT INTO usage_windows(sample_key, snapshot_ordinal, ordinal, label, used_percent, "
        "reset_at, window_seconds, used_units, remaining_units, total_units, unit, unlimited) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        window_rows,
    )


def _delete_all(connection: sqlite3.Connection) -> int:
    row = connection.execute("SELECT COUNT(*) FROM usage_samples").fetchone()
    connection.execute("DELETE FROM usage_windows")
    connection.execute("DELETE FROM usage_snapshots")
    connection.execute("DELETE FROM usage_samples")
    return int(row[0]) if row is not None else 0


def _time_window(since: datetime | None, until: datetime | None) -> tuple[str, tuple[str, ...]]:
    """An inclusive ``sampled_at`` range filter; canonical text compares as time."""
    clauses: list[str] = []
    parameters: list[str] = []
    if since is not None:
        clauses.append("s.sampled_at >= ?")
        parameters.append(_format_canonical(since))
    if until is not None:
        clauses.append("s.sampled_at <= ?")
        parameters.append(_format_canonical(until))
    if not clauses:
        return "", ()
    return " WHERE " + " AND ".join(clauses), tuple(parameters)


def _assemble_samples(
    sample_rows: Sequence[Sequence[Any]],
    snapshot_rows: Sequence[Sequence[Any]],
    window_rows: Sequence[Sequence[Any]],
) -> list[UsageHistorySample]:
    windows: dict[tuple[int, int], list[JsonObject]] = {}
    for row in window_rows:
        (
            sample_key,
            snapshot_ordinal,
            label,
            used_percent,
            reset_at,
            window_seconds,
            used_units,
            remaining_units,
            total_units,
            unit,
            unlimited,
        ) = row
        windows.setdefault((sample_key, snapshot_ordinal), []).append(
            {
                "label": label,
                "used_percent": used_percent,
                "reset_at": reset_at,
                "window_seconds": window_seconds,
                "used_units": used_units,
                "remaining_units": remaining_units,
                "total_units": total_units,
                "unit": unit,
                "unlimited": None if unlimited is None else bool(unlimited),
            }
        )
    snapshots: dict[int, list[JsonObject]] = {}
    for row in snapshot_rows:
        (
            sample_key,
            ordinal,
            connection,
            account,
            display_name,
            plan,
            credits_enabled,
            credits_balance,
            error,
        ) = row
        snapshots.setdefault(sample_key, []).append(
            {
                "connection": connection,
                "account": account,
                "display_name": display_name,
                "plan": plan,
                "windows": windows.get((sample_key, ordinal), []),
                "credits": None
                if credits_enabled is None
                else {"enabled": bool(credits_enabled), "balance": credits_balance},
                "error": error,
            }
        )
    return [
        UsageHistorySample(sampled_at=sampled_at, providers=tuple(snapshots.get(sample_key, ())))
        for sample_key, sampled_at in sample_rows
    ]


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _snapshot_from_dict(raw: Any) -> JsonObject:
    if not isinstance(raw, dict):
        raise UsageHistoryError("provider snapshot must be an object")
    _require_exact_keys(raw, _SNAPSHOT_KEYS, "provider snapshot")
    windows_raw = raw["windows"]
    if not isinstance(windows_raw, list):
        raise UsageHistoryError("provider windows must be a list")
    return {
        "connection": _required_string(raw["connection"], "connection"),
        "account": _required_string(raw["account"], "account"),
        "display_name": _required_string(raw["display_name"], "display_name"),
        "plan": _optional_string(raw["plan"], "plan"),
        "windows": [_window_from_dict(item) for item in windows_raw],
        "credits": _credits_from_dict(raw["credits"]),
        "error": _optional_string(raw["error"], "error"),
    }


def _window_from_dict(raw: Any) -> JsonObject:
    if not isinstance(raw, dict):
        raise UsageHistoryError("usage window must be an object")
    _require_exact_keys(raw, _WINDOW_KEYS, "usage window")
    used_percent = _optional_number(raw["used_percent"], "used_percent")
    if used_percent is None:
        raise UsageHistoryError("used_percent must be a number")
    window_seconds = raw["window_seconds"]
    if window_seconds is not None and (
        isinstance(window_seconds, bool)
        or not isinstance(window_seconds, int)
        or window_seconds <= 0
    ):
        raise UsageHistoryError("window_seconds must be a positive integer or null")
    unlimited = raw["unlimited"]
    if unlimited is not None and not isinstance(unlimited, bool):
        raise UsageHistoryError("unlimited must be a boolean or null")
    reset_at = _optional_string(raw["reset_at"], "reset_at")
    return {
        "label": _required_string(raw["label"], "label"),
        "used_percent": max(0.0, min(100.0, used_percent)),
        "reset_at": None if reset_at is None else _canonical_timestamp(reset_at),
        "window_seconds": window_seconds,
        "used_units": _optional_number(raw["used_units"], "used_units"),
        "remaining_units": _optional_number(raw["remaining_units"], "remaining_units"),
        "total_units": _optional_number(raw["total_units"], "total_units"),
        "unit": _optional_string(raw["unit"], "unit"),
        "unlimited": unlimited,
    }


def _credits_from_dict(raw: Any) -> JsonObject | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise UsageHistoryError("credits must be an object or null")
    _require_exact_keys(raw, _CREDITS_KEYS, "credits")
    if not isinstance(raw["enabled"], bool):
        raise UsageHistoryError("credits.enabled must be a boolean")
    return {
        "enabled": raw["enabled"],
        "balance": _optional_number(raw["balance"], "credits.balance"),
    }


def _required_string(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise UsageHistoryError(f"{field_name} must be a non-empty string")
    return value


def _optional_string(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise UsageHistoryError(f"{field_name} must be a non-empty string or null")
    return value


def _optional_number(value: Any, field_name: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise UsageHistoryError(f"{field_name} must be a number or null")
    number = float(value)
    if not isfinite(number):
        raise UsageHistoryError(f"{field_name} must be finite")
    return number


def _require_exact_keys(raw: JsonObject, expected: frozenset[str], context: str) -> None:
    if set(raw) != expected:
        raise UsageHistoryError(f"{context} fields are invalid")


# ---------------------------------------------------------------------------
# Timestamps
# ---------------------------------------------------------------------------


def _parse_iso_timestamp(value: str) -> datetime | None:
    """Parse ISO 8601 with an explicit offset (``Z`` included) as aware UTC."""
    try:
        normalized = value.removesuffix("Z") + "+00:00" if value.endswith("Z") else value
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def _canonical_timestamp(value: str) -> str:
    parsed = _parse_iso_timestamp(value)
    if parsed is None:
        raise UsageHistoryError("timestamp must be ISO 8601 with an explicit offset")
    return _format_canonical(parsed)


def _format_canonical(value: datetime) -> str:
    """Fixed-width ``YYYY-MM-DDTHH:MM:SS.ffffffZ``; a naive value is taken as UTC."""
    aware = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return aware.replace(tzinfo=None).isoformat(timespec="microseconds") + "Z"
