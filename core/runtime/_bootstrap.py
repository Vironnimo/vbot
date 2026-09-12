"""Construct the service graph inside the Runtime lifecycle owner."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from functools import partial
from typing import TYPE_CHECKING, Protocol, cast
from uuid import uuid4

from core.agents.agents import AgentStore
from core.agents.temporary import TemporaryAgentRegistry
from core.attachments import AttachmentStore
from core.automation import BootstrapService, CronService, ReflectionService, TriggerService
from core.calendar import CalendarService
from core.channels import ChannelService
from core.chat import ChatLoop, ChatLoopDependencies, CommandDispatcher
from core.chat.block_resolver import ContentBlockResolver
from core.compaction import CompactionService
from core.extensions import ExtensionRegistry
from core.extensions.runtime import ExtensionRuntime
from core.memory import MemoryService
from core.model_tasks import (
    EmbeddingService,
    ImageService,
    LocalSpeechExecutor,
    MusicService,
    SpeechService,
    TaskModelService,
    VideoService,
)
from core.models.models import ModelRegistry
from core.projects import ProjectStore, build_agent_resolver
from core.prompts import (
    PromptAgentStore,
    SkillPromptRegistry,
    SystemPromptManager,
)
from core.providers.credentials import ProviderCredentialResolver
from core.providers.providers import ProviderRegistry
from core.providers.runtime import ProviderRuntime
from core.providers.token_store import TokenStore
from core.providers.usage import ProviderUsageService
from core.runs import ChatRunManager
from core.runtime._configuration import (
    _SKILLS_DIRNAME,
    _VBOT_ROOT,
    _detect_vbot_version,
    _disabled_skill_names,
    _extension_load_options,
    _extra_extension_directories,
    _global_agent_defaults,
    _positive_size_setting,
    _provider_connection_enabled_overrides,
    _resolve_resources_path,
)
from core.runtime._prompt_blocks import (
    _collect_prompt_block_definitions,
    _loaded_extension_names,
    _StorageManagerBlockStore,
)
from core.runtime._recall import RecallIntegration
from core.runtime.interfaces import (
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
from core.skills.skills import SkillRegistry
from core.storage.storage import StorageManager
from core.subagents import SubAgentCoordinator
from core.tools import (
    ChangeTracker,
    FileReadState,
    register_analyze_image_tool,
    register_apply_patch_tool,
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

if TYPE_CHECKING:
    from core.runtime.runtime import Runtime


def bootstrap(runtime: Runtime) -> None:
    """Start the runtime and initialise all services.

    Creates the ``vbot.core`` logger, loads provider and model
    registries from the resources directory, and signals that the
    application is ready.  Idempotent — calling ``start()``
    more than once is a no-op (logged at debug level).
    """
    if runtime._started:
        logger = runtime._log_manager.get_logger("core")
        logger.debug("Runtime already started — skipping")
        return

    try:
        runtime._started_at = datetime.now(UTC)
        runtime._startup_id = str(uuid4())
        resources_path = _resolve_resources_path(runtime.config)

        runtime._storage = StorageManager(config=runtime._config, resources_dir=resources_path)
        storage = runtime._storage
        if storage is None:
            raise RuntimeError("Storage service not available")
        runtime._storage.ensure_directories()
        runtime.logger = runtime._log_manager.get_logger("core")
        runtime.logger.info("Runtime startup initiated")
        runtime._storage.temporary_files.start()
        settings = runtime._storage.load_settings()
        timezone_name = effective_timezone_name(settings)
        attachment_max_size_bytes = _positive_size_setting(
            runtime.logger,
            settings,
            key="attachment_max_size_bytes",
            default=DEFAULT_ATTACHMENT_MAX_SIZE_BYTES,
        )
        runtime._speech_upload_max_size_bytes = _positive_size_setting(
            runtime.logger,
            settings,
            key="speech_upload_max_size_bytes",
            default=DEFAULT_SPEECH_UPLOAD_MAX_SIZE_BYTES,
        )
        # Keep-awake is applied as soon as Settings are usable so an enabled
        # server holds its power request for the whole lifetime of the process.
        runtime._keep_awake = KeepAwakeController(runtime.logger)
        runtime._keep_awake.set_enabled(settings.get("keep_awake") is True)
        runtime._attachment_store = AttachmentStore(
            runtime._storage.data_dir,
            max_size_bytes=attachment_max_size_bytes,
        )
        data_dir_credentials = runtime._storage.load_environment()
        runtime._fallback_environment = dict(data_dir_credentials)
        custom_providers = runtime._storage.load_custom_providers_settings()

        runtime._providers = ProviderRegistry.load(
            resources_path,
            custom_providers=custom_providers,
            tolerate_invalid=True,
        )
        runtime._token_store = TokenStore(runtime._storage.data_dir)
        runtime._provider_credentials = ProviderCredentialResolver(
            runtime._providers,
            fallback_credentials=data_dir_credentials,
            token_store=runtime._token_store,
            enabled_overrides_loader=lambda: _provider_connection_enabled_overrides(
                runtime._storage, runtime.logger
            ),
        )
        runtime._provider_usage = ProviderUsageService(
            runtime,
            data_root=runtime._storage.data_dir,
        )
        runtime._models = ModelRegistry.load(
            resources_path,
            runtime_models_dir=runtime._storage.layout.models,
            custom_providers=custom_providers,
        )
        runtime._provider_runtime = ProviderRuntime(
            providers=runtime._providers,
            models=runtime._models,
            credentials=runtime._provider_credentials,
            token_store=runtime._token_store,
            storage=runtime._storage,
            resources_path=resources_path,
            logger=runtime.logger,
        )
        local_speech = LocalSpeechExecutor(engines_dir=runtime._storage.layout.speech_engines)
        runtime._model_tasks = TaskModelService(
            runtime._providers,
            runtime._models,
            runtime._provider_credentials,
            runtime._storage,
            local_targets=local_speech.targets,
        )
        runtime._speech = SpeechService(
            runtime._model_tasks,
            runtime,
            runtime._storage.data_dir,
            local_executor=local_speech,
            transcription_audio_getter=runtime._storage.load_speech_settings,
        )
        runtime._image = ImageService(
            runtime._model_tasks,
            runtime,
            max_input_bytes=runtime._attachment_store.max_size_bytes,
        )
        runtime._video = VideoService(runtime._model_tasks, runtime)
        runtime._music = MusicService(runtime._model_tasks, runtime)
        runtime._embeddings = EmbeddingService(runtime._model_tasks, runtime)
        # Sessions are a canonical service: it opens and verifies one database
        # before any Agent lifecycle operation can create or validate a Session.
        # Partial startup must close every Session resource in reverse order so a
        # failed start does not leak descriptors or leave a half-open database.
        runtime._chat_sessions = ChatSessionManager(
            runtime._storage.data_dir,
            store_path=runtime._storage.layout.sessions_db_path,
        )
        runtime._agents = AgentStore(
            runtime._storage.data_dir,
            template_dir=resources_path / "workspace-templates",
            defaults_provider=lambda: storage.load_defaults().get("agent", {}),
            sessions=runtime._chat_sessions,
        )
        runtime._process_manager = ProcessManager(
            temporary_files=runtime._storage.temporary_files,
        )
        runtime._start_process_manager()
        runtime._tools = ToolRegistry()
        # Tool-owned System Prompt block declarations (D6): the tool side of the
        # unified contributor path. Project and Sub-Agent contribute their dynamic
        # catalogs/guidance here; the runtime hands declarations to the prompt
        # manager without importing tool classes into the prompt domain.
        runtime._tool_prompt_blocks = ToolPromptBlockRegistry()
        runtime._memory_service = MemoryService()
        # One read-before-write guard shared by read/write/edit: read stamps each
        # file, write/edit refuse an unread or externally-changed file (file_state.py).
        runtime._file_state = FileReadState()
        # Session-scoped file-content tracker for git-style change statistics
        # (change_tracker.py). Shared by read/write/edit and the chat loop.
        runtime._change_tracker = ChangeTracker()
        register_read_tool(
            runtime._tools,
            attachment_store=runtime._attachment_store,
            speech_service=runtime._speech,
            file_state=runtime._file_state,
            speech_max_size_bytes=runtime._speech_upload_max_size_bytes,
        )
        register_edit_tool(runtime._tools, file_state=runtime._file_state)
        register_apply_patch_tool(runtime._tools, file_state=runtime._file_state)
        register_glob_tool(runtime._tools)
        register_grep_tool(runtime._tools)
        register_write_tool(runtime._tools, file_state=runtime._file_state)
        register_memory_tool(runtime._tools, runtime._memory_service)
        register_web_fetch_tool(runtime._tools, attachment_store=runtime._attachment_store)
        register_web_search_tool(
            runtime._tools,
            runtime.resolve_environment_credential,
            runtime._storage.load_web_search_settings,
        )
        register_process_tool(runtime._tools, runtime._process_manager)
        register_text_to_speech_tool(runtime._tools, runtime._speech)
        register_analyze_image_tool(runtime._tools, runtime._image)
        register_image_generation_tool(runtime._tools, runtime._image)
        register_generate_video_tool(runtime._tools, runtime._video)
        register_generate_music_tool(runtime._tools, runtime._music)
        extension_dirs = _extra_extension_directories(runtime.logger, settings)
        disabled_extensions, extension_config = _extension_load_options(runtime.logger, settings)
        runtime._extensions = ExtensionRegistry.load(
            runtime._storage.data_dir / "extensions",
            extra_dirs=extension_dirs,
            disabled=disabled_extensions,
            config=extension_config,
            bundled_dir=resources_path / "extensions",
            config_provider=runtime._live_extension_config,
            credential_resolver=runtime.resolve_environment_credential,
        )
        failed_extension_count = len(runtime._extensions.diagnostics())
        if failed_extension_count > 0:
            runtime.logger.warning(
                "Loaded extensions with %s failed extensions; "
                "see vbot.extensions errors for details",
                failed_extension_count,
            )
        runtime._extension_runtime = ExtensionRuntime(
            storage=runtime._storage,
            resources_path=resources_path,
            tools=runtime._tools,
            get_registry=lambda: runtime._extensions,
            set_registry=runtime._set_extension_registry,
            get_command_dispatcher=lambda: runtime._command_dispatcher,
            extra_directories=partial(_extra_extension_directories, runtime.logger),
            load_options=partial(_extension_load_options, runtime.logger),
            live_config=runtime._live_extension_config,
            resolve_credential=runtime.resolve_environment_credential,
            reload_recall=runtime.reload_recall_backend,
            refresh_prompts=runtime._refresh_prompt_block_definitions,
            reload_skills=runtime.reload_skills,
            recover_recall=runtime._recover_recall_backend_if_deactivated,
            logger=runtime.logger,
            make_host=runtime._extension_host,
        )
        # Skills load after extensions: a loaded extension may bundle its own skills
        # under ``<extension>/skills/``, which ``_skill_scan_roots`` folds into the
        # global pool, so the extension layer must be in place first.
        runtime._skill_policy = SkillPolicyService(runtime._storage)
        runtime._skills = load_global_skill_registry(
            storage=runtime._storage,
            resources_path=resources_path,
            settings=settings,
            fallback_environment=data_dir_credentials,
            extensions=runtime._extensions,
            excluded_names=_disabled_skill_names(runtime._skill_policy),
            logger=runtime.logger,
        )
        invalid_skill_count = len(runtime._skills.invalid_diagnostics())
        if invalid_skill_count > 0:
            runtime.logger.warning(
                "Loaded skills with %s invalid skill directories; "
                "see vbot.skills warnings for details",
                invalid_skill_count,
            )
        register_skill_tool(runtime._tools, runtime.skills_for, runtime.reload_skills_async)
        # The agent skill-authoring write core refuses the bundled skills root;
        # ``skill_manage`` writes only the calling agent's private home.
        runtime._skill_authoring = SkillAuthoringService(
            protected_roots=[resources_path / _SKILLS_DIRNAME],
        )
        register_skill_manage_tool(
            runtime._tools,
            runtime._skill_authoring,
            runtime.agent_skills_dir,
            runtime.invalidate_agent_skills,
            runtime._resolve_shared_skills_dir,
            runtime._resolve_external_skill_scope,
        )
        register_history_tool(runtime._tools, runtime._chat_sessions)
        runtime._projects = ProjectStore(runtime._storage.data_dir, sessions=runtime._chat_sessions)
        runtime._skill_runtime = SkillRuntime(
            registry=runtime._skills,
            policy=runtime._skill_policy,
            storage=runtime._storage,
            agents=runtime._agents,
            projects=lambda: runtime.projects,
            extensions=runtime._extensions,
            resources_path=resources_path,
            logger=runtime.logger,
            reload_skills=runtime.reload_skills,
        )
        register_project_tool(
            runtime._tools,
            runtime._projects,
            lambda: runtime.system_prompts,
            runtime.project_context_skills,
            runtime._file_state,
            runtime._tool_prompt_blocks,
        )
        runtime._temporary_agents = TemporaryAgentRegistry(runtime._chat_sessions)
        runtime._agent_resolver = build_agent_resolver(
            runtime._agents,
            runtime._projects,
            runtime._models,
            runtime._providers,
            runtime._provider_credentials,
            lambda: _global_agent_defaults(runtime._storage),
            project_skill_names=runtime.project_skill_names,
            temporary_agents=runtime._temporary_agents,
        )
        runtime._agents.ensure_bootstrap()
        runtime._recall = RecallIntegration(
            storage=runtime._storage,
            sessions=runtime._chat_sessions,
            tools=runtime._tools,
            models=runtime._models,
            embeddings=runtime._embeddings,
            extensions=lambda: runtime._extensions,
            logger=runtime.logger,
        )
        runtime._chat_run_manager = ChatRunManager(
            admission_validator=runtime._validate_temporary_admission
        )
        runtime.chat_runs = runtime._chat_run_manager
        if runtime._attachment_store is None:
            raise RuntimeError("Attachment store not available")
        resolver = ContentBlockResolver(runtime._attachment_store, transcriber=runtime._speech)
        compaction_service = CompactionService()
        # The reflection service starts review runs through the runtime's
        # streaming loop lazily at review time, so constructing it before the
        # loops is safe — the loops only need its notify hook.
        runtime._reflection_service = ReflectionService(runtime)
        runtime._session_title_service = SessionTitleService(runtime)
        assert runtime._agent_resolver is not None
        assert runtime._projects is not None
        assert runtime._providers is not None
        assert runtime._models is not None
        assert runtime._provider_credentials is not None
        assert runtime._chat_sessions is not None
        assert runtime._chat_run_manager is not None
        assert runtime._tools is not None
        assert runtime._process_manager is not None
        assert runtime._file_state is not None
        assert runtime._storage is not None
        assert runtime._image is not None
        chat_dependencies = ChatLoopDependencies(
            agent_resolver=runtime._agent_resolver,
            projects=runtime._projects,
            providers=runtime._providers,
            models=runtime._models,
            provider_credentials=runtime._provider_credentials,
            sessions=runtime._chat_sessions,
            run_manager=runtime._chat_run_manager,
            tools=runtime._tools,
            process_manager=runtime._process_manager,
            file_read_state=runtime._file_state,
            change_tracker=runtime._change_tracker,
            storage=runtime._storage,
            get_extension_registry=lambda: runtime.extensions,
            get_system_prompts=lambda: runtime.system_prompts,
            get_adapter=runtime.get_adapter,
            resolve_skills=runtime.skills_for,
            refresh_skills=runtime.refresh_skills_for,
            get_local_context_windows=runtime.local_context_windows,
            image_understanding_available=runtime._image.analysis_is_available,
            deliver_background_completions=lambda run, session: (
                runtime._trigger_service.deliver_background_completions(run, session)
                if runtime._trigger_service is not None
                else False
            ),
        )
        runtime._chat_loop = ChatLoop(
            chat_dependencies,
            streaming=False,
            attachment_resolver=resolver,
            compaction_service=compaction_service,
            reflection_service=runtime._reflection_service,
            session_title_service=runtime._session_title_service,
        )
        runtime._streaming_chat_loop = ChatLoop(
            chat_dependencies,
            streaming=True,
            attachment_resolver=resolver,
            compaction_service=compaction_service,
            reflection_service=runtime._reflection_service,
            session_title_service=runtime._session_title_service,
        )
        runtime._trigger_service = TriggerService(
            runtime._chat_loop,
            runtime._chat_run_manager,
            runtime,
            trigger_chat_loop=runtime._streaming_chat_loop,
            sessions=runtime._chat_sessions,
        )
        runtime._trigger_service.set_owned_completion_starter(runtime._start_owned_completion)
        runtime._terminal_manager = TerminalManager(
            runtime._trigger_service,
            temporary_files=runtime._storage.temporary_files,
            launch_history_path=runtime._storage.layout.terminals / "launch-history.json",
            groups_path=runtime._storage.layout.terminals / "groups.json",
            data_dir=runtime._storage.data_dir,
        )
        runtime._start_terminal_manager()
        register_terminal_tool(runtime._tools, runtime._terminal_manager, runtime._projects)
        runtime._bootstrap_service = BootstrapService(
            runtime._trigger_service,
            runtime._storage.data_dir,
            startup_id=runtime._startup_id,
            agent_resolver=runtime._agent_resolver,
            sessions=runtime._chat_sessions,
        )
        runtime._command_dispatcher = CommandDispatcher(
            runtime._chat_run_manager,
            agent_resolver=runtime._agent_resolver,
            sessions=runtime._chat_sessions,
            models=runtime._models,
            started_at=runtime._started_at,
            providers=runtime._providers,
            projects=runtime._projects,
            agents=runtime._agents,
            local_context_windows_loader=runtime.local_context_windows,
            trigger_service=runtime._trigger_service,
            reflection_service=runtime._reflection_service,
            storage=runtime._storage,
            terminal_manager=runtime._terminal_manager,
            reasoning_render_describer=runtime.describe_reasoning_render,
        )
        if runtime._extensions is not None:
            runtime._extensions.apply_commands(runtime._command_dispatcher)
        runtime._channel_service = ChannelService(
            runtime._trigger_service,
            runtime._chat_sessions,
            agent_store=runtime._agents,
            data_root=runtime._storage.data_dir,
            credential_resolver=runtime.resolve_environment_credential,
            attachment_store=runtime._attachment_store,
            command_dispatcher=runtime._command_dispatcher,
            interaction_dispatcher=runtime._dispatch_channel_interaction,
        )
        runtime._trigger_service.set_completion_run_relay(
            runtime._channel_service.relay_completion_run
        )
        runtime._channel_service._notify_tool_registration_changed_hook = (
            runtime._reload_channel_tool_if_started
        )
        runtime._start_channel_service()
        runtime._sync_channel_tool_registration()
        runtime._cron_service = CronService(
            runtime._trigger_service,
            runtime._storage.data_dir,
            agent_resolver=runtime._agent_resolver,
            sessions=runtime._chat_sessions,
            tz=timezone_name,
        )
        runtime._start_cron_service()
        runtime._calendar_service = CalendarService(runtime._storage.data_dir, tz=timezone_name)
        runtime._calendar_service.actions.configure(
            runtime._trigger_service, runtime._agent_resolver, runtime._chat_sessions
        )
        runtime._start_calendar_service()
        register_cron_tool(runtime._tools, runtime._cron_service)
        register_calendar_tool(runtime._tools, runtime._calendar_service)
        register_bash_tool(
            runtime._tools,
            runtime._process_manager,
            runtime._trigger_service,
            credential_resolver=runtime.resolve_environment_credential,
            prompt_blocks=runtime._tool_prompt_blocks,
        )
        runtime._subagent_coordinator = SubAgentCoordinator(
            runtime,
            runtime._trigger_service,
            sessions=runtime._chat_sessions,
        )
        register_subagent_tools(
            runtime._tools,
            runtime._subagent_coordinator,
            runtime._tool_prompt_blocks,
        )
        register_status_tool(
            runtime._tools,
            runtime._agent_resolver,
            runtime._chat_sessions,
            runtime._models,
            runtime._chat_run_manager,
            runtime._started_at,
            runtime._providers,
            runtime._projects,
            runtime.local_context_windows,
            runtime.describe_reasoning_render,
            runtime.timezone_name,
        )
        # Built-ins are all registered now; apply extension tools last so a
        # collision with any built-in name is skipped (built-in wins), right
        # before SystemPromptManager consumes the registry.
        if runtime._extensions is not None:
            runtime._extensions.apply_tools(runtime._tools)
        runtime._system_prompts = SystemPromptManager(
            runtime._storage,
            runtime._tools,
            cast(SkillPromptRegistry, runtime._skills),
            channel_registry=cast(ChannelService, runtime._channel_service),
            vbot_version=str(runtime._config.get("VBOT_VERSION") or _detect_vbot_version()),
            vbot_root=_VBOT_ROOT,
            data_root=runtime._storage.data_dir,
            memory_provider=runtime._memory_service,
            block_definitions=_collect_prompt_block_definitions(
                runtime._tool_prompt_blocks, runtime._extensions
            ),
            loaded_extensions=_loaded_extension_names(runtime._extensions),
            block_store=_StorageManagerBlockStore(runtime._storage),
            agent_store=cast(PromptAgentStore, runtime._agents),
            timezone_name=runtime.timezone_name,
        )

        _log_startup_inventory(
            runtime.logger,
            runtime._providers,
            runtime._provider_credentials,
            runtime._tools,
            runtime._skills,
        )
        runtime._started = True
        runtime._start_provider_usage_service()
        runtime.logger.info("Runtime started")
    except Exception:
        runtime._cleanup_failed_startup()
        raise


def _log_startup_inventory(
    logger: LoggerProtocol | None,
    providers: ProviderRegistry | None,
    provider_credentials: ProviderCredentialResolverProtocol | None,
    tools: ToolRegistry | None,
    skills: SkillRegistry | None,
) -> None:
    if (
        logger is None
        or providers is None
        or provider_credentials is None
        or tools is None
        or skills is None
    ):
        return

    provider_ids = providers.list_ids()
    usable_provider_count = 0
    total_connection_count = 0
    usable_connection_count = 0

    for provider_id in provider_ids:
        provider_config = providers.get(provider_id)
        provider_is_usable = False

        for connection in provider_config.connections:
            total_connection_count += 1
            connection_id = f"{provider_id}:{connection.id}"
            if provider_credentials.is_usable(provider_id, connection_id):
                usable_connection_count += 1
                provider_is_usable = True

        if provider_is_usable:
            usable_provider_count += 1

    logger.info(
        "Runtime inventory: %s tools, %s skills, %s/%s usable providers, %s/%s usable connections",
        len(tools.list_tools()),
        len(skills.list_all()),
        usable_provider_count,
        len(provider_ids),
        usable_connection_count,
        total_connection_count,
    )


class _LoopService(Protocol):
    def start(self) -> None: ...


def start_event_loop_service(service: _LoopService | None, unavailable: str) -> None:
    """Start one producer only when bootstrap runs inside a serving Event Loop."""
    if service is None:
        raise RuntimeError(unavailable)
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    service.start()
