"""vBot runtime bootstrap.

The ``Runtime`` class is the single entry point that wires together
all core services and manages the application lifecycle.
"""

import asyncio
import inspect
import os
import sqlite3
import time
import tomllib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _installed_package_version
from pathlib import Path
from typing import Any, Protocol, cast
from uuid import uuid4

from core.agents.agents import AgentStore
from core.attachments import AttachmentStore
from core.automation import BootstrapService, CronService, ReflectionService, TriggerService
from core.channels import ChannelService
from core.chat import ChatLoop, ChatLoopDependencies, CommandDispatcher
from core.chat.block_resolver import ContentBlockResolver
from core.compaction import CompactionService
from core.debug import DebugTraceStore, ProviderDebugRecorder
from core.extensions import (
    ExtensionRegistry,
    InteractionEvent,
    InteractionResponder,
    purge_extension_modules,
)
from core.memory import MemoryService
from core.model_tasks import (
    EmbeddingService,
    ImageService,
    MusicService,
    SpeechService,
    TaskModelService,
    VideoService,
)
from core.models.database import begin_runtime_model_database_refresh
from core.models.models import Model, ModelRegistry
from core.projects import (
    AgentResolver,
    ProjectStore,
    build_agent_resolver,
    effective_project_allowed_skills,
)
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
from core.providers.kimi import KimiAdapter
from core.providers.lmstudio import LMStudioAdapter
from core.providers.minimax import MiniMaxAdapter
from core.providers.mistral import MistralAdapter
from core.providers.nous import NousAdapter
from core.providers.ollama import OllamaAdapter, OllamaCloudAdapter
from core.providers.openai import OpenAIAdapter
from core.providers.openai_compatible import OpenAICompatibleAdapter
from core.providers.opencode_go import OpenCodeGoAdapter
from core.providers.opencode_zen import OpenCodeZenAdapter
from core.providers.openrouter import OpenRouterAdapter
from core.providers.providers import (
    ConnectionConfig,
    ProviderConfig,
    ProviderRegistry,
    model_is_local,
    resolve_effective_context_window,
)
from core.providers.stepfun import StepFunAdapter
from core.providers.token_getter import (
    COPILOT_API_ENDPOINT_EXTRA_KEY,
    OAuthTokenGetter,
    StaticTokenGetter,
    TokenGetter,
)
from core.providers.token_store import TokenStore
from core.providers.usage import ProviderUsageService
from core.providers.xai import XAIAdapter
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
from core.sessions.titles import SessionTitleService
from core.settings.paths import (
    DEFAULT_ATTACHMENT_MAX_SIZE_BYTES,
    DEFAULT_SPEECH_UPLOAD_MAX_SIZE_BYTES,
)
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
    SESSION_READ_TOOL_NAME,
    SKILL_LIST_TOOL_NAME,
    FileReadState,
    register_analyze_image_tool,
    register_bash_tool,
    register_edit_tool,
    register_generate_music_tool,
    register_generate_video_tool,
    register_glob_tool,
    register_grep_tool,
    register_history_tool,
    register_image_generation_tool,
    register_memory_tool,
    register_process_tool,
    register_project_tool,
    register_read_tool,
    register_session_search_tool,
    register_skill_manage_tool,
    register_skill_tool,
    register_terminal_tool,
    register_text_to_speech_tool,
    register_web_fetch_tool,
    register_web_search_tool,
    register_write_tool,
)
from core.tools.cron import register_cron_tool
from core.tools.process_manager import ProcessManager
from core.tools.status import register_status_tool
from core.tools.subagent import register_subagent_tools
from core.tools.terminal_manager import TerminalManager
from core.tools.tools import ToolPromptBlockRegistry, ToolRegistry
from core.utils.config import VBOT_ROOT
from core.utils.errors import ConfigError, StorageError
from core.utils.logging import LogManager

# ---------------------------------------------------------------------------
# Project root / default resources directory
# ---------------------------------------------------------------------------

_VBOT_ROOT = VBOT_ROOT
_DEFAULT_RESOURCES_DIR = _VBOT_ROOT / "resources"
_PACKAGE_NAME = "vbot"
_UNKNOWN_VBOT_VERSION = "0.0.0+unknown"
_SKILLS_DIRNAME = "skills"
_AGENTS_DIRNAME = "agents"


def _detect_vbot_version() -> str:
    """Resolve the running vBot version from its single source of truth.

    The version lives once, in ``pyproject.toml`` → ``project.version``. Read
    that file directly when it sits next to the running code (the dev and
    clone-based deployments vBot actually ships as): it is the *live* value, so a
    version bump — or a ``vbot update`` git pull — flows through without a
    reinstall. Installed package metadata is only a fallback for a pure wheel
    install where the source tree is absent; it is a snapshot frozen at install
    time and would otherwise drift behind an edited ``pyproject.toml``.
    """
    try:
        with (_VBOT_ROOT / "pyproject.toml").open("rb") as handle:
            version = tomllib.load(handle)["project"]["version"]
        if isinstance(version, str) and version:
            return version
    except (OSError, KeyError, tomllib.TOMLDecodeError):
        pass
    try:
        return _installed_package_version(_PACKAGE_NAME)
    except PackageNotFoundError:
        return _UNKNOWN_VBOT_VERSION


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

# At most one auto-refresh sweep of local model catalogs per this window —
# successes AND failures count, so a stopped local server (e.g. Ollama down)
# is not re-probed on every picker open.
_LOCAL_CATALOG_REFRESH_TTL_SECONDS = 30.0

_ADAPTER_MAP: dict[
    str,
    type[ProviderAdapter],
] = {
    "openai_compatible": OpenAICompatibleAdapter,
    "openai": OpenAIAdapter,
    "openrouter": OpenRouterAdapter,
    "kimi": KimiAdapter,
    "minimax": MiniMaxAdapter,
    "mistral": MistralAdapter,
    "nous": NousAdapter,
    "stepfun": StepFunAdapter,
    "opencode_go": OpenCodeGoAdapter,
    "opencode_zen": OpenCodeZenAdapter,
    "github_copilot": GitHubCopilotAdapter,
    "anthropic": AnthropicAdapter,
    "ollama": OllamaAdapter,
    "ollama_cloud": OllamaCloudAdapter,
    "lmstudio": LMStudioAdapter,
    "xai": XAIAdapter,
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
        # Auto-refresh throttle state for local model catalogs (see
        # ``maybe_refresh_local_catalogs``). The lock is created lazily inside
        # the first call so it binds to the running event loop.
        self._local_catalog_refresh_lock: asyncio.Lock | None = None
        self._local_catalog_refresh_at: float | None = None
        # Last probe outcome per auto-refresh connection id ("provider:conn" →
        # bool). Absent means "never probed"; consumed by the connection/model
        # payloads so accessors can mark a not-running local endpoint.
        self._connection_reachability: dict[str, bool] = {}
        self._providers: ProviderRegistry | None = None
        self._provider_credentials: ProviderCredentialResolverProtocol | None = None
        self._provider_usage: ProviderUsageService | None = None
        self._token_store: TokenStore | None = None
        self._models: ModelRegistry | None = None
        self._model_tasks: TaskModelService | None = None
        self._speech: SpeechService | None = None
        self._image: ImageService | None = None
        self._video: VideoService | None = None
        self._music: MusicService | None = None
        self._embeddings: EmbeddingService | None = None
        self._storage: StorageManager | None = None
        self._attachment_store: AttachmentStore | None = None
        self._speech_upload_max_size_bytes = DEFAULT_SPEECH_UPLOAD_MAX_SIZE_BYTES
        self._agents: AgentStore | None = None
        self._tools: ToolRegistry | None = None
        self._tool_prompt_blocks: ToolPromptBlockRegistry | None = None
        self._memory_service: MemoryService | None = None
        self._file_state: FileReadState | None = None
        self._process_manager: ProcessManager | None = None
        self._terminal_manager: TerminalManager | None = None
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
        # Serializes every extension-layer mutation (full reload + live disable) so
        # rapid WebUI toggles / concurrent CLI calls queue instead of interleaving
        # rebuilds; each mutation then reads the then-current persisted settings, so
        # the final state is the last write's state regardless of timing (AD3).
        self._extension_reload_lock = asyncio.Lock()
        self._chat_sessions: ChatSessionManager | None = None
        self._projects: ProjectStore | None = None
        self._agent_resolver: AgentResolver | None = None
        self._recall_backend_registry: RecallBackendRegistry | None = None
        self._recall_backend: RecallBackend | None = None
        self._recall_backend_name: str | None = None
        self._chat_run_manager: ChatRunManager | None = None
        self._command_dispatcher: CommandDispatcher | None = None
        self.chat_runs: ChatRunManager | None = None
        self._chat_loop: ChatLoop | None = None
        self._streaming_chat_loop: ChatLoop | None = None
        self._started_at: datetime | None = None
        self._startup_id: str | None = None
        self._trigger_service: TriggerService | None = None
        self._reflection_service: ReflectionService | None = None
        self._session_title_service: SessionTitleService | None = None
        self._channel_service: ChannelService | None = None
        self._cron_service: CronService | None = None
        self._bootstrap_service: BootstrapService | None = None
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
        self._startup_id = str(uuid4())
        self.logger = self._log_manager.get_logger("core")
        self.logger.info("Runtime startup initiated")

        resources_path = self._resolve_resources_path()

        self._storage = StorageManager(config=self._config, resources_dir=resources_path)
        storage = self._storage
        if storage is None:
            raise RuntimeError("Storage service not available")
        self._storage.ensure_directories()
        self._storage.temporary_files.start()
        settings = self._storage.load_settings()
        attachment_max_size_bytes = self._positive_size_setting(
            settings,
            key="attachment_max_size_bytes",
            default=DEFAULT_ATTACHMENT_MAX_SIZE_BYTES,
        )
        self._speech_upload_max_size_bytes = self._positive_size_setting(
            settings,
            key="speech_upload_max_size_bytes",
            default=DEFAULT_SPEECH_UPLOAD_MAX_SIZE_BYTES,
        )
        self._attachment_store = AttachmentStore(
            self._storage.data_dir,
            max_size_bytes=attachment_max_size_bytes,
        )
        data_dir_credentials = self._storage.load_environment()
        self._fallback_environment = dict(data_dir_credentials)
        custom_providers = self._storage.load_custom_providers_settings()

        self._providers = ProviderRegistry.load(
            resources_path,
            custom_providers=custom_providers,
            tolerate_invalid=True,
        )
        self._token_store = TokenStore(self._storage.data_dir)
        self._provider_credentials = ProviderCredentialResolver(
            self._providers,
            fallback_credentials=data_dir_credentials,
            token_store=self._token_store,
            enabled_overrides_loader=self._provider_connection_enabled_overrides,
        )
        self._provider_usage = ProviderUsageService(
            self,
            data_root=self._storage.data_dir,
        )
        self._models = ModelRegistry.load(
            resources_path,
            runtime_models_dir=self._storage.layout.models,
            custom_providers=custom_providers,
        )
        self._model_tasks = TaskModelService(
            self._providers,
            self._models,
            self._provider_credentials,
            self._storage,
        )
        self._speech = SpeechService(
            self._model_tasks,
            self,
            self._storage.data_dir,
            transcription_audio_getter=self._storage.load_speech_settings,
        )
        self._image = ImageService(
            self._model_tasks,
            self,
            max_input_bytes=self._attachment_store.max_size_bytes,
        )
        self._video = VideoService(self._model_tasks, self)
        self._music = MusicService(self._model_tasks, self)
        self._embeddings = EmbeddingService(self._model_tasks, self)
        self._agents = AgentStore(
            self._storage.data_dir,
            template_dir=resources_path / "workspace-templates",
            defaults_provider=lambda: storage.load_defaults().get("agent", {}),
        )
        self._process_manager = ProcessManager(
            temporary_files=self._storage.temporary_files,
        )
        self._start_process_manager()
        self._tools = ToolRegistry()
        # Tool-owned System Prompt block declarations (D6): the tool side of the
        # unified contributor path. Project and Sub-Agent contribute their dynamic
        # catalogs/guidance here; the runtime hands declarations to the prompt
        # manager without importing tool classes into the prompt domain.
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
            speech_max_size_bytes=self._speech_upload_max_size_bytes,
        )
        register_edit_tool(self._tools, file_state=self._file_state)
        register_glob_tool(self._tools)
        register_grep_tool(self._tools)
        register_write_tool(self._tools, file_state=self._file_state)
        register_memory_tool(self._tools, self._memory_service)
        register_web_fetch_tool(self._tools, attachment_store=self._attachment_store)
        register_web_search_tool(
            self._tools,
            self.resolve_environment_credential,
            self._storage.load_web_search_settings,
        )
        register_process_tool(self._tools, self._process_manager)
        register_text_to_speech_tool(self._tools, self._speech)
        register_analyze_image_tool(self._tools, self._image)
        register_image_generation_tool(self._tools, self._image)
        register_generate_video_tool(self._tools, self._video)
        register_generate_music_tool(self._tools, self._music)
        extension_dirs = self._extra_extension_directories(settings)
        disabled_extensions, extension_config = self._extension_load_options(settings)
        self._extensions = ExtensionRegistry.load(
            self._storage.data_dir / "extensions",
            extra_dirs=extension_dirs,
            disabled=disabled_extensions,
            config=extension_config,
            bundled_dir=resources_path / "extensions",
            config_provider=self._live_extension_config,
            credential_resolver=self.resolve_environment_credential,
        )
        failed_extension_count = len(self._extensions.diagnostics())
        if failed_extension_count > 0:
            self.logger.warning(
                "Loaded extensions with %s failed extensions; "
                "see vbot.extensions errors for details",
                failed_extension_count,
            )
        # Skills load after extensions: a loaded extension may bundle its own skills
        # under ``<extension>/skills/``, which ``_skill_scan_roots`` folds into the
        # global pool, so the extension layer must be in place first.
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
        register_skill_tool(self._tools, self.skills_for, self.reload_skills_async)
        # The agent skill-authoring write core refuses the bundled skills root;
        # ``skill_manage`` writes only the calling agent's private home.
        self._skill_authoring = SkillAuthoringService(
            protected_roots=[resources_path / _SKILLS_DIRNAME],
        )
        register_skill_manage_tool(
            self._tools,
            self._skill_authoring,
            self.agent_skills_dir,
            self.invalidate_agent_skills,
        )
        self._chat_sessions = ChatSessionManager(self._storage.data_dir)
        register_history_tool(self._tools, self._chat_sessions)
        self._projects = ProjectStore(self._storage.data_dir)
        register_project_tool(
            self._tools,
            self._projects,
            lambda: self.system_prompts,
            self.project_context_skills,
            self._file_state,
            self._tool_prompt_blocks,
        )
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
        register_session_search_tool(
            self._tools,
            self._recall_backend,
            self._chat_sessions,
            self._recall_backend_name,
        )
        self._chat_run_manager = ChatRunManager()
        self.chat_runs = self._chat_run_manager
        if self._attachment_store is None:
            raise RuntimeError("Attachment store not available")
        resolver = ContentBlockResolver(self._attachment_store, transcriber=self._speech)
        compaction_service = CompactionService()
        # The reflection service starts review runs through the runtime's
        # streaming loop lazily at review time, so constructing it before the
        # loops is safe — the loops only need its notify hook.
        self._reflection_service = ReflectionService(self)
        self._session_title_service = SessionTitleService(self)
        assert self._agent_resolver is not None
        assert self._projects is not None
        assert self._providers is not None
        assert self._models is not None
        assert self._provider_credentials is not None
        assert self._chat_sessions is not None
        assert self._chat_run_manager is not None
        assert self._tools is not None
        assert self._process_manager is not None
        assert self._file_state is not None
        assert self._storage is not None
        assert self._image is not None
        chat_dependencies = ChatLoopDependencies(
            agent_resolver=self._agent_resolver,
            projects=self._projects,
            providers=self._providers,
            models=self._models,
            provider_credentials=self._provider_credentials,
            sessions=self._chat_sessions,
            run_manager=self._chat_run_manager,
            tools=self._tools,
            process_manager=self._process_manager,
            file_read_state=self._file_state,
            storage=self._storage,
            get_extension_registry=lambda: self.extensions,
            get_system_prompts=lambda: self.system_prompts,
            get_adapter=self.get_adapter,
            resolve_skills=self.skills_for,
            refresh_skills=self.refresh_skills_for,
            get_local_context_windows=self.local_context_windows,
            image_understanding_available=self._image.analysis_is_available,
            deliver_background_completions=lambda run, session: (
                self._trigger_service.deliver_background_completions(run, session)
                if self._trigger_service is not None
                else False
            ),
        )
        self._chat_loop = ChatLoop(
            chat_dependencies,
            streaming=False,
            attachment_resolver=resolver,
            compaction_service=compaction_service,
            reflection_service=self._reflection_service,
            session_title_service=self._session_title_service,
        )
        self._streaming_chat_loop = ChatLoop(
            chat_dependencies,
            streaming=True,
            attachment_resolver=resolver,
            compaction_service=compaction_service,
            reflection_service=self._reflection_service,
            session_title_service=self._session_title_service,
        )
        self._trigger_service = TriggerService(
            self._chat_loop,
            self._chat_run_manager,
            self,
            trigger_chat_loop=self._streaming_chat_loop,
            sessions=self._chat_sessions,
        )
        self._terminal_manager = TerminalManager(
            self._trigger_service,
            temporary_files=self._storage.temporary_files,
            launch_history_path=self._storage.layout.terminals / "launch-history.json",
            data_dir=self._storage.data_dir,
        )
        self._start_terminal_manager()
        register_terminal_tool(self._tools, self._terminal_manager, self._projects)
        self._bootstrap_service = BootstrapService(
            self._trigger_service,
            self._storage.data_dir,
            startup_id=self._startup_id,
            agent_resolver=self._agent_resolver,
            sessions=self._chat_sessions,
        )
        self._command_dispatcher = CommandDispatcher(
            self._chat_run_manager,
            agent_resolver=self._agent_resolver,
            sessions=self._chat_sessions,
            models=self._models,
            started_at=self._started_at,
            providers=self._providers,
            projects=self._projects,
            agents=self._agents,
            local_context_windows_loader=self.local_context_windows,
            trigger_service=self._trigger_service,
            reflection_service=self._reflection_service,
            storage=self._storage,
            terminal_manager=self._terminal_manager,
        )
        if self._extensions is not None:
            self._extensions.apply_commands(self._command_dispatcher)
        self._channel_service = ChannelService(
            self._trigger_service,
            self._chat_sessions,
            agent_store=self._agents,
            data_root=self._storage.data_dir,
            credential_resolver=self.resolve_environment_credential,
            attachment_store=self._attachment_store,
            command_dispatcher=self._command_dispatcher,
            interaction_dispatcher=self._dispatch_channel_interaction,
        )
        self._channel_service._notify_tool_registration_changed_hook = (
            self._reload_channel_tool_if_started
        )
        self._start_channel_service()
        self._sync_channel_tool_registration()
        self._cron_service = CronService(
            self._trigger_service,
            self._storage.data_dir,
            agent_resolver=self._agent_resolver,
            sessions=self._chat_sessions,
        )
        self._start_cron_service()
        register_cron_tool(self._tools, self._cron_service)
        register_bash_tool(
            self._tools,
            self._process_manager,
            self._trigger_service,
            credential_resolver=self.resolve_environment_credential,
            prompt_blocks=self._tool_prompt_blocks,
        )
        self._subagent_coordinator = SubAgentCoordinator(
            self,
            self._trigger_service,
            sessions=self._chat_sessions,
        )
        register_subagent_tools(
            self._tools,
            self._subagent_coordinator,
            self._tool_prompt_blocks,
        )
        register_status_tool(
            self._tools,
            self._agent_resolver,
            self._chat_sessions,
            self._models,
            self._chat_run_manager,
            self._started_at,
            self._providers,
            self._projects,
            self.local_context_windows,
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
            vbot_version=str(self._config.get("VBOT_VERSION") or _detect_vbot_version()),
            vbot_root=_VBOT_ROOT,
            data_root=self._storage.data_dir,
            memory_provider=self._memory_service,
            block_definitions=self._collect_prompt_block_definitions(),
            loaded_extensions=self._loaded_extension_names(),
            block_store=self._resolve_prompt_block_store(),
            agent_store=cast(PromptAgentStore, self._agents),
        )

        self._log_startup_inventory()
        self._started = True
        self._start_provider_usage_service()
        self.logger.info("Runtime started")

    async def fire_extension_startup(self) -> None:
        """Fire extension startup handlers once bootstrap is complete and serving.

        Called by the server from inside its async lifespan, so startup handlers
        run on the live serving loop (they may schedule background tasks there).
        No-op before ``start()`` / after shutdown.
        """
        if self._extensions is not None:
            await self._extensions.fire_startup()

    def activate_bootstrap(self) -> None:
        """Start eligible Bootstrap Runs after the serving lifespan is ready."""
        self.bootstrap_service.activate()

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
        if self._bootstrap_service is not None:
            self._bootstrap_service.stop()
        if self._provider_usage is not None:
            self._provider_usage.stop()
        if self._process_manager is not None:
            self._process_manager.stop()
        if self._terminal_manager is not None:
            self._terminal_manager.stop()
        if self._storage is not None:
            self._storage.temporary_files.stop()

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
        if self._bootstrap_service is not None:
            await self._bootstrap_service.aclose()
        if self._trigger_service is not None:
            await self._trigger_service.aclose()
        if self._reflection_service is not None:
            await self._reflection_service.aclose()
        if self._session_title_service is not None:
            await self._session_title_service.aclose()
        if self._chat_run_manager is not None:
            await self._chat_run_manager.aclose()
        if self._provider_usage is not None:
            await self._provider_usage.aclose()
        if self._process_manager is not None:
            await self._process_manager.aclose()
        if self._terminal_manager is not None:
            await self._terminal_manager.aclose()
        if self._storage is not None:
            await self._storage.temporary_files.aclose()

        self._clear_service_references()
        self._log_manager.close()

    def _log_shutdown(self) -> None:
        if self.logger is not None:
            self.logger.info("Runtime stopped")

    def _clear_service_references(self) -> None:
        self._connection_reachability = {}
        self._providers = None
        self._provider_credentials = None
        self._provider_usage = None
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
        self._file_state = None
        self._process_manager = None
        self._terminal_manager = None
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
        self._recall_backend_name = None
        self._channel_service = None
        self._cron_service = None
        self._bootstrap_service = None
        self._trigger_service = None
        self._reflection_service = None
        self._session_title_service = None
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
        self._agents.ensure_bootstrap()

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
                if self._provider_credentials.is_usable(provider_id, connection_id):
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

    def _live_extension_config(self, name: str) -> dict[str, Any]:
        """Read one extension's persisted config **live** from ``settings.json``.

        Backs ``ExtensionAPI.get_config()``: ``settings.update`` writes the
        ``extensions.config`` section, and the next call here sees it without a
        restart. Defensive shape checks mirror ``_extension_load_options`` — a
        malformed section yields ``{}`` rather than raising into extension code.
        """
        if self._storage is None:
            return {}
        extensions_settings = self._storage.load_extensions_settings()
        config = extensions_settings.get("config", {})
        if not isinstance(config, dict):
            return {}
        value = config.get(name, {})
        return value if isinstance(value, dict) else {}

    def _start_process_manager(self) -> None:
        if self._process_manager is None:
            raise RuntimeError("Process manager service not available")
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        self._process_manager.start()

    def _start_terminal_manager(self) -> None:
        if self._terminal_manager is None:
            raise RuntimeError("Terminal manager service not available")
        self._terminal_manager.start()

    def _start_cron_service(self) -> None:
        if self._cron_service is None:
            raise RuntimeError("Cron service not available")
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        self._cron_service.start()

    def _start_provider_usage_service(self) -> None:
        if self._provider_usage is None:
            raise RuntimeError("Provider usage service not available")
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        self._provider_usage.start()

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

    def environment_credential_source(self, key: str) -> str | None:
        """Return the effective source for one environment credential key."""
        if key in os.environ:
            return "process_environment"
        if key in self._fallback_environment:
            return "data_dir"
        return None

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
        (``<data_dir>/skills``, the bundled ``resources/skills``, the
        settings-configured extras, then the ``skills/`` folder of every loaded
        extension). A project registry prepends its own skill directory (its
        declared source format's location) ahead of these. Everything from the
        bundled root onward is tagged ``global`` by ``_bundled_skill_origins``, so
        extension-bundled skills present as global skills; the user's own
        ``<data_dir>/skills`` is scanned first and therefore wins a name collision.
        """
        if self._storage is None:
            raise RuntimeError("Storage service not available")
        return [
            self._storage.data_dir / _SKILLS_DIRNAME,
            resources_path / _SKILLS_DIRNAME,
            *self._extra_skill_directories(settings),
            *self._extension_skill_dirs(),
        ]

    def _extension_skill_dirs(self) -> list[Path]:
        """Return the ``skills/`` directory of every currently-loaded extension.

        A loaded extension in package/directory form may bundle skills under
        ``<extension>/skills/`` (GLOSSARY -> Skill); ``_skill_scan_roots`` folds
        them into the global pool, so an extension ships a skill with no code —
        only the folder. Only ``loaded`` records contribute: a disabled, failed, or
        overridden extension adds nothing. A single-file extension's ``root_path``
        is its ``.py`` file, whose ``skills`` child is not a directory and is simply
        skipped by the scan. Empty until the extension layer exists.
        """
        if self._extensions is None:
            return []
        return [
            record.root_path / _SKILLS_DIRNAME
            for record in self._extensions.records()
            if record.status == "loaded"
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

    def skills_for(
        self, project_id: str | None, identity_agent_id: str | None = None
    ) -> SkillRegistry:
        """Return the skill registry a run should use, scoped to project and agent.

        ``project_id is None`` and ``identity_agent_id is None`` (a plain identity
        run) returns the global registry byte-for-byte. A set ``project_id`` returns
        the project's merged registry — the project's own skill directory (its
        declared source format's location) first,
        then the bundled pool. When ``identity_agent_id`` names an **identity** agent,
        its private home is layered on top when present (agent > project > global >
        bundled). The agent's own Skills and the effective Skill set of a selected
        Project are always allowed in that scoped registry: Project Context therefore
        grants what the Project uses without mutating the Agent's configured personal
        allowlist. This is the single seam every run-time skill consumer (prompt
        assembly, triggers, the ``skill`` tool, autocomplete) resolves through, so
        scoping lives in exactly one place.

        **Contract:** ``identity_agent_id`` carries the run's agent id only when the
        run executes as an identity agent (plain or rooted — a rooted run passes its
        home project as ``project_id``). A config-agent run passes ``None``: config
        agents own no private home, and agent ids are project-local, so a team slug
        that merely collides with an identity agent's id must never pull that
        identity agent's private skills into the project run (the project skill
        whitelist is a trust boundary; private skills bypass it as always-allowed).
        The identity-store existence check below is defense in depth against a stray
        ``agents/<id>/skills`` directory that belongs to no stored agent.
        """
        self._ensure_started()
        if (
            identity_agent_id is not None
            and self.agents.exists(identity_agent_id)
            and (project_id is not None or self.agent_skills_dir(identity_agent_id).is_dir())
        ):
            return self._agent_skill_registry(project_id, identity_agent_id)
        if project_id is None:
            return self.skills
        return self._project_skill_bundle(project_id).registry

    def refresh_skills_for(
        self, project_id: str | None, identity_agent_id: str | None = None
    ) -> SkillRegistry:
        """Rescan every Skill source, then resolve one fresh scoped registry.

        Compaction uses this as an explicit prompt-refresh boundary. The global reload
        also invalidates Project- and Agent-scoped caches, so the returned registry
        reflects bundled, global, extension, Project, and private Skill changes from
        one coherent scan generation.
        """
        self.reload_skills()
        return self.skills_for(project_id, identity_agent_id)

    def project_own_skills(self, project_id: str) -> list[SkillMetadata]:
        """Return a Project's own skills for explicit Project Context loading.

        Scans only the Project's own Skill directory (its declared Source Format's
        location), so the result is exactly the Project-owned Skills with their
        ``SKILL.md`` paths. The Project Tool lists them in its persisted result, and
        Chat routes later Skill activation through that loaded Project context. A
        missing directory yields an empty list.
        """
        self._ensure_started()
        project = self.projects.get(project_id)
        environment = self._skill_environment(self.storage.load_environment())
        registry = SkillRegistry.load(
            project_skills_dir(Path(project.cwd), project.source_format),
            environment=environment,
        )
        return registry.list_all()

    def project_context_skills(self, project_id: str) -> list[SkillMetadata]:
        """Return the complete effective Skill set carried by Project Context.

        Project-owned Skills are active by default except explicit Project
        disables; bundled and global Skills join only through the Project's opt-in
        lists. This is the same Project policy used for Config Agents and the
        temporary Project grant applied to Identity Runs.
        """
        self._ensure_started()
        project = self.projects.get(project_id)
        bundle = self._project_skill_bundle(project_id)
        allowed_names = set(effective_project_allowed_skills(project, bundle.names))
        return [skill for skill in bundle.registry.list_all() if skill.name in allowed_names]

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
        project_allowed_names: set[str] = set()
        if project_id is not None:
            project = self.projects.get(project_id)
            roots.append(project_skills_dir(Path(project.cwd), project.source_format))
            origins.append(project_skill_origin(project.display_name))
            project_allowed_names.update(
                effective_project_allowed_skills(
                    project,
                    self._project_skill_bundle(project_id).names,
                )
            )
        roots.extend(scan_roots)
        origins.extend(self._bundled_skill_origins(scan_roots))
        # First-found-wins ordering makes agent skills win over project, project over
        # bundled. The agent's own skills are always-allowed for it, so they bypass
        # the owner's ``allowed_skills`` filter without leaking to other agents
        # (whose registries never scan this home). Project Context is itself the
        # authorization to use that Project's effective Skill set: those exact
        # Project-granted names also bypass the Identity Agent's unrelated personal
        # allowlist while this project-scoped registry is active.
        agent_own_names = scan_skill_names(agent_root, environment)
        return SkillRegistry.load(
            roots[0],
            extra_dirs=roots[1:],
            environment=environment,
            always_allowed=agent_own_names | project_allowed_names,
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
            project.source_format,
            scan_roots,
            environment,
            project_origin=project_skill_origin(project.display_name),
            bundled_origins=self._bundled_skill_origins(scan_roots),
        )
        names = scan_project_skill_names(project_cwd, project.source_format, environment)
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
        if not self._channel_service.has_enabled_channels():
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
            backend = registry.create(backend_name, context)
            self._recall_backend_name = backend_name
            return backend
        except KeyError:
            if self.logger is not None:
                self.logger.warning(
                    "Unknown recall backend %r; using %s",
                    backend_name,
                    DEFAULT_RECALL_BACKEND,
                )
            self._recall_backend_name = DEFAULT_RECALL_BACKEND
            return registry.create(DEFAULT_RECALL_BACKEND, context)
        except Exception as error:
            if backend_name == DEFAULT_RECALL_BACKEND:
                raise
            if self.logger is not None:
                self.logger.warning(
                    "Recall backend %r could not start; using %s: %s",
                    backend_name,
                    DEFAULT_RECALL_BACKEND,
                    error,
                )
            self._recall_backend_name = DEFAULT_RECALL_BACKEND
            return registry.create(DEFAULT_RECALL_BACKEND, context)

    def reload_channel_tool(self) -> None:
        """Re-register channel_send based on persisted enabled Channel configs."""
        self._ensure_started()
        self._sync_channel_tool_registration()

    def reload_recall_backend(self) -> None:
        """Reload Session Recall tools from the current persisted backend setting.

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
            self._tools.unregister(SESSION_READ_TOOL_NAME)
            register_session_search_tool(
                self._tools,
                self._recall_backend,
                self._chat_sessions,
                self._recall_backend_name,
            )

    async def reload_extensions(self) -> None:
        """Rebuild the whole extension layer from disk — restart-equivalent, live.

        The explicit reload seam behind ``extensions.reload`` (RPC/CLI/WebUI) and
        behind enabling an extension: it tears the current layer down and builds a
        fresh one from the current persisted settings, so the end state equals
        exactly what a restart from those settings would produce. It picks up edited
        code of loaded extensions (including submodules of package extensions, via
        the module-cache purge), extensions newly added to or deleted from a scan
        root, previously ``failed`` extensions whose code was fixed, and
        boot-disabled extensions that were enabled.

        The rebuild is **async** on purpose: extension startup/shutdown handlers run
        on the live serving loop (they may schedule background tasks there), so they
        are awaited directly here — never driven through a blocking worker loop. The
        whole body runs under ``_extension_reload_lock`` (AD3), so a rapid burst of
        toggles / concurrent calls serialize and the last write wins.

        Sequence (AD2): read fresh inputs; detach the old layer's tools and fire its
        shutdown; purge the ``vbot_ext`` module cache; ``ExtensionRegistry.load`` a
        new registry with the same arguments ``start()`` uses; swap it in; re-apply
        tools, recall backends (via ``reload_recall_backend``), and prompt blocks;
        then fire the new layer's startup. During the swap window a concurrent run
        can still dispatch into an already-shut-down old extension for a moment; that
        is accepted (per-handler fail-open isolation catches it, the same window
        exists in live disable) — no drain.
        """
        async with self._extension_reload_lock:
            self._ensure_started()
            storage = self.storage
            tool_registry = self.tools
            settings = storage.load_settings()
            resources_path = self._resolve_resources_path()
            extension_dirs = self._extra_extension_directories(settings)
            disabled, config = self._extension_load_options(settings)

            old_registry = self._extensions
            if old_registry is not None:
                old_registry.remove_applied_tools(tool_registry)
                if self._command_dispatcher is not None:
                    old_registry.remove_applied_commands(self._command_dispatcher)
                await old_registry.fire_shutdown()

            # An edited submodule of a package extension keeps its stale cached copy
            # unless the whole vbot_ext namespace is dropped before the fresh load.
            purge_extension_modules()

            new_registry = ExtensionRegistry.load(
                storage.data_dir / "extensions",
                extra_dirs=extension_dirs,
                disabled=disabled,
                config=config,
                bundled_dir=resources_path / "extensions",
                config_provider=self._live_extension_config,
                credential_resolver=self.resolve_environment_credential,
            )
            failed_extension_count = len(new_registry.diagnostics())
            if failed_extension_count > 0 and self.logger is not None:
                self.logger.warning(
                    "Reloaded extensions with %s failed extensions; "
                    "see vbot.extensions errors for details",
                    failed_extension_count,
                )

            self._extensions = new_registry

            new_registry.apply_tools(tool_registry)
            if self._command_dispatcher is not None:
                new_registry.apply_commands(self._command_dispatcher)
            self.reload_recall_backend()
            self._refresh_prompt_block_definitions()
            # The rebuilt layer may change which extensions bundle skills (added,
            # removed, edited, or enabled), so rebuild the skill registry — and its
            # project/agent caches — against the fresh set of extension skill dirs.
            self.reload_skills()

            await new_registry.fire_startup()

            if self.logger is not None:
                records = new_registry.records()
                self.logger.info(
                    "Extension layer reloaded: %s loaded, %s failed, %s disabled, %s overridden",
                    sum(1 for record in records if record.status == "loaded"),
                    sum(1 for record in records if record.status == "failed"),
                    sum(1 for record in records if record.status == "disabled"),
                    sum(1 for record in records if record.status == "overridden"),
                )

    async def apply_extension_disabled_change(self, newly_disabled: set[str]) -> None:
        """Deactivate each newly-disabled extension live, without a restart.

        Called by ``settings.update`` after the new ``disabled`` set is persisted,
        with the names that were **added** to it (enabling routes through the full
        ``reload_extensions`` instead). For each name it drives
        ``ExtensionRegistry.deactivate`` (hooks off, tools unregistered, shutdown
        fired, record marked disabled), then refreshes the prompt block definitions
        so any extension-owned block drops immediately, and guards the recall edge
        (below). Names that are not currently ``loaded`` are clean no-ops inside the
        registry. The whole mutation runs under ``_extension_reload_lock`` (AD3), so
        it can never interleave with a concurrent ``reload_extensions``.
        """
        self._ensure_started()
        if not newly_disabled:
            return

        async with self._extension_reload_lock:
            if self._extensions is None:
                return

            # Capture each deactivating extension's declared recall backends before
            # the registry clears its declarations, so we can detect whether one of
            # them is the currently-active backend and fall back (see below).
            deactivating_backend_names = self._extension_recall_backend_names(newly_disabled)

            for name in newly_disabled:
                await self._extensions.deactivate(
                    name,
                    self._tools,
                    self._command_dispatcher,
                )

            self._refresh_prompt_block_definitions()
            # A deactivated extension's bundled skills must drop from the global
            # pool too, so rebuild the skill registry against the now-smaller set.
            self.reload_skills()
            self._recover_recall_backend_if_deactivated(deactivating_backend_names)

    def _extension_recall_backend_names(self, names: set[str]) -> set[str]:
        """Return the recall-backend names declared by the given loaded extensions."""
        if self._extensions is None:
            return set()
        backend_names: set[str] = set()
        for record in self._extensions.records():
            if record.name in names and record.status == "loaded":
                for declaration in record.declarations.recall_backends:
                    backend_names.add(declaration.name)
        return backend_names

    def _recover_recall_backend_if_deactivated(self, removed_backend_names: set[str]) -> None:
        """Fall the active recall backend back to the default if its provider left.

        If a deactivated extension provided the *currently-selected* recall backend,
        the live backend instance now points into dormant extension code. Rather than
        leave recall broken, rebuild the registry (which no longer contains the
        deactivated extension's backend) and re-resolve: an unknown selected name
        falls back to the built-in default (``jsonl_scan``) with a warning, exactly
        as it would on the next restart. The persisted ``recall.backend`` selection
        is left untouched — re-enabling the extension (a restart) restores it.
        """
        if not removed_backend_names or self._storage is None:
            return
        active_backend = self._storage.load_recall_settings()["backend"]
        if active_backend not in removed_backend_names:
            return
        if self.logger is not None:
            self.logger.warning(
                "Recall backend %r was provided by a disabled extension; "
                "falling back to %s until the extension is re-enabled",
                active_backend,
                DEFAULT_RECALL_BACKEND,
            )
        self.reload_recall_backend()

    def available_recall_backends(self) -> list[str]:
        """Return all selectable recall backend names (built-ins + extensions)."""
        self._ensure_started()
        if self._recall_backend_registry is None:
            raise RuntimeError("Recall backend registry not available")
        return self._recall_backend_registry.names()

    async def remove_session_from_recall(
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
            remove_session = backend.remove_session
            if inspect.iscoroutinefunction(remove_session):
                await remove_session(agent_id, session_id, project_id)
            else:
                result = await asyncio.to_thread(
                    remove_session,
                    agent_id,
                    session_id,
                    project_id,
                )
                if inspect.isawaitable(result):
                    await result
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
        self._apply_reloaded_skills(self._load_reloaded_skills())

    async def reload_skills_async(self) -> None:
        """Reload Skills without scanning their files on the Event Loop."""
        skills = await asyncio.to_thread(self._load_reloaded_skills)
        self._apply_reloaded_skills(skills)

    def _load_reloaded_skills(self) -> SkillRegistry:
        """Build one replacement Skill registry without mutating Runtime state."""
        self._ensure_started()
        settings = self.storage.load_settings()
        resources_path = self._resolve_resources_path()
        skill_scan_roots = self._skill_scan_roots(settings, resources_path)
        return SkillRegistry.load(
            skill_scan_roots[0],
            extra_dirs=skill_scan_roots[1:],
            environment=self._skill_environment(self.storage.load_environment()),
            origins=self._bundled_skill_origins(skill_scan_roots),
        )

    def _apply_reloaded_skills(self, skills: SkillRegistry) -> None:
        """Install a fully built Skill registry and refresh its loop-owned consumers."""
        self._skills = skills
        # Project- and agent-scoped registries merge in the same bundled roots, so a
        # global skill reload makes every cached project *and* agent registry stale —
        # invalidate_project_skills() with no project drops both caches so the next
        # run rebuilds against the fresh pool.
        self.invalidate_project_skills()
        invalid_skill_count = len(self._skills.invalid_diagnostics())
        if self.logger is not None:
            # Routine cache maintenance (fires on every project open), not an event.
            self.logger.debug("Reloaded skill registry")
            if invalid_skill_count > 0:
                self.logger.warning(
                    "Reloaded skills with %s invalid skill directories; "
                    "see vbot.skills warnings for details",
                    invalid_skill_count,
                )
        if self._tools is not None:
            self._tools.unregister("skill")
            self._tools.unregister(SKILL_LIST_TOOL_NAME)
            register_skill_tool(self._tools, self.skills_for, self.reload_skills_async)
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

    def reload_environment_credentials(self) -> None:
        """Reload shared credential fallback values from the data-dir `.env`."""

        self._ensure_started()
        data_dir_credentials = self.storage.load_environment()
        self._fallback_environment = dict(data_dir_credentials)
        self.provider_credentials.reload_fallback_credentials(data_dir_credentials)

    def reload_custom_providers(self) -> None:
        """Reload Settings-owned Provider and Model overlays in place."""

        self._ensure_started()
        resources_path = self._resolve_resources_path()
        custom_providers = self.storage.load_custom_providers_settings()
        self.providers.reload(
            resources_path,
            custom_providers=custom_providers,
            tolerate_invalid=True,
        )
        self.models.reload(
            resources_path,
            runtime_models_dir=self.storage.layout.models,
            custom_providers=custom_providers,
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
    def video(self) -> VideoService:
        """Access to Video generation execution."""
        self._ensure_started()
        if self._video is None:
            raise RuntimeError("Video service not available")
        return self._video

    @property
    def music(self) -> MusicService:
        """Access to Music generation execution."""
        self._ensure_started()
        if self._music is None:
            raise RuntimeError("Music service not available")
        return self._music

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
    def memory(self) -> MemoryService:
        """Access to the pinned Memory service used by Tools and accessors."""
        self._ensure_started()
        if self._memory_service is None:
            raise RuntimeError("Memory service not available")
        return self._memory_service

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
    def terminal_manager(self) -> TerminalManager:
        """Access to Session-scoped interactive Terminal lifecycle management."""
        self._ensure_started()
        if self._terminal_manager is None:
            raise RuntimeError("Terminal manager service not available")
        return self._terminal_manager

    @property
    def file_read_state(self) -> FileReadState:
        """Access to the shared per-session read-before-write guard.

        The same instance the read/write/edit tools use, so an ``@``-mention
        snapshot at send time counts as a read for the session's later edits.
        """
        self._ensure_started()
        if self._file_state is None:
            raise RuntimeError("File read state service not available")
        return self._file_state

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

    async def _dispatch_channel_interaction(
        self, event: InteractionEvent, responder: InteractionResponder
    ) -> bool:
        """Route a channel button tap into the live extension registry.

        Injected into channel adapters as a bound method, so it always reads the
        current ``self._extensions`` — an extension reload/disable needs no channel
        re-wiring. Returns ``False`` when no registry is loaded (the adapter then
        still acknowledges the tap).
        """
        if self._extensions is None:
            return False
        return await self._extensions.dispatch_channel_interaction(event, responder)

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
    def subagents(self) -> SubAgentCoordinator:
        """Access to live Sub-Agent coordination and batch tracking."""
        self._ensure_started()
        if self._subagent_coordinator is None:
            raise RuntimeError("Sub-agent coordinator is not available")
        return self._subagent_coordinator

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
        """Access to the shared live slash Command dispatcher for Chat entry points."""
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
    def reflection(self) -> ReflectionService:
        """Access to background self-improvement reviews (fork + cadence)."""
        self._ensure_started()
        if self._reflection_service is None:
            raise RuntimeError("Reflection service not available")
        return self._reflection_service

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
    def bootstrap_service(self) -> BootstrapService:
        """Access persisted startup-triggered Runs and Bootstrap CRUD."""
        self._ensure_started()
        if self._bootstrap_service is None:
            raise RuntimeError("Bootstrap service not available")
        return self._bootstrap_service

    @property
    def provider_usage(self) -> ProviderUsageService:
        """Access to shared live usage caching and durable hourly history."""

        self._ensure_started()
        if self._provider_usage is None:
            raise RuntimeError("Provider usage service not available")
        return self._provider_usage

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
        if not self.provider_credentials.is_connection_enabled(provider_id, connection_id):
            raise ConfigError(
                f"Provider connection '{provider_id}:{connection.id}' is disabled — "
                f"enable it in Settings → Providers or via the provider CLI"
            )
        token_getter = self._get_token_getter(provider_id, connection_id, connection, account_id)

        adapter_class = _ADAPTER_MAP.get(provider_config.adapter)
        if adapter_class is None:
            raise ConfigError(
                f"Unknown adapter type '{provider_config.adapter}' for provider '{provider_id}'"
            )

        debug_recorder = self._build_debug_recorder()

        extra_kwargs: dict[str, Any] = {}
        if adapter_class in (OllamaAdapter, LMStudioAdapter):
            # Local adapters enforce the live effective context when they load
            # a model; no runtime reload hook is needed.
            extra_kwargs["local_context_resolver"] = self._local_context_resolver_for(provider_id)
        if adapter_class is OpenRouterAdapter:
            # Snapshot routing policy for this Adapter/Run. A Settings save
            # applies to the next Run without changing providers midway through
            # an active Tool loop and invalidating its warm prompt cache.
            extra_kwargs["routing"] = self.storage.load_openrouter_routing_settings()

        base_url = connection.base_url
        if adapter_class is GitHubCopilotAdapter:
            copilot_endpoint = self.get_connection_token_extra(provider_id, connection_id).get(
                COPILOT_API_ENDPOINT_EXTRA_KEY
            )
            if copilot_endpoint:
                base_url = copilot_endpoint

        adapter = cast(Any, adapter_class)(
            provider_config,
            token_getter,
            base_url,
            connection.auth,
            model_lookup=self._model_lookup_for(provider_id),
            debug_recorder=debug_recorder,
            connection_mode=connection.mode,
            **extra_kwargs,
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

    async def maybe_refresh_local_catalogs(self, *, force: bool = False) -> None:
        """Refresh every enabled ``auto_refresh`` connection's model catalog, throttled.

        Local providers (e.g. Ollama) change their installed-model set outside
        vBot, so their catalogs refresh automatically — at startup
        (fire-and-forget) and when a model picker opens (``model.list``,
        awaited with a small time budget). Disabled connections are never
        probed. Semantics:

        - **Throttled**: at most one refresh sweep per
          :data:`_LOCAL_CATALOG_REFRESH_TTL_SECONDS`, counting failures too —
          a stopped local server must not be re-probed on every picker open.
          ``force=True`` bypasses the throttle (used right after the user
          enables a local connection, so feedback is immediate).
        - **Never raises**: a refresh failure (server down, malformed catalog)
          logs and leaves the last known catalog untouched.
        - **Serialized**: concurrent callers share one sweep; the second
          caller waits for the in-flight sweep and returns.
        - **Reachability**: each probe outcome is recorded per connection
          (see :meth:`connection_reachability`).
        """

        if not self._started:
            return
        if self._local_catalog_refresh_lock is None:
            self._local_catalog_refresh_lock = asyncio.Lock()
        async with self._local_catalog_refresh_lock:
            now = time.monotonic()
            last = self._local_catalog_refresh_at
            if not force and last is not None and now - last < _LOCAL_CATALOG_REFRESH_TTL_SECONDS:
                return

            targets = self._auto_refresh_targets()
            if not targets:
                return
            # Stamp before the work so a failing local server is also throttled.
            self._local_catalog_refresh_at = now

            # Local import: core.models.discovery imports core.providers.* at
            # module load; importing it lazily here keeps runtime import time flat.
            from core.models.discovery import ModelDiscoveryError, refresh_models

            system_resources_dir = self._resolve_resources_path()
            database_refresh = None
            refresh_resources_dir = None
            refreshed_any = False
            try:
                database_refresh = begin_runtime_model_database_refresh(
                    system_resources_dir,
                    self.storage.data_dir,
                )
                refresh_resources_dir = database_refresh.resources_dir
                for provider_id, provider, connection in targets:
                    connection_id = f"{provider_id}:{connection.id}"
                    try:
                        credential_value = self.provider_credentials.get_credentials(
                            provider_id, connection_id
                        )
                    except ConfigError as error:
                        if self.logger is not None:
                            self.logger.debug(
                                f"Skipping local catalog refresh for {connection_id}: {error}"
                            )
                        continue
                    try:
                        await refresh_models(
                            provider,
                            credential_value,
                            refresh_resources_dir,
                            credential_connection=connection,
                        )
                    except ModelDiscoveryError as error:
                        # Expected when the local server is not running — keep the
                        # last known catalog, never block or error the caller.
                        previous_reachability = self._connection_reachability.get(connection_id)
                        self._connection_reachability[connection_id] = False
                        if self.logger is not None:
                            if previous_reachability is True:
                                self.logger.warning(
                                    "Local provider connection became unreachable "
                                    "(provider=%s connection=%s): %s",
                                    provider_id,
                                    connection.id,
                                    error,
                                )
                            else:
                                self.logger.debug(
                                    f"Local catalog refresh failed for {connection_id}: {error}"
                                )
                        continue
                    previous_reachability = self._connection_reachability.get(connection_id)
                    self._connection_reachability[connection_id] = True
                    if previous_reachability is False and self.logger is not None:
                        self.logger.info(
                            "Local provider connection recovered (provider=%s connection=%s)",
                            provider_id,
                            connection.id,
                        )
                    refreshed_any = True

                if refreshed_any:
                    ModelRegistry.invalidate(refresh_resources_dir)
                    ModelRegistry.load(refresh_resources_dir)
                    ModelRegistry.invalidate(refresh_resources_dir)
                    database_refresh.commit()
                    # In-place reload so services holding the registry instance
                    # (task targets, status, recall) see the fresh catalog.
                    self.models.reload(
                        system_resources_dir,
                        runtime_models_dir=self.storage.layout.models,
                        custom_providers=self.storage.load_custom_providers_settings(),
                    )
            except Exception as error:
                # This background convenience path is deliberately fail-soft:
                # an unpublished staging failure must not disturb runtime or the
                # last complete Model DB.
                if self.logger is not None:
                    self.logger.warning("Local catalog refresh could not be published: %s", error)
            finally:
                if refresh_resources_dir is not None:
                    ModelRegistry.invalidate(refresh_resources_dir)
                if database_refresh is not None:
                    database_refresh.discard()

    def connection_reachability(self, connection_id: str) -> bool | None:
        """Return the last catalog-probe outcome for one auto-refresh connection.

        ``True``/``False`` reflect the most recent sweep; ``None`` means the
        connection was never probed (not an auto-refresh connection, disabled,
        or no sweep has run yet). Only meaningful for local auto-refresh
        connections — remote connections are never probed and stay ``None``.
        """

        return self._connection_reachability.get(connection_id)

    def _auto_refresh_targets(self) -> list[tuple[str, ProviderConfig, ConnectionConfig]]:
        """Return ``(provider_id, provider, connection)`` for auto-refresh connections.

        Only enabled connections qualify — a disabled local provider is
        completely passive (no startup probe, no picker probe).
        """

        targets: list[tuple[str, ProviderConfig, ConnectionConfig]] = []
        for provider_id in self.providers.list_ids():
            provider = self.providers.get(provider_id)
            for connection in provider.connections:
                if not connection.auto_refresh:
                    continue
                if not (connection.models_endpoint or provider.models_endpoint):
                    continue
                if not self.provider_credentials.is_usable(
                    provider_id, f"{provider_id}:{connection.id}"
                ):
                    continue
                targets.append((provider_id, provider, connection))
        return targets

    def _provider_connection_enabled_overrides(self) -> Mapping[str, bool]:
        """Return the live per-connection enabled overrides from settings, or empty.

        Injected into the provider credential resolver; read from settings at
        every check (no reload hook) so an enable/disable applies immediately.
        """

        if self._storage is None:
            return {}
        try:
            connections = self._storage.load_providers_settings()["connections"]
        except StorageError as error:
            if self.logger is not None:
                self.logger.warning(
                    f"Failed to load provider connection overrides from settings: {error}"
                )
            return {}
        return cast("Mapping[str, bool]", connections)

    def local_context_windows(self) -> Mapping[str, Any]:
        """Return the live user-configured local-model window map, or empty.

        Read from settings at every call (no reload hook) so a change applies
        to the next request/status/list without a restart.
        """
        if self._storage is None:
            return {}
        try:
            windows = self._storage.load_local_models_settings()["context_windows"]
        except StorageError as error:
            if self.logger is not None:
                self.logger.warning(
                    f"Failed to load local-model context windows from settings: {error}"
                )
            return {}
        return cast("Mapping[str, Any]", windows)

    def _local_context_resolver_for(self, provider_id: str) -> Callable[[str], int | None]:
        """Build the per-provider context resolver local adapters enforce on load.

        Returns the effective context window for flagged-local models and
        ``None`` for everything else (proxied ``:cloud`` models, unknown
        models) — the adapter then omits ``options.num_ctx`` entirely.
        """

        def _resolve(model_id: str) -> int | None:
            bare_model_id = model_id.split("::", 1)[0]
            try:
                model = self.models.get(provider_id, bare_model_id)
            except (KeyError, AttributeError):
                return None
            if not model_is_local(model.metadata):
                return None
            try:
                provider_config = self.providers.get(provider_id)
            except (KeyError, AttributeError):
                provider_config = None
            return resolve_effective_context_window(
                model.context_window,
                provider_config,
                model_metadata=model.metadata,
                model_key=f"{provider_id}/{bare_model_id}",
                local_context_windows=self.local_context_windows(),
            )

        return _resolve

    def _get_token_getter(
        self,
        provider_id: str,
        connection_id: str,
        connection: ConnectionConfig,
        account_id: str | None,
    ) -> TokenGetter:
        if connection.type == "none":
            # Keyless connection: adapters and discovery skip the auth header
            # when the credential value is empty.
            return StaticTokenGetter("")
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
