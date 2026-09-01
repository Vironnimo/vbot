"""vBot runtime bootstrap.

The ``Runtime`` class is the single entry point that wires together
all core services and manages the application lifecycle.
"""

import asyncio
import inspect
import os
import sqlite3
import tomllib
from collections.abc import Mapping, Sequence
from contextlib import suppress
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _installed_package_version
from pathlib import Path
from typing import Any, Protocol, cast
from uuid import uuid4

from core.agents.agents import AgentStore
from core.attachments import AttachmentStore
from core.automation import BootstrapService, CronService, ReflectionService, TriggerService
from core.calendar import CalendarService
from core.channels import ChannelService
from core.chat import ChatLoop, ChatLoopDependencies, CommandDispatcher
from core.chat.block_resolver import ContentBlockResolver
from core.compaction import CompactionService
from core.extensions import (
    ExtensionRegistry,
    InteractionEvent,
    InteractionResponder,
)
from core.extensions.runtime import ExtensionRuntime
from core.memory import MemoryService
from core.model_tasks import (
    EmbeddingService,
    ImageService,
    MusicService,
    SpeechService,
    TaskModelService,
    VideoService,
)
from core.models.models import Model, ModelRegistry
from core.projects import (
    AgentResolver,
    ProjectStore,
    build_agent_resolver,
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
from core.providers.accounts import ConnectionRef
from core.providers.adapter import ProviderAdapter
from core.providers.credentials import ProviderCredentialResolver
from core.providers.providers import (
    ProviderRegistry,
)
from core.providers.reasoning import ReasoningIntent
from core.providers.runtime import ProviderRuntime
from core.providers.token_getter import TokenGetter
from core.providers.token_store import TokenStore
from core.providers.usage import ProviderUsageService
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
from core.runtime.keep_awake import KeepAwakeController
from core.sessions import ChatSessionManager
from core.sessions.titles import SessionTitleService
from core.settings.paths import (
    DEFAULT_ATTACHMENT_MAX_SIZE_BYTES,
    DEFAULT_SPEECH_UPLOAD_MAX_SIZE_BYTES,
)
from core.settings.settings import effective_timezone_name
from core.skills.authoring import SkillAuthoringService
from core.skills.policy import SkillPolicyService
from core.skills.runtime import SkillRuntime, load_global_skill_registry
from core.skills.skills import SkillMetadata, SkillRegistry
from core.storage.storage import StorageManager
from core.subagents import SubAgentCoordinator
from core.tools import (
    SESSION_READ_TOOL_NAME,
    ChangeTracker,
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
from core.tools.calendar import register_calendar_tool
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
        self._provider_runtime: ProviderRuntime | None = None
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
        self._keep_awake: KeepAwakeController | None = None
        self._speech_upload_max_size_bytes = DEFAULT_SPEECH_UPLOAD_MAX_SIZE_BYTES
        self._agents: AgentStore | None = None
        self._tools: ToolRegistry | None = None
        self._tool_prompt_blocks: ToolPromptBlockRegistry | None = None
        self._memory_service: MemoryService | None = None
        self._file_state: FileReadState | None = None
        self._process_manager: ProcessManager | None = None
        self._terminal_manager: TerminalManager | None = None
        self._skills: SkillRegistry | None = None
        # Shared, validated skill-authoring write core (Phase 1), constructed at
        # start() with the bundled skills root as a protected target. Used by the
        # agent ``skill_manage`` tool and (later) the skill-mutation RPCs.
        self._skill_authoring: SkillAuthoringService | None = None
        # The Skills domain's central policy overlay (disable switch + sharing).
        # Constructed at start() beside the skill registries; every runtime-owned
        # registry build threads its disable set through ``SkillRegistry.load``.
        self._skill_policy: SkillPolicyService | None = None
        self._skill_runtime: SkillRuntime | None = None
        self._extensions: ExtensionRegistry | None = None
        self._extension_runtime: ExtensionRuntime | None = None
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
        self._calendar_service: CalendarService | None = None
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
        resources_path = self._resolve_resources_path()

        self._storage = StorageManager(config=self._config, resources_dir=resources_path)
        storage = self._storage
        if storage is None:
            raise RuntimeError("Storage service not available")
        self._storage.ensure_directories()
        self.logger = self._log_manager.get_logger("core")
        self.logger.info("Runtime startup initiated")
        self._storage.temporary_files.start()
        settings = self._storage.load_settings()
        timezone_name = effective_timezone_name(settings)
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
        # Keep-awake is applied as soon as Settings are usable so an enabled
        # server holds its power request for the whole lifetime of the process.
        self._keep_awake = KeepAwakeController(self.logger)
        self._keep_awake.set_enabled(settings.get("keep_awake") is True)
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
        self._provider_runtime = ProviderRuntime(
            providers=self._providers,
            models=self._models,
            credentials=self._provider_credentials,
            token_store=self._token_store,
            storage=self._storage,
            resources_path=resources_path,
            logger=self.logger,
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
        # Sessions are a canonical service: it opens and verifies one database
        # before any Agent lifecycle operation can create or validate a Session.
        # Partial startup must close every Session resource in reverse order so a
        # failed start does not leak descriptors or leave a half-open database.
        try:
            self._chat_sessions = ChatSessionManager(
                self._storage.data_dir,
                store_path=self._storage.layout.sessions_db_path,
            )
            self._agents = AgentStore(
                self._storage.data_dir,
                template_dir=resources_path / "workspace-templates",
                defaults_provider=lambda: storage.load_defaults().get("agent", {}),
                sessions=self._chat_sessions,
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
            # Session-scoped file-content tracker for git-style change statistics
            # (change_tracker.py). Shared by read/write/edit and the chat loop.
            self._change_tracker = ChangeTracker()
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
            self._extension_runtime = ExtensionRuntime(
                storage=self._storage,
                resources_path=resources_path,
                tools=self._tools,
                get_registry=lambda: self._extensions,
                set_registry=self._set_extension_registry,
                get_command_dispatcher=lambda: self._command_dispatcher,
                extra_directories=self._extra_extension_directories,
                load_options=self._extension_load_options,
                live_config=self._live_extension_config,
                resolve_credential=self.resolve_environment_credential,
                reload_recall=self.reload_recall_backend,
                refresh_prompts=self._refresh_prompt_block_definitions,
                reload_skills=self.reload_skills,
                recover_recall=self._recover_recall_backend_if_deactivated,
                logger=self.logger,
            )
            # Skills load after extensions: a loaded extension may bundle its own skills
            # under ``<extension>/skills/``, which ``_skill_scan_roots`` folds into the
            # global pool, so the extension layer must be in place first.
            self._skill_policy = SkillPolicyService(self._storage)
            self._skills = load_global_skill_registry(
                storage=self._storage,
                resources_path=resources_path,
                settings=settings,
                fallback_environment=data_dir_credentials,
                extensions=self._extensions,
                excluded_names=self._disabled_skill_names(),
                logger=self.logger,
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
                self._resolve_shared_skills_dir,
                self._resolve_external_skill_scope,
            )
            register_history_tool(self._tools, self._chat_sessions)
            self._projects = ProjectStore(self._storage.data_dir, sessions=self._chat_sessions)
            self._skill_runtime = SkillRuntime(
                registry=self._skills,
                policy=self._skill_policy,
                storage=self._storage,
                agents=self._agents,
                projects=lambda: self.projects,
                extensions=self._extensions,
                resources_path=resources_path,
                logger=self.logger,
                reload_skills=self.reload_skills,
            )
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
                change_tracker=self._change_tracker,
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
                groups_path=self._storage.layout.terminals / "groups.json",
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
                reasoning_render_describer=self.describe_reasoning_render,
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
            self._trigger_service.set_completion_run_relay(
                self._channel_service.relay_completion_run
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
                tz=timezone_name,
            )
            self._start_cron_service()
            self._calendar_service = CalendarService(self._storage.data_dir, tz=timezone_name)
            register_cron_tool(self._tools, self._cron_service)
            register_calendar_tool(self._tools, self._calendar_service)
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
                self.describe_reasoning_render,
                self.timezone_name,
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
                timezone_name=self.timezone_name,
            )

            self._log_startup_inventory()
            self._started = True
            self._start_provider_usage_service()
            self.logger.info("Runtime started")
        except Exception:
            self._cleanup_failed_startup()
            raise

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
        if self._keep_awake is not None:
            self._keep_awake.close()
        if self._storage is not None:
            self._storage.temporary_files.stop()
        if self._chat_sessions is not None:
            self._chat_sessions.close()

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
        if self._keep_awake is not None:
            self._keep_awake.close()
        if self._storage is not None:
            await self._storage.temporary_files.aclose()
        if self._chat_sessions is not None:
            self._chat_sessions.close()

        self._clear_service_references()
        self._log_manager.close()

    def _log_shutdown(self) -> None:
        if self.logger is not None:
            self.logger.info("Runtime stopped")

    def _cleanup_failed_startup(self) -> None:
        """Release every started resource after a failed synchronous bootstrap."""
        cleanup_actions = (
            (self._extensions, "fire_shutdown_blocking"),
            (self._channel_service, "stop"),
            (self._cron_service, "stop"),
            (self._bootstrap_service, "stop"),
            (self._provider_usage, "stop"),
            (self._process_manager, "stop"),
            (self._terminal_manager, "stop"),
            (self._keep_awake, "close"),
        )
        for service, method_name in cleanup_actions:
            if service is None:
                continue
            method = getattr(service, method_name)
            with suppress(Exception):
                method()
        if self._storage is not None:
            with suppress(Exception):
                self._storage.temporary_files.stop()
        if self._chat_sessions is not None:
            with suppress(Exception):
                self._chat_sessions.close()
        self._clear_service_references()
        self._log_manager.close()

    def _clear_service_references(self) -> None:
        self._provider_runtime = None
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
        self._keep_awake = None
        self._agents = None
        self._tools = None
        self._memory_service = None
        self._file_state = None
        self._process_manager = None
        self._terminal_manager = None
        self._skills = None
        self._skill_authoring = None
        self._skill_policy = None
        self._skill_runtime = None
        self._extensions = None
        self._extension_runtime = None
        self._chat_sessions = None
        self._projects = None
        self._agent_resolver = None
        self._recall_backend_registry = None
        self._recall_backend = None
        self._recall_backend_name = None
        self._channel_service = None
        self._cron_service = None
        self._calendar_service = None
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

    def _skill_operations(self) -> SkillRuntime:
        self._ensure_started()
        if self._skill_runtime is None or self._skills is None:
            raise RuntimeError("Skill runtime not available")
        self._skill_runtime.rebind(
            registry=self._skills,
            extensions=self._extensions,
            logger=self.logger,
        )
        return self._skill_runtime

    def agent_skills_dir(self, agent_id: str) -> Path:
        return self._skill_operations().agent_skills_dir(agent_id)

    def agent_owns_private_skill(self, agent_id: str, name: str) -> bool:
        return self._skill_operations().agent_owns_private_skill(agent_id, name)

    @property
    def global_skills_dir(self) -> Path:
        return self._skill_operations().global_skills_dir

    def skills_for(
        self,
        project_id: str | None,
        identity_agent_id: str | None = None,
    ) -> SkillRegistry:
        return self._skill_operations().skills_for(project_id, identity_agent_id)

    def refresh_skills_for(
        self,
        project_id: str | None,
        identity_agent_id: str | None = None,
    ) -> SkillRegistry:
        return self._skill_operations().refresh_skills_for(project_id, identity_agent_id)

    def project_own_skills(self, project_id: str) -> list[SkillMetadata]:
        return self._skill_operations().project_own_skills(project_id)

    def project_context_skills(self, project_id: str) -> list[SkillMetadata]:
        return self._skill_operations().project_context_skills(project_id)

    def skill_inventory(self) -> dict[str, Any]:
        return self._skill_operations().skill_inventory()

    def project_skill_names(self, project_id: str | None) -> frozenset[str]:
        return self._skill_operations().project_skill_names(project_id)

    def invalidate_project_skills(self, project_id: str | None = None) -> None:
        self._skill_operations().invalidate_project_skills(project_id)

    def invalidate_agent_skills(self, agent_id: str | None = None) -> None:
        self._skill_operations().invalidate_agent_skills(agent_id)

    def _resolve_shared_skills_dir(self, receiver_agent_id: str, name: str) -> Path | None:
        return self._skill_operations()._resolve_shared_skills_dir(receiver_agent_id, name)

    def _resolve_external_skill_scope(
        self,
        agent_id: str,
        name: str,
        project_id: str | None,
    ) -> str | None:
        return self._skill_operations()._resolve_external_skill_scope(
            agent_id,
            name,
            project_id,
        )

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

    def reload_keep_awake(self) -> None:
        """Apply the persisted ``keep_awake`` setting to the running process."""

        self._ensure_started()
        if self._storage is None or self._keep_awake is None:
            return
        settings = self._storage.load_settings()
        self._keep_awake.set_enabled(settings.get("keep_awake") is True)

    def timezone_name(self) -> str:
        """Return the effective persisted application timezone."""
        if self._storage is None:
            return "UTC"
        return effective_timezone_name(self._storage.load_settings())

    def reload_timezone(self) -> None:
        """Apply the persisted timezone to all live wall-clock services."""
        self._ensure_started()
        timezone_name = self.timezone_name()
        if self._cron_service is not None:
            self._cron_service.set_timezone(timezone_name)
        if self._calendar_service is not None:
            self._calendar_service.set_timezone(timezone_name)

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

    def _set_extension_registry(self, registry: ExtensionRegistry) -> None:
        self._extensions = registry

    def _extension_operations(self) -> ExtensionRuntime:
        self._ensure_started()
        if self._extension_runtime is None:
            raise RuntimeError("Extension runtime not available")
        return self._extension_runtime

    async def reload_extensions(self) -> None:
        """Rebuild the entire Extension layer from current Settings and disk."""
        await self._extension_operations().reload()

    async def apply_extension_disabled_change(self, newly_disabled: set[str]) -> None:
        """Deactivate newly disabled Extensions through the serialized live layer."""
        await self._extension_operations().apply_disabled_change(newly_disabled)

    def _recover_recall_backend_if_deactivated(self, removed_backend_names: set[str]) -> None:
        """Fall the active recall backend back to the default if its provider left.

        If a deactivated extension provided the *currently-selected* recall backend,
        the live backend instance now points into dormant extension code. Rather than
        leave recall broken, rebuild the registry (which no longer contains the
        deactivated extension's backend) and re-resolve: an unknown selected name
        falls back to the built-in default (``sqlite_fts``) with a warning, exactly
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
        reconcile. Backends without a derived index (the canonical scan) do not
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
        return self._skill_operations().load_global_registry()

    def _apply_reloaded_skills(self, skills: SkillRegistry) -> None:
        """Install a fully built Skill registry and refresh its loop-owned consumers."""
        self._skills = skills
        self._skill_operations().replace_registry(skills)
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
            register_skill_tool(self._tools, self.skills_for, self.reload_skills_async)
            if self._skill_authoring is not None:
                self._tools.unregister("skill_manage")
                register_skill_manage_tool(
                    self._tools,
                    self._skill_authoring,
                    self.agent_skills_dir,
                    self.invalidate_agent_skills,
                    self._resolve_shared_skills_dir,
                    self._resolve_external_skill_scope,
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
    def change_tracker(self) -> ChangeTracker:
        """Access to the shared session-scoped file-content change tracker.

        The same instance the read/write/edit tools and the chat loop use, so
        run-end change statistics are computed from the tools' recorded
        before/after content.
        """
        self._ensure_started()
        if self._change_tracker is None:
            raise RuntimeError("Change tracker service not available")
        return self._change_tracker

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
    def skill_policy(self) -> SkillPolicyService:
        """The Skills domain's central policy overlay (disable switch + sharing)."""
        self._ensure_started()
        if self._skill_policy is None:
            raise RuntimeError("Skill policy service not available")
        return self._skill_policy

    def _disabled_skill_names(self) -> frozenset[str]:
        """Return the policy's disabled names for registry-load exclusion.

        Called on every runtime-owned registry build; the manager-facing editor
        loads deliberately bypass this so disabled skills stay visible there.
        """
        if self._skill_policy is None:
            return frozenset()
        return self._skill_policy.load().disabled

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
        """Access to canonical Sessions."""
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
    def calendar_service(self) -> CalendarService:
        """Access to the persisted local calendar."""
        self._ensure_started()
        if self._calendar_service is None:
            raise RuntimeError("Calendar service not available")
        return self._calendar_service

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

    def _provider_operations(self) -> ProviderRuntime:
        self._ensure_started()
        if self._provider_runtime is None:
            raise RuntimeError("Provider runtime not available")
        self._provider_runtime.rebind(
            providers=self.providers,
            models=self.models,
            credentials=self.provider_credentials,
            token_store=self.token_store,
            storage=self.storage,
            logger=self.logger,
        )
        return self._provider_runtime

    def get_adapter(self, connection: ConnectionRef) -> ProviderAdapter:
        """Return a fully wired Adapter for one exact Provider Connection."""
        return self._provider_operations().get_adapter(connection)

    def get_connection_token_getter(self, connection: ConnectionRef) -> TokenGetter:
        """Return the refresh-capable token getter for one Provider Connection."""
        return self._provider_operations().get_connection_token_getter(connection)

    def get_connection_token_extra(self, connection: ConnectionRef) -> Mapping[str, str]:
        """Return persisted OAuth metadata for one Provider Connection."""
        return self._provider_operations().get_connection_token_extra(connection)

    def describe_reasoning_render(
        self,
        provider_id: str,
        model_id: str,
        effort: str | None,
    ) -> ReasoningIntent | None:
        """Return the Adapter's Provider-neutral Reasoning render description."""
        return self._provider_operations().describe_reasoning_render(
            provider_id,
            model_id,
            effort,
        )

    async def maybe_refresh_local_catalogs(self, *, force: bool = False) -> None:
        """Refresh enabled local Provider catalogs without disrupting live registries."""
        if self._provider_runtime is None:
            return
        await self._provider_operations().maybe_refresh_local_catalogs(force=force)

    def connection_reachability(self, connection_id: str) -> bool | None:
        """Return the latest local catalog probe outcome for one Connection."""
        if self._provider_runtime is None:
            return None
        return self._provider_operations().connection_reachability(connection_id)

    def _provider_connection_enabled_overrides(self) -> Mapping[str, bool]:
        """Return live per-Connection enabled overrides used during credential checks."""
        if self._storage is None:
            return {}
        try:
            connections = self._storage.load_providers_settings()["connections"]
        except StorageError as error:
            if self.logger is not None:
                self.logger.warning(
                    "Failed to load Provider Connection overrides: %s",
                    error,
                )
            return {}
        return cast("Mapping[str, bool]", connections)

    def local_context_windows(self) -> Mapping[str, Any]:
        """Return live user-configured context windows for local Models."""
        return self._provider_operations().local_context_windows()

    def has_provider_credentials(self, provider_id: str) -> bool:
        """Return whether a Provider has usable configured credentials."""
        return self._provider_operations().has_provider_credentials(provider_id)

    def get_provider_credentials(self, provider_id: str) -> str:
        """Return the configured credential value for a Provider."""
        return self._provider_operations().get_provider_credentials(provider_id)

    def get_model(self, provider_id: str, model_id: str) -> Model:
        """Look up one Model through the live Provider-owned registry view."""
        return self._provider_operations().get_model(provider_id, model_id)

    def _ensure_started(self) -> None:
        if not self._started:
            raise RuntimeError("Runtime not started — call start() first")
