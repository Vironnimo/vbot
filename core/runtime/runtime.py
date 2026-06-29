"""vBot runtime bootstrap.

The ``Runtime`` class is the single entry point that wires together
all core services and manages the application lifecycle.
"""

import asyncio
import os
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, cast

from core.agents.agents import AgentStore
from core.attachments import AttachmentStore
from core.automation import CronService, TriggerService
from core.channels import ChannelService
from core.chat import ChatLoop, CommandDispatcher
from core.chat.block_resolver import ContentBlockResolver
from core.compaction import CompactionService, SummarizationStrategy
from core.debug import DebugTraceStore, ProviderDebugRecorder
from core.extensions import ExtensionRegistry
from core.memory import MemoryService
from core.model_tasks import EmbeddingService, ImageService, SpeechService, TaskModelService
from core.models.models import Model, ModelRegistry
from core.projects import AgentResolver, ProjectStore, build_agent_resolver
from core.prompts import (
    AGENT_SCOPE_KEY_PREFIX,
    DEFAULT_SCOPE_KEY,
    BlockDefinition,
    BlockStore,
    LayoutEntry,
    PromptAgentStore,
    SkillPromptRegistry,
    SystemPromptManager,
)
from core.providers.accounts import DEFAULT_ACCOUNT_ID, split_connection_id
from core.providers.adapter import ModelLookup, ProviderAdapter
from core.providers.anthropic import AnthropicAdapter
from core.providers.credentials import ProviderCredentialResolver
from core.providers.github_copilot import GitHubCopilotAdapter
from core.providers.minimax import MiniMaxAdapter
from core.providers.mistral import MistralAdapter
from core.providers.openai import OpenAIAdapter
from core.providers.openai_compatible import OpenAICompatibleAdapter
from core.providers.opencode_go import OpenCodeGoAdapter
from core.providers.openrouter import OpenRouterAdapter
from core.providers.providers import ConnectionConfig, ProviderConfig, ProviderRegistry
from core.providers.token_getter import OAuthTokenGetter, StaticTokenGetter, TokenGetter
from core.providers.token_store import TokenStore
from core.recall import (
    DEFAULT_RECALL_BACKEND,
    RecallBackend,
    RecallBackendContext,
    RecallBackendRegistry,
    SupportsSessionRemoval,
)
from core.runs import ChatRunManager
from core.runtime.interfaces import (
    ConfigProtocol,
    LoggerProtocol,
    ProviderCredentialResolverProtocol,
)
from core.sessions import ChatSessionManager
from core.skills.authoring import SkillAuthoringService
from core.skills.skills import (
    SKILL_ORIGIN_AGENT,
    SKILL_ORIGIN_BUNDLED,
    SKILL_ORIGIN_GLOBAL,
    SkillMetadata,
    SkillRegistry,
    load_project_skill_registry,
    project_skill_origin,
    project_skills_dir,
    scan_project_skill_names,
    scan_skill_names,
)
from core.storage.storage import StorageManager
from core.subagents import SubAgentCoordinator
from core.tools import (
    FileReadState,
    register_bash_tool,
    register_edit_tool,
    register_glob_tool,
    register_grep_tool,
    register_homeassistant_tools,
    register_image_generation_tool,
    register_memory_tool,
    register_process_tool,
    register_read_tool,
    register_session_search_tool,
    register_skill_manage_tool,
    register_skill_tool,
    register_text_to_speech_tool,
    register_web_fetch_tool,
    register_web_search_tool,
    register_write_tool,
)
from core.tools.cron import register_cron_tool
from core.tools.process_manager import ProcessManager
from core.tools.status import register_status_tool
from core.tools.subagent import register_subagent_tools
from core.tools.tools import ToolPromptBlockRegistry, ToolRegistry
from core.utils.errors import ConfigError
from core.utils.logging import LogManager

# ---------------------------------------------------------------------------
# Project root / default resources directory
# ---------------------------------------------------------------------------

# Three directories up from this file (core/runtime/runtime.py) → project root.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_DEFAULT_RESOURCES_DIR = _PROJECT_ROOT / "resources"
_DEFAULT_APP_VERSION = "0.1.0"
_DEFAULT_ATTACHMENT_MAX_SIZE_BYTES = 20_971_520
_DEFAULT_SPEECH_UPLOAD_MAX_SIZE_BYTES = 20_971_520
_SKILLS_DIRNAME = "skills"
_AGENTS_DIRNAME = "agents"


@dataclass(frozen=True)
class _ProjectSkillBundle:
    """A project's merged skill registry plus the names of its own skills.

    Cached per ``project_id`` on the runtime, like the resolver's Team cache: built
    on miss and dropped on the same per-project triggers (project open, cwd change,
    project removal, global skill reload), so an open re-scans the repo into a fresh
    bundle. ``registry`` is the project-first merge of project + bundled skills (the
    ``skills_for`` answer); ``names`` is the project-owned skill set the resolver
    subtracts ``skills_project_disabled`` from.
    """

    registry: SkillRegistry
    names: frozenset[str]


class _StorageBlockBackend(Protocol):
    """The storage block surface the storage-backed ``BlockStore`` adapter bridges to.

    Phase 2's ``StorageManager`` exposes these with the storage scope convention:
    ``None`` = default, a bare ``"<agent-id>"`` = that agent's scope. Declared as a
    Protocol so the adapter depends on the read/write surface, not the concrete
    ``StorageManager``.
    """

    def read_block_layout(self, scope: str | None) -> list[LayoutEntry]:
        """Return a scope's saved block layout (``[]`` when none)."""
        ...

    def read_block_override(self, scope: str | None, block_id: str) -> str | None:
        """Return a block's saved override text in a scope (``None`` when absent)."""
        ...

    def write_block_layout(self, scope: str | None, entries: Sequence[LayoutEntry]) -> Path:
        """Atomically write a scope's ordered block layout."""
        ...

    def prune_block_layout(
        self,
        scope: str | None,
        entries: Sequence[LayoutEntry],
        known_ids: frozenset[str] | set[str],
    ) -> Path:
        """Write a scope's layout keeping only entries with a live definition."""
        ...

    def seed_agent_block_layout(
        self,
        agent_id: str,
        default_layout: Sequence[LayoutEntry],
        *,
        overwrite: bool = False,
    ) -> Path | None:
        """Seed an agent scope's block layout from the current default layout."""
        ...

    def write_block_override(self, scope: str | None, block_id: str, content: str) -> Path:
        """Atomically write a block's text override in a scope."""
        ...

    def remove_block_override(self, scope: str | None, block_id: str) -> bool:
        """Remove a block's text override in a scope (``True`` when one existed)."""
        ...


class _StorageManagerBlockStore:
    """Adapt the storage manager's block I/O to the prompts ``BlockStore``.

    This is the composition-root seam where the prompts-domain scope-key convention
    (``"default"`` / ``"agent:<id>"``) meets the storage-domain scope-token
    convention (``None`` / bare ``"<id>"``). It bridges **both** the method-name
    difference (``read_layout`` → ``read_block_layout``) and the scope translation,
    in one place, for the read **and** the write side. Every method routes its scope
    key through the single :meth:`_to_store_scope` translation so the two
    conventions never diverge.
    """

    def __init__(self, storage: _StorageBlockBackend) -> None:
        self._storage = storage

    def read_layout(self, scope_key: str) -> list[LayoutEntry]:
        return self._storage.read_block_layout(self._to_store_scope(scope_key))

    def read_block_override(self, scope_key: str, block_id: str) -> str | None:
        return self._storage.read_block_override(self._to_store_scope(scope_key), block_id)

    def write_layout(self, scope_key: str, entries: Sequence[LayoutEntry]) -> None:
        self._storage.write_block_layout(self._to_store_scope(scope_key), entries)

    def prune_layout(
        self, scope_key: str, entries: Sequence[LayoutEntry], known_ids: frozenset[str]
    ) -> None:
        self._storage.prune_block_layout(self._to_store_scope(scope_key), entries, known_ids)

    def seed_agent_layout(
        self, scope_key: str, default_layout: Sequence[LayoutEntry], *, overwrite: bool = False
    ) -> None:
        # Only an agent scope key seeds an agent layout; the storage method keys by
        # the bare agent id, so translate and pass it through.
        store_scope = self._to_store_scope(scope_key)
        if store_scope is None:
            return
        self._storage.seed_agent_block_layout(store_scope, default_layout, overwrite=overwrite)

    def write_block_override(self, scope_key: str, block_id: str, content: str) -> None:
        self._storage.write_block_override(self._to_store_scope(scope_key), block_id, content)

    def remove_block_override(self, scope_key: str, block_id: str) -> bool:
        return self._storage.remove_block_override(self._to_store_scope(scope_key), block_id)

    @staticmethod
    def _to_store_scope(scope_key: str) -> str | None:
        """Translate a prompts scope key to the storage scope token.

        ``"default"`` → ``None`` (the storage default scope); ``"agent:<id>"`` →
        the bare ``"<id>"`` the storage layer keys an agent scope by. Any other
        value is passed through unchanged as a defensive fallback.
        """
        if scope_key == DEFAULT_SCOPE_KEY:
            return None
        if scope_key.startswith(AGENT_SCOPE_KEY_PREFIX):
            return scope_key[len(AGENT_SCOPE_KEY_PREFIX) :]
        return scope_key


# ---------------------------------------------------------------------------
# Adapter factory mapping
# ---------------------------------------------------------------------------

_ADAPTER_MAP: dict[
    str,
    type[ProviderAdapter],
] = {
    "openai_compatible": OpenAICompatibleAdapter,
    "openai": OpenAIAdapter,
    "openrouter": OpenRouterAdapter,
    "minimax": MiniMaxAdapter,
    "mistral": MistralAdapter,
    "opencode_go": OpenCodeGoAdapter,
    "github_copilot": GitHubCopilotAdapter,
    "anthropic": AnthropicAdapter,
}


class Runtime:
    """Bootstraps and manages the vBot application lifecycle.

    Constructor injection via :class:`ConfigProtocol` keeps the
    runtime decoupled from any concrete configuration implementation.

    Usage::

        from core.runtime.runtime import Runtime
        from core.utils.config import Config

        runtime = Runtime(Config())
        runtime.start()
        # ... application runs ...
        runtime.stop()
    """

    def __init__(self, config: ConfigProtocol) -> None:
        """Initialise the runtime with injected configuration.

        Creates the core services (currently only ``LogManager``)
        using settings from *config*.

        Args:
            config: Any object satisfying :class:`ConfigProtocol`.
        """
        self._config: ConfigProtocol = config
        self._data_dir = self._resolve_data_dir()
        self._fallback_environment: dict[str, str] = {}
        log_level = config.get("LOG_LEVEL", "INFO")
        self._log_manager = LogManager(level=log_level, data_dir=self._data_dir)
        self.logger: LoggerProtocol | None = None
        self._started: bool = False
        self._providers: ProviderRegistry | None = None
        self._provider_credentials: ProviderCredentialResolverProtocol | None = None
        self._token_store: TokenStore | None = None
        self._models: ModelRegistry | None = None
        self._model_tasks: TaskModelService | None = None
        self._speech: SpeechService | None = None
        self._image: ImageService | None = None
        self._embeddings: EmbeddingService | None = None
        self._storage: StorageManager | None = None
        self._attachment_store: AttachmentStore | None = None
        self._speech_upload_max_size_bytes = _DEFAULT_SPEECH_UPLOAD_MAX_SIZE_BYTES
        self._agents: AgentStore | None = None
        self._tools: ToolRegistry | None = None
        self._tool_prompt_blocks: ToolPromptBlockRegistry | None = None
        self._memory_service: MemoryService | None = None
        self._process_manager: ProcessManager | None = None
        self._skills: SkillRegistry | None = None
        # Per-project merged skill registries + project-skill names, cached by
        # project id like the resolver's Team cache; ``skills_for`` / project skill
        # resolution build on miss and drop on project open, cwd change, project
        # removal, or a global skill reload.
        self._project_skills: dict[str, _ProjectSkillBundle] = {}
        # Agent-aware skill registries, cached by ``(project_id, agent_id)``. Each
        # layers an agent's own private skills (``<data_dir>/agents/<id>/skills``)
        # over the project/global pool and marks them always-allowed for that owner.
        # Dropped per agent on an agent skill write and per project on the same
        # triggers as the project cache (so the embedded project layer stays fresh).
        self._agent_skills: dict[tuple[str | None, str], SkillRegistry] = {}
        # Shared, validated skill-authoring write core (Phase 1), constructed at
        # start() with the bundled skills root as a protected target. Used by the
        # agent ``skill_manage`` tool and (later) the skill-mutation RPCs.
        self._skill_authoring: SkillAuthoringService | None = None
        self._extensions: ExtensionRegistry | None = None
        self._chat_sessions: ChatSessionManager | None = None
        self._projects: ProjectStore | None = None
        self._agent_resolver: AgentResolver | None = None
        self._recall_backend_registry: RecallBackendRegistry | None = None
        self._recall_backend: RecallBackend | None = None
        self._chat_run_manager: ChatRunManager | None = None
        self._command_dispatcher: CommandDispatcher | None = None
        self.chat_runs: ChatRunManager | None = None
        self._chat_loop: ChatLoop | None = None
        self._streaming_chat_loop: ChatLoop | None = None
        self._started_at: datetime | None = None
        self._trigger_service: TriggerService | None = None
        self._channel_service: ChannelService | None = None
        self._cron_service: CronService | None = None
        self._subagent_coordinator: SubAgentCoordinator | None = None
        self._system_prompts: SystemPromptManager | None = None

    def start(self) -> None:
        """Start the runtime and initialise all services.

        Creates the ``vbot.core`` logger, loads provider and model
        registries from the resources directory, and signals that the
        application is ready.  Idempotent — calling ``start()``
        more than once is a no-op (logged at debug level).
        """
        if self._started:
            logger = self._log_manager.get_logger("core")
            logger.debug("Runtime already started — skipping")
            return

        self._started_at = datetime.now(UTC)
        self.logger = self._log_manager.get_logger("core")
        self.logger.info("Runtime startup initiated")

        resources_path = self._resolve_resources_path()

        self._storage = StorageManager(config=self._config, resources_dir=resources_path)
        storage = self._storage
        if storage is None:
            raise RuntimeError("Storage service not available")
        self._storage.ensure_directories()
        settings = self._storage.load_settings()
        attachment_max_size_bytes = self._positive_size_setting(
            settings,
            key="attachment_max_size_bytes",
            default=_DEFAULT_ATTACHMENT_MAX_SIZE_BYTES,
        )
        self._speech_upload_max_size_bytes = self._positive_size_setting(
            settings,
            key="speech_upload_max_size_bytes",
            default=_DEFAULT_SPEECH_UPLOAD_MAX_SIZE_BYTES,
        )
        self._attachment_store = AttachmentStore(
            self._storage.data_dir,
            max_size_bytes=attachment_max_size_bytes,
        )
        data_dir_credentials = self._storage.load_environment()
        self._fallback_environment = dict(data_dir_credentials)
        self._storage.copy_prompt_fragments()

        self._providers = ProviderRegistry.load(resources_path)
        self._token_store = TokenStore(self._storage.data_dir)
        self._provider_credentials = ProviderCredentialResolver(
            self._providers,
            fallback_credentials=data_dir_credentials,
            token_store=self._token_store,
        )
        self._models = ModelRegistry.load(resources_path)
        self._model_tasks = TaskModelService(
            self._providers,
            self._models,
            self._provider_credentials,
            self._storage,
        )
        self._speech = SpeechService(self._model_tasks, self, self._storage.data_dir)
        self._image = ImageService(self._model_tasks, self, self._storage.data_dir)
        self._embeddings = EmbeddingService(self._model_tasks, self)
        self._agents = AgentStore(
            self._storage.data_dir,
            template_dir=resources_path / "workspace-templates",
            defaults_provider=lambda: storage.load_defaults().get("agent", {}),
        )
        self._process_manager = ProcessManager()
        self._start_process_manager()
        self._tools = ToolRegistry()
        # Tool-owned System Prompt block declarations (D6): the tool side of the
        # unified contributor path. No built-in tool declares a block today; the
        # seam exists so a tool's register_* can contribute prompt content the
        # runtime gathers and hands to the prompt manager (never importing tool
        # classes into the prompts domain).
        self._tool_prompt_blocks = ToolPromptBlockRegistry()
        self._memory_service = MemoryService()
        # One read-before-write guard shared by read/write/edit: read stamps each
        # file, write/edit refuse an unread or externally-changed file (file_state.py).
        self._file_state = FileReadState()
        register_read_tool(
            self._tools,
            attachment_store=self._attachment_store,
            speech_service=self._speech,
            file_state=self._file_state,
        )
        register_edit_tool(self._tools, file_state=self._file_state)
        register_glob_tool(self._tools)
        register_grep_tool(self._tools)
        register_write_tool(self._tools, file_state=self._file_state)
        register_memory_tool(self._tools, self._memory_service)
        register_web_fetch_tool(self._tools)
        register_web_search_tool(
            self._tools,
            self.resolve_environment_credential,
            self._storage.load_web_search_settings,
        )
        register_homeassistant_tools(self._tools, self.resolve_environment_credential)
        register_process_tool(self._tools, self._process_manager)
        register_text_to_speech_tool(self._tools, self._speech)
        register_image_generation_tool(self._tools, self._image)
        skill_scan_roots = self._skill_scan_roots(settings, resources_path)
        self._skills = SkillRegistry.load(
            skill_scan_roots[0],
            extra_dirs=skill_scan_roots[1:],
            environment=self._skill_environment(data_dir_credentials),
            origins=self._bundled_skill_origins(skill_scan_roots),
        )
        invalid_skill_count = len(self._skills.invalid_diagnostics())
        if invalid_skill_count > 0:
            self.logger.warning(
                "Loaded skills with %s invalid skill directories; "
                "see vbot.skills warnings for details",
                invalid_skill_count,
            )
        register_skill_tool(self._tools, self.skills_for)
        # The agent skill-authoring write core refuses the bundled skills root; the
        # ``skill_manage`` tool writes only the calling agent's private home.
        self._skill_authoring = SkillAuthoringService(
            protected_roots=[resources_path / _SKILLS_DIRNAME]
        )
        register_skill_manage_tool(
            self._tools,
            self._skill_authoring,
            self.agent_skills_dir,
            self.invalidate_agent_skills,
        )
        extension_dirs = self._extra_extension_directories(settings)
        disabled_extensions, extension_config = self._extension_load_options(settings)
        self._extensions = ExtensionRegistry.load(
            self._storage.data_dir / "extensions",
            extra_dirs=extension_dirs,
            disabled=disabled_extensions,
            config=extension_config,
        )
        failed_extension_count = len(self._extensions.diagnostics())
        if failed_extension_count > 0:
            self.logger.warning(
                "Loaded extensions with %s failed extensions; "
                "see vbot.extensions errors for details",
                failed_extension_count,
            )
        self._chat_sessions = ChatSessionManager(self._storage.data_dir)
        self._projects = ProjectStore(self._storage.data_dir)
        self._agent_resolver = build_agent_resolver(
            self._agents,
            self._projects,
            self._models,
            self._providers,
            self._provider_credentials,
            self._global_agent_defaults,
            project_skill_names=self.project_skill_names,
        )
        self._ensure_bootstrap_agent()
        recall_registry = self._build_recall_backend_registry()
        self._recall_backend_registry = recall_registry
        self._recall_backend = self._create_recall_backend(recall_registry)
        register_session_search_tool(self._tools, self._recall_backend)
        self._chat_run_manager = ChatRunManager()
        self._command_dispatcher = CommandDispatcher(
            self._chat_run_manager,
            agent_resolver=self._agent_resolver,
            sessions=self._chat_sessions,
            models=self._models,
            started_at=self._started_at,
            providers=self._providers,
            projects=self._projects,
            agents=self._agents,
        )
        self.chat_runs = self._chat_run_manager
        if self._attachment_store is None:
            raise RuntimeError("Attachment store not available")
        resolver = ContentBlockResolver(self._attachment_store, transcriber=self._speech)
        compaction_service = CompactionService(SummarizationStrategy())
        self._chat_loop = ChatLoop(
            self,
            streaming=False,
            attachment_resolver=resolver,
            compaction_service=compaction_service,
        )
        self._streaming_chat_loop = ChatLoop(
            self,
            streaming=True,
            attachment_resolver=resolver,
            compaction_service=compaction_service,
        )
        self._trigger_service = TriggerService(
            self._chat_loop,
            self._chat_run_manager,
            self,
            trigger_chat_loop=self._streaming_chat_loop,
        )
        self._channel_service = ChannelService(
            self._trigger_service,
            self._chat_sessions,
            agent_store=self._agents,
            data_root=self._storage.data_dir,
            credential_resolver=self.resolve_environment_credential,
            attachment_store=self._attachment_store,
            command_dispatcher=self._command_dispatcher,
        )
        self._channel_service._notify_tool_registration_changed_hook = (
            self._reload_channel_tool_if_started
        )
        self._start_channel_service()
        self._sync_channel_tool_registration()
        self._cron_service = CronService(self._trigger_service, self._storage.data_dir)
        self._start_cron_service()
        register_cron_tool(self._tools, self._cron_service)
        register_bash_tool(self._tools, self._process_manager, self._trigger_service)
        self._subagent_coordinator = SubAgentCoordinator(self, self._trigger_service)
        register_subagent_tools(self._tools, self._subagent_coordinator)
        register_status_tool(
            self._tools,
            self._agent_resolver,
            self._chat_sessions,
            self._models,
            self._chat_run_manager,
            self._started_at,
            self._providers,
            self._projects,
        )
        # Built-ins are all registered now; apply extension tools last so a
        # collision with any built-in name is skipped (built-in wins), right
        # before SystemPromptManager consumes the registry.
        if self._extensions is not None:
            self._extensions.apply_tools(self._tools)
        self._system_prompts = SystemPromptManager(
            self._storage,
            self._tools,
            cast(SkillPromptRegistry, self._skills),
            channel_registry=cast(ChannelService, self._channel_service),
            app_version=str(self._config.get("APP_VERSION", _DEFAULT_APP_VERSION)),
            app_dir=_PROJECT_ROOT,
            data_root=self._storage.data_dir,
            memory_provider=self._memory_service,
            block_definitions=self._collect_prompt_block_definitions(),
            loaded_extensions=self._loaded_extension_names(),
            block_store=self._resolve_prompt_block_store(),
            agent_store=cast(PromptAgentStore, self._agents),
        )

        self._log_startup_inventory()
        self._started = True
        self.logger.info("Runtime started")

    async def fire_extension_startup(self) -> None:
        """Fire extension startup handlers once bootstrap is complete and serving.

        Called by the server from inside its async lifespan, so startup handlers
        run on the live serving loop (they may schedule background tasks there).
        No-op before ``start()`` / after shutdown.
        """
        if self._extensions is not None:
            await self._extensions.fire_startup()

    def stop(self) -> None:
        """Gracefully shut down the runtime.

        Logs the shutdown event and performs cleanup.
        """
        self._log_shutdown()
        self._started = False

        if self._extensions is not None:
            self._extensions.fire_shutdown_blocking()

        if self._channel_service is not None:
            self._channel_service.stop()
        if self._cron_service is not None:
            self._cron_service.stop()
        if self._process_manager is not None:
            self._process_manager.stop()

        self._clear_service_references()
        self._log_manager.close()

    async def aclose(self) -> None:
        """Gracefully shut down the runtime and await async service cleanup."""
        self._log_shutdown()
        self._started = False

        if self._extensions is not None:
            await self._extensions.fire_shutdown()

        if self._channel_service is not None:
            await self._channel_service.aclose()
        if self._cron_service is not None:
            await self._cron_service.aclose()
        if self._process_manager is not None:
            await self._process_manager.aclose()

        self._clear_service_references()
        self._log_manager.close()

    def _log_shutdown(self) -> None:
        if self.logger is not None:
            self.logger.info("Runtime stopped")

    def _clear_service_references(self) -> None:
        self._providers = None
        self._provider_credentials = None
        self._token_store = None
        self._fallback_environment = {}
        self._models = None
        self._model_tasks = None
        self._speech = None
        self._image = None
        self._embeddings = None
        self._storage = None
        self._attachment_store = None
        self._agents = None
        self._tools = None
        self._memory_service = None
        self._process_manager = None
        self._skills = None
        self._project_skills = {}
        self._agent_skills = {}
        self._skill_authoring = None
        self._extensions = None
        self._chat_sessions = None
        self._projects = None
        self._agent_resolver = None
        self._recall_backend_registry = None
        self._recall_backend = None
        self._channel_service = None
        self._cron_service = None
        self._trigger_service = None
        self._subagent_coordinator = None
        self._chat_loop = None
        self._streaming_chat_loop = None
        self._command_dispatcher = None
        self._chat_run_manager = None
        self.chat_runs = None
        self._system_prompts = None

    def _resolve_resources_path(self) -> Path:
        resources_path_raw = self._config.get("RESOURCES_PATH")
        if resources_path_raw is not None:
            return Path(resources_path_raw)
        return _DEFAULT_RESOURCES_DIR

    def _resolve_data_dir(self) -> Path:
        data_dir_raw = self._config.get("DATA_DIR") or self._config.get("VBOT_DATA_DIR")
        if data_dir_raw:
            return Path(cast(str, data_dir_raw)).expanduser()
        if hasattr(self._config, "data_dir"):
            return Path(cast(Any, self._config).data_dir).expanduser()
        raise ConfigError("Runtime requires a data directory to initialize logging")

    def _ensure_bootstrap_agent(self) -> None:
        if self._agents is None:
            raise RuntimeError("Agent service not available")
        if not self._agents.list():
            self._agents.create("main", "Main")

    def _log_startup_inventory(self) -> None:
        if (
            self.logger is None
            or self._providers is None
            or self._provider_credentials is None
            or self._tools is None
            or self._skills is None
        ):
            return

        provider_ids = self._providers.list_ids()
        usable_provider_count = 0
        total_connection_count = 0
        usable_connection_count = 0

        for provider_id in provider_ids:
            provider_config = self._providers.get(provider_id)
            provider_is_usable = False

            for connection in provider_config.connections:
                total_connection_count += 1
                connection_id = f"{provider_id}:{connection.id}"
                if self._provider_credentials.has_credentials(provider_id, connection_id):
                    usable_connection_count += 1
                    provider_is_usable = True

            if provider_is_usable:
                usable_provider_count += 1

        self.logger.info(
            "Runtime inventory: %s tools, %s skills, %s/%s usable providers, "
            "%s/%s usable connections",
            len(self._tools.list_tools()),
            len(self._skills.list_all()),
            usable_provider_count,
            len(provider_ids),
            usable_connection_count,
            total_connection_count,
        )

    def _positive_size_setting(self, settings: dict[str, object], *, key: str, default: int) -> int:
        raw_limit = settings.get(key, default)
        if isinstance(raw_limit, int) and not isinstance(raw_limit, bool) and raw_limit > 0:
            return raw_limit
        if self.logger is not None:
            self.logger.warning(
                "settings.%s must be a positive integer; using default %s",
                key,
                default,
            )
        return default

    def _extra_skill_directories(self, settings: dict[str, object]) -> list[Path]:
        raw_directories = settings.get("skill_directories", [])
        if not isinstance(raw_directories, list):
            if self.logger is not None:
                cast(Any, self.logger).warning(
                    "settings.skill_directories must be a list; ignoring value"
                )
            return []

        directories: list[Path] = []
        for raw_directory in raw_directories:
            if not isinstance(raw_directory, str) or not raw_directory.strip():
                if self.logger is not None:
                    cast(Any, self.logger).warning(
                        "Ignoring invalid skill directory setting: %r", raw_directory
                    )
                continue
            directories.append(Path(raw_directory).expanduser())
        return directories

    def _extra_extension_directories(self, settings: dict[str, object]) -> list[Path]:
        raw_directories = settings.get("extension_directories", [])
        if not isinstance(raw_directories, list):
            if self.logger is not None:
                cast(Any, self.logger).warning(
                    "settings.extension_directories must be a list; ignoring value"
                )
            return []

        directories: list[Path] = []
        for raw_directory in raw_directories:
            if not isinstance(raw_directory, str) or not raw_directory.strip():
                if self.logger is not None:
                    cast(Any, self.logger).warning(
                        "Ignoring invalid extension directory setting: %r", raw_directory
                    )
                continue
            directories.append(Path(raw_directory).expanduser())
        return directories

    def _extension_load_options(
        self, settings: dict[str, object]
    ) -> tuple[set[str], dict[str, dict[str, object]]]:
        """Read the disabled set and per-extension config from ``settings.extensions``.

        Settings are validated before runtime reads them, so this defensive
        parse mirrors ``_extra_extension_directories`` and normalizes shape
        without re-validating: malformed pieces are ignored with a warning.
        """
        raw = settings.get("extensions")
        if raw is None:
            return set(), {}
        if not isinstance(raw, dict):
            if self.logger is not None:
                cast(Any, self.logger).warning(
                    "settings.extensions must be an object; ignoring value"
                )
            return set(), {}

        disabled: set[str] = set()
        raw_disabled = raw.get("disabled", [])
        if isinstance(raw_disabled, list):
            for item in raw_disabled:
                if isinstance(item, str) and item.strip():
                    disabled.add(item)
        elif self.logger is not None:
            cast(Any, self.logger).warning(
                "settings.extensions.disabled must be a list; ignoring value"
            )

        config: dict[str, dict[str, object]] = {}
        raw_config = raw.get("config", {})
        if isinstance(raw_config, dict):
            for name, value in raw_config.items():
                if isinstance(name, str) and isinstance(value, dict):
                    config[name] = value
        elif self.logger is not None:
            cast(Any, self.logger).warning(
                "settings.extensions.config must be an object; ignoring value"
            )

        return disabled, config

    def _start_process_manager(self) -> None:
        if self._process_manager is None:
            raise RuntimeError("Process manager service not available")
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        self._process_manager.start()

    def _start_cron_service(self) -> None:
        if self._cron_service is None:
            raise RuntimeError("Cron service not available")
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        self._cron_service.start()

    def _start_channel_service(self) -> None:
        if self._channel_service is None:
            raise RuntimeError("Channel service not available")
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        self._channel_service.start()

    def resolve_environment_credential(self, key: str) -> str:
        """Resolve one environment credential using runtime precedence rules."""
        if key in os.environ:
            return os.environ[key]
        return self._fallback_environment.get(key, "")

    def _global_agent_defaults(self) -> dict[str, Any]:
        """Return the instance-wide ``defaults.agent`` map, or ``{}`` when unset.

        Read live from persisted ``defaults.agent`` so the resolver's chains
        (agent → project default → **global**) for model, temperature, and
        thinking effort always see the current values without a restart. Mirrors
        how ``AgentStore`` reads agent defaults; one read per resolve feeds all
        three chains.
        """
        if self._storage is None:
            return {}
        agent_defaults = self._storage.load_defaults().get("agent", {})
        return agent_defaults if isinstance(agent_defaults, dict) else {}

    def _skill_environment(self, fallback_environment: dict[str, str]) -> dict[str, str]:
        environment = dict(fallback_environment)
        environment.update(os.environ)
        return environment

    def _skill_scan_roots(self, settings: dict[str, object], resources_path: Path) -> list[Path]:
        """Return the ordered bundled skill scan roots, data dir first.

        One source of the bundled skill roots so the global registry and every
        project-scoped registry scan exactly the same directories
        (``<data_dir>/skills``, the bundled ``resources/skills``, then the
        settings-configured extras). A project registry prepends its own
        ``.opencode/skills`` ahead of these.
        """
        if self._storage is None:
            raise RuntimeError("Storage service not available")
        return [
            self._storage.data_dir / _SKILLS_DIRNAME,
            resources_path / _SKILLS_DIRNAME,
            *self._extra_skill_directories(settings),
        ]

    @staticmethod
    def _bundled_skill_origins(scan_roots: list[Path]) -> list[str | None]:
        """Origin tags parallel to ``_skill_scan_roots``: data-dir global, then bundled.

        The first root is the data-dir global pool, the second the shipped bundled
        pool; any configured extra ``skill_directories`` after them are user-curated,
        so they are tagged global too.
        """
        origins: list[str | None] = [SKILL_ORIGIN_GLOBAL, SKILL_ORIGIN_BUNDLED]
        origins.extend(SKILL_ORIGIN_GLOBAL for _ in scan_roots[2:])
        return origins

    def agent_skills_dir(self, agent_id: str) -> Path:
        """Return an agent's private skill home (``<data_dir>/agents/<id>/skills``)."""
        if self._storage is None:
            raise RuntimeError("Storage service not available")
        return self._storage.data_dir / _AGENTS_DIRNAME / agent_id / _SKILLS_DIRNAME

    @property
    def global_skills_dir(self) -> Path:
        """Return the user-curated global skills directory (``<data_dir>/skills``)."""
        if self._storage is None:
            raise RuntimeError("Storage service not available")
        return self._storage.data_dir / _SKILLS_DIRNAME

    def skills_for(self, project_id: str | None, agent_id: str | None = None) -> SkillRegistry:
        """Return the skill registry a run should use, scoped to project and agent.

        ``project_id is None`` and ``agent_id is None`` (a plain identity run) returns
        the global registry byte-for-byte. A set ``project_id`` returns the project's
        merged registry — the project's own ``.opencode/skills`` first, then the
        bundled pool. When ``agent_id`` names an agent that has its own private skills
        home, that home is layered on top (agent > project > global > bundled) and the
        agent's own skills are always-allowed for it; an agent with no private skills
        falls through to the project/global path unchanged. This is the single seam
        every run-time skill consumer (prompt assembly, triggers, the ``skill`` tool,
        autocomplete) resolves through, so scoping lives in exactly one place.
        """
        self._ensure_started()
        if agent_id is not None and self.agent_skills_dir(agent_id).is_dir():
            return self._agent_skill_registry(project_id, agent_id)
        if project_id is None:
            return self.skills
        return self._project_skill_bundle(project_id).registry

    def project_own_skills(self, project_id: str) -> list[SkillMetadata]:
        """Return a project's own skills (name/description/path) for the visit reminder.

        Scans only the project's ``.opencode/skills`` directory, so the result is
        exactly the project-owned skills with their ``SKILL.md`` paths — a visiting
        agent reads those files directly with the ``read`` tool. A missing directory
        yields an empty list.
        """
        self._ensure_started()
        project = self.projects.get(project_id)
        environment = self._skill_environment(self.storage.load_environment())
        registry = SkillRegistry.load(
            project_skills_dir(Path(project.cwd)), environment=environment
        )
        return registry.list_all()

    def project_skill_names(self, project_id: str | None) -> frozenset[str]:
        """Return the names of a project's own scanned skills (empty for identity).

        The resolver uses this to compute a config agent's effective skills
        ``(project skills − disabled) ∪ enabled-bundled``. Cached with the project's
        merged registry so it does not re-scan the repo every resolve.
        """
        self._ensure_started()
        if project_id is None:
            return frozenset()
        return self._project_skill_bundle(project_id).names

    def invalidate_project_skills(self, project_id: str | None = None) -> None:
        """Drop the cached project skills for one project, or for all when ``None``.

        Agent-aware registries embed the project layer, so this also drops the
        cached agent registries for that project (or all of them when ``None``) to
        keep them coherent with the project pool.
        """
        if project_id is None:
            self._project_skills.clear()
            self._agent_skills.clear()
            return
        self._project_skills.pop(project_id, None)
        self._drop_agent_skills(lambda key: key[0] == project_id)

    def invalidate_agent_skills(self, agent_id: str | None = None) -> None:
        """Drop the cached agent skills for one agent, or for all when ``None``.

        Called after an agent's private skill home changes (a skill write) so the
        next run rebuilds that agent's registry against the new pool. Drops only
        that agent's cached registries across every project context it ran in.
        """
        if agent_id is None:
            self._agent_skills.clear()
            return
        self._drop_agent_skills(lambda key: key[1] == agent_id)

    def _drop_agent_skills(self, predicate: Callable[[tuple[str | None, str]], bool]) -> None:
        for key in [key for key in self._agent_skills if predicate(key)]:
            del self._agent_skills[key]

    def _agent_skill_registry(self, project_id: str | None, agent_id: str) -> SkillRegistry:
        key = (project_id, agent_id)
        cached = self._agent_skills.get(key)
        if cached is not None:
            return cached
        registry = self._build_agent_skill_registry(project_id, agent_id)
        self._agent_skills[key] = registry
        return registry

    def _build_agent_skill_registry(self, project_id: str | None, agent_id: str) -> SkillRegistry:
        settings = self.storage.load_settings()
        environment = self._skill_environment(self.storage.load_environment())
        agent_root = self.agent_skills_dir(agent_id)
        scan_roots = self._skill_scan_roots(settings, self._resolve_resources_path())
        roots: list[Path] = [agent_root]
        origins: list[str | None] = [SKILL_ORIGIN_AGENT]
        if project_id is not None:
            project = self.projects.get(project_id)
            roots.append(project_skills_dir(Path(project.cwd)))
            origins.append(project_skill_origin(project.display_name))
        roots.extend(scan_roots)
        origins.extend(self._bundled_skill_origins(scan_roots))
        # First-found-wins ordering makes agent skills win over project, project over
        # bundled. The agent's own skills are always-allowed for it, so they bypass
        # the owner's ``allowed_skills`` filter without leaking to other agents
        # (whose registries never scan this home).
        agent_own_names = scan_skill_names(agent_root, environment)
        return SkillRegistry.load(
            roots[0],
            extra_dirs=roots[1:],
            environment=environment,
            always_allowed=agent_own_names,
            origins=origins,
        )

    def _project_skill_bundle(self, project_id: str) -> _ProjectSkillBundle:
        cached = self._project_skills.get(project_id)
        if cached is not None:
            return cached
        bundle = self._build_project_skill_bundle(project_id)
        self._project_skills[project_id] = bundle
        return bundle

    def _build_project_skill_bundle(self, project_id: str) -> _ProjectSkillBundle:
        project = self.projects.get(project_id)
        project_cwd = Path(project.cwd)
        settings = self.storage.load_settings()
        scan_roots = self._skill_scan_roots(settings, self._resolve_resources_path())
        environment = self._skill_environment(self.storage.load_environment())
        registry = load_project_skill_registry(
            project_cwd,
            scan_roots,
            environment,
            project_origin=project_skill_origin(project.display_name),
            bundled_origins=self._bundled_skill_origins(scan_roots),
        )
        names = scan_project_skill_names(project_cwd, environment)
        return _ProjectSkillBundle(registry=registry, names=names)

    def _reload_channel_tool_if_started(self) -> None:
        if not self._started:
            return
        self.reload_channel_tool()

    def _sync_channel_tool_registration(self) -> None:
        if self._tools is None:
            raise RuntimeError("Tool service not available")
        if self._channel_service is None:
            raise RuntimeError("Channel service not available")
        if self._chat_sessions is None:
            raise RuntimeError("Chat session service not available")
        if self._attachment_store is None:
            raise RuntimeError("Attachment store not available")

        self._tools.unregister("channel_send")
        if not self._channel_service.has_active_channels():
            return

        try:
            from core.tools.channel import register_channel_send_tool
        except ModuleNotFoundError as error:
            raise RuntimeError("Channel tool registration is unavailable") from error

        register_channel_send_tool(
            self._tools,
            self._channel_service,
            self._chat_sessions,
            max_attachment_size_bytes=self._attachment_store.max_size_bytes,
        )

    def _build_recall_backend_registry(self) -> RecallBackendRegistry:
        """Build a builtins registry with extension recall backends applied.

        Extension declarations were collected during extension load, so a fresh
        ``with_builtins()`` registry plus ``apply_recall_backends`` yields the
        same backend set on first build and on every ``reload_recall_backend``.
        """
        registry = RecallBackendRegistry.with_builtins()
        if self._extensions is not None:
            self._extensions.apply_recall_backends(registry)
        return registry

    def _create_recall_backend(self, registry: RecallBackendRegistry) -> RecallBackend:
        if self._storage is None:
            raise RuntimeError("Storage service not available")
        if self._chat_sessions is None:
            raise RuntimeError("Chat session service not available")

        settings = self._storage.load_recall_settings()
        backend_name = settings["backend"]
        context = RecallBackendContext(
            data_dir=self._storage.data_dir,
            sessions=self._chat_sessions,
            logger=self.logger,
            embeddings=self._embeddings,
            model_registry=self._models,
        )
        try:
            return registry.create(backend_name, context)
        except KeyError:
            if self.logger is not None:
                self.logger.warning(
                    "Unknown recall backend %r; using %s",
                    backend_name,
                    DEFAULT_RECALL_BACKEND,
                )
            return registry.create(DEFAULT_RECALL_BACKEND, context)

    def reload_channel_tool(self) -> None:
        """Re-register channel_send based on current active channel adapters."""
        self._ensure_started()
        self._sync_channel_tool_registration()

    def reload_recall_backend(self) -> None:
        """Reload session_search from the current persisted recall backend setting.

        Rebuilds the registry from ``with_builtins()`` and re-applies extension
        recall backends, so a live backend switch can still resolve an
        extension-registered backend.
        """
        self._ensure_started()
        recall_registry = self._build_recall_backend_registry()
        self._recall_backend_registry = recall_registry
        self._recall_backend = self._create_recall_backend(recall_registry)
        if self._tools is not None:
            self._tools.unregister("session_search")
            register_session_search_tool(self._tools, self._recall_backend)

    def available_recall_backends(self) -> list[str]:
        """Return all selectable recall backend names (built-ins + extensions)."""
        self._ensure_started()
        if self._recall_backend_registry is None:
            raise RuntimeError("Recall backend registry not available")
        return self._recall_backend_registry.names()

    def remove_session_from_recall(
        self, agent_id: str, session_id: str, project_id: str | None = None
    ) -> None:
        """Evict a removed session from the active recall index (best-effort).

        Session deletion calls this so a deleted session stops surfacing in
        search immediately, rather than waiting for the next self-healing
        reconcile. Backends without a derived index (the JSONL live scan) do not
        implement removal and are skipped. Index cleanup is non-fatal — the index
        is disposable and reconciles on the next search — so an index I/O error is
        logged and swallowed instead of failing the delete.
        """
        self._ensure_started()
        backend = self._recall_backend
        if not isinstance(backend, SupportsSessionRemoval):
            return
        try:
            backend.remove_session(agent_id, session_id, project_id)
        except (OSError, sqlite3.Error) as error:
            if self.logger is not None:
                self.logger.warning(
                    "Recall index cleanup failed for session %s/%s: %s",
                    agent_id,
                    session_id,
                    error,
                )

    def reload_skills(self) -> None:
        """Reload the runtime skill registry from current persisted settings."""
        self._ensure_started()
        settings = self.storage.load_settings()
        resources_path = self._resolve_resources_path()
        skill_scan_roots = self._skill_scan_roots(settings, resources_path)
        self._skills = SkillRegistry.load(
            skill_scan_roots[0],
            extra_dirs=skill_scan_roots[1:],
            environment=self._skill_environment(self.storage.load_environment()),
            origins=self._bundled_skill_origins(skill_scan_roots),
        )
        # Project- and agent-scoped registries merge in the same bundled roots, so a
        # global skill reload makes every cached project *and* agent registry stale —
        # invalidate_project_skills() with no project drops both caches so the next
        # run rebuilds against the fresh pool.
        self.invalidate_project_skills()
        invalid_skill_count = len(self._skills.invalid_diagnostics())
        if self.logger is not None:
            self.logger.info("Reloaded skill registry")
            if invalid_skill_count > 0:
                self.logger.warning(
                    "Reloaded skills with %s invalid skill directories; "
                    "see vbot.skills warnings for details",
                    invalid_skill_count,
                )
        if self._tools is not None:
            self._tools.unregister("skill")
            register_skill_tool(self._tools, self.skills_for)
            if self._skill_authoring is not None:
                self._tools.unregister("skill_manage")
                register_skill_manage_tool(
                    self._tools,
                    self._skill_authoring,
                    self.agent_skills_dir,
                    self.invalidate_agent_skills,
                )
        if self._system_prompts is not None:
            self._system_prompts.update_skill_registry(cast(SkillPromptRegistry, self._skills))
            self._refresh_prompt_block_definitions()

    def _collect_prompt_block_definitions(self) -> list[BlockDefinition]:
        """Gather the contributed block definitions (tool + extension blocks).

        The runtime side of the unified contributor path (D6): it merges the
        tool-owned blocks (from :class:`ToolPromptBlockRegistry`) with the loaded
        extensions' blocks (from the extension registry) and hands the list to the
        prompt manager. The core/data/memory blocks are built by the manager
        itself; this method supplies only what contributors declare. Rebuilt on
        every extension/skill reload so the list never goes stale.
        """
        definitions: list[BlockDefinition] = []
        if self._tool_prompt_blocks is not None:
            definitions.extend(self._tool_prompt_blocks.block_definitions())
        if self._extensions is not None:
            definitions.extend(self._extensions.prompt_block_declarations())
        return definitions

    def _loaded_extension_names(self) -> set[str]:
        """Return the loaded-extension name set for the prompt manager's gate 2."""
        if self._extensions is None:
            return set()
        return self._extensions.loaded_extension_names()

    def _resolve_prompt_block_store(self) -> BlockStore | None:
        """Return the persisted block store (layout + overrides) for the manager.

        The β persistence (``layout.json`` + per-block overrides) lives on
        ``StorageManager`` (Phase 2), which exposes ``read_block_layout`` /
        ``read_block_override`` with the storage scope convention (``None`` =
        default, bare ``"<id>"`` = agent). The manager depends on the prompts
        ``BlockStore`` interface with its own scope-key convention, so an adapter
        bridges the method names and the scope translation — this is the seam where
        the two conventions meet (see :class:`_StorageManagerBlockStore`).
        """
        if self._storage is None:
            return None
        return _StorageManagerBlockStore(self._storage)

    def _refresh_prompt_block_definitions(self) -> None:
        """Re-hand the rebuilt block list + loaded-extension set to the manager.

        Keeps the prompt manager's contributed-block list and gate-2 membership in
        step with the live tool/extension/skill state after a reload — matching the
        old ``update_skill_registry`` refresh, now extended to the block model.
        """
        if self._system_prompts is None:
            return
        self._system_prompts.update_block_definitions(
            self._collect_prompt_block_definitions(),
            self._loaded_extension_names(),
        )

    def reload_provider_credentials(self) -> None:
        """Reload provider credential fallback values from the data-dir `.env`."""

        self._ensure_started()
        data_dir_credentials = self.storage.load_environment()
        self._fallback_environment = dict(data_dir_credentials)
        self._provider_credentials = ProviderCredentialResolver(
            self.providers,
            fallback_credentials=data_dir_credentials,
            token_store=self.token_store,
        )

    # ------------------------------------------------------------------
    # Read-only registry access
    # ------------------------------------------------------------------

    @property
    def config(self) -> ConfigProtocol:
        """The injected configuration. Available before ``start()``."""
        return self._config

    @property
    def providers(self) -> ProviderRegistry:
        """Read-only access to the provider registry.

        Returns:
            The populated ``ProviderRegistry``.

        Raises:
            RuntimeError: If the runtime has not been started.
        """
        self._ensure_started()
        if self._providers is None:
            raise RuntimeError("Provider registry not available")
        return self._providers

    @property
    def models(self) -> ModelRegistry:
        """Read-only access to the model registry.

        Returns:
            The populated ``ModelRegistry``.

        Raises:
            RuntimeError: If the runtime has not been started.
        """
        self._ensure_started()
        if self._models is None:
            raise RuntimeError("Model registry not available")
        return self._models

    @property
    def provider_credentials(self) -> ProviderCredentialResolverProtocol:
        """Access to centralized provider credential resolution."""
        self._ensure_started()
        if self._provider_credentials is None:
            raise RuntimeError("Provider credential service not available")
        return self._provider_credentials

    @property
    def model_tasks(self) -> TaskModelService:
        """Access to specialized task-model settings and discovery."""
        self._ensure_started()
        if self._model_tasks is None:
            raise RuntimeError("Task-model service not available")
        return self._model_tasks

    @property
    def speech(self) -> SpeechService:
        """Access to speech-to-text and text-to-speech execution."""
        self._ensure_started()
        if self._speech is None:
            raise RuntimeError("Speech service not available")
        return self._speech

    @property
    def image(self) -> ImageService:
        """Access to image generation execution."""
        self._ensure_started()
        if self._image is None:
            raise RuntimeError("Image service not available")
        return self._image

    @property
    def embeddings(self) -> EmbeddingService:
        """Access to text-embedding execution for the ``text_embedding`` binding."""
        self._ensure_started()
        if self._embeddings is None:
            raise RuntimeError("Embedding service not available")
        return self._embeddings

    @property
    def token_store(self) -> TokenStore:
        """Access to persisted OAuth provider tokens."""
        self._ensure_started()
        if self._token_store is None:
            raise RuntimeError("Token store not available")
        return self._token_store

    @property
    def storage(self) -> StorageManager:
        """Access to data-directory and prompt-fragment storage."""
        self._ensure_started()
        if self._storage is None:
            raise RuntimeError("Storage service not available")
        return self._storage

    @property
    def attachment_store(self) -> AttachmentStore:
        """Access to persisted blob attachment storage."""
        self._ensure_started()
        if self._attachment_store is None:
            raise RuntimeError("Attachment store not available")
        return self._attachment_store

    @property
    def speech_upload_max_size_bytes(self) -> int:
        """Maximum accepted uploaded audio size for speech transcription."""

        self._ensure_started()
        return self._speech_upload_max_size_bytes

    @property
    def agents(self) -> AgentStore:
        """Access to persisted agent CRUD and workspace lifecycle."""
        self._ensure_started()
        if self._agents is None:
            raise RuntimeError("Agent service not available")
        return self._agents

    @property
    def tools(self) -> ToolRegistry:
        """Access to the runtime tool registry."""
        self._ensure_started()
        if self._tools is None:
            raise RuntimeError("Tool service not available")
        return self._tools

    @property
    def process_manager(self) -> ProcessManager:
        """Access to shared host process lifecycle management."""
        self._ensure_started()
        if self._process_manager is None:
            raise RuntimeError("Process manager service not available")
        return self._process_manager

    @property
    def skills(self) -> SkillRegistry:
        """Access to local skill prompt metadata."""
        self._ensure_started()
        if self._skills is None:
            raise RuntimeError("Skill service not available")
        return self._skills

    @property
    def skill_authoring(self) -> SkillAuthoringService:
        """Shared, validated skill-authoring write core (agent tool + skill RPCs)."""
        self._ensure_started()
        if self._skill_authoring is None:
            raise RuntimeError("Skill authoring service not available")
        return self._skill_authoring

    @property
    def extensions(self) -> ExtensionRegistry | None:
        return self._extensions

    @property
    def chat_sessions(self) -> ChatSessionManager:
        """Access to agent chat session files."""
        self._ensure_started()
        if self._chat_sessions is None:
            raise RuntimeError("Chat session service not available")
        return self._chat_sessions

    @property
    def projects(self) -> ProjectStore:
        """Access to persisted project anchors (cwd, defaults, sessions)."""
        self._ensure_started()
        if self._projects is None:
            raise RuntimeError("Project service not available")
        return self._projects

    @property
    def agent_resolver(self) -> AgentResolver:
        """Uniform ``(project_id | None, agent_id)`` → runtime-agent resolution.

        The single fork between identity-store agents and project config agents;
        run paths resolve through here instead of ``runtime.agents.get`` directly.
        """
        self._ensure_started()
        if self._agent_resolver is None:
            raise RuntimeError("Agent resolver service not available")
        return self._agent_resolver

    @property
    def recall_backend(self) -> RecallBackend:
        """Access to the selected Session recall backend."""
        self._ensure_started()
        if self._recall_backend is None:
            raise RuntimeError("Recall backend is not available")
        return self._recall_backend

    @property
    def chat_run_manager(self) -> ChatRunManager:
        """Access to shared chat run lifecycle management."""
        self._ensure_started()
        if self._chat_run_manager is None:
            raise RuntimeError("Chat run manager service not available")
        return self._chat_run_manager

    @property
    def command_dispatcher(self) -> CommandDispatcher:
        """Access to built-in slash command dispatch for chat entry points."""
        self._ensure_started()
        if self._command_dispatcher is None:
            raise RuntimeError("Command dispatcher service not available")
        return self._command_dispatcher

    @property
    def chat_loop(self) -> ChatLoop:
        """Access to the resolver-wired non-streaming chat loop."""
        self._ensure_started()
        if self._chat_loop is None:
            raise RuntimeError("Chat loop service not available")
        return self._chat_loop

    @property
    def trigger_service(self) -> TriggerService:
        """Access to programmatic run triggering."""
        self._ensure_started()
        if self._trigger_service is None:
            raise RuntimeError("Trigger service not available")
        return self._trigger_service

    @property
    def streaming_chat_loop(self) -> ChatLoop:
        """Access to the resolver-wired streaming chat loop."""
        self._ensure_started()
        if self._streaming_chat_loop is None:
            raise RuntimeError("Streaming chat loop is not available")
        return self._streaming_chat_loop

    @property
    def channel_service(self) -> ChannelService:
        """Access to channel config management and adapter lifecycle."""
        self._ensure_started()
        if self._channel_service is None:
            raise RuntimeError("Channel service not available")
        return self._channel_service

    @property
    def cron_service(self) -> CronService:
        """Access to persisted cron scheduling and job execution."""
        self._ensure_started()
        if self._cron_service is None:
            raise RuntimeError("Cron service not available")
        return self._cron_service

    @property
    def system_prompts(self) -> SystemPromptManager:
        """Access to system prompt assembly."""
        self._ensure_started()
        if self._system_prompts is None:
            raise RuntimeError("System prompt service not available")
        return self._system_prompts

    # ------------------------------------------------------------------
    # Adapter factory
    # ------------------------------------------------------------------

    def get_adapter(self, provider_id: str, connection_id: str) -> ProviderAdapter:
        """Return a wired adapter instance for the given provider.

        Looks up the provider config from the registry, resolves the
        provider credential through the runtime's central credential
        resolver, and instantiates the correct adapter class.

        Args:
            provider_id: Unique provider identifier (e.g. ``"openai"``).
            connection_id: Compositional connection identifier using the
                ``provider:connection[:account]`` grammar (e.g.
                ``"openai:api-key"`` or ``"openai:api-key:work"``). An
                absent account resolves to the connection's first usable
                account (``default`` first, then sorted alphabetically).

        Returns:
            A ``ProviderAdapter`` instance ready to make API calls.

        Raises:
            RuntimeError: If the runtime has not been started.
            KeyError: If no provider with *provider_id* is registered.
            ConfigError: If the provider credential is not configured,
                or if the adapter type is unknown.
        """
        if not self._started:
            raise RuntimeError("Runtime not started — call start() first")

        provider_config = self.providers.get(provider_id)
        connection, account_id = self._get_connection_config(provider_config, connection_id)
        token_getter = self._get_token_getter(provider_id, connection_id, connection, account_id)

        adapter_class = _ADAPTER_MAP.get(provider_config.adapter)
        if adapter_class is None:
            raise ConfigError(
                f"Unknown adapter type '{provider_config.adapter}' for provider '{provider_id}'"
            )

        debug_recorder = self._build_debug_recorder()

        adapter = cast(Any, adapter_class)(
            provider_config,
            token_getter,
            connection.base_url,
            connection.auth,
            model_lookup=self._model_lookup_for(provider_id),
            debug_recorder=debug_recorder,
            connection_mode=connection.mode,
        )

        return cast(ProviderAdapter, adapter)

    def get_connection_token_getter(self, provider_id: str, connection_id: str) -> TokenGetter:
        """Return a token getter for one provider connection.

        Public, DI-friendly wrapper over the same connection resolution and
        token-getter construction :meth:`get_adapter` uses, so non-chat
        provider clients (e.g. the usage probe) can obtain a per-connection
        token without re-implementing OAuth refresh. The returned getter is a
        :class:`StaticTokenGetter` for api-key connections, or a refresh-capable
        :class:`OAuthTokenGetter` for OAuth connections.

        Args:
            provider_id: Unique provider identifier (e.g. ``"openai"``).
            connection_id: Compositional ``provider:connection[:account]`` id.

        Raises:
            RuntimeError: If the runtime has not been started.
            KeyError: If no provider with *provider_id* is registered.
            ConfigError: If the connection id is unknown.
        """
        if not self._started:
            raise RuntimeError("Runtime not started — call start() first")

        provider_config = self.providers.get(provider_id)
        connection, account_id = self._get_connection_config(provider_config, connection_id)
        return self._get_token_getter(provider_id, connection_id, connection, account_id)

    def get_connection_token_extra(self, provider_id: str, connection_id: str) -> Mapping[str, str]:
        """Return the stored OAuth token ``extra`` metadata for a connection.

        Reads the persisted token-store ``extra`` map for the resolved account
        (e.g. Copilot's ``github_oauth_token`` or OpenAI's mirrored
        ``chatgpt_account_id``). Returns an empty mapping when no token is
        stored — api-key connections and not-yet-connected OAuth connections
        both yield ``{}`` rather than raising.

        Args:
            provider_id: Unique provider identifier (e.g. ``"github-copilot"``).
            connection_id: Compositional ``provider:connection[:account]`` id.

        Raises:
            RuntimeError: If the runtime has not been started.
            KeyError: If no provider with *provider_id* is registered.
            ConfigError: If the connection id is unknown.
        """
        if not self._started:
            raise RuntimeError("Runtime not started — call start() first")

        provider_config = self.providers.get(provider_id)
        connection, account_id = self._get_connection_config(provider_config, connection_id)
        resolved_account_id = account_id
        if resolved_account_id is None:
            try:
                resolved_account_id = self.provider_credentials.resolve_account_id(
                    provider_id, connection.id
                )
            except ConfigError:
                resolved_account_id = DEFAULT_ACCOUNT_ID
        token = self.token_store.load(provider_id, connection.id, account_id=resolved_account_id)
        if token is None:
            return {}
        return dict(token.extra)

    def _build_debug_recorder(self) -> ProviderDebugRecorder | None:
        """Create a debug recorder when debug mode is enabled, else ``None``.

        The recorder is passed into the adapter constructor so its HTTP
        client is built with wire capture wired into the transport.
        """
        if self._storage is None:
            return None
        debug_settings = self._storage.load_debug_settings()
        if not debug_settings.get("enabled", False):
            return None
        trace_limit = debug_settings.get("trace_limit", 50)
        debug_store = DebugTraceStore(self._data_dir, trace_limit=trace_limit)
        return ProviderDebugRecorder(store=debug_store)

    def _model_lookup_for(self, provider_id: str) -> ModelLookup:
        def _lookup(model_id: str) -> Model | None:
            try:
                return self.models.get(provider_id, model_id)
            except KeyError:
                return None

        return _lookup

    def _get_token_getter(
        self,
        provider_id: str,
        connection_id: str,
        connection: ConnectionConfig,
        account_id: str | None,
    ) -> TokenGetter:
        if connection.type == "api_key":
            raw_token = self.provider_credentials.get_credentials(provider_id, connection_id)
            return StaticTokenGetter(raw_token)
        if connection.type == "oauth":
            if connection.oauth is None:
                # OAuth stubs with a credential_key still resolve through the
                # central credential path until they get token-store metadata.
                raw_token = self.provider_credentials.get_credentials(provider_id, connection_id)
                return StaticTokenGetter(raw_token)
            # An explicitly pinned account is used exactly as given (a
            # mid-flight login must still work); only an absent account
            # resolves to the first usable one.
            resolved_account_id = account_id
            if resolved_account_id is None:
                resolved_account_id = self.provider_credentials.resolve_account_id(
                    provider_id,
                    connection.id,
                )
            return OAuthTokenGetter(
                self.token_store,
                provider_id,
                connection.id,
                connection.oauth,
                account_id=resolved_account_id,
            )
        raise ConfigError(
            f"Unknown connection type '{connection.type}' for provider '{provider_id}' "
            f"connection '{connection.id}'"
        )

    def _get_connection_config(
        self,
        provider_config: ProviderConfig,
        connection_id: str,
    ) -> tuple[ConnectionConfig, str | None]:
        local_connection_id, account_id = split_connection_id(provider_config.id, connection_id)
        try:
            return provider_config.get_connection(local_connection_id), account_id
        except KeyError as error:
            raise ConfigError(
                f"Unknown connection id '{connection_id}' for provider '{provider_config.id}'"
            ) from error

    def has_provider_credentials(self, provider_id: str) -> bool:
        """Return whether *provider_id* has usable configured credentials."""

        if not self._started:
            raise RuntimeError("Runtime not started — call start() first")

        return self.provider_credentials.has_credentials(provider_id)

    def get_provider_credentials(self, provider_id: str) -> str:
        """Return the configured credential value for *provider_id*."""

        if not self._started:
            raise RuntimeError("Runtime not started — call start() first")

        return self.provider_credentials.get_credentials(provider_id)

    # ------------------------------------------------------------------
    # Model lookup convenience
    # ------------------------------------------------------------------

    def get_model(self, provider_id: str, model_id: str) -> Model:
        """Look up a model by provider ID and model ID.

        Convenience method that delegates to
        :meth:`ModelRegistry.get`.

        Args:
            provider_id: The provider identifier (e.g. ``"openai"``).
            model_id: The exact model ID sent in API requests.

        Returns:
            The matching :class:`Model` entry.

        Raises:
            RuntimeError: If the runtime has not been started.
            KeyError: If no model matches the given provider and model ID.
        """
        if not self._started:
            raise RuntimeError("Runtime not started — call start() first")

        return self.models.get(provider_id, model_id)

    def _ensure_started(self) -> None:
        if not self._started:
            raise RuntimeError("Runtime not started — call start() first")
