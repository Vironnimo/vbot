"""Runtime and persistence doubles shared by server RPC tests."""

from __future__ import annotations

import asyncio
import os
from collections import OrderedDict
from collections.abc import Callable, Collection, Mapping
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

from core.automation import ReflectionService, TriggerService
from core.chat import (
    ChatMessage,
    ChatSessionManager,
    CommandDispatcher,
)
from core.database import write_bootstrap_marker
from core.memory import MemoryService
from core.providers.accounts import (
    DEFAULT_ACCOUNT_ID,
    ConnectionRef,
    ProviderAccount,
    account_id_from_credential_key,
    derive_credential_key,
    split_connection_id,
)
from core.providers.reasoning import DEFAULT_REASONING_REPLAY_POLICY, ReasoningReplayPolicy
from core.runs import ChatRunManager
from core.runtime import AgentRenameOutcome, SettingsChangeEffects
from core.runtime._agent_rename import (
    AgentRenameServices,
    identity_agent_references,
    rename_identity_agent,
)
from core.runtime.runtime import Runtime
from core.storage import StorageManager
from core.tools import FileReadState, ToolRegistry
from core.utils.errors import ConfigError
from core.utils.version import BuildIdentity
from server.events import ServerEventBus
from server.file_delivery import FileDelivery
from tests.core.chat.chat_loop_support import build_chat_loop
from tests.core.providers.adapter_test_support import AdapterHookDefaults
from tests.server.rpc_test_support_common import (
    StubAgent,
    StubAgentResolver,
    StubAgents,
    StubModels,
    StubProjects,
    StubProviders,
)

JsonObject = dict[str, Any]


class StubPrompts:
    vbot_root = Path("app")

    def __init__(self, tools: ToolRegistry) -> None:
        self._tools = tools

    def validate_scope(self, scope: object = None) -> Any:
        # The preview handler validates an explicit scope through the manager; mirror
        # the real PromptScope shape the handler reads (``type`` / ``agent_id``).
        if isinstance(scope, dict) and scope.get("type") == "agent":
            return SimpleNamespace(type="agent", agent_id=scope.get("agent_id"))
        return SimpleNamespace(type="default", agent_id=None)

    def build_system_prompt(
        self,
        agent: StubAgent,
        scope: object = None,
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
        effective_tool_definitions: object = None,
        session_tool_grants: object = (),
        request_block_definitions: object = (),
    ) -> str:
        del agent_project_id, request_block_definitions
        if getattr(scope, "type", None) == "agent":
            scope_agent_id = getattr(scope, "agent_id", None)
            return f"Custom system for {scope_agent_id}"
        if scope is None and agent.custom_system_prompt_enabled:
            base = f"Effective custom system for {agent.id}"
        else:
            base = f"System for {agent.id}"
        # Echo the two project-preview inputs so wiring tests can assert they
        # reached the builder; both empty for an identity preview, leaving the
        # identity-path output byte-identical.
        extras = []
        if agent_body:
            extras.append(f"body={agent_body}")
        if project_context is not None:
            extras.append(f"project_cwd={getattr(project_context, 'cwd', '')}")
        if working_project_context is not None:
            extras.append(f"working_project={working_project_context}")
        return " ".join([base, *extras])

    def render_working_project_context(
        self,
        project_context: object,
        *,
        on_read: object = None,
    ) -> str:
        del on_read
        return str(getattr(project_context, "project_id", ""))

    def render_soul(self, _agent: StubAgent, *, on_read: object = None) -> str:
        return ""

    def render_memory_files(self, _agent: StubAgent, *, on_read: object = None) -> str:
        return ""

    def render_skill_catalog(self, _agent: StubAgent, skill_registry: object = None) -> Any:
        from core.prompts import PinnedSkillCatalog

        return PinnedSkillCatalog(catalog_text="")

    def provider_tool_definitions(
        self,
        _agent: StubAgent,
        *,
        skill_registry: object = None,
        skill_catalog: object = None,
        session_tool_grants: tuple[str, ...] = (),
        ready_only: bool = True,
    ) -> list[JsonObject]:
        assert _agent.tool_access is not None
        return self._tools.provider_definitions(
            ["*"] if _agent.tool_access.mode == "all" else list(_agent.tool_access.allowed),
            session_grants=session_tool_grants,
            ready_only=ready_only,
        )

    async def build_system_prompt_async(self, agent: StubAgent, **options: Any) -> str:
        return self.build_system_prompt(agent, **options)

    async def provider_tool_definitions_async(
        self, agent: StubAgent, **options: Any
    ) -> list[JsonObject]:
        return self.provider_tool_definitions(agent, **options)


@dataclass(frozen=True)
class StubSkill:
    name: str
    description: str


class StubSkills:
    def __init__(self) -> None:
        self._skills = [
            StubSkill("debugging", "Debug failures."),
            StubSkill("warned", "Loads with warnings."),
        ]
        self._warnings = {"debugging": [], "warned": ["Name does not match directory."]}
        self._invalid = [
            SimpleNamespace(
                name="broken",
                path=Path("/skills/broken/SKILL.md"),
                valid=False,
                warnings=["missing description"],
                loadable=False,
            )
        ]

    def list_all(self) -> list[StubSkill]:
        return list(self._skills)

    def warnings_for(self, name: str) -> list[str]:
        return list(self._warnings[name])

    def availability_for(self, _name: str) -> Any:
        return SimpleNamespace(state="available", missing=(), optional_missing=())

    def invalid_diagnostics(self) -> list[Any]:
        return list(self._invalid)


class ReloadableStubRuntimeSkills:
    def __init__(self, runtime: Any) -> None:
        self._runtime = runtime

    def list_all(self) -> list[StubSkill]:
        return [
            StubSkill(name, f"{name} skill.")
            for name in self._runtime.storage.load_skill_directory_settings()
        ]

    def warnings_for(self, _name: str) -> list[str]:
        return []

    def availability_for(self, _name: str) -> Any:
        return SimpleNamespace(state="available", missing=(), optional_missing=())

    def invalid_diagnostics(self) -> list[Any]:
        return []


class StubAdapter(AdapterHookDefaults):
    def __init__(
        self,
        responses: list[JsonObject] | None = None,
        *,
        stream_deltas: list[Any] | None = None,
        block: bool = False,
    ) -> None:
        """Answer ``send`` from ``responses`` and ``stream`` from ``stream_deltas``.

        ``stream_deltas`` is one delta list replayed for every stream, or one delta
        list per stream request.
        """

        self._responses = responses or []
        self._stream_deltas: list[Any] = stream_deltas or []
        self._block = block
        self.request_started = asyncio.Event()
        self.release = asyncio.Event()
        self.requests: list[JsonObject] = []
        self.stream_requests: list[JsonObject] = []

    async def send(self, messages: list[JsonObject], *, model_id: str, **kwargs: Any) -> JsonObject:
        self.requests.append(
            {"messages": deepcopy(messages), "model_id": model_id, "kwargs": deepcopy(kwargs)}
        )
        self.request_started.set()
        if self._block:
            await self.release.wait()
        if not self._responses:
            return {"content": "OK", "tool_calls": None}
        return self._responses.pop(0)

    def normalize_response(
        self, response: JsonObject, *, model_id: str | None = None
    ) -> JsonObject:
        return response

    def reasoning_replay_policy(self, _model_id: str) -> ReasoningReplayPolicy:
        return DEFAULT_REASONING_REPLAY_POLICY

    def wire_media_support(self, _model_id: str) -> frozenset[str]:
        return frozenset()

    async def stream(self, messages: list[JsonObject], *, model_id: str, **kwargs: Any) -> Any:
        self.stream_requests.append(
            {"messages": deepcopy(messages), "model_id": model_id, "kwargs": deepcopy(kwargs)}
        )
        self.request_started.set()
        if self._block:
            await self.release.wait()
        deltas = self._next_stream_deltas()
        for delta in deltas:
            yield deepcopy(delta)

    def _next_stream_deltas(self) -> list[JsonObject]:
        if self._stream_deltas and isinstance(self._stream_deltas[0], list):
            return cast(list[JsonObject], self._stream_deltas.pop(0))
        return cast(list[JsonObject], self._stream_deltas)


def _unsubscribe() -> None:
    return None


class StubProcessManager:
    async def cancel_scope_async(self, run_id: str) -> None:
        del run_id

    def release_scope(self, run_id: str) -> None:
        del run_id

    def add_terminal_callback(self, _callback: Callable[[Any], None]) -> Callable[[], None]:
        return _unsubscribe


class RecordingCompactionService:
    def __init__(self) -> None:
        self.calls = 0

    async def compact(self, *args: Any, **kwargs: Any) -> ChatMessage:
        self.calls += 1
        context_tokens_before = kwargs.get("context_tokens_before")
        return ChatMessage.compaction_checkpoint(
            summary="Compacted context",
            projection=[ChatMessage.user("tail")],
            compacted_token_count=1,
            context_tokens_before=context_tokens_before,
            context_tokens_after=1_234 if context_tokens_before is not None else None,
        )


class StubTerminalManager:
    async def close_scope(self, _owner: Any) -> None:
        return None

    async def close_agent_scope(self, _agent_id: str, _project_id: str | None) -> None:
        return None

    async def close_project_scope(self, _project_id: str) -> None:
        return None

    def transfer_scope(self, _source: Any, _target: Any) -> int:
        return 0

    def add_changed_callback(self, _callback: Callable[[str], None]) -> Callable[[], None]:
        return _unsubscribe


class StubJobService:
    """Cron or Bootstrap service double that holds no jobs."""

    def list_jobs(self) -> list[Any]:
        return []

    def add_changed_callback(self, _callback: Callable[[], None]) -> Callable[[], None]:
        return _unsubscribe


class StubChannelService:
    """Channel service double that holds no Channels."""

    def list_channels(self) -> list[Any]:
        return []

    async def retarget_agent_async(self, _agent_id: str, _new_agent_id: str) -> tuple[str, ...]:
        return ()


class StubCalendarActions:
    def list_actions(self) -> list[Any]:
        return []

    def retarget_identity(self, _old_agent_id: str, _new_agent_id: str) -> int:
        return 0


class StubCalendarService:
    """Calendar double that holds no actions."""

    def __init__(self) -> None:
        self.actions = StubCalendarActions()

    def add_changed_callback(self, _callback: Callable[[], None]) -> Callable[[], None]:
        return _unsubscribe


class StubStatisticsIndex:
    """Statistics index double: the startup warmup finds nothing to reconcile."""

    async def run_async(
        self, _function: Callable[..., Any], /, *_args: Any, **_kwargs: Any
    ) -> None:
        return None


class StubRuntime:
    def __init__(self, tmp_path: Path, adapter: StubAdapter) -> None:
        self._model_database_refresh_lock = asyncio.Lock()
        self.storage = StorageManager(tmp_path)
        self.build = BuildIdentity("0.4.4", "a" * 40, branch="main")
        self.agents = StubAgents(
            StubAgent(id="coder", allowed_tools=["*"]),
            defaults_provider=lambda: self.storage.load_defaults().get("agent", {}),
        )
        self.memory = MemoryService(history_root=self.storage.layout.agents)
        self.projects = StubProjects()
        tmp_path.mkdir(parents=True, exist_ok=True)
        marker = tmp_path / "data-store.json"
        if not marker.exists():
            write_bootstrap_marker(tmp_path)
        self.chat_sessions = ChatSessionManager(tmp_path)
        self.agent_resolver = StubAgentResolver(self.agents, sessions=self.chat_sessions)
        self.file_read_state = FileReadState()
        self.tools = ToolRegistry()
        self.system_prompts = StubPrompts(self.tools)
        self.skills: Any = StubSkills()
        self.models: Any = StubModels()
        self.providers = StubProviders()
        self.adapter = adapter
        self.chat_runs: ChatRunManager | None = None
        self.extensions: Any = None
        self.process_manager = StubProcessManager()
        self.terminal_manager = StubTerminalManager()
        self.cron_service: Any = StubJobService()
        self.bootstrap_service: Any = StubJobService()
        self.calendar_service: Any = StubCalendarService()
        self.channel_service: Any = StubChannelService()
        self.subagents: Any = SimpleNamespace(
            batch_tracker=SimpleNamespace(references_identity_agent=lambda _agent_id: False)
        )
        self.speech: Any = SimpleNamespace(preload_configured=_unsubscribe)
        self.statistics_index: Any = StubStatisticsIndex()
        self.usage_recorder: Any = None
        self.trigger_service: Any = None
        self.recall_reload_count = 0
        self.extension_reload_count = 0
        self.skill_changed_callbacks: list[Callable[[], None]] = []
        self.extension_disabled_changes: list[set[str]] = []
        self.chat_loop = build_chat_loop(cast(Any, self))
        self.streaming_chat_loop = build_chat_loop(cast(Any, self), streaming=True)
        self.command_dispatcher = CommandDispatcher(
            self.chat_run_manager,
            agent_resolver=cast(Any, self.agent_resolver),
            sessions=self.chat_sessions,
            models=cast(Any, self.models),
            projects=cast(Any, self.projects),
            agents=cast(Any, self.agents),
            storage=cast(Any, self.storage),
            terminal_manager=cast(Any, self.terminal_manager),
        )

    @property
    def chat_run_manager(self) -> ChatRunManager:
        if self.chat_runs is None:
            self.chat_runs = ChatRunManager(persistence=self.chat_sessions)
        return self.chat_runs

    async def rename_agent(self, agent_id: str, new_agent_id: str) -> AgentRenameOutcome:
        return await rename_identity_agent(self._rename_services(), agent_id, new_agent_id)

    def agent_references(self, agent_id: str) -> tuple[str, ...]:
        return identity_agent_references(self._rename_services(), agent_id)

    def _rename_services(self) -> AgentRenameServices:
        return AgentRenameServices(
            agents=cast(Any, self.agents),
            sessions=self.chat_sessions,
            channels=cast(Any, self.channel_service),
            cron=cast(Any, self.cron_service),
            bootstrap=cast(Any, self.bootstrap_service),
            calendar=cast(Any, self.calendar_service),
        )

    def skills_for(self, _project_id: str | None = None, _agent_id: str | None = None) -> Any:
        return self.skills

    def invalidate_agent_skills(self, _agent_id: str | None = None) -> None:
        return None

    def project_skill_names(self, _project_id: str | None = None) -> frozenset[str]:
        return frozenset()

    def add_skill_changed_callback(self, callback: Callable[[], None]) -> Callable[[], None]:
        self.skill_changed_callbacks.append(callback)
        return lambda: self.skill_changed_callbacks.remove(callback)

    def start(self) -> None:
        return None

    def stop(self) -> None:
        return None

    def get_adapter(self, connection: ConnectionRef) -> StubAdapter:
        return self.adapter

    def model_database_refresh(self) -> asyncio.Lock:
        return self._model_database_refresh_lock

    def has_provider_credentials(self, provider_id: str) -> bool:
        provider = cast(Any, self.providers.get(provider_id))
        return any(
            bool(self._credential_value(connection.auth.credential_key))
            for connection in provider.connections
        )

    def resolve_environment_credential(self, key: str) -> str:
        return self._credential_value(key)

    def environment_credential_source(self, key: str) -> str | None:
        if key in os.environ:
            return "process_environment"
        if key in self.storage.load_environment():
            return "data_dir"
        return None

    def _credential_value(self, key: str) -> str:
        if key in os.environ:
            return os.environ[key]
        return self.storage.load_environment().get(key, "")

    def _connection(self, provider_id: str, local_connection_id: str) -> Any:
        provider = cast(Any, self.providers.get(provider_id))
        return next(
            connection
            for connection in provider.connections
            if connection.id == local_connection_id
        )

    @property
    def provider_credentials(self) -> Any:
        runtime = self

        class CredentialResolver:
            def list_accounts(
                self, provider_id: str, local_connection_id: str
            ) -> list[ProviderAccount]:
                connection = runtime._connection(provider_id, local_connection_id)
                if getattr(connection, "oauth", None) is not None:
                    return []
                base_key = connection.auth.credential_key
                accounts: dict[str, ProviderAccount] = {}
                sources: list[tuple[str, dict[str, str]]] = [
                    ("process_env", dict(os.environ)),
                    ("data_dir", runtime.storage.load_environment()),
                ]
                for source, mapping in sources:
                    for env_key, value in mapping.items():
                        account_id = account_id_from_credential_key(base_key, env_key)
                        if account_id is None or account_id in accounts:
                            continue
                        accounts[account_id] = ProviderAccount(
                            id=account_id,
                            usable=bool(value),
                            source=source,
                            credential_key=derive_credential_key(base_key, account_id),
                        )
                return sorted(
                    accounts.values(),
                    key=lambda account: (account.id != DEFAULT_ACCOUNT_ID, account.id),
                )

            def has_credentials(self, provider_id: str, connection_id: str | None = None) -> bool:
                if connection_id is None:
                    return runtime.has_provider_credentials(provider_id)
                local_id, account_id = split_connection_id(provider_id, connection_id)
                accounts = self.list_accounts(provider_id, local_id)
                if account_id is None:
                    return any(account.usable for account in accounts)
                return any(account.id == account_id and account.usable for account in accounts)

            def is_connection_enabled(
                self, provider_id: str, connection_id: str | None = None
            ) -> bool:
                return True

            def is_usable(self, provider_id: str, connection_id: str | None = None) -> bool:
                return self.has_credentials(provider_id, connection_id)

            def resolve_account_id(
                self,
                provider_id: str,
                local_connection_id: str,
                account_id: str | None = None,
            ) -> str:
                accounts = self.list_accounts(provider_id, local_connection_id)
                if account_id is not None:
                    if any(account.id == account_id and account.usable for account in accounts):
                        return account_id
                    raise ConfigError(f"Provider account not usable: {account_id}")
                for account in accounts:
                    if account.usable:
                        return account.id
                # This lightweight runtime can inject a fake adapter without a
                # backing environment credential; keep that legacy test seam on
                # the implicit default account.
                return DEFAULT_ACCOUNT_ID

            def get_credentials(self, provider_id: str, connection_id: str | None = None) -> str:
                provider = cast(Any, runtime.providers.get(provider_id))
                if connection_id is None:
                    for connection in provider.connections:
                        credential = runtime._credential_value(connection.auth.credential_key)
                        if credential:
                            return credential
                    raise ConfigError(
                        f"Provider credentials not found for provider '{provider_id}'"
                    )
                local_id, account_id = split_connection_id(provider_id, connection_id)
                accounts = self.list_accounts(provider_id, local_id)
                if account_id is not None:
                    accounts = [account for account in accounts if account.id == account_id]
                for account in accounts:
                    if account.usable:
                        return runtime._credential_value(account.credential_key)
                raise ConfigError(f"Provider credentials not found for provider '{provider_id}'")

        return CredentialResolver()

    def _ensure_started(self) -> None:
        pass

    async def apply_settings_change(
        self,
        previous: Mapping[str, Any],
        current: Mapping[str, Any],
        *,
        refresh_sections: Collection[str] = (),
    ) -> SettingsChangeEffects:
        return await Runtime.apply_settings_change(
            cast(Runtime, self), previous, current, refresh_sections=refresh_sections
        )

    def reload_keep_awake(self) -> None:
        pass

    def reload_timezone(self) -> None:
        pass

    def reload_skills(self) -> None:
        self.skills = ReloadableStubRuntimeSkills(self)

    async def reload_skills_async(self) -> None:
        self.reload_skills()

    def reload_recall_backend(self) -> None:
        self.recall_reload_count += 1

    async def reload_extensions(self) -> None:
        self.extension_reload_count += 1

    async def apply_extension_disabled_change(self, newly_disabled: set[str]) -> None:
        self.extension_disabled_changes.append(set(newly_disabled))

    def reload_environment_credentials(self) -> None:
        return None


def make_state(
    tmp_path: Path,
    adapter: StubAdapter,
    *,
    compaction_service: Any | None = None,
) -> SimpleNamespace:
    runtime: Any = StubRuntime(tmp_path, adapter)
    chat_runs = ChatRunManager(persistence=runtime.chat_sessions)
    runtime.chat_runs = chat_runs
    chat_loop = build_chat_loop(runtime, compaction_service=compaction_service)
    streaming_chat_loop = build_chat_loop(
        runtime, streaming=True, compaction_service=compaction_service
    )
    runtime.streaming_chat_loop = streaming_chat_loop
    runtime.trigger_service = TriggerService(chat_loop, chat_runs, cast(Any, runtime))
    runtime.reflection = ReflectionService(cast(Any, runtime))
    return SimpleNamespace(
        runtime=runtime,
        chat_runs=chat_runs,
        chat_loop=chat_loop,
        streaming_chat_loop=streaming_chat_loop,
        command_dispatcher=CommandDispatcher(
            chat_runs,
            agent_resolver=cast(Any, runtime.agent_resolver),
            sessions=runtime.chat_sessions,
            models=cast(Any, runtime.models),
            projects=cast(Any, runtime.projects),
            agents=cast(Any, runtime.agents),
            trigger_service=runtime.trigger_service,
            reflection_service=runtime.reflection,
            storage=cast(Any, runtime.storage),
        ),
        event_bus=ServerEventBus(),
        run_event_bridge_run_ids=OrderedDict(),
        file_delivery=FileDelivery(),
        agent_delete_lock=asyncio.Lock(),
        server_bind={"listen_host": "127.0.0.1", "listen_port": 8420, "port_source": "default"},
    )
