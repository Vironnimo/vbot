"""Server test fixtures.

Server tests boot real ``Runtime`` instances through the FastAPI lifespan
(``create_app`` + ``TestClient``). The lifespan fires the local-catalog
auto-refresh as a background task, which would hit a live local Ollama on the
developer machine and write refreshed catalogs into the repo's ``resources/``
directory — network I/O and repo writes tests must never do. The sweep is
neutralized here for the whole server test package; its own behavior is
covered by ``tests/core/runtime/test_runtime_providers.py`` with a mocked
discovery layer.
"""

from __future__ import annotations

import pytest

from core.runtime.runtime import Runtime


@pytest.fixture(autouse=True)
def _disable_local_catalog_auto_refresh(monkeypatch: pytest.MonkeyPatch):
    async def _noop(self: Runtime, *, force: bool = False) -> None:
        return None

    monkeypatch.setattr(Runtime, "maybe_refresh_local_catalogs", _noop)
