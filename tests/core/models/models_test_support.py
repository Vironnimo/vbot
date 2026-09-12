"""Shared fixtures and fakes for models behavior tests."""




from pathlib import Path

import pytest

from core.models.models import (
    ModelRegistry,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _clear_registry_cache():
    """Clear the registry cache before and after each test for independence."""
    ModelRegistry._cache.clear()
    yield
    ModelRegistry._cache.clear()
