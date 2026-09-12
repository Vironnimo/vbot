"""Runtime and persistence doubles shared by server RPC tests."""

from __future__ import annotations

import asyncio
import os
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
from core.memory import MemoryService
from core.providers.accounts import (
    DEFAULT_ACCOUNT_ID,
    ConnectionRef,
    ProviderAccount,
    account_id_from_credential_key,
    derive_credential_key,
    split_connection_id,
)
from core.runs import ChatRunManager, RunKind
from core.sessions.format import write_bootstrap_marker
from core.tools import FileReadState, ToolRegistry
from core.utils.errors import ConfigError
from server.events import ServerEventBus
from tests.core.chat.chat_loop_support import build_chat_loop
from tests.server.rpc_test_support_common import (
    StubAgent,
    StubAgentResolver,
    StubAgents,
    StubModels,
    StubProjects,
    StubProviders,
)
from tests.server.rpc_test_support_storage import (
    StubStorage,
)

__all__ = ["StubStorage"]

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
        effective_tool_names: object = None,
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
    ) -> list[JsonObject]:
        assert _agent.tool_access is not None
        return self._tools.provider_definitions(
            ["*"] if _agent.tool_access.mode == "all" else list(_agent.tool_access.allowed),
            session_grants=session_tool_grants,
        )


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
            StubSkill(name, f"{name} skill.") for name in self._runtime.storage._skill_directories
        ]

    def warnings_for(self, _name: str) -> list[str]:
        return []

    def availability_for(self, _name: str) -> Any:
        return SimpleNamespace(state="available", missing=(), optional_missing=())

    def invalid_diagnostics(self) -> list[Any]:
        return []


class StubAdapter:
    def __init__(
        self,
        responses: list[JsonObject] | None = None,
        *,
        stream_deltas: list[JsonObject] | None = None,
        block: bool = False,
    ) -> None:
        self._responses = responses or []
        self._stream_deltas = stream_deltas or []
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


class StubProcessManager:
    def cancel_scope(self, run_id: str) -> None:
        del run_id

    async def cancel_scope_async(self, run_id: str) -> None:
        del run_id


class RecordingCompactionService:
    def __init__(self) -> None:
        self.calls = 0

    @staticmethod
    def estimate_messages_tokens(_messages: list[JsonObject]) -> int:
        return 12_345

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


class StubRuntime:
    def __init__(self, tmp_path: Path, adapter: StubAdapter) -> None:
        self.resources_dir = tmp_path / "resources"
        self.storage = StubStorage(tmp_path)
        self.agents = StubAgents(
            StubAgent(id="coder", allowed_tools=["*"]),
            defaults_provider=lambda: self.storage.load_defaults().get("agent", {}),
        )
        self.memory = MemoryService()
        self.agent_resolver = StubAgentResolver(self.agents)
        self.projects = StubProjects()
        tmp_path.mkdir(parents=True, exist_ok=True)
        marker = tmp_path / "session-store.json"
        if not marker.exists():
            write_bootstrap_marker(tmp_path)
        self.chat_sessions = ChatSessionManager(tmp_path)
        self.file_read_state = FileReadState()
        self.tools = ToolRegistry()
        self.system_prompts = StubPrompts(self.tools)
        self.skills: Any = StubSkills()
        self._models = StubModels()
        self.providers = StubProviders()
        self.adapter = adapter
        self.chat_runs: ChatRunManager | None = None
        self.extensions: Any = None
        self.process_manager = StubProcessManager()
        self.terminal_manager = StubTerminalManager()
        self.trigger_service: Any = None
        self.recall_reload_count = 0
        self.extension_reload_count = 0
        self.extension_disabled_changes: list[set[str]] = []
        self.chat_loop = build_chat_loop(cast(Any, self))
        self.streaming_chat_loop = build_chat_loop(cast(Any, self), streaming=True)
        self.command_dispatcher = CommandDispatcher(
            self.chat_run_manager,
            agent_resolver=cast(Any, self.agent_resolver),
            sessions=self.chat_sessions,
            models=cast(Any, self._models),
            projects=cast(Any, self.projects),
            agents=cast(Any, self.agents),
            storage=cast(Any, self.storage),
            terminal_manager=cast(Any, self.terminal_manager),
        )

    @property
    def chat_run_manager(self) -> ChatRunManager:
        if self.chat_runs is None:
            self.chat_runs = ChatRunManager()
        return self.chat_runs

    def skills_for(self, _project_id: str | None = None, _agent_id: str | None = None) -> Any:
        return self.skills

    def project_skill_names(self, _project_id: str | None = None) -> frozenset[str]:
        return frozenset()

    def start(self) -> None:
        return None

    def stop(self) -> None:
        return None

    def get_adapter(self, connection: ConnectionRef) -> StubAdapter:
        return self.adapter

    @property
    def models(self) -> Any:
        return self._models

    def has_provider_credentials(self, provider_id: str) -> bool:
        provider = cast(Any, self.providers.get(provider_id))
        return any(
            bool(self._credential_value(connection.auth.credential_key))
            for connection in provider.connections
        )

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

    def _resolve_resources_path(self) -> Path:
        return self.resources_dir

    def reload_skills(self) -> None:
        self.skills = ReloadableStubRuntimeSkills(self)

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
    chat_runs = ChatRunManager()
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
        agent_delete_lock=asyncio.Lock(),
        server_bind={"listen_host": "127.0.0.1", "listen_port": 8420, "port_source": "default"},
    )


class StubDelegateRun:
    def __init__(
        self,
        *,
        run_id: str,
        agent_id: str,
        session_id: str,
        status: str,
        final_message: ChatMessage | None = None,
        iteration_count: int = 0,
        created_at: str = "2026-05-03T14:30:01+00:00",
    ) -> None:
        self.id = run_id
        self.agent_id = agent_id
        self.session_id = session_id
        self.status = SimpleNamespace(value=status)
        self.run_kind = RunKind.USER
        self.iteration_count = iteration_count
        self.created_at = created_at
        self.events: list[Any] = []
        self._final_message = final_message or ChatMessage.assistant(
            model="openai/gpt-5.2",
            content="OK",
        )

    async def wait(self) -> ChatMessage:
        return self._final_message

    def controls(self) -> dict:
        return {"compaction": "unavailable", "background_tool_call_ids": []}
