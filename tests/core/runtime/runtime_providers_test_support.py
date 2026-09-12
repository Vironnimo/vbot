"""Shared fixtures and fakes for runtime providers behavior tests."""

from pathlib import Path

import pytest

from core.runtime.runtime import Runtime
from core.utils.config import Config


@pytest.fixture
def runtime(tmp_path: Path) -> Runtime:
    """Provide a started Runtime instance loaded from resources."""
    config = Config(data_dir=tmp_path / "data")
    runtime = Runtime(config)
    runtime.start()
    return runtime
