"""Shared pytest fixtures and global test isolation."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import pytest


@pytest.fixture(autouse=True)
def _remove_inherited_vbot_run_context(monkeypatch: pytest.MonkeyPatch) -> None:
    """Require tests to opt into vBot Run context explicitly."""
    for name in tuple(os.environ):
        if name.startswith("VBOT_RUN_"):
            monkeypatch.delenv(name)


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


@pytest.fixture(autouse=True)
def _isolate_vbot_loggers() -> Iterator[None]:
    """Keep ``vbot`` logger configuration isolated across tests.

    ``LogManager`` configures the ``vbot`` namespace for production: it sets the
    configured level (``INFO`` by default) on ``vbot`` and on every child logger
    it hands out, disables ``vbot`` propagation, and attaches its handlers.
    ``LogManager.close()`` detaches handlers and restores propagation but keeps
    the levels, and tests that build a ``Runtime`` without closing it leak all
    of it process-wide. A leaked ``vbot`` level lets later ``caplog`` tests
    capture records their default ``WARNING`` threshold should filter; leaked
    propagation hides ``vbot.*`` records from ``caplog`` entirely, and leaked
    handlers keep writing into earlier tests' log files and streams.

    Snapshot level, propagation, ``disabled`` and handlers of every existing
    ``vbot``/``vbot.*`` logger before each test and restore them afterwards.
    Loggers first created during the test return to the pristine default state
    because the ``logging`` module cannot forget a created logger.
    """
    snapshot = {name: _LoggerState.capture(logger) for name, logger in _vbot_loggers().items()}
    yield
    for name, logger in _vbot_loggers().items():
        snapshot.get(name, _PRISTINE_LOGGER_STATE).apply(logger)


@pytest.fixture(autouse=True)
def _require_loop_started_runtimes_closed(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Fail a test that leaves a ``Runtime`` started inside an Event Loop running.

    Such a ``Runtime`` leaves periodic service tasks, such as the performance
    Event Loop monitor, on the session-scoped test Event Loop, where they keep
    running during later tests on the same worker. Close it with ``await
    runtime.aclose()`` before the test ends.

    Only an already imported ``Runtime`` class is guarded, which covers every
    test module that imports ``Runtime`` at module level.
    """
    runtime_module = sys.modules.get("core.runtime.runtime")
    if runtime_module is None:
        yield
        return
    runtime_class = runtime_module.Runtime
    original_start = runtime_class.start
    started: dict[int, Any] = {}

    def start(runtime: Any) -> None:
        original_start(runtime)
        if _event_loop_running():
            started[id(runtime)] = runtime

    monkeypatch.setattr(runtime_class, "start", start)
    yield
    leaked = sum(1 for runtime in started.values() if runtime._started)
    if leaked:
        pytest.fail(
            f"{leaked} Runtime(s) started inside the Event Loop are still running; "
            "await runtime.aclose() before the test ends",
            pytrace=False,
        )


def _event_loop_running() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True
