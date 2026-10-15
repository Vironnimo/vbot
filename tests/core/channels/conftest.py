"""Ownership of persistent stores opened by the Channel test builders."""

from collections.abc import Iterator

import pytest

from tests.core.channels.engine_test_support import close_test_stores


@pytest.fixture(autouse=True)
def _close_helper_stores() -> Iterator[None]:
    try:
        yield
    finally:
        close_test_stores()
