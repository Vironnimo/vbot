"""Runtime selection, reload, and release of the configured Recall backend."""

from __future__ import annotations

from typing import Any

import pytest

from core.recall import (
    RecallBackendRegistry,
    SqliteFtsRecallBackend,
    VectorRecallBackend,
)
from core.runtime.runtime import Runtime
from core.utils.config import Config
from tests.core.runtime.runtime_test_support import write_settings


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        (None, SqliteFtsRecallBackend),
        ("team_backend", SqliteFtsRecallBackend),
        ("broken_backend", SqliteFtsRecallBackend),
        ("vector", VectorRecallBackend),
    ],
    ids=["default", "unknown-falls-back", "failing-factory-falls-back", "vector"],
)
def test_runtime_selects_the_configured_recall_backend_at_start(
    config: Config,
    monkeypatch: pytest.MonkeyPatch,
    configured: str | None,
    expected: type,
) -> None:
    if configured is not None:
        write_settings(config.data_dir, {"recall": {"backend": configured}})
    if configured == "broken_backend":
        registry = RecallBackendRegistry()
        registry.register("sqlite_fts", SqliteFtsRecallBackend)

        def create_broken_backend(_context: Any) -> Any:
            raise RuntimeError("broken derived index")

        registry.register("broken_backend", create_broken_backend)
        monkeypatch.setattr(RecallBackendRegistry, "with_builtins", classmethod(lambda _: registry))
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-fake-key-12345")
    runtime = Runtime(config, safe_startup_mode="test")

    runtime.start()
    try:
        backend = runtime.recall_backend
        assert type(backend) is expected
        if isinstance(backend, VectorRecallBackend):
            assert backend.embeddings is runtime.embeddings
            assert backend.model_registry is runtime.models
    finally:
        runtime.stop()


def test_reload_recall_backend_swaps_backend_and_tools_and_releases_replaced_indexes(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    closed: list[object] = []
    monkeypatch.setattr(SqliteFtsRecallBackend, "close", lambda self: closed.append(self))
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-fake-key-12345")
    runtime = Runtime(config, safe_startup_mode="test")
    runtime.start()
    try:
        initial = runtime.recall_backend
        assert isinstance(initial, SqliteFtsRecallBackend)
        lexical_tool = runtime.tools.get("session_search")
        lexical_parameters = lexical_tool.parameters
        assert set(lexical_parameters["properties"]) == {
            "query",
            "period",
            "agent_id",
            "session_id",
            "include_subagents",
        }

        write_settings(config.data_dir, {"recall": {"backend": "vector"}})
        runtime.reload_recall_backend()

        # A reload releases the replaced backend's index.
        assert closed == [initial]
        assert isinstance(runtime.recall_backend, VectorRecallBackend)
        vector_tool = runtime.tools.get("session_search")
        vector_properties = vector_tool.parameters["properties"]
        assert set(vector_properties) == set(lexical_parameters["properties"])
        assert (
            vector_properties["query"]["description"]
            != lexical_parameters["properties"]["query"]["description"]
        )
        for field in ("period", "agent_id", "session_id", "include_subagents"):
            assert vector_properties[field] == lexical_parameters["properties"][field]
        assert vector_tool.description != lexical_tool.description
        assert "session_read" not in [tool.name for tool in runtime.tools.list_tools()]

        write_settings(config.data_dir, {"recall": {"backend": "sqlite_fts"}})
        runtime.reload_recall_backend()
        replaced = runtime.recall_backend
        assert isinstance(replaced, SqliteFtsRecallBackend)
        runtime.reload_recall_backend()
        active = runtime.recall_backend
        assert active is not replaced
        assert closed == [initial, replaced]
    finally:
        runtime.stop()
    # Stopping releases the active one.
    assert closed == [initial, replaced, active]
