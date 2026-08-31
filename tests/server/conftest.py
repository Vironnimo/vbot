"""Authorize isolated Session roots used by server integration tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.sessions.format import write_bootstrap_marker


@pytest.fixture(autouse=True)
def authorize_existing_session_roots(tmp_path: Path) -> None:
    """Authorize conventional roots without creating unrelated data directories."""
    for data_dir in (tmp_path, tmp_path / "data"):
        marker = data_dir / "session-store.json"
        if data_dir.is_dir() and not marker.exists():
            write_bootstrap_marker(data_dir)
