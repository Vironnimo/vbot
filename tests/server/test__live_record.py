"""Local Live call records: one JSON line per record, one file per UTC day, bounded retention."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from server._live_record import LiveCallRecorder


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 9, 25, 23, 59, 30, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


def lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


@pytest.mark.asyncio
async def test_appends_records_in_order_to_the_file_of_their_utc_day(tmp_path: Path) -> None:
    clock = Clock()
    recorder = LiveCallRecorder(tmp_path / "live-calls", clock=clock)
    recorder.record("call-1", {"type": "tool", "tool": "overview", "result": "Agents: Ä."})
    recorder.record("call-1", {"type": "delegation", "request": "was läuft?"})
    clock.now += timedelta(minutes=1)
    recorder.record("call-1", {"type": "tool", "tool": "read", "arguments": {"at": clock.now}})
    await recorder.drain()

    first = tmp_path / "live-calls" / "2026-09-25.jsonl"
    assert lines(first) == [
        {
            "at": "2026-09-25T23:59:30+00:00",
            "call_id": "call-1",
            "type": "tool",
            "tool": "overview",
            "result": "Agents: Ä.",
        },
        {
            "at": "2026-09-25T23:59:30+00:00",
            "call_id": "call-1",
            "type": "delegation",
            "request": "was läuft?",
        },
    ]
    assert b"\r\n" not in first.read_bytes()
    # Values JSON cannot hold are kept as text.
    assert lines(tmp_path / "live-calls" / "2026-09-26.jsonl")[0]["arguments"] == {
        "at": "2026-09-26 00:00:30+00:00"
    }


@pytest.mark.asyncio
async def test_deletes_days_older_than_the_retention_window(tmp_path: Path) -> None:
    directory = tmp_path / "live-calls"
    directory.mkdir()
    for name in ("2026-08-25.jsonl", "2026-08-26.jsonl", "notes.jsonl", "2026-08-01.txt"):
        (directory / name).write_bytes(b"{}\n")
    recorder = LiveCallRecorder(directory, retention_days=30, clock=Clock())
    recorder.record("call-1", {"type": "tool"})
    await recorder.drain()

    assert sorted(path.name for path in directory.iterdir()) == [
        "2026-08-01.txt",
        "2026-08-26.jsonl",
        "2026-09-25.jsonl",
        "notes.jsonl",
    ]


@pytest.mark.asyncio
async def test_a_failing_write_is_logged_without_content_and_never_raises(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    blocked = tmp_path / "live-calls"
    blocked.write_bytes(b"not a directory")
    recorder = LiveCallRecorder(blocked, clock=Clock())
    with caplog.at_level(logging.WARNING):
        recorder.record("call-1", {"type": "delegation", "request": "secret task"})
        await recorder.drain()

    assert "Live call record failed" in caplog.text
    assert "secret task" not in caplog.text
