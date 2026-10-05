"""The Runtime's schedule of configuration backups (``core.database.config_backups``).

A backup is checked when the Runtime starts, every :data:`CHECK_INTERVAL_SECONDS`
while it serves, and once more when it shuts down. A check compares path, size
and modification time of the configuration files with the last backed-up state
and backs them up only when that changed; the capture itself skips a state equal
to the newest backup. All file work runs on one worker, never on the Event Loop.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from core.database import (
    DatabaseError,
    OperationLockBusyError,
    SnapshotBarrier,
    capture_config_backup,
    configuration_fingerprint,
)
from core.utils.log_conditions import LoggedConditions
from core.utils.workers import BoundedWorkerPool

_LOGGER = logging.getLogger("vbot.database")

CHECK_INTERVAL_SECONDS = 300.0

_WORKERS = BoundedWorkerPool(name="config-backups", max_workers=1)
_FAILURES = LoggedConditions()

type Fingerprint = tuple[tuple[str, int, int], ...]


async def _sleep(seconds: float) -> None:
    """Wait between checks (the schedule's test seam)."""
    await asyncio.sleep(seconds)


class ConfigBackupSchedule:
    """Backs up the configuration files of one data directory while the Runtime runs."""

    def __init__(self, data_dir: Path, barrier: SnapshotBarrier) -> None:
        self._data_dir = Path(data_dir)
        self._barrier = barrier
        self._backed_up: Fingerprint | None = None
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        """Start checking; call inside the serving Event Loop."""
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.get_running_loop().create_task(self._run(), name="config-backups")

    def stop(self) -> None:
        """Stop checking; a backup in progress still finishes on its worker."""
        if self._task is not None:
            self._task.cancel()

    async def aclose(self) -> None:
        """Stop checking, then back up a configuration changed since the last check."""
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await self._check("stop")

    async def _run(self) -> None:
        reason = "start"
        while True:
            await self._check(reason)
            reason = "change"
            await _sleep(CHECK_INTERVAL_SECONDS)

    async def _check(self, reason: str) -> None:
        try:
            fingerprint = await _WORKERS.run(configuration_fingerprint, self._data_dir)
            if fingerprint == self._backed_up:
                return
            await _WORKERS.run(
                capture_config_backup, self._data_dir, reason=reason, barrier=self._barrier
            )
        except OperationLockBusyError:
            # A data snapshot or restore runs; the next check backs the change up.
            _LOGGER.debug("Configuration backup deferred: another data-store operation runs")
            return
        except (OSError, DatabaseError) as error:
            if _FAILURES.started(self._data_dir, type(error).__name__):
                _LOGGER.warning("Configuration backup failed; the next check retries it: %s", error)
            return
        except Exception as error:
            if _FAILURES.started(self._data_dir, type(error).__name__):
                _LOGGER.error(
                    "Configuration backup failed unexpectedly; the next check retries it: %s",
                    error,
                    exc_info=True,
                )
            return
        if _FAILURES.ended(self._data_dir):
            _LOGGER.info("Configuration backups work again")
        self._backed_up = fingerprint
