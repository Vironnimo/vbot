"""Disposable canonical Usage stores for task execution regressions."""

from collections.abc import Iterator
from pathlib import Path

import pytest

from core.database import write_bootstrap_marker
from core.usage import UsageRecorder


@pytest.fixture
def recorder(tmp_path: Path) -> Iterator[UsageRecorder]:
    write_bootstrap_marker(tmp_path)
    owner = UsageRecorder(tmp_path / "model-usage.db")
    try:
        yield owner
    finally:
        owner.close()
