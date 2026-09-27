"""Shared Runtime fixtures: isolated current-format data directories and Runtimes."""

from __future__ import annotations

import logging
import shutil
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from core.runtime.runtime import Runtime
from core.utils.config import Config


def _clone_current_store(template: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    shutil.copy2(template / "data-store.json", destination / "data-store.json")
    shutil.copy2(template / "sessions.db", destination / "sessions.db")


@pytest.fixture(autouse=True)
def current_format_runtime_data_directory(tmp_path: Path, current_session_store_template: Path):
    """Clone the empty store for conventional Runtime roots used by tests."""
    _clone_current_store(current_session_store_template, tmp_path)
    _clone_current_store(current_session_store_template, tmp_path / "data")


@pytest.fixture(autouse=True)
def _clean_extension_modules() -> Iterator[None]:
    """Drop the synthetic ``vbot_ext`` namespace that user Extensions load into."""
    yield
    for module_name in list(sys.modules):
        if module_name == "vbot_ext" or module_name.startswith("vbot_ext."):
            del sys.modules[module_name]


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config(data_dir=tmp_path / "data")


@pytest.fixture
def runtime(config: Config) -> Iterator[Runtime]:
    """A started normal-mode Runtime over the isolated data directory."""
    runtime = Runtime(config)
    runtime.start()
    yield runtime
    runtime.stop()


@contextmanager
def _restored_vbot_loggers() -> Iterator[None]:
    """Restore ``vbot`` logger state around a Runtime that outlives one test.

    The per-test logger isolation in ``tests/conftest.py`` snapshots after a
    module-scoped Runtime configured logging, so it cannot undo that Runtime's
    levels. Restore the state from before its start once it has stopped.
    """

    def loggers() -> dict[str, logging.Logger]:
        return {
            name: logger
            for name, logger in list(logging.Logger.manager.loggerDict.items())
            if isinstance(logger, logging.Logger) and (name == "vbot" or name.startswith("vbot."))
        }

    def capture(logger: logging.Logger) -> tuple[int, bool, bool, list[logging.Handler]]:
        return logger.level, logger.propagate, logger.disabled, list(logger.handlers)

    before = {name: capture(logger) for name, logger in loggers().items()}
    try:
        yield
    finally:
        for name, logger in loggers().items():
            level, propagate, disabled, handlers = before.get(
                name, (logging.NOTSET, True, False, [])
            )
            logger.setLevel(level)
            logger.propagate = propagate
            logger.disabled = disabled
            logger.handlers = handlers


@pytest.fixture(scope="module")
def shared_runtime(
    tmp_path_factory: pytest.TempPathFactory, current_session_store_template: Path
) -> Iterator[Runtime]:
    """One started Runtime per module for tests that only read its wiring.

    Tests using it must not change its data directory, settings, or registries;
    process environment changes through ``monkeypatch`` are fine because
    credential resolution reads the live process environment.
    """
    data_dir = tmp_path_factory.mktemp("shared-runtime") / "data"
    _clone_current_store(current_session_store_template, data_dir)
    with _restored_vbot_loggers():
        runtime = Runtime(Config(data_dir=data_dir))
        runtime.start()
        try:
            yield runtime
        finally:
            runtime.stop()
