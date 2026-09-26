"""Runtime lifetime and Extension sampling use the canonical accounting owner."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.chat.messages import ChatMessage
from core.runtime.databases import canonical_database_specs
from core.runtime.runtime import Runtime
from core.utils.config import Config
from tests.core.sessions.history_fixtures import seed_history


@pytest.mark.asyncio
async def test_runtime_imports_history_and_registers_canonical_accounting(tmp_path):
    runtime = Runtime(Config(data_dir=tmp_path / "data"), safe_startup_mode="test")
    runtime.start()
    try:
        session = runtime.chat_sessions.create("legacy")
        seed_history(
            session,
            [ChatMessage.assistant(model="p/m", content="legacy", usage={"input_tokens": 6})],
        )
        await runtime.chat_sessions.archive(session.address)
    finally:
        await runtime.aclose()
    runtime = Runtime(Config(data_dir=tmp_path / "data"), safe_startup_mode="test")
    runtime.start()
    recorder = runtime.usage_recorder
    try:
        records = recorder.read_since()[1]
        assert len(records) == 1
        assert records[0].usage["input_tokens"] == 6
        assert recorder.database in runtime.canonical_databases()
        specs = {spec.name: spec for spec in canonical_database_specs(tmp_path / "data")}
        assert specs["model_usage"].path == recorder.database.path
        assert specs["model_usage"].profile == "canonical"
    finally:
        await runtime.aclose()
    assert recorder.database.is_closed()
    assert runtime.canonical_databases() == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("withdraw_readiness", [False, True])
async def test_extension_sampling_records_usage_before_returning(
    tmp_path, monkeypatch, withdraw_readiness
):
    runtime = Runtime(Config(data_dir=tmp_path / "data"), safe_startup_mode="test")
    runtime.start()
    recorder = runtime.usage_recorder
    try:

        async def send(*args, **kwargs):
            if withdraw_readiness:
                runtime._started = False
            return {}

        adapter = SimpleNamespace(
            send=AsyncMock(side_effect=send),
            normalize_response=lambda *args, **kwargs: {
                "content": "sample",
                "usage": {"input_tokens": 10, "output_tokens": 2},
            },
            request_context_kwargs=lambda **kwargs: {},
            aclose=AsyncMock(),
        )
        monkeypatch.setattr(runtime, "get_adapter", lambda _: adapter)
        monkeypatch.setattr(
            runtime, "_extension_tool_agent", lambda _: SimpleNamespace(temperature=0)
        )
        monkeypatch.setattr(
            "core.chat.model_resolution.resolve_agent_model_target",
            lambda *args: ("p", "m", "p:api"),
        )
        context = SimpleNamespace(
            agent_id="agent",
            session_id="session",
            project_id=None,
            run_id="run",
            execution_owner=SimpleNamespace(extension="fixture", group_id="group"),
        )
        result = await runtime._sample_extension(context, {"messages": [], "max_tokens": 100})
        assert result["model"] == "p/m"
        record = recorder.read_since()[1][0]
        assert record.kind == "extension_sampling"
        assert record.usage["input_tokens"] == 10
        assert record.run_id == "run"
        assert record.owner_name == "fixture"
        adapter.aclose.assert_awaited_once()
    finally:
        await runtime.aclose()
