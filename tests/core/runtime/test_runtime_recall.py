"""Runtime selection and reload of the Recall backend, and its shared Passage index."""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import Mock

import pytest

from core.chat import ChatMessage
from core.recall import (
    RecallBackendRegistry,
    SqliteFtsRecallBackend,
    VectorRecallBackend,
)
from core.recall._passage_catalog import CatalogPlan, SessionPassages
from core.recall.passages import build_session_passages
from core.runs import Run
from core.runtime.runtime import Runtime
from core.utils.config import Config
from tests.core.runtime.runtime_test_support import write_settings


# The unconfigured default is the starting point of the reload test below.
@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        ("team_backend", SqliteFtsRecallBackend),
        ("broken_backend", SqliteFtsRecallBackend),
        ("vector", VectorRecallBackend),
    ],
    ids=["unknown-falls-back", "failing-factory-falls-back", "vector"],
)
def test_runtime_selects_the_configured_recall_backend_at_start(
    config: Config,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    configured: str,
    expected: type,
) -> None:
    write_settings(config.data_dir, {"recall": {"backend": configured}})
    if configured == "broken_backend":
        registry = RecallBackendRegistry()
        registry.register("sqlite_fts", SqliteFtsRecallBackend)

        def create_broken_backend(_context: Any) -> Any:
            raise RuntimeError("broken derived index")

        registry.register("broken_backend", create_broken_backend)
        monkeypatch.setattr(
            RecallBackendRegistry, "with_builtins", classmethod(lambda _cls, **_kwargs: registry)
        )
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-fake-key-12345")
    runtime = Runtime(config, safe_startup_mode="test")

    # Runtime logging stops propagation at the ``vbot`` logger: capture there.
    monkeypatch.setattr(logging.getLogger("vbot"), "handlers", [caplog.handler])
    with caplog.at_level(logging.WARNING):
        runtime.start()
        try:
            backend = runtime.recall_backend
            assert type(backend) is expected
            # Only a backend that ranks by meaning has documents embedded.
            assert runtime.recall.indexer.enabled is (expected is VectorRecallBackend)
            if isinstance(backend, VectorRecallBackend):
                assert backend.embeddings is runtime.embeddings
                assert backend.index is runtime.recall.index
            # A reload with the unchanged setting does not report it again.
            runtime.reload_recall_backend()
        finally:
            runtime.stop()
    reports = [record for record in caplog.records if "team_backend" in record.getMessage()]
    assert len(reports) == (configured == "team_backend")


def test_reload_recall_backend_swaps_backend_and_tools_and_keeps_the_shared_index(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    closed: list[object] = []
    monkeypatch.setattr(VectorRecallBackend, "close", lambda self: closed.append(self))
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-fake-key-12345")
    runtime = Runtime(config, safe_startup_mode="test")
    runtime.start()
    try:
        index = runtime.recall.index
        # Without a configured backend the Runtime starts on lexical search.
        initial = runtime.recall_backend
        assert isinstance(initial, SqliteFtsRecallBackend)
        assert runtime.recall.indexer.enabled is False
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

        vector = runtime.recall_backend
        assert isinstance(vector, VectorRecallBackend)
        assert vector.index is index
        assert runtime.recall.indexer.enabled is True
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

        # A reload releases the replaced backend; the shared index stays open.
        assert closed == [vector]
        assert isinstance(runtime.recall_backend, SqliteFtsRecallBackend)
        assert runtime.recall.index is index
        assert runtime.recall.indexer.enabled is False
    finally:
        runtime.stop()


def test_runtime_start_removes_files_of_earlier_recall_indexes(config: Config) -> None:
    directory = config.data_dir / "recall"
    (directory / "kept").mkdir(parents=True)
    stale = [
        "session_index.sqlite",
        "session_index.sqlite-wal",
        "session_passage_vectors.sqlite",
        "notes.txt",
    ]
    for name in stale:
        (directory / name).write_bytes(b"stale")
    runtime = Runtime(config, safe_startup_mode="test")

    runtime.start()
    try:
        remaining = sorted(path.name for path in directory.iterdir())
    finally:
        runtime.stop()

    # Recall owns the directory: only files of the current index may stay.
    assert remaining == ["kept"]


def _session_passages(session_id: str, text: str) -> SessionPassages:
    passages = tuple(build_session_passages([ChatMessage.user(text)]))
    return SessionPassages(
        session_id=session_id,
        previous=None,
        version=("generation", 1),
        passages=passages,
        owned=(True,) * len(passages),
    )


@pytest.mark.asyncio
async def test_deleted_sessions_and_agents_leave_the_index_under_any_backend(
    config: Config,
) -> None:
    runtime = Runtime(config, safe_startup_mode="test")
    runtime.start()
    try:
        assert isinstance(runtime.recall_backend, SqliteFtsRecallBackend)
        index = runtime.recall.index
        await index.apply(
            CatalogPlan(
                "main",
                "",
                (),
                (_session_passages("one", "first"), _session_passages("two", "second")),
            )
        )
        await index.apply(CatalogPlan("writer", "", (), (_session_passages("draft", "draft"),)))

        await runtime.remove_session_from_recall("main", "one")
        await runtime.recall.remove_agent_from_recall("writer")

        assert set(await index.list_indexed_sessions("main")) == {"two"}
        assert await index.indexed_scopes() == {(None, "main")}
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
async def test_finished_run_schedules_an_indexing_pass(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = Runtime(config, safe_startup_mode="test")
    runtime.start()
    try:
        finished = Mock()
        monkeypatch.setattr(runtime.recall.indexer, "run_finished", finished)
        session = runtime.chat_sessions.create("main")

        async def execute(run: Run) -> ChatMessage:
            answer = ChatMessage.assistant(model="test", content="done")
            await session.for_run(run.id).append_async(answer)
            return answer

        run = await runtime.chat_run_manager.start(session.address, execute)
        await run.wait()

        assert finished.call_count == 1
    finally:
        await runtime.aclose()
