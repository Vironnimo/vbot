"""Tests for durable normalized Provider subscription-usage history."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from core.providers.usage_history import ProviderUsageHistoryStore, UsageHistoryError
from core.storage.layout import DataDirectoryLayout


def _snapshot() -> dict[str, Any]:
    return {
        "connection": "openai:subscription",
        "account": "default",
        "display_name": "OpenAI",
        "plan": "pro",
        "windows": [
            {
                "label": "5h",
                "used_percent": 25.0,
                "reset_at": "2026-07-01T05:00:00+00:00",
                "window_seconds": 18_000,
                "used_units": None,
                "remaining_units": None,
                "total_units": None,
                "unit": None,
                "unlimited": None,
            }
        ],
        "credits": {"enabled": True, "balance": 12.0},
        "error": None,
    }


def test_history_store_filters_monthly_files_and_clears_explicitly(tmp_path: Any) -> None:
    store = ProviderUsageHistoryStore(tmp_path)
    assert store.directory == DataDirectoryLayout(tmp_path).provider_usage
    june_at = "2026-06-30T23:00:00+00:00"
    july_at = "2026-07-01T01:00:00+00:00"

    assert store.append(june_at, [_snapshot()]) is True
    assert store.append(july_at, [_snapshot()]) is True
    report = store.report(
        since=datetime(2026, 7, 1, tzinfo=UTC),
        until=datetime(2026, 7, 31, tzinfo=UTC),
    )
    cleared = store.clear()

    assert [sample.sampled_at for sample in report.samples] == [july_at]
    assert report.samples[0].providers[0]["credits"] == {
        "enabled": True,
        "balance": 12.0,
    }
    assert cleared.deleted_samples == 2
    assert cleared.deleted_files == 2
    assert store.report().samples == []


def test_history_store_skips_invalid_rows_without_losing_valid_samples(tmp_path: Any) -> None:
    store = ProviderUsageHistoryStore(tmp_path)
    sampled_at = "2026-07-01T01:00:00+00:00"
    assert store.append(sampled_at, [_snapshot()]) is True
    history_path = store.directory / "2026-07.jsonl"
    with history_path.open("a", encoding="utf-8") as handle:
        handle.write('{"schema_version":1,"sampled_at":"not-a-date","providers":[]}\n')

    report = store.report()

    assert [sample.sampled_at for sample in report.samples] == [sampled_at]


def test_history_store_rejects_non_finite_provider_numbers(tmp_path: Any) -> None:
    store = ProviderUsageHistoryStore(tmp_path)
    snapshot = _snapshot()
    snapshot["windows"][0]["used_percent"] = float("nan")

    with pytest.raises(UsageHistoryError):
        store.append("2026-07-01T01:00:00+00:00", [snapshot])
