"""Disposable canonical Usage stores and a network guard for Task Model tests."""

from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
import pytest_asyncio

from core.database import write_bootstrap_marker
from core.model_tasks import model_files
from core.providers.http import ProviderHTTPTransports
from core.usage import UsageRecorder
from tests.core.model_tasks import model_tasks_test_support


@pytest_asyncio.fixture(autouse=True)
async def _provider_http_transports(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[None]:
    pool = ProviderHTTPTransports()
    monkeypatch.setattr(model_tasks_test_support, "http_transports", pool, raising=False)
    try:
        yield
    finally:
        await pool.aclose()


@pytest.fixture(autouse=True)
def _no_model_downloads(monkeypatch: pytest.MonkeyPatch) -> None:
    """A local model installation never reaches Hugging Face from a test."""

    def refuse() -> None:
        raise AssertionError("A test tried to download model files")

    monkeypatch.setattr(model_files, "open_client", refuse)


@pytest.fixture
def recorder(tmp_path: Path) -> Iterator[UsageRecorder]:
    write_bootstrap_marker(tmp_path)
    owner = UsageRecorder(tmp_path / "model-usage.db")
    try:
        yield owner
    finally:
        owner.close()
