"""Statistics fixtures: a current-format data directory and services whose resources close."""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest

from core.sessions import ChatSessionManager
from core.statistics import AgentDirectory, ProjectDirectory, StatisticsService
from core.statistics.index import StatisticsIndex
from tests.core.statistics.statistics_test_support import (
    StatisticsFactory,
    _FakeAgents,
    _FakeProjects,
)


@pytest.fixture(autouse=True)
def current_format_data_directory(tmp_path, current_session_store_template):
    """Clone the current empty store; Statistics tests exercise a derived read model."""
    shutil.copy2(current_session_store_template / "data-store.json", tmp_path)
    shutil.copy2(current_session_store_template / "sessions.db", tmp_path)


@pytest.fixture
def manager(tmp_path: Path, current_format_data_directory: None) -> Iterator[ChatSessionManager]:
    sessions = ChatSessionManager(tmp_path)
    yield sessions
    sessions.close()


@pytest.fixture
def index(tmp_path: Path, current_format_data_directory: None) -> Iterator[StatisticsIndex]:
    """The Statistics index over the test's data directory, for ``statistics(index=...)``."""
    statistics_index = StatisticsIndex(tmp_path)
    yield statistics_index
    statistics_index.close()


@pytest.fixture
def statistics(tmp_path: Path, manager: ChatSessionManager) -> Iterator[StatisticsFactory]:
    """Build services over ``manager``; every index they use is closed afterwards.

    Each call opens its own index over the shared file unless ``index`` is given,
    like a restarted service.
    """
    indexes: list[StatisticsIndex] = []

    def build(
        agent_ids: tuple[str, ...] | list[str] = ("main",),
        *,
        projects: dict[str, list[str]] | None = None,
        index: StatisticsIndex | None = None,
        **options: Any,
    ) -> StatisticsService:
        service_index = index if index is not None else StatisticsIndex(tmp_path)
        indexes.append(service_index)
        return StatisticsService(
            manager,
            cast(AgentDirectory, _FakeAgents(list(agent_ids))),
            None if projects is None else cast(ProjectDirectory, _FakeProjects(projects)),
            index=service_index,
            **options,
        )

    yield build
    for service_index in indexes:
        service_index.close()
