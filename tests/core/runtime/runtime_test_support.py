"""Shared fixtures and fakes for runtime behavior tests."""

from pathlib import Path

import pytest

from core.sessions.format import write_bootstrap_marker
from core.utils.config import Config


def _authorize_session_store(data_dir: Path) -> None:
    if not (data_dir / "sessions.db").is_file():
        write_bootstrap_marker(data_dir)


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config(data_dir=tmp_path / "data")
