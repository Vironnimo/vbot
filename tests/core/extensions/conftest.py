"""Shared fixtures for Extension tests."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from core.extensions import purge_extension_modules


@pytest.fixture(autouse=True)
def _clean_extension_modules() -> Iterator[None]:
    """Drop the synthetic ``vbot_ext`` namespace a loaded test Extension leaves behind."""
    yield
    purge_extension_modules()
