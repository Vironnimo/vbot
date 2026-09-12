"""Tests for runtime integrations."""

import asyncio
import json
import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core.agents.agents import AgentStore
from core.channels import ChannelService
from core.providers.accounts import ConnectionRef
from core.recall import CanonicalSessionRecallBackend, RecallBackendRegistry, SqliteFtsRecallBackend
from core.runs import ChatRunManager, RunCancelledError
from core.runtime._configuration import _resolve_resources_path
from core.runtime.runtime import Runtime
from core.sessions import ChatSessionManager
from core.skills.skills import SkillRegistry
from core.subagents import SubAgentCoordinator
from core.tools import ToolAccess
from core.tools.file_state import FileReadState
from core.tools.tools import ToolRegistry
from core.utils.config import Config
from tests.core.chat.chat_loop_support import build_chat_loop
from tests.core.runtime.runtime_test_support import (
    _authorize_session_store,
)
from tests.core.runtime.runtime_test_support import (
    config as config,
)


def test_runtime_selects_sqlite_fts_recall_backend_by_default(config: Config) -> None:
    logging.getLogger("vbot").handlers = []
    runtime = Runtime(config)

    runtime.start()

    assert isinstance(runtime.recall_backend, SqliteFtsRecallBackend)


def test_runtime_selects_sqlite_recall_backend_from_settings(config: Config) -> None:
    logging.getLogger("vbot").handlers = []
    config.data_dir.mkdir(parents=True, exist_ok=True)
    _authorize_session_store(config.data_dir)
    config.data_dir.joinpath("settings.json").write_text(
        json.dumps({"recall": {"backend": "sqlite_fts"}}),
        encoding="utf-8",
    )
    runtime = Runtime(config)

    runtime.start()

    assert isinstance(runtime.recall_backend, SqliteFtsRecallBackend)


def test_runtime_unknown_recall_backend_falls_back_to_sqlite_fts(config: Config) -> None:
    logging.getLogger("vbot").handlers = []
    config.data_dir.mkdir(parents=True, exist_ok=True)
    _authorize_session_store(config.data_dir)
    config.data_dir.joinpath("settings.json").write_text(
        json.dumps({"recall": {"backend": "team_backend"}}),
        encoding="utf-8",
    )
    runtime = Runtime(config)

    runtime.start()

    assert isinstance(runtime.recall_backend, SqliteFtsRecallBackend)


def test_runtime_failing_recall_backend_factory_falls_back_to_sqlite_fts(
    config: Config,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logging.getLogger("vbot").handlers = []
    config.data_dir.mkdir(parents=True, exist_ok=True)
    _authorize_session_store(config.data_dir)
    config.data_dir.joinpath("settings.json").write_text(
        json.dumps({"recall": {"backend": "broken_backend"}}),
        encoding="utf-8",
    )
    registry = RecallBackendRegistry()
    registry.register(
        "canonical_scan",
        lambda context: CanonicalSessionRecallBackend(context.sessions),
    )
    registry.register("sqlite_fts", SqliteFtsRecallBackend)

    def create_broken_backend(_context: Any) -> Any:
        raise RuntimeError("broken derived index")

    registry.register("broken_backend", create_broken_backend)
    monkeypatch.setattr(
        RecallBackendRegistry,
        "with_builtins",
        classmethod(lambda _cls: registry),
    )
    runtime = Runtime(config)

    runtime.start()

    assert isinstance(runtime.recall_backend, SqliteFtsRecallBackend)


def test_runtime_resolve_environment_credential_prefers_process_env(
    config: Config,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config.data_dir.mkdir(parents=True, exist_ok=True)
    _authorize_session_store(config.data_dir)
    config.data_dir.joinpath(".env").write_text(
        "TELEGRAM_BOT_TOKEN_TG_ASSISTANT=fallback-token\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN_TG_ASSISTANT", "process-token")

    runtime = Runtime(config)
    runtime.start()

    assert (
        runtime.resolve_environment_credential("TELEGRAM_BOT_TOKEN_TG_ASSISTANT") == "process-token"
    )

    runtime.stop()


def test_runtime_resolve_environment_credential_uses_data_dir_fallback(
    config: Config,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config.data_dir.mkdir(parents=True, exist_ok=True)
    _authorize_session_store(config.data_dir)
    config.data_dir.joinpath(".env").write_text(
        "TELEGRAM_BOT_TOKEN_TG_ASSISTANT=fallback-token\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN_TG_ASSISTANT", raising=False)

    runtime = Runtime(config)
    runtime.start()

    assert (
        runtime.resolve_environment_credential("TELEGRAM_BOT_TOKEN_TG_ASSISTANT")
        == "fallback-token"
    )

    runtime.stop()


@pytest.mark.asyncio
async def test_runtime_start_does_not_crash_when_channel_adapter_cannot_start(
    config: Config,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN_TG_ASSISTANT", raising=False)
    runtime = Runtime(config)

    seed_agent_store = AgentStore(
        config.data_dir,
        template_dir=_resolve_resources_path(runtime.config) / "workspace-templates",  # noqa: SLF001
    )
    seed_agent_store.create("assistant", "Assistant")

    channel_dir = config.data_dir / "channels" / "tg-assistant"
    channel_dir.mkdir(parents=True, exist_ok=True)
    channel_dir.joinpath("channel.json").write_text(
        "\n".join(
            (
                "{",
                '  "id": "tg-assistant",',
                '  "platform": "telegram",',
                '  "agent_id": "assistant",',
                '  "dm_scope": "per_conversation",',
                '  "allowed_chat_ids": [12345],',
                '  "token_env_var": "TELEGRAM_BOT_TOKEN_TG_ASSISTANT",',
                '  "enabled": true',
                "}",
            )
        )
        + "\n",
        encoding="utf-8",
    )

    runtime.start()

    assert runtime.channel_service.has_active_channels() is False
    assert runtime.channel_service.is_failed("tg-assistant") is True
    failure_reason = runtime.channel_service.failure_reason("tg-assistant")
    assert failure_reason
    assert "TELEGRAM_BOT_TOKEN_TG_ASSISTANT" in failure_reason

    runtime.stop()


@pytest.mark.asyncio
async def test_runtime_start_does_not_crash_when_channel_agent_is_missing(
    config: Config,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN_TG_ASSISTANT", "test-token")
    channel_dir = config.data_dir / "channels" / "tg-assistant"
    channel_dir.mkdir(parents=True, exist_ok=True)
    _authorize_session_store(config.data_dir)
    channel_dir.joinpath("channel.json").write_text(
        "\n".join(
            (
                "{",
                '  "id": "tg-assistant",',
                '  "platform": "telegram",',
                '  "agent_id": "missing-agent",',
                '  "dm_scope": "per_conversation",',
                '  "allowed_chat_ids": [12345],',
                '  "token_env_var": "TELEGRAM_BOT_TOKEN_TG_ASSISTANT",',
                '  "enabled": true',
                "}",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    runtime = Runtime(config)

    runtime.start()

    assert runtime.agents.get("main").id == "main"
    assert runtime.channel_service.has_active_channels() is False
    assert runtime.channel_service.is_failed("tg-assistant") is True
    failure_reason = runtime.channel_service.failure_reason("tg-assistant")
    assert failure_reason
    assert "missing-agent" in failure_reason

    runtime.stop()


@pytest.mark.asyncio
async def test_runtime_start_registers_channel_send_when_enabled_channel_starts(
    config: Config,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = Runtime(config)

    seed_agent_store = AgentStore(
        config.data_dir,
        template_dir=_resolve_resources_path(runtime.config) / "workspace-templates",  # noqa: SLF001
    )
    seed_agent_store.create("assistant", "Assistant")

    channel_dir = config.data_dir / "channels" / "tg-assistant"
    channel_dir.mkdir(parents=True, exist_ok=True)
    channel_dir.joinpath("channel.json").write_text(
        "\n".join(
            (
                "{",
                '  "id": "tg-assistant",',
                '  "platform": "telegram",',
                '  "agent_id": "assistant",',
                '  "dm_scope": "per_conversation",',
                '  "allowed_chat_ids": [12345],',
                '  "token_env_var": "TELEGRAM_BOT_TOKEN_TG_ASSISTANT",',
                '  "enabled": true',
                "}",
            )
        )
        + "\n",
        encoding="utf-8",
    )

    adapter = _BlockingChannelAdapter()
    monkeypatch.setattr(ChannelService, "_create_adapter", lambda _service, _config: adapter)

    runtime.start()
    await asyncio.wait_for(adapter.started.wait(), timeout=1)

    tool_names = sorted(tool.name for tool in runtime.tools.list_tools())
    assert "channel_send" in tool_names
    assert runtime.channel_service.has_active_channels() is True

    runtime.stop()
    await asyncio.wait_for(adapter.stopped.wait(), timeout=1)
    await asyncio.sleep(0)


def test_runtime_registers_channel_send_for_enabled_channel_without_running_adapter(
    config: Config,
) -> None:
    runtime = Runtime(config)
    seed_agent_store = AgentStore(
        config.data_dir,
        template_dir=_resolve_resources_path(runtime.config) / "workspace-templates",  # noqa: SLF001
    )
    seed_agent_store.create("assistant", "Assistant")
    channel_dir = config.data_dir / "channels" / "tg-assistant"
    channel_dir.mkdir(parents=True, exist_ok=True)
    channel_dir.joinpath("channel.json").write_text(
        "\n".join(
            (
                "{",
                '  "id": "tg-assistant",',
                '  "platform": "telegram",',
                '  "agent_id": "assistant",',
                '  "dm_scope": "per_conversation",',
                '  "allowed_chat_ids": [12345],',
                '  "token_env_var": "TELEGRAM_BOT_TOKEN_TG_ASSISTANT",',
                '  "enabled": true',
                "}",
            )
        )
        + "\n",
        encoding="utf-8",
    )

    runtime.start()

    tool_names = sorted(tool.name for tool in runtime.tools.list_tools())
    assert "channel_send" in tool_names
    assert runtime.channel_service.has_enabled_channels() is True
    assert runtime.channel_service.has_active_channels() is False

    runtime.stop()


def test_runtime_registers_bash_and_process_tools(config: Config) -> None:
    """Runtime.start() registers host process tools backed by ProcessManager."""
    logging.getLogger("vbot").handlers = []
    runtime = Runtime(config)

    runtime.start()

    assert runtime.tools.get("bash").name == "bash"
    assert runtime.tools.get("process").name == "process"


def test_runtime_registers_subagent_tools(config: Config) -> None:
    """Runtime.start() registers sub-agent tools and owns their coordinator."""
    logging.getLogger("vbot").handlers = []
    runtime = Runtime(config)

    runtime.start()

    assert isinstance(runtime._subagent_coordinator, SubAgentCoordinator)  # noqa: SLF001
    assert runtime.tools.get("subagent").name == "subagent"


@pytest.mark.asyncio
async def test_runtime_process_manager_cancels_run_scoped_sessions(config: Config) -> None:
    """ProcessManager cancellation kills all processes associated with one Run."""
    logging.getLogger("vbot").handlers = []
    runtime = Runtime(config)
    runtime.start()
    try:
        process_manager = runtime.process_manager
        session_id = await process_manager.spawn(
            "run-one",
            "agent-one",
            [sys.executable, "-c", "import time; time.sleep(30)"],
            env={},
            cwd=config.data_dir,
        )

        process_manager.cancel_scope("run-one")
        poll_result = await process_manager.poll(session_id, "agent-one", timeout_ms=1000)

        assert poll_result["status"] == "killed"
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
async def test_chat_run_cancellation_calls_runtime_process_manager(tmp_path: Path) -> None:
    """ChatLoop wires Run cancellation to Runtime.process_manager.cancel_scope()."""
    adapter = _BlockingAdapter()
    process_manager = _RecordingProcessManager()
    runtime: Any = _ChatRuntimeStub(tmp_path, adapter, process_manager)
    runtime.chat_sessions.create("agent-one", session_id="session-one")
    chat_loop = build_chat_loop(runtime)

    run = await chat_loop.start_run("agent-one", "hello", session_id="session-one")
    await adapter.request_started.wait()
    run.request_cancel()

    with pytest.raises(RunCancelledError):
        await run.wait()

    assert process_manager.cancelled_scopes == [run.id]


class _BlockingChannelAdapter:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.stopped = asyncio.Event()

    async def start(self) -> None:
        self.started.set()
        await asyncio.Future()

    async def stop(self) -> None:
        self.stopped.set()

    async def send(self, _message: str, _platform_target: str) -> None:
        return


class _BlockingAdapter:
    def __init__(self) -> None:
        self.request_started = asyncio.Event()

    async def send(self, _messages: object, **_kwargs: object) -> dict[str, object]:
        self.request_started.set()
        await asyncio.Event().wait()
        return {"content": "unreachable", "tool_calls": None}

    def normalize_response(
        self, response: dict[str, object], *, model_id: str | None = None
    ) -> dict[str, object]:
        return response

    async def aclose(self) -> None:
        return None


class _RecordingProcessManager:
    def __init__(self) -> None:
        self.cancelled_scopes: list[str] = []

    def cancel_scope(self, scope_key: str) -> None:
        self.cancelled_scopes.append(scope_key)

    async def cancel_scope_async(self, scope_key: str) -> None:
        self.cancelled_scopes.append(scope_key)


class _ChatRuntimeStub:
    def __init__(
        self,
        tmp_path: Path,
        adapter: _BlockingAdapter,
        process_manager: _RecordingProcessManager,
    ) -> None:
        self.agents = _StubAgents()
        self.agent_resolver = _StubAgentResolver(self.agents)
        self.projects = _StubProjects()
        self.providers = _StubProviders()
        self.provider_credentials = _StubCredentials()
        _authorize_session_store(tmp_path)
        self.chat_sessions = ChatSessionManager(tmp_path)
        self.chat_runs = ChatRunManager()
        self.chat_run_manager = self.chat_runs
        self.extensions = None
        self.system_prompts = _StubPrompts()
        self.file_read_state = FileReadState()
        self.tools = ToolRegistry()
        self.storage = SimpleNamespace(data_dir=tmp_path)
        self._process_manager = process_manager
        self._adapter = adapter

    def get_adapter(self, connection: ConnectionRef) -> _BlockingAdapter:
        return self._adapter

    def skills_for(
        self, _project_id: str | None = None, _agent_id: str | None = None
    ) -> SkillRegistry:
        return SkillRegistry({})

    def project_skill_names(self, _project_id: str | None = None) -> frozenset[str]:
        return frozenset()

    @property
    def process_manager(self) -> _RecordingProcessManager:
        return self._process_manager


class _StubProjects:
    """Empty project store: this runtime stub registers no project, so the rooting
    lookup never matches and the visit list is always empty."""

    def get(self, project_id: str) -> object:
        raise KeyError(project_id)

    def list(self) -> list[object]:
        return []

    def find_by_cwd(self, _cwd: object) -> object | None:
        return None


class _StubAgents:
    def get(self, agent_id: str) -> object:
        return SimpleNamespace(
            id=agent_id,
            model="provider/model::default",
            temperature=0.0,
            thinking_effort="",
            tool_access=ToolAccess(mode="all"),
            allowed_skills=["*"],
            workspace="",
        )


class _StubAgentResolver:
    """Identity-only resolver seam for the chat-cancellation runtime stub."""

    def __init__(self, agents: _StubAgents) -> None:
        self._agents = agents

    def resolve_agent(self, _project_id: str | None, agent_id: str) -> object:
        return self._agents.get(agent_id)


class _StubProviders:
    def get(self, provider_id: str) -> object:
        return SimpleNamespace(id=provider_id)


class _StubCredentials:
    def has_credentials(self, _provider_id: str, _connection_id: str | None = None) -> bool:
        return True

    def is_connection_enabled(self, _provider_id: str, _connection_id: str | None = None) -> bool:
        return True

    def is_usable(self, _provider_id: str, _connection_id: str | None = None) -> bool:
        return True

    def resolve_account_id(
        self,
        _provider_id: str,
        _local_connection_id: str,
        account_id: str | None = None,
    ) -> str:
        return account_id or "default"


class _StubPrompts:
    def build_system_prompt(
        self,
        _agent: object,
        _scope: object = None,
        *,
        agent_body: str = "",
        project_context: object = None,
        working_project_context: str | None = None,
        soul_context: str | None = None,
        memory_files_context: str | None = None,
        agent_project_id: str | None = None,
        nesting_depth: int = 0,
        skill_registry: object = None,
        skill_catalog: object = None,
        read_paths: list[Path] | None = None,
        effective_tool_names: object = None,
        session_tool_grants: object = (),
        request_block_definitions: object = (),
    ) -> str:
        del agent_project_id, request_block_definitions
        return "System prompt"

    def render_soul(self, _agent: object, *, on_read: object = None) -> str:
        return ""

    def render_memory_files(self, _agent: object, *, on_read: object = None) -> str:
        return ""

    def render_skill_catalog(self, _agent: object, skill_registry: object = None) -> object:
        from core.prompts import PinnedSkillCatalog

        return PinnedSkillCatalog(catalog_text="")

    def provider_tool_definitions(
        self,
        _agent: object,
        *,
        skill_registry: object = None,
        skill_catalog: object = None,
        session_tool_grants: object = (),
    ) -> list[dict[str, object]]:
        return []
