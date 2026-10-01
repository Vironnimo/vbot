"""The retention sweep: which entries it purges, when it runs and how it stops.

The sweep timer is replaced (``_retention._wait``) and the clock is fixed, so
no test waits for real time: a test ends a timer wait with ``elapse()`` and
knows a sweep finished when the next wait begins.
"""

from __future__ import annotations

import asyncio
import errno
import logging
import sqlite3
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from core.archive import ArchiveService, PendingPurge, _purge, _retention
from core.sessions import ARCHIVE_KIND_FILES, ARCHIVE_TREE_FILES, ArchiveTree
from core.utils.timestamps import format_canonical_timestamp
from tests.core.archive.archive_test_support import ArchiveWorld, legacy_agent_entry
from tests.core.archive.archive_test_support import world as world

_NOW = datetime(2026, 10, 1, 12, tzinfo=UTC)


class _Timer:
    """The sweep timer: records each wait; ``elapse`` ends the current one."""

    def __init__(self) -> None:
        self._waits: asyncio.Queue[float] = asyncio.Queue()
        self._elapsed = asyncio.Event()

    async def wait(self, wake: asyncio.Event, seconds: float) -> None:
        self._elapsed = asyncio.Event()
        self._waits.put_nowait(seconds)
        waiters = {
            asyncio.ensure_future(self._elapsed.wait()),
            asyncio.ensure_future(wake.wait()),
        }
        try:
            await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for waiter in waiters:
                waiter.cancel()

    async def next_wait(self) -> float:
        """The length of the next timer wait, once the sweep reached it."""
        return await asyncio.wait_for(self._waits.get(), timeout=10)

    def elapse(self) -> None:
        self._elapsed.set()


@pytest.fixture
def timer(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Timer]:
    fake = _Timer()
    monkeypatch.setattr(_retention, "_wait", fake.wait)
    yield fake


class _Setting:
    """``archive.retention_days`` as the running service reads it."""

    def __init__(self, days: int | None) -> None:
        self.days = days

    def __call__(self) -> int | None:
        return self.days


def _service(world: ArchiveWorld, setting: _Setting) -> ArchiveService:
    service = ArchiveService(replace(world.services, retention_days=setting, clock=lambda: _NOW))
    service.add_changed_callback(lambda: world.changes.append(None))
    return service


def _rest(world: ArchiveWorld, entry_id: str, days: float) -> None:
    """Start the entry's retention clock ``days`` before now."""
    start = format_canonical_timestamp(_NOW - timedelta(days=days))
    with sqlite3.connect(world.data_dir / "sessions.db") as connection:
        connection.execute(
            "UPDATE archive_entries SET retention_start = ? WHERE entry_id = ?", (start, entry_id)
        )


async def _archived_agent(world: ArchiveWorld, agent_id: str, *, days: float) -> str:
    world.agents.create(agent_id)
    entry_id = (await world.service.archive_agent(agent_id)).entry_id
    _rest(world, entry_id, days)
    return entry_id


async def _sweep_once(service: ArchiveService, timer: _Timer) -> None:
    """Start the service, let the first sweep run and close the service."""
    service.start()
    assert await timer.next_wait() == _retention.FIRST_SWEEP_DELAY_SECONDS
    timer.elapse()
    assert await timer.next_wait() == _retention.SWEEP_INTERVAL_SECONDS
    await service.aclose()


def _purged(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("Archive entry purged")
    ]


@pytest.mark.asyncio
async def test_a_sweep_purges_due_entries_oldest_first_and_keeps_every_other_entry(
    world: ArchiveWorld, timer: _Timer, caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    due = await _archived_agent(world, "due", days=31)
    oldest = await _archived_agent(world, "oldest", days=45)
    fresh = await _archived_agent(world, "fresh", days=29)
    # Retention never deletes folders the user may own or other files; only a manual purge does.
    legacy = legacy_agent_entry(world, tmp_path / "notes")
    (world.data_dir / "archive" / "loose").mkdir(parents=True)
    files = world.sessions.archive_ledger.adopt_payload(
        "arc_files",
        ARCHIVE_KIND_FILES,
        subject_id="loose",
        archived_at=format_canonical_timestamp(_NOW),
        trees=(ArchiveTree("archive/loose", ARCHIVE_TREE_FILES),),
    ).entry_id
    for entry_id in (legacy, files):
        _rest(world, entry_id, 400)
    caplog.set_level(logging.INFO, logger="vbot.archive")

    await _sweep_once(_service(world, _Setting(30)), timer)

    ledger = world.sessions.archive_ledger
    assert {
        entry_id for entry_id in (due, oldest, fresh, legacy, files) if ledger.entry(entry_id)
    } == {
        fresh,
        legacy,
        files,
    }
    assert world.session_rows("due") == world.session_rows("oldest") == []
    assert _purged(caplog) == [
        f"Archive entry purged (entry={entry_id} kind=agent subject={subject} sessions=1 "
        "reason=retention actor=retention)"
        for entry_id, subject in ((oldest, "oldest"), (due, "due"))
    ]
    assert world.changes
    assert not [record for record in caplog.records if record.levelno >= logging.WARNING]


@pytest.mark.asyncio
async def test_a_changed_retention_period_applies_at_once(
    world: ArchiveWorld, timer: _Timer
) -> None:
    entry_id = await _archived_agent(world, "coder", days=10)
    setting = _Setting(30)
    service = _service(world, setting)
    service.start()
    assert await timer.next_wait() == _retention.FIRST_SWEEP_DELAY_SECONDS
    timer.elapse()
    assert await timer.next_wait() == _retention.SWEEP_INTERVAL_SECONDS
    assert world.sessions.archive_ledger.entry(entry_id) is not None

    # A lowered period makes the entry due; the change wakes the sweep without the timer.
    setting.days = 7
    service.retention_changed()
    assert await timer.next_wait() == _retention.SWEEP_INTERVAL_SECONDS
    await service.aclose()

    assert world.sessions.archive_ledger.entry(entry_id) is None


@pytest.mark.asyncio
async def test_a_manual_purge_left_pending_is_continued_at_once(
    world: ArchiveWorld, timer: _Timer, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry_id = await _archived_agent(world, "coder", days=0)
    service = _service(world, _Setting(30))
    service.start()
    assert await timer.next_wait() == _retention.FIRST_SWEEP_DELAY_SECONDS
    failures = iter([PermissionError(errno.EACCES, "held open by another program")])
    remove_tree = _purge.remove_tree

    def remove_once_held(path: Path, *, within: Path) -> None:
        if error := next(failures, None):
            raise error
        remove_tree(path, within=within)

    monkeypatch.setattr(_purge, "remove_tree", remove_once_held)

    outcome = await service.purge([entry_id])

    assert outcome.pending == (PendingPurge(entry_id, "PermissionError"),)
    # The pending purge wakes the sweep before its timer ends.
    assert await timer.next_wait() == _retention.SWEEP_INTERVAL_SECONDS
    await service.aclose()
    assert world.sessions.archive_ledger.entry(entry_id) is None


@pytest.mark.asyncio
async def test_without_a_retention_period_only_interrupted_purges_continue(
    world: ArchiveWorld, timer: _Timer, caplog: pytest.LogCaptureFixture
) -> None:
    kept = await _archived_agent(world, "kept", days=4000)
    interrupted = await _archived_agent(world, "interrupted", days=0)
    world.sessions.archive_ledger.begin_purge(interrupted)
    caplog.set_level(logging.INFO, logger="vbot.archive")

    await _sweep_once(_service(world, _Setting(None)), timer)

    assert world.sessions.archive_ledger.entry(kept) is not None
    assert world.sessions.archive_ledger.entry(interrupted) is None
    assert _purged(caplog) == [
        f"Archive entry purged (entry={interrupted} kind=agent subject=interrupted sessions=1 "
        "reason=resumed actor=retention)"
    ]


@pytest.mark.asyncio
async def test_stopping_ends_a_purge_between_sessions_and_the_next_start_finishes_it(
    world: ArchiveWorld, timer: _Timer
) -> None:
    world.agents.create("coder")
    for session_id in ("two", "three"):
        world.sessions.create("coder", session_id=session_id)
    entry_id = (await world.service.archive_agent("coder")).entry_id
    _rest(world, entry_id, 31)
    service = _service(world, _Setting(30))
    ledger = world.sessions.archive_ledger
    purge_next_session = ledger.purge_next_session
    loop = asyncio.get_running_loop()
    stopped = asyncio.Event()

    async def stop() -> None:
        service.stop()
        stopped.set()

    def purge_one_then_stop(entry_key: int) -> bool:
        # Runtime shutdown arrives while the first Session is deleted.
        deleted = purge_next_session(entry_key)
        asyncio.run_coroutine_threadsafe(stop(), loop).result(timeout=10)
        return deleted

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(ledger, "purge_next_session", purge_one_then_stop)
        service.start()
        assert await timer.next_wait() == _retention.FIRST_SWEEP_DELAY_SECONDS
        timer.elapse()
        await asyncio.wait_for(stopped.wait(), timeout=10)
        await service.aclose()

    assert world.entry(entry_id).state == "purging"
    assert len(world.session_rows("coder")) == 2
    assert world.payload(entry_id, "agent").is_dir()

    await _sweep_once(_service(world, _Setting(30)), timer)

    assert ledger.entry(entry_id) is None
    assert world.session_rows("coder") == []
    assert not world.payload(entry_id, "agent").exists()


@pytest.mark.asyncio
async def test_a_failing_purge_warns_once_until_it_succeeds(
    world: ArchiveWorld, timer: _Timer, caplog: pytest.LogCaptureFixture
) -> None:
    entry_id = await _archived_agent(world, "coder", days=31)
    failures = iter([OSError("usage ledger unavailable")] * 2)

    def import_usage() -> None:
        if error := next(failures, None):
            raise error

    caplog.set_level(logging.INFO, logger="vbot.archive")
    service = ArchiveService(
        replace(
            world.services,
            import_usage=import_usage,
            retention_days=_Setting(30),
            clock=lambda: _NOW,
        )
    )
    service.start()
    assert await timer.next_wait() == _retention.FIRST_SWEEP_DELAY_SECONDS
    for _attempt in range(3):
        timer.elapse()
        assert await timer.next_wait() == _retention.SWEEP_INTERVAL_SECONDS
    await service.aclose()

    assert world.sessions.archive_ledger.entry(entry_id) is None
    assert [
        (record.levelname, record.getMessage().split(";")[0])
        for record in caplog.records
        if record.levelno >= logging.WARNING or "no longer fails" in record.getMessage()
    ] == [
        ("WARNING", "Usage import before a purge failed"),
        ("INFO", "Usage import before a purge no longer fails"),
    ]
