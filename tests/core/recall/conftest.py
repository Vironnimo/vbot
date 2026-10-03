"""Recall tests start from the current empty store and share one Session manager."""

from __future__ import annotations

import shutil
from collections.abc import Callable, Iterator
from contextlib import ExitStack
from pathlib import Path
from typing import Any

import pytest

from core.recall import HybridRecallBackend, RecallBackendContext, VectorRecallBackend
from core.sessions import ChatSessionManager
from tests.core.recall.recall_test_support import HybridBackendFactory, VectorBackendFactory


@pytest.fixture(autouse=True)
def current_format_data_directory(tmp_path: Path, current_session_store_template: Path) -> None:
    """Clone the current empty store; Recall tests do not exercise schema bootstrap."""
    shutil.copy2(current_session_store_template / "data-store.json", tmp_path)
    shutil.copy2(current_session_store_template / "sessions.db", tmp_path)


@pytest.fixture
def sessions(tmp_path: Path, current_format_data_directory: None) -> Iterator[ChatSessionManager]:
    manager = ChatSessionManager(tmp_path)
    yield manager
    manager.close()


@pytest.fixture
def vector_backend(tmp_path: Path, sessions: ChatSessionManager) -> Iterator[VectorBackendFactory]:
    """Open ``vector`` backends over the test's Sessions, closed when the test ends.

    Each backend opens and owns its own Passage index under ``tmp_path``.
    """

    with ExitStack() as opened:

        def open_backend(
            *,
            embeddings: Any | None = None,
            logger: Any | None = None,
            on_waiting: Callable[[], None] | None = None,
        ) -> VectorRecallBackend:
            context = RecallBackendContext(
                data_dir=tmp_path, sessions=sessions, embeddings=embeddings, logger=logger
            )
            backend = VectorRecallBackend(context, on_waiting=on_waiting)
            opened.callback(backend.close)
            return backend

        yield open_backend


@pytest.fixture
def hybrid_backend(tmp_path: Path, sessions: ChatSessionManager) -> Iterator[HybridBackendFactory]:
    """Open ``hybrid`` backends over the test's Sessions, closed when the test ends.

    Each backend opens and owns its own Passage index under ``tmp_path``.
    """

    with ExitStack() as opened:

        def open_backend(*, embeddings: Any | None = None) -> HybridRecallBackend:
            context = RecallBackendContext(
                data_dir=tmp_path, sessions=sessions, embeddings=embeddings
            )
            backend = HybridRecallBackend(context)
            opened.callback(backend.close)
            return backend

        yield open_backend
