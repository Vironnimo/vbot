"""Tests for the Runtime bootstrap class.

Verifies that ``Runtime`` initialises, starts, and stops without errors,
and that the logger is properly created after ``start()``.
"""

import logging

from core.runtime.runtime import Runtime
from core.utils.config import Config


def test_runtime_start_no_error():
    """Instantiating Runtime and calling start() raises no exception."""
    # Arrange
    config = Config()
    runtime = Runtime(config)

    # Act
    runtime.start()

    # Assert
    assert runtime.logger is not None


def test_runtime_logger_exists_after_start():
    """After start(), runtime.logger is a valid logger object."""
    # Arrange
    config = Config()
    runtime = Runtime(config)

    # Act
    runtime.start()

    # Assert
    logger = runtime.logger
    assert logger is not None
    assert hasattr(logger, "info")
    assert hasattr(logger, "error")
    assert hasattr(logger, "debug")
    # Verify it is a logging.Logger (the concrete implementation)
    assert isinstance(logger, logging.Logger)


def test_runtime_stop_runs_cleanly():
    """After start(), calling stop() completes without exception."""
    # Arrange
    config = Config()
    runtime = Runtime(config)
    runtime.start()

    # Act
    runtime.stop()

    # Assert — reaching here without exception is success


def test_runtime_stop_without_start_does_not_crash():
    """Calling stop() before start() is a no-op and does not crash."""
    # Arrange
    config = Config()
    runtime = Runtime(config)

    # Act
    runtime.stop()

    # Assert — reaching here without exception proves it is a safe no-op
