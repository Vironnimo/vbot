"""Tests for the Generation 1 conversion of the Provider usage JSONL history."""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
from typing import Any

import pytest

from core.database import open_offline_database
from core.providers.usage_history import (
    ProviderUsageHistoryStore,
    UsageHistorySample,
    provider_usage_database_spec,
)
from scripts.converters.persistence_generation_1._context import ConversionContext
from scripts.converters.persistence_generation_1.provider_usage import AREA, convert


def _snapshot(**overrides: Any) -> dict[str, Any]:
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
        **overrides,
    }


def _row(sampled_at: str, *providers: dict[str, Any], **overrides: Any) -> str:
    row = {
        "schema_version": 1,
        "sampled_at": sampled_at,
        "providers": list(providers) or [_snapshot()],
        **overrides,
    }
    return json.dumps(row, separators=(",", ":"))


def _write_history(source: Path, name: str, lines: list[str]) -> None:
    directory = source / "statistics" / "provider-usage"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")


def _context(tmp_path: Path) -> ConversionContext:
    source = tmp_path / "data"
    source.mkdir(exist_ok=True)
    return ConversionContext(source=source, staging=tmp_path / "staging")


def _staged_samples(context: ConversionContext) -> list[UsageHistorySample]:
    path = context.staging / "provider-usage.db"
    store = ProviderUsageHistoryStore(open_offline_database(provider_usage_database_spec(path)))
    try:
        with store.database.read() as connection:
            rows = connection.execute(
                "SELECT sample_key, sampled_at FROM usage_samples ORDER BY sample_key"
            ).fetchall()
        assert [sampled_at for _key, sampled_at in rows] == sorted(
            sampled_at for _key, sampled_at in rows
        )
        return store._read_samples(None, None)  # noqa: SLF001
    finally:
        store.close()


def test_converts_every_valid_row_oldest_first(tmp_path: Path) -> None:
    context = _context(tmp_path)
    _write_history(
        context.source,
        "2026-07.jsonl",
        [
            _row("2026-07-01T02:00:00+00:00"),
            _row("2026-07-01T01:00:00+00:00", _snapshot(), _snapshot(account="work")),
        ],
    )
    _write_history(
        context.source,
        "2026-06.jsonl",
        [_row("2026-06-30T23:00:00+00:00", _snapshot(windows=[], credits=None, error="HTTP 500"))],
    )

    convert(context)
    samples = _staged_samples(context)

    assert [sample.sampled_at for sample in samples] == [
        "2026-06-30T23:00:00.000000Z",
        "2026-07-01T01:00:00.000000Z",
        "2026-07-01T02:00:00.000000Z",
    ]
    assert samples[0].providers[0]["error"] == "HTTP 500"
    assert [snapshot["account"] for snapshot in samples[1].providers] == ["default", "work"]
    assert samples[2].providers[0]["windows"][0]["reset_at"] == "2026-07-01T05:00:00.000000Z"
    assert context.report.counts[AREA] == {"files": 2, "samples": 3}
    assert context.report.skipped == []
    assert context.retired == [
        PurePosixPath("statistics/provider-usage/2026-06.jsonl"),
        PurePosixPath("statistics/provider-usage/2026-07.jsonl"),
    ]


@pytest.mark.parametrize(
    "line",
    [
        "{not json",
        "[]",
        _row("2026-07-01T01:00:00+00:00", schema_version=2),
        _row("2026-07-01T01:00:00+00:00", extra=True),
        _row("2026-07-01T01:00:00"),
        _row("not-a-date"),
        json.dumps({"schema_version": 1, "sampled_at": "2026-07-01T01:00:00Z", "providers": []}),
        _row("2026-07-01T01:00:00+00:00", _snapshot(windows=[{"label": "5h"}])),
        _row("2026-07-01T01:00:00+00:00", {**_snapshot(), "raw": {}}),
    ],
)
def test_invalid_rows_are_skipped_and_reported(tmp_path: Path, line: str) -> None:
    context = _context(tmp_path)
    _write_history(
        context.source,
        "2026-07.jsonl",
        [_row("2026-07-01T00:00:00+00:00"), line, "", _row("2026-07-01T02:00:00+00:00")],
    )

    convert(context)

    assert len(_staged_samples(context)) == 2
    assert context.report.counts[AREA] == {"files": 1, "invalid_rows": 1, "samples": 2}
    assert [(item.area, item.item) for item in context.report.skipped] == [
        (AREA, "statistics/provider-usage/2026-07.jsonl:2")
    ]


def test_an_unreadable_file_is_reported_and_retired(tmp_path: Path) -> None:
    context = _context(tmp_path)
    _write_history(context.source, "2026-07.jsonl", [_row("2026-07-01T00:00:00+00:00")])
    broken = context.source / "statistics" / "provider-usage" / "2026-06.jsonl"
    broken.write_bytes(b"\xff\xfe not utf-8\n")

    convert(context)

    assert len(_staged_samples(context)) == 1
    assert [item.item for item in context.report.skipped] == [
        "statistics/provider-usage/2026-06.jsonl"
    ]
    assert PurePosixPath("statistics/provider-usage/2026-06.jsonl") in context.retired


def test_a_missing_history_stages_an_empty_database(tmp_path: Path) -> None:
    context = _context(tmp_path)

    convert(context)

    assert _staged_samples(context) == []
    assert context.retired == []
    assert context.report.counts[AREA] == {"samples": 0}


def test_a_repeated_run_replaces_its_staged_database(tmp_path: Path) -> None:
    context = _context(tmp_path)
    _write_history(context.source, "2026-07.jsonl", [_row("2026-07-01T00:00:00+00:00")])

    convert(context)
    convert(context)

    assert len(_staged_samples(context)) == 1


def test_the_source_files_are_never_modified(tmp_path: Path) -> None:
    context = _context(tmp_path)
    _write_history(context.source, "2026-07.jsonl", [_row("2026-07-01T00:00:00+00:00"), "{bad"])
    source_file = context.source / "statistics" / "provider-usage" / "2026-07.jsonl"
    before = source_file.read_bytes()

    convert(context)

    assert source_file.read_bytes() == before
