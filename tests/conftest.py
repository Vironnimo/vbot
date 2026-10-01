"""Shared pytest fixtures and global test isolation."""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import logging
import os
import shutil
import subprocess
import sys
import zlib
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from tests import cpu_pool

# Tests run git in temporary repositories. Started from a git hook, pytest inherits
# variables such as GIT_DIR and GIT_INDEX_FILE that would point those git calls at
# the committing repository.
if any(name.startswith("GIT_") for name in os.environ):
    with contextlib.suppress(OSError):
        local_git_variables = subprocess.run(
            ["git", "rev-parse", "--local-env-vars"], capture_output=True, text=True, check=False
        ).stdout.split()
        for name in local_git_variables:
            os.environ.pop(name, None)


@pytest.fixture(autouse=True)
def _remove_inherited_vbot_run_context(monkeypatch: pytest.MonkeyPatch) -> None:
    """Require tests to opt into vBot Run context explicitly."""
    for name in tuple(os.environ):
        if name.startswith("VBOT_RUN_"):
            monkeypatch.delenv(name)


@pytest.fixture(scope="session")
def _home_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("homes")


_HOME_NUMBERS = itertools.count()


@pytest.fixture(autouse=True)
def _isolated_home(_home_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Give every test an empty home directory of its own.

    ``Path.home()`` and ``~`` read HOME on POSIX and USERPROFILE on Windows,
    so no test reads or writes the real home: its ``~/.vbot`` data, the
    ``~/.vbot-dev`` data a worktree checkout resolves, or Git configuration.
    Tests reach the directory through ``Path.home()``.
    """
    home = _home_root / str(next(_HOME_NUMBERS))
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)


@pytest.fixture(scope="session")
def current_session_store_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build one empty current-format store per test worker."""
    # Imported here so that collecting a test module costs only its own imports.
    from core.database import write_bootstrap_marker
    from core.sessions import ChatSessionManager

    template = tmp_path_factory.mktemp("current-session-store")
    write_bootstrap_marker(template)
    ChatSessionManager(template).close()
    return template


@pytest.fixture
def current_format_data_directory(tmp_path: Path, current_session_store_template: Path) -> None:
    """Clone an empty current-format store for tests that consume Sessions."""
    shutil.copy2(current_session_store_template / "data-store.json", tmp_path)
    shutil.copy2(current_session_store_template / "sessions.db", tmp_path)


_VBOT_LOGGER_NAMESPACE = "vbot"


@dataclass(frozen=True)
class _LoggerState:
    level: int
    propagate: bool
    disabled: bool
    handlers: tuple[logging.Handler, ...]

    @classmethod
    def capture(cls, logger: logging.Logger) -> _LoggerState:
        return cls(
            level=logger.level,
            propagate=logger.propagate,
            disabled=logger.disabled,
            handlers=tuple(logger.handlers),
        )

    def apply(self, logger: logging.Logger) -> None:
        logger.setLevel(self.level)
        logger.propagate = self.propagate
        logger.disabled = self.disabled
        logger.handlers = list(self.handlers)


_PRISTINE_LOGGER_STATE = _LoggerState(
    level=logging.NOTSET, propagate=True, disabled=False, handlers=()
)


def _vbot_loggers() -> dict[str, logging.Logger]:
    return {
        name: logger
        for name, logger in list(logging.Logger.manager.loggerDict.items())
        if isinstance(logger, logging.Logger)
        and (name == _VBOT_LOGGER_NAMESPACE or name.startswith(f"{_VBOT_LOGGER_NAMESPACE}."))
    }


def _managed_root_handlers() -> list[logging.Handler]:
    return [
        handler
        for handler in logging.getLogger().handlers
        if getattr(handler, "_vbot_managed_handler", None) is not None
    ]


@pytest.fixture(autouse=True)
def _isolate_vbot_loggers() -> Iterator[None]:
    """Keep ``vbot`` logger configuration isolated across tests.

    ``LogManager`` configures the ``vbot`` namespace for production: it sets the
    configured level (``INFO`` by default) on ``vbot`` and on every child logger
    it hands out, disables ``vbot`` propagation, attaches its handlers, and adds
    a router for other libraries' warnings to the root logger.
    ``LogManager.close()`` detaches handlers and restores propagation but keeps
    the child levels, and tests that start a ``Runtime`` without closing it leak
    all of it process-wide. A leaked ``vbot`` level lets later ``caplog`` tests
    capture records their default ``WARNING`` threshold should filter; leaked
    propagation hides ``vbot.*`` records from ``caplog`` entirely, and leaked
    handlers keep writing into earlier tests' log files and streams.

    Snapshot level, propagation, ``disabled`` and handlers of every existing
    ``vbot``/``vbot.*`` logger before each test and restore them afterwards.
    Loggers first created during the test return to the pristine default state
    because the ``logging`` module cannot forget a created logger. Managed root
    handlers are restored the same way; the root's other handlers belong to
    pytest, which swaps them per test phase.
    """
    snapshot = {name: _LoggerState.capture(logger) for name, logger in _vbot_loggers().items()}
    managed_root_handlers = _managed_root_handlers()
    yield
    for name, logger in _vbot_loggers().items():
        snapshot.get(name, _PRISTINE_LOGGER_STATE).apply(logger)
    root = logging.getLogger()
    for handler in _managed_root_handlers():
        if handler not in managed_root_handlers:
            root.removeHandler(handler)
    for handler in managed_root_handlers:
        if handler not in root.handlers:
            root.addHandler(handler)


@pytest.fixture(autouse=True)
def _require_loop_started_runtimes_closed(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Fail a test that leaves a ``Runtime`` started inside an Event Loop running.

    Such a ``Runtime`` leaves periodic service tasks, such as the performance
    Event Loop monitor, on the session-scoped test Event Loop, where they keep
    running during later tests on the same worker. Close it with ``await
    runtime.aclose()`` before the test ends. A Runtime whose shutdown withdrew
    readiness but left the performance monitor running, or a canonical database
    its start opened still open, counts as still running.

    Only an already imported ``Runtime`` class is guarded, which covers every
    test module that imports ``Runtime`` at module level.
    """
    runtime_module = sys.modules.get("core.runtime.runtime")
    if runtime_module is None:
        yield
        return
    runtime_class = runtime_module.Runtime
    original_start = runtime_class.start
    started: list[tuple[Any, Any, tuple[Any, ...]]] = []

    def start(runtime: Any) -> None:
        original_start(runtime)
        if _event_loop_running():
            started.append((runtime, runtime.performance, runtime.canonical_databases()))

    monkeypatch.setattr(runtime_class, "start", start)
    yield
    leaked = {
        id(runtime)
        for runtime, performance, databases in started
        if runtime._started
        or performance.monitoring
        or not all(database.is_closed() for database in databases)
    }
    if leaked:
        pytest.fail(
            f"{len(leaked)} Runtime(s) started inside the Event Loop are still running; "
            "await runtime.aclose() before the test ends",
            pytrace=False,
        )


def _selected_shard() -> tuple[int, int] | None:
    """Return ``(index, count)`` from ``VBOT_TEST_SHARD=<index>/<count>``, 1-based."""
    raw = os.environ.get("VBOT_TEST_SHARD", "").strip()
    if not raw:
        return None
    index_text, _, count_text = raw.partition("/")
    try:
        index, count = int(index_text), int(count_text)
    except ValueError:
        raise pytest.UsageError(f"VBOT_TEST_SHARD must be <index>/<count>, got {raw!r}") from None
    if not 1 <= index <= count:
        raise pytest.UsageError(f"VBOT_TEST_SHARD index must be within 1..{count}, got {raw!r}")
    return index, count


def in_shard(nodeid: str, index: int, count: int) -> bool:
    """Return whether *nodeid* belongs to shard *index* of *count* (stable across runs)."""
    return zlib.crc32(nodeid.encode("utf-8")) % count == index - 1


def pytest_xdist_auto_num_workers(config: pytest.Config) -> int | None:
    return cpu_pool.auto_workers()


# First, so that pytest-xdist starts only the workers the pool grants.
@pytest.hookimpl(tryfirst=True)
def pytest_configure(config: pytest.Config) -> None:
    cpu_pool.claim(config)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Keep one CI shard of the collected tests when ``VBOT_TEST_SHARD`` is set."""
    shard = _selected_shard()
    if shard is None:
        return
    selected = [item for item in items if in_shard(item.nodeid, *shard)]
    deselected = [item for item in items if not in_shard(item.nodeid, *shard)]
    if deselected:
        config.hook.pytest_deselected(items=deselected)
    items[:] = selected


def _event_loop_running() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True
