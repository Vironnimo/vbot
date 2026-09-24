"""Tests for the durable Provider usage history in ``provider-usage.db``."""

from __future__ import annotations

import copy
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from core.database import read_marker, write_bootstrap_marker
from core.providers.usage_history import (
    DATABASE_NAME,
    ProviderUsageHistoryStore,
    UsageHistoryError,
    usage_history_sample,
)


def _window(**overrides: Any) -> dict[str, Any]:
    return {
        "label": "5h",
        "used_percent": 25.0,
        "reset_at": "2026-07-01T05:00:00+00:00",
        "window_seconds": 18_000,
        "used_units": None,
        "remaining_units": None,
        "total_units": None,
        "unit": None,
        "unlimited": None,
        **overrides,
    }


def _snapshot(**overrides: Any) -> dict[str, Any]:
    return {
        "connection": "openai:subscription",
        "account": "default",
        "display_name": "OpenAI",
        "plan": "pro",
        "windows": [_window()],
        "credits": {"enabled": True, "balance": 12.0},
        "error": None,
        **overrides,
    }


@pytest.fixture
def store(tmp_path: Path) -> Iterator[ProviderUsageHistoryStore]:
    write_bootstrap_marker(tmp_path)
    history = ProviderUsageHistoryStore.open(tmp_path)
    try:
        yield history
    finally:
        history.close()


def _row_counts(store: ProviderUsageHistoryStore) -> tuple[int, int, int]:
    with store.database.read() as connection:
        return tuple(  # type: ignore[return-value]
            int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in ("usage_samples", "usage_snapshots", "usage_windows")
        )


def test_the_history_is_a_registered_canonical_database(tmp_path: Path) -> None:
    write_bootstrap_marker(tmp_path)

    history = ProviderUsageHistoryStore.open(tmp_path)
    try:
        marker = read_marker(tmp_path)
        assert history.database.path == tmp_path / "provider-usage.db"
        assert marker is not None
        assert marker.databases[DATABASE_NAME].database_id == history.database.database_id
    finally:
        history.close()


@pytest.mark.asyncio
async def test_samples_read_an_inclusive_window_oldest_first(
    store: ProviderUsageHistoryStore,
) -> None:
    for sampled_at in (
        "2026-07-01T01:00:00+00:00",
        "2026-06-30T23:00:00+00:00",
        "2026-07-31T23:59:59.999999+00:00",
        "2026-08-01T00:00:00+00:00",
        "2026-07-01T00:00:00+00:00",
    ):
        assert await store.append(sampled_at, [_snapshot()]) is True

    window = await store.samples(
        since=datetime(2026, 7, 1, tzinfo=UTC),
        until=datetime(2026, 7, 31, 23, 59, 59, 999_999, tzinfo=UTC),
    )
    since_only = await store.samples(since=datetime(2026, 7, 31, tzinfo=UTC))
    until_only = await store.samples(until=datetime(2026, 6, 30, 23, tzinfo=UTC))
    everything = await store.samples()

    assert [sample.sampled_at for sample in window] == [
        "2026-07-01T00:00:00.000000Z",
        "2026-07-01T01:00:00.000000Z",
        "2026-07-31T23:59:59.999999Z",
    ]
    assert [sample.sampled_at for sample in since_only] == [
        "2026-07-31T23:59:59.999999Z",
        "2026-08-01T00:00:00.000000Z",
    ]
    assert [sample.sampled_at for sample in until_only] == ["2026-06-30T23:00:00.000000Z"]
    assert [sample.sampled_at for sample in everything] == sorted(
        sample.sampled_at for sample in everything
    )
    assert len(everything) == 5


@pytest.mark.asyncio
async def test_samples_reassemble_the_public_snapshot_projection(
    store: ProviderUsageHistoryStore,
) -> None:
    complete = _snapshot(
        windows=[
            _window(label="5h", unlimited=False, used_units=3, total_units=10, unit="requests"),
            _window(
                label="Week",
                used_percent=40.5,
                reset_at=None,
                window_seconds=None,
                remaining_units=7.5,
                unlimited=True,
            ),
        ]
    )
    credits_only = _snapshot(
        connection="openrouter:api-key",
        display_name="OpenRouter",
        plan=None,
        windows=[],
        credits={"enabled": True, "balance": None},
    )
    failed = _snapshot(
        connection="github-copilot:oauth",
        account="work",
        display_name="GitHub Copilot",
        plan=None,
        windows=[],
        credits=None,
        error="Timeout",
    )

    await store.append("2026-07-01T01:00:00+00:00", [complete, credits_only, failed])
    [sample] = await store.samples()

    assert sample.to_dict() == {
        "sampled_at": "2026-07-01T01:00:00.000000Z",
        "providers": [
            {
                **complete,
                "windows": [
                    {
                        **complete["windows"][0],
                        "reset_at": "2026-07-01T05:00:00.000000Z",
                        "used_units": 3.0,
                        "total_units": 10.0,
                    },
                    complete["windows"][1],
                ],
            },
            credits_only,
            failed,
        ],
    }


@pytest.mark.asyncio
async def test_append_normalizes_timestamps_and_clamps_percentages(
    store: ProviderUsageHistoryStore,
) -> None:
    offset = timezone(timedelta(hours=2))
    await store.append(
        datetime(2026, 7, 1, 3, 0, tzinfo=offset).isoformat(),
        [
            _snapshot(
                windows=[
                    _window(used_percent=150, reset_at="2026-07-01T07:00:00Z"),
                    _window(label="Week", used_percent=-5.0, reset_at="2026-07-02T10:00:00+05:30"),
                ]
            )
        ],
    )

    [sample] = await store.samples()
    windows = sample.providers[0]["windows"]

    assert sample.sampled_at == "2026-07-01T01:00:00.000000Z"
    assert [window["used_percent"] for window in windows] == [100.0, 0.0]
    assert [window["reset_at"] for window in windows] == [
        "2026-07-01T07:00:00.000000Z",
        "2026-07-02T04:30:00.000000Z",
    ]


@pytest.mark.parametrize(
    ("sampled_at", "snapshot"),
    [
        ("2026-07-01T01:00:00+00:00", _snapshot(windows=[_window(used_percent=float("nan"))])),
        ("2026-07-01T01:00:00+00:00", _snapshot(windows=[_window(used_units=float("inf"))])),
        ("2026-07-01T01:00:00+00:00", _snapshot(windows=[_window(used_percent=True)])),
        ("2026-07-01T01:00:00+00:00", _snapshot(windows=[_window(window_seconds=0)])),
        ("2026-07-01T01:00:00+00:00", _snapshot(windows=[_window(reset_at="2026-07-01")])),
        ("2026-07-01T01:00:00+00:00", _snapshot(windows=[_window(unlimited="yes")])),
        ("2026-07-01T01:00:00+00:00", _snapshot(windows=[{**_window(), "extra": 1}])),
        ("2026-07-01T01:00:00+00:00", _snapshot(credits={"enabled": 1, "balance": None})),
        ("2026-07-01T01:00:00+00:00", _snapshot(account="")),
        ("2026-07-01T01:00:00+00:00", {**_snapshot(), "raw": {}}),
        ("2026-07-01T01:00:00", _snapshot()),
        ("not-a-date", _snapshot()),
    ],
)
@pytest.mark.asyncio
async def test_append_rejects_an_invalid_sample_and_writes_nothing(
    store: ProviderUsageHistoryStore, sampled_at: str, snapshot: dict[str, Any]
) -> None:
    with pytest.raises(UsageHistoryError):
        await store.append(sampled_at, [_snapshot(), copy.deepcopy(snapshot)])

    assert _row_counts(store) == (0, 0, 0)


@pytest.mark.asyncio
async def test_an_empty_report_is_not_a_sample(store: ProviderUsageHistoryStore) -> None:
    assert await store.append("2026-07-01T01:00:00+00:00", []) is False
    assert await store.samples() == []


def test_sample_validation_needs_at_least_one_provider() -> None:
    with pytest.raises(UsageHistoryError):
        usage_history_sample("2026-07-01T01:00:00+00:00", [])


@pytest.mark.asyncio
async def test_latest_sampled_at_is_the_newest_sample(store: ProviderUsageHistoryStore) -> None:
    assert await store.latest_sampled_at() is None

    await store.append("2026-07-01T02:00:00+00:00", [_snapshot()])
    await store.append("2026-07-01T01:00:00+00:00", [_snapshot()])

    assert await store.latest_sampled_at() == datetime(2026, 7, 1, 2, tzinfo=UTC)


@pytest.mark.asyncio
async def test_clear_deletes_every_sample_and_reports_the_counts(
    store: ProviderUsageHistoryStore,
) -> None:
    await store.append("2026-06-30T23:00:00+00:00", [_snapshot(), _snapshot(account="work")])
    await store.append("2026-07-01T01:00:00+00:00", [_snapshot()])

    cleared = await store.clear()
    cleared_again = await store.clear()

    assert cleared.to_dict() == {"deleted_samples": 2, "deleted_files": 1}
    assert cleared_again.to_dict() == {"deleted_samples": 0, "deleted_files": 0}
    assert _row_counts(store) == (0, 0, 0)
    assert await store.samples() == []
    assert await store.latest_sampled_at() is None


@pytest.mark.asyncio
async def test_a_failed_clear_deletes_nothing(store: ProviderUsageHistoryStore) -> None:
    await store.append("2026-07-01T01:00:00+00:00", [_snapshot()])
    # The sample rows are deleted last; failing there must roll back the
    # already-deleted snapshot and window rows as well.
    store.database.writer.execute(
        "CREATE TEMP TRIGGER refuse_sample_delete BEFORE DELETE ON main.usage_samples "
        "BEGIN SELECT RAISE(ABORT, 'refused'); END"
    )

    with pytest.raises(sqlite3.IntegrityError):
        await store.clear()

    assert _row_counts(store) == (1, 1, 1)


def test_import_samples_writes_normalized_samples_in_one_call(
    store: ProviderUsageHistoryStore,
) -> None:
    samples = [
        usage_history_sample("2026-07-01T01:00:00+00:00", [_snapshot()]),
        usage_history_sample("2026-07-01T02:00:00Z", [_snapshot(), _snapshot(account="work")]),
    ]

    assert store.import_samples(samples) == 2
    assert _row_counts(store) == (2, 3, 3)


@pytest.mark.parametrize(
    "query",
    [
        # ProviderUsageHistoryStore.samples: the inclusive time range, oldest first.
        "SELECT s.sample_key, s.sampled_at FROM usage_samples AS s "
        "WHERE s.sampled_at >= ? AND s.sampled_at <= ? ORDER BY s.sampled_at, s.sample_key",
        # ProviderUsageHistoryStore.latest_sampled_at.
        "SELECT MAX(sampled_at) FROM usage_samples",
    ],
)
def test_time_reads_use_the_sampled_at_index(store: ProviderUsageHistoryStore, query: str) -> None:
    parameters = ("2026-07-01T00:00:00.000000Z",) * query.count("?")
    with store.database.read() as connection:
        plan = " ".join(
            str(row[-1]) for row in connection.execute(f"EXPLAIN QUERY PLAN {query}", parameters)
        )

    assert "usage_samples_by_time" in plan
