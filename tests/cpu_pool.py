"""Share the machine's CPU cores among the local pytest runs of every checkout.

Several Agents test in checkouts of their own at the same time, and every pytest
run would otherwise start as many workers as the machine has cores; together they
overload it until tests time out. So before it starts its workers, each local run
claims cores from one pool per machine: one lock file per pool slot, held until
the run ends. The operating system releases a lock when its process dies, so a
crashed run never keeps its cores. A run waits until the cores it asks for are
free instead of starting beside the others; waiting runs take their turn one
after another.

``-n auto`` asks for ``LOCAL_AUTO_WORKERS`` cores here; an explicit ``-n N`` asks for
N, at most the pool's size. Every run appends one line to ``RUN_LOG`` in the pool
directory: the checkout, cores, wait, duration and outcome.

CI runs on machines of its own and skips the pool, as does a pytest run started by
a test inside a run that holds cores, which would otherwise wait for its parent.
"""

from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any

import psutil  # type: ignore[import-untyped]
import pytest

POOL_DIR = Path.home() / ".cache" / "vbot-test-cores"
RUN_LOG = "runs.jsonl"
# The log starts afresh beside one previous generation once it grows beyond this.
RUN_LOG_LIMIT = 2_000_000
LOCAL_AUTO_WORKERS = 2
# Logical cores the pool leaves to the rest of the machine.
POOL_HEADROOM = 2
# Set while a run holds cores; the pytest processes it starts inherit it.
HELD_VARIABLE = "VBOT_TEST_CORES_HELD"
_POLL_SECONDS = 0.2


def pool_size() -> int:
    """The most workers local runs may use together.

    All logical cores but ``POOL_HEADROOM``, at least the physical ones. Measured
    2026-10-01 on 6 cores with 12 threads, the full suite took about 117 s at 6
    workers, 110 s at 8, 92 s at 10 and 94 s at 12; the headroom keeps the machine
    responsive for the work beside the tests.
    """
    logical = psutil.cpu_count() or os.cpu_count() or 1
    physical = psutil.cpu_count(logical=False) or logical
    return max(physical, logical - POOL_HEADROOM)


def active() -> bool:
    """Whether this process's pytest run claims its cores from the pool."""
    return not os.environ.get("CI") and not os.environ.get(HELD_VARIABLE)


def auto_workers() -> int | None:
    """The workers ``-n auto`` starts, or None to leave the choice to pytest-xdist."""
    return min(LOCAL_AUTO_WORKERS, pool_size()) if active() else None


def _try_lock(handle: IO[bytes]) -> bool:
    handle.seek(0)
    if sys.platform == "win32":
        import msvcrt

        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True
    import fcntl

    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


def _unlock(handle: IO[bytes]) -> None:
    handle.seek(0)
    if sys.platform == "win32":
        import msvcrt

        with suppress(OSError):
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def release(handles: list[IO[bytes]]) -> None:
    """Free the cores *handles* hold."""
    for handle in handles:
        _unlock(handle)
        handle.close()


def take(
    directory: Path,
    cores: int,
    size: int,
    on_wait: Callable[[], None] = lambda: None,
) -> list[IO[bytes]]:
    """Wait until *cores* of the *size* cores in *directory* are free; hold and return them.

    A run takes all its cores at once and holds none while it waits. Only one run
    collects at a time, so a run waiting for many cores does not lose them to
    smaller runs behind it. *on_wait* is called once when the run has to wait.
    """
    directory.mkdir(parents=True, exist_ok=True)
    cores = max(1, min(cores, size))
    waiting = False

    def wait() -> None:
        nonlocal waiting
        if not waiting:
            waiting = True
            on_wait()
        time.sleep(_POLL_SECONDS)

    queue = (directory / "queue.lock").open("a+b")
    held: dict[int, IO[bytes]] = {}
    try:
        while not _try_lock(queue):
            wait()
        try:
            while True:
                for index in range(size):
                    if len(held) == cores:
                        break
                    handle = (directory / f"core-{index}.lock").open("a+b")
                    if _try_lock(handle):
                        held[index] = handle
                    else:
                        handle.close()
                if len(held) == cores:
                    return list(held.values())
                release(list(held.values()))
                held.clear()
                wait()
        finally:
            _unlock(queue)
    except BaseException:
        release(list(held.values()))
        raise
    finally:
        queue.close()


def append_run(directory: Path, record: dict[str, Any]) -> None:
    """Append one run's *record* to the run log in *directory*."""
    log = directory / RUN_LOG
    with suppress(OSError):
        if log.stat().st_size > RUN_LOG_LIMIT:
            log.replace(directory / f"{RUN_LOG}.1")
    with suppress(OSError), log.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")


class _Claim:
    """The cores one pytest run holds; logs the run and releases them when it ends."""

    def __init__(
        self, handles: list[IO[bytes]], asked: int, waited: float, directory: Path
    ) -> None:
        self.handles = handles
        self.asked = asked
        self.waited = waited
        self.directory = directory
        self.started = time.monotonic()
        self.exit_status: int | None = None

    def pytest_sessionfinish(self, exitstatus: int) -> None:
        self.exit_status = int(exitstatus)

    def pytest_unconfigure(self, config: pytest.Config) -> None:
        reporter = config.pluginmanager.get_plugin("terminalreporter")
        stats = getattr(reporter, "stats", {})
        outcomes = {
            outcome: len(stats.get(outcome, [])) for outcome in ("passed", "failed", "error")
        }
        record = {
            "at": datetime.now(UTC).isoformat(timespec="seconds"),
            "checkout": str(config.rootpath),
            "cores_asked": self.asked,
            "cores": len(self.handles),
            "waited_s": round(self.waited, 1),
            "ran_s": round(time.monotonic() - self.started, 1),
            "exit": self.exit_status,
            **outcomes,
        }
        append_run(self.directory, record)
        release(self.handles)
        self.handles = []


def claim(config: pytest.Config, directory: Path = POOL_DIR) -> None:
    """Hold the cores this pytest run's workers need; start no more workers than it may hold.

    Runs in the controller before pytest-xdist starts its workers: xdist has
    already resolved ``-n``, and a run asking for more cores than it may take
    starts as many workers as it may take.
    """
    if hasattr(config, "workerinput") or not active():
        return
    workers = config.getoption("numprocesses", None)
    workers = workers if isinstance(workers, int) else 0
    size = pool_size()
    asked = max(1, workers)
    start = time.monotonic()

    def announce() -> None:
        print(
            "pytest: waiting for CPU cores that other "
            f"test runs on this machine hold (log: {directory / RUN_LOG})...",
            file=sys.stderr,
            flush=True,
        )

    handles = take(directory, asked, size, announce)
    if workers > len(handles):
        config.option.numprocesses = len(handles)
        config.option.tx = ["popen"] * len(handles)
    os.environ[HELD_VARIABLE] = "1"
    config.pluginmanager.register(
        _Claim(handles, asked, time.monotonic() - start, directory), "vbot-cpu-pool"
    )
