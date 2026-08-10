"""Tests for the model.list local-catalog auto-refresh await budget."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import server.rpc.connection_methods as connection_methods
from server.rpc.connection_methods import _await_local_catalog_refresh


class TestAwaitLocalCatalogRefresh:
    @pytest.mark.asyncio
    async def test_awaits_fast_refresh_to_completion(self) -> None:
        # Arrange
        calls: list[int] = []

        async def maybe_refresh_local_catalogs() -> None:
            calls.append(1)

        runtime = SimpleNamespace(maybe_refresh_local_catalogs=maybe_refresh_local_catalogs)

        # Act
        await _await_local_catalog_refresh(runtime)

        # Assert
        assert calls == [1]

    @pytest.mark.asyncio
    async def test_slow_refresh_continues_in_background(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """On timeout the sweep is not cancelled — it finishes in the background."""
        # Arrange
        monkeypatch.setattr(connection_methods, "LOCAL_CATALOG_REFRESH_WAIT_SECONDS", 0.0)
        release = asyncio.Event()
        finished = asyncio.Event()

        async def maybe_refresh_local_catalogs() -> None:
            await release.wait()
            finished.set()

        runtime = SimpleNamespace(maybe_refresh_local_catalogs=maybe_refresh_local_catalogs)

        # Act — returns after the wait budget, before the sweep completes.
        await _await_local_catalog_refresh(runtime)

        # Assert — the sweep was not cancelled and completes on its own.
        assert not finished.is_set()
        release.set()
        await asyncio.wait_for(finished.wait(), timeout=1.0)

    @pytest.mark.asyncio
    async def test_runtime_without_method_is_a_no_op(self) -> None:
        # Arrange — stub runtimes in tests may not implement the sweep.
        runtime = SimpleNamespace()

        # Act / Assert — must not raise.
        await _await_local_catalog_refresh(runtime)

    @pytest.mark.asyncio
    async def test_refresh_exception_is_consumed_not_raised(self) -> None:
        # Arrange
        async def maybe_refresh_local_catalogs() -> None:
            raise RuntimeError("unexpected")

        runtime = SimpleNamespace(maybe_refresh_local_catalogs=maybe_refresh_local_catalogs)

        # Act / Assert — model.list must serve the stale catalog, not fail.
        await _await_local_catalog_refresh(runtime)
