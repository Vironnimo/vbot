"""Isolate the process-wide performance sink between tests."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from core.performance.performance import reset_for_tests


@pytest.fixture(autouse=True)
def _reset_performance_sink() -> Iterator[None]:
    reset_for_tests()
    yield
    reset_for_tests()
