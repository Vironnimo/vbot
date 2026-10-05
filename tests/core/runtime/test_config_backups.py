"""The Runtime's configuration backup schedule: start, changes, contention and stop."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from core.database import SnapshotBarrier, list_config_backups, write_bootstrap_marker
from core.database import config_backups as config_backups_module
from core.database.marker import acquire_operation_lock
from core.runtime import _config_backups as schedule_module
from core.runtime._config_backups import ConfigBackupSchedule


def _write_settings(data_dir: Path, timezone: str) -> None:
    payload = json.dumps({"format_version": 1, "timezone": timezone}) + "\n"
    data_dir.joinpath("settings.json").write_text(payload, encoding="utf-8")


def _reasons(data_dir: Path) -> list[str]:
    return [backup.reason for backup in list_config_backups(data_dir)]


@pytest.mark.asyncio
async def test_the_schedule_backs_up_at_start_after_changes_and_at_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = tmp_path / "config-data"
    data_dir.mkdir()
    write_bootstrap_marker(data_dir)
    _write_settings(data_dir, "UTC")
    ticks: asyncio.Queue[None] = asyncio.Queue()
    sleeping = asyncio.Event()

    async def controlled_sleep(_seconds: float) -> None:
        sleeping.set()
        await ticks.get()

    async def next_check() -> None:
        sleeping.clear()
        ticks.put_nowait(None)
        await asyncio.wait_for(sleeping.wait(), 10)

    monkeypatch.setattr(schedule_module, "_sleep", controlled_sleep)
    monkeypatch.setattr(config_backups_module, "CONFIG_BACKUP_LOCK_SECONDS", 0.05)
    schedule = ConfigBackupSchedule(data_dir, SnapshotBarrier())
    schedule.start()
    try:
        await asyncio.wait_for(sleeping.wait(), 10)
        assert _reasons(data_dir) == ["start"]

        await next_check()
        assert _reasons(data_dir) == ["start"]

        _write_settings(data_dir, "Europe/Berlin")
        await next_check()
        assert _reasons(data_dir) == ["change", "start"]

        # While a data snapshot holds the operation lock the change waits for the next check.
        _write_settings(data_dir, "America/New_York")
        lock = acquire_operation_lock(data_dir)
        assert lock is not None
        try:
            await next_check()
        finally:
            lock.release()
        assert _reasons(data_dir) == ["change", "start"]
        await next_check()
        assert _reasons(data_dir) == ["change", "change", "start"]

        _write_settings(data_dir, "Asia/Tokyo")
    finally:
        await schedule.aclose()

    assert _reasons(data_dir) == ["stop", "change", "change", "start"]
