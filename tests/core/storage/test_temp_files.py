"""Tests for shared temporary-file allocation and retention."""

from __future__ import annotations

import asyncio
import os
import time
from datetime import timedelta
from pathlib import Path

import pytest

from core.storage import DataDirectoryLayout, TemporaryFileManager


def _age(path: Path, *, seconds: float) -> None:
    timestamp = time.time() - seconds
    os.utime(path, (timestamp, timestamp))


def test_create_allocates_unique_category_confined_files(tmp_path: Path) -> None:
    manager = TemporaryFileManager(tmp_path)

    first = manager.create("bash", ".log")
    second = manager.create("bash", ".log")

    assert first.path != second.path
    assert first.path.parent == DataDirectoryLayout(tmp_path).bash_temporary
    assert first.path.is_file()
    assert second.path.is_file()


def test_terminal_files_use_their_canonical_retained_category(tmp_path: Path) -> None:
    manager = TemporaryFileManager(tmp_path)

    lease = manager.create("terminals", ".events.jsonl")

    assert lease.path.parent == DataDirectoryLayout(tmp_path).terminal_temporary


@pytest.mark.parametrize(
    ("category", "suffix"),
    [
        ("unknown", ".log"),
        ("atomic", ".tmp"),
        ("bash", "log"),
        ("bash", "../escape"),
    ],
)
def test_create_rejects_unknown_categories_and_unsafe_suffixes(
    tmp_path: Path,
    category: str,
    suffix: str,
) -> None:
    manager = TemporaryFileManager(tmp_path)

    with pytest.raises(ValueError):
        manager.create(category, suffix)


def test_constructor_rejects_category_path_traversal(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        TemporaryFileManager(tmp_path, retention={"..": timedelta(hours=1)})


def test_sweep_applies_category_retention_and_spares_active_files(tmp_path: Path) -> None:
    manager = TemporaryFileManager(tmp_path)
    active = manager.create("subagents", ".md")
    expired_subagent = manager.create("subagents", ".md")
    retained_bash = manager.create("bash", ".log")
    expired_bash = manager.create("bash", ".log")
    expired_subagent.finish()
    retained_bash.finish()
    expired_bash.finish()
    _age(active.path, seconds=96 * 60 * 60)
    _age(expired_subagent.path, seconds=25 * 60 * 60)
    _age(retained_bash.path, seconds=25 * 60 * 60)
    _age(expired_bash.path, seconds=73 * 60 * 60)

    manager.sweep()

    assert active.path.exists()
    assert not expired_subagent.path.exists()
    assert retained_bash.path.exists()
    assert not expired_bash.path.exists()


def test_finish_is_idempotent_and_restarts_retention_clock(tmp_path: Path) -> None:
    manager = TemporaryFileManager(
        tmp_path,
        retention={"subagents": timedelta(seconds=1)},
    )
    lease = manager.create("subagents", ".md")
    _age(lease.path, seconds=60)

    lease.finish()
    first_finished_mtime = lease.path.stat().st_mtime
    lease.finish()
    manager.sweep()

    assert lease.path.exists()
    assert lease.path.stat().st_mtime == first_finished_mtime


def test_start_sweeps_expired_crash_leftovers(tmp_path: Path) -> None:
    stale_dir = DataDirectoryLayout(tmp_path).subagent_temporary
    stale_dir.mkdir(parents=True)
    stale = stale_dir / "crash-leftover.md"
    stale.write_text("partial", encoding="utf-8")
    _age(stale, seconds=60)
    manager = TemporaryFileManager(
        tmp_path,
        retention={"subagents": timedelta(seconds=1)},
    )

    manager.start()

    assert not stale.exists()


@pytest.mark.asyncio
async def test_periodic_sweep_and_async_close(tmp_path: Path) -> None:
    manager = TemporaryFileManager(
        tmp_path,
        retention={"subagents": timedelta(milliseconds=1)},
        sweep_interval_seconds=0.01,
    )
    lease = manager.create("subagents", ".md")
    lease.finish()
    _age(lease.path, seconds=1)

    manager.start()
    for _ in range(20):
        if not lease.path.exists():
            break
        await asyncio.sleep(0.01)
    await manager.aclose()

    assert not lease.path.exists()
    assert manager._sweeper_task is None


def test_one_unlink_failure_does_not_block_other_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = TemporaryFileManager(
        tmp_path,
        retention={"subagents": timedelta(seconds=1)},
    )
    blocked = manager.create("subagents", ".md")
    removable = manager.create("subagents", ".md")
    blocked.finish()
    removable.finish()
    _age(blocked.path, seconds=60)
    _age(removable.path, seconds=60)
    original_unlink = Path.unlink

    def selective_unlink(path: Path, missing_ok: bool = False) -> None:
        if path == blocked.path:
            raise OSError("busy")
        original_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", selective_unlink)

    manager.sweep()

    assert blocked.path.exists()
    assert not removable.path.exists()
