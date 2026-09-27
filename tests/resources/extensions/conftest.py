"""Shared fixtures for the bundled Extension suites."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from core.extensions import purge_extension_modules


@pytest.fixture(autouse=True)
def _clean_extension_modules() -> Iterator[None]:
    """Drop the ``vbot_ext`` modules a registry load imported during the test."""
    yield
    purge_extension_modules()
