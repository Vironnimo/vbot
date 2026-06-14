"""Shared pytest fixtures and global test isolation."""

from __future__ import annotations

import logging

import pytest


@pytest.fixture(autouse=True)
def _restore_vbot_logger_propagation() -> object:
    """Keep the ``vbot`` logger's propagation isolated across tests.

    ``LogManager._ensure_configured`` sets ``logging.getLogger("vbot").propagate
    = False`` — correct in production so vBot logs don't double-emit through the
    root logger. But any test that builds a ``Runtime``/``LogManager`` without
    calling ``close()`` leaks that flag process-wide. After that, ``caplog`` —
    whose capture handler lives on the root logger — can no longer see ``vbot.*``
    records, so later caplog-based tests under that namespace silently fail.

    Save and restore the flag around every test so order can't break capture.
    """
    vbot_logger = logging.getLogger("vbot")
    previous_propagate = vbot_logger.propagate
    yield
    vbot_logger.propagate = previous_propagate
