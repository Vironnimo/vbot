"""Construct the service graph inside the Runtime lifecycle owner."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from typing import TYPE_CHECKING, Any, Protocol, cast
from uuid import uuid4

from core.agents.agents import AgentStore
from core.agents.temporary import TemporaryAgentRegistry
from core.archive import ArchiveRetentionUnknownError, ArchiveService, ArchiveServices
from core.attachments import AttachmentStore
from core.automation import (
    AutomationReferences,
    BootstrapService,
    CronService,
    LearningChanges,
    LibrarianService,
    ReflectionService,
    TriggerService,
)
from core.calendar import CalendarService
from core.channels import ChannelService
from core.chat import ChatLoop, ChatLoopDependencies, CommandDispatcher
from core.chat.block_resolver import ContentBlockResolver
from core.compaction import CompactionService
from core.database import DatabaseError, canonical_database_path, retire_core_databases
from core.extensions import ExtensionRegistry
from core.extensions.runtime import ExtensionRuntime
from core.memory import MemoryService
from core.model_tasks import (
    EmbeddingService,
    ImageService,
    LocalEmbeddingExecutor,
    LocalSpeechExecutor,
    LocalTaskTargetRegistry,
    MusicService,
    SpeechService,
    TaskModelService,
    VideoService,
)
from core.model_tasks.decisions import DecisionService
from core.model_tasks.live import LiveVoiceService
from core.models.models import ModelRegistry
from core.performance import PerformanceService
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
from core.runtime._agent_rename import complete_pending_rename
from core.runtime._configuration import (
    _SKILLS_DIRNAME,
    _VBOT_ROOT,
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
from core.runtime._shutdown import clean_up_failed_startup
from core.runtime.databases import RETIRED_CANONICAL_DATABASES
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
from core.statistics import StatisticsIndex, StatisticsService
from core.storage.errors import StorageError
from core.storage.storage import StorageManager
from core.subagents import SubAgentCoordinator
from core.tools import (
    ChangeTracker,
    FileReadState,
    UpdateHandoffs,
    register_analyze_image_tool,
    register_apply_patch_tool,
    register_edit_tools,
    register_generate_music_tool,
    register_generate_video_tool,
    register_image_generation_tool,
    register_memory_tool,
    register_project_tool,
    register_read_tool,
    register_search_files_tool,
    register_shell_tool,
    register_skill_manage_tool,
    register_skill_tool,
    register_terminal_tool,
    register_text_to_speech_tool,
    register_web_fetch_tool,
    register_web_search_tool,
)
from core.tools.calendar import register_calendar_tool
from core.tools.classify import register_classify_tool
from core.tools.cron import register_cron_tool
from core.tools.status import register_status_tool
from core.tools.subagent import register_subagent_tools
from core.tools.terminal_manager import TerminalManager
from core.tools.tools import ToolPromptBlockRegistry, ToolRegistry
from core.usage import UsageRecorder
from core.utils.tls import prewarm_outbound_http
from core.utils.version import detect_build_identity

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
        runtime._open_log_manager().get_logger("core").debug("Runtime already started — skipping")
        return

    try:
        runtime._started_at = datetime.now(UTC)
        runtime._startup_id = str(uuid4())
        runtime._build = build = detect_build_identity()
        resources_path = _resolve_resources_path(runtime.config)

        runtime._storage = StorageManager(config=runtime._config, resources_dir=resources_path)
        storage = runtime._storage
        if storage is None:
            raise RuntimeError("Storage service not available")
        runtime._storage.ensure_directories()
        runtime.logger = runtime._open_log_manager().get_logger("core")
        runtime.logger.debug("Runtime startup initiated")
        runtime._storage.temporary_files.start()
        if runtime.safe_startup_mode is None:
            # Not in an update's verification start: its rollback needs every
            # database still registered as the pre-update snapshot recorded it.
            try:
                retire_core_databases(runtime._storage.data_dir, RETIRED_CANONICAL_DATABASES)
            except DatabaseError as exc:
                runtime.logger.warning(
                    "Retired databases stay registered until the next start: %s", exc
                )
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
        # Opens the canonical provider-usage.db in every startup mode, so an
        # update's verification start checks it; only normal startup samples.
        runtime._provider_usage = ProviderUsageService(
            runtime,
            data_root=runtime._storage.data_dir,
        )
        runtime._models = ModelRegistry.load(
            resources_path,
            runtime_models_dir=runtime._storage.layout.models,
            custom_providers=custom_providers,
        )
        runtime._usage_recorder = UsageRecorder(
            canonical_database_path(runtime._storage.data_dir, "model_usage"),
            pricing_lookup=runtime._models.pricing_for,
        )
        runtime._provider_runtime = ProviderRuntime(
            providers=runtime._providers,
            models=runtime._models,
            credentials=runtime._provider_credentials,
            token_store=runtime._token_store,
            storage=runtime._storage,
            resources_path=resources_path,
            logger=runtime.logger,
            custom_providers=custom_providers,
        )
        # Outbound HTTP clients share one TLS context and lazily imported
        # transport modules; prepare both off the Event Loop.
        prewarm_outbound_http()
        local_speech = LocalSpeechExecutor(engines_dir=runtime._storage.layout.speech_engines)
        local_embeddings = LocalEmbeddingExecutor(
            engines_dir=runtime._storage.layout.embedding_engines
        )
        runtime._model_tasks = TaskModelService(
            runtime._providers,
            runtime._models,
            runtime._provider_credentials,
            runtime._storage,
            local_targets=LocalTaskTargetRegistry(
                [*local_speech.targets.descriptors(), *local_embeddings.targets.descriptors()]
            ),
        )
        runtime._speech = SpeechService(
            runtime._model_tasks,
            runtime,
            runtime._storage.data_dir,
            local_executor=local_speech,
            transcription_audio_getter=runtime._storage.load_speech_settings,
            usage_recorder=runtime._usage_recorder,
        )
        runtime._image = ImageService(
            runtime._model_tasks,
            runtime,
            max_input_bytes=runtime._attachment_store.max_size_bytes,
            usage_recorder=runtime._usage_recorder,
        )
        runtime._video = VideoService(
            runtime._model_tasks, runtime, usage_recorder=runtime._usage_recorder
        )
        runtime._music = MusicService(
            runtime._model_tasks, runtime, usage_recorder=runtime._usage_recorder
        )
        runtime._embeddings = EmbeddingService(
            runtime._model_tasks,
            runtime,
            usage_recorder=runtime._usage_recorder,
            local_executor=local_embeddings,
        )
        runtime._decisions = DecisionService(
            runtime._model_tasks, runtime, usage_recorder=runtime._usage_recorder
        )
        runtime._live_voice = LiveVoiceService(
            runtime._model_tasks, runtime, usage_recorder=runtime._usage_recorder
        )
        # Sessions are a canonical service: it opens and verifies one database
        # before any Agent lifecycle operation can create or validate a Session.
        # Partial startup must close every Session resource in reverse order so a
        # failed start does not leak descriptors or leave a half-open database.
        runtime._chat_sessions = ChatSessionManager(
            runtime._storage.data_dir,
            store_path=runtime._storage.layout.sessions_db_path,
        )
        # Retained Usage enters the ledger before any Agent lifecycle work or
        # accessor can delete history. It resumes from its persisted cursor, so a
        # normal start reads only entries written since the previous import.
        runtime._usage_recorder.import_session_history(runtime._chat_sessions)
        # One owner of the disposable Statistics index for every reader (RPC
        # reports and Extension group usage); it holds no open resources.
        runtime._statistics_index = StatisticsIndex(runtime._storage.data_dir)
        runtime._agents = AgentStore(
            runtime._storage.data_dir,
            template_dir=resources_path / "workspace-templates",
            defaults_provider=lambda: storage.load_defaults().get("agent", {}),
            sessions=runtime._chat_sessions,
            snapshot_barrier=runtime._snapshot_barrier,
        )
        # A Session of an Identity Agent whose creator names no working Project
        # (Channels, Automation, wake word) works in the Agent's default Project.
        runtime._chat_sessions.set_agent_default_project(
            partial(_agent_default_project, runtime._agents)
        )
        # An Identity Agent rename interrupted by the last process ends before any
        # roster read or bootstrap Agent: its Agent-owned half first, its references
        # once their owners exist and before any of them starts.
        pending_rename = runtime._agents.recover_rename()
        # Shell update handoffs are claimable only by this server process; files
        # claimed by earlier processes are retained for one update's lifetime.
        runtime._update_handoffs = UpdateHandoffs(runtime._storage.data_dir)
        runtime._update_handoffs.remove_expired_files()
        runtime._tools = ToolRegistry()
        # Tool-owned System Prompt block declarations (D6): the tool side of the
        # unified contributor path. Project and Sub-Agent contribute their dynamic
        # catalogs/guidance here; the runtime hands declarations to the prompt
        # manager without importing tool classes into the prompt domain.
        runtime._tool_prompt_blocks = ToolPromptBlockRegistry()
        # Each Agent's Memory history lives beside its default Workspace.
        runtime._memory_service = MemoryService(history_root=runtime._storage.layout.agents)
        # Read stamps protect full-file writes; the file edit Tools use the same
        # state for mutation locks and post-success drift warnings (file_state.py).
        runtime._file_state = FileReadState()
        # Session-scoped file-content tracker for git-style change statistics
        # (change_tracker.py). Shared by the file edit Tools and the chat loop.
        runtime._change_tracker = ChangeTracker()
        register_read_tool(
            runtime._tools,
            attachment_store=runtime._attachment_store,
            speech_service=runtime._speech,
            file_state=runtime._file_state,
            speech_max_size_bytes=runtime._speech_upload_max_size_bytes,
        )
        register_apply_patch_tool(runtime._tools, file_state=runtime._file_state)
        register_edit_tools(runtime._tools, file_state=runtime._file_state)
        register_search_files_tool(runtime._tools)
        register_memory_tool(runtime._tools, runtime._memory_service)
        register_web_fetch_tool(
            runtime._tools,
            attachment_store=runtime._attachment_store,
            temporary_files=runtime._storage.temporary_files,
            credential_resolver=runtime.resolve_environment_credential,
            settings_loader=runtime._storage.load_web_fetch_settings,
        )
        register_web_search_tool(
            runtime._tools,
            runtime.resolve_environment_credential,
            runtime._storage.load_web_search_settings,
        )
        register_text_to_speech_tool(runtime._tools, runtime._speech)
        register_classify_tool(runtime._tools, runtime._decisions)
        register_analyze_image_tool(
            runtime._tools, runtime._image, attachment_store=runtime._attachment_store
        )
        register_image_generation_tool(runtime._tools, runtime._image)
        register_generate_video_tool(runtime._tools, runtime._video)
        register_generate_music_tool(runtime._tools, runtime._music)
        extension_dirs = _extra_extension_directories(runtime.logger, settings)
        disabled_extensions, extension_config = _extension_load_options(runtime.logger, settings)
        if runtime.safe_startup_mode is None:
            from core.extensions.dependencies import activate as activate_extension_dependencies

            activate_extension_dependencies(runtime._storage.data_dir)
            runtime._extensions = ExtensionRegistry.load(
                runtime._storage.data_dir / "extensions",
                extra_dirs=extension_dirs,
                disabled=disabled_extensions,
                config=extension_config,
                bundled_dir=resources_path / "extensions",
                config_provider=runtime._live_extension_config,
                credential_resolver=runtime.resolve_environment_credential,
            )
        else:
            runtime._extensions = ExtensionRegistry()
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
            reload_skills=runtime.reload_skills_async,
            recover_recall=runtime._recover_recall_backend_if_deactivated,
            logger=runtime.logger,
            make_host=runtime._extension_host,
            archive_uninstalled_groups=runtime._archive_uninstalled_extension_groups,
        )
        # Skills load after extensions: a loaded extension may bundle its own skills
        # under ``<extension>/skills/``, which ``_global_roots`` folds into the
        # global pool, so the extension layer must be in place first.
        runtime._skill_policy = SkillPolicyService(runtime._storage)
        runtime._skills = load_global_skill_registry(
            storage=runtime._storage,
            resources_path=resources_path,
            settings=settings,
            fallback_environment=data_dir_credentials,
            extensions=runtime._extensions,
            disabled_packages=runtime._skill_policy.load().disabled_packages,
            logger=runtime.logger,
        )
        invalid_skill_count = len(runtime._skills.invalid_diagnostics())
        if invalid_skill_count > 0:
            runtime.logger.warning(
                "Loaded skills with %s invalid skill directories; "
                "see vbot.skills warnings for details",
                invalid_skill_count,
            )
        register_skill_tool(
            runtime._tools,
            runtime.skills_for,
            runtime.reload_skills_async,
            runtime.archived_skill,
            runtime.background_skill_protection,
        )
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
            lifecycle_guard=runtime._agents.lifecycle_guard,
            on_changed=runtime._notify_skills_changed,
            run_started_at=runtime.run_started_at,
            follow_merge=runtime.follow_skill_merge,
            resolve_global_skills_dir=lambda: runtime.global_skills_dir,
            publish_skill=runtime._publish_agent_skill,
            refresh_global_skills=runtime.reload_skills_async,
        )
        runtime._projects = ProjectStore(
            runtime._storage.data_dir,
            sessions=runtime._chat_sessions,
            snapshot_barrier=runtime._snapshot_barrier,
        )
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
            authoring=runtime._skill_authoring,
        )
        register_project_tool(
            runtime._tools,
            runtime._projects,
            lambda: runtime.system_prompts,
            runtime.project_context_skills,
            runtime._file_state,
            runtime._tool_prompt_blocks,
        )
        runtime._temporary_agents = TemporaryAgentRegistry(
            runtime._chat_sessions,
            prepare_config=lambda config, project_id: (
                runtime.agent_resolver.prepare_temporary_config(config, project_id)
            ),
        )
        runtime._agent_resolver = build_agent_resolver(
            runtime._agents,
            runtime._projects,
            runtime._models,
            runtime._providers,
            runtime._provider_credentials,
            lambda: _global_agent_defaults(runtime._storage),
            project_skill_names=runtime.project_skill_names,
            profile_skill_content=runtime.profile_skill_content,
            skill_pool_names=lambda project_id: frozenset(
                skill.name for skill in runtime.skills_for(project_id).list_all()
            ),
            tool_names=lambda: tuple(tool.name for tool in runtime.tools.list_tools()),
            temporary_agents=runtime._temporary_agents,
            sessions=runtime._chat_sessions,
        )
        # Creating the bootstrap Agent and the built-in Agents enters the snapshot
        # barrier on the calling thread, the Event Loop in the server lifespan. It never
        # waits there: no request, and so no data snapshot, is served before startup
        # completes.
        runtime._agents.ensure_bootstrap()
        runtime._agents.ensure_builtin_agents()
        runtime._recall = RecallIntegration(
            storage=runtime._storage,
            sessions=runtime._chat_sessions,
            tools=runtime._tools,
            models=runtime._models,
            embeddings=runtime._embeddings,
            extensions=lambda: runtime._extensions,
            logger=runtime.logger,
        )
        runtime._chat_sessions.recover_interrupted_runs()
        runtime._chat_run_manager = ChatRunManager(
            persistence=runtime._chat_sessions,
            admission_validator=runtime._validate_temporary_admission,
        )
        runtime.chat_runs = runtime._chat_run_manager
        runtime._recall.observe_runs(runtime._chat_run_manager)
        runtime._performance = _build_performance_service(
            runtime._storage, runtime._chat_run_manager
        )
        if runtime._attachment_store is None:
            raise RuntimeError("Attachment store not available")
        resolver = ContentBlockResolver(runtime._attachment_store, transcriber=runtime._speech)
        assert runtime._models is not None
        compaction_service = CompactionService(
            pricing_lookup=runtime._models.pricing_for, usage_recorder=runtime._usage_recorder
        )
        # The reflection service starts review runs through the runtime's
        # streaming loop lazily at review time, so constructing it before the
        # loops is safe — the loops only need its notify hook.
        runtime._reflection_service = ReflectionService(
            runtime,
            # Built later; a background review leaves the Skills to a running pass.
            librarian_running=lambda agent_id: (
                runtime._librarian_service is not None
                and runtime._librarian_service.running(agent_id)
            ),
        )
        # What a Run changed in Memory and the Agent's own Skills, and its undo.
        assert runtime._memory_service is not None
        assert runtime._skill_authoring is not None
        assert runtime._chat_run_manager is not None
        runtime._learning_changes = LearningChanges(
            memory=runtime._memory_service,
            skills=runtime._skill_authoring,
            skill_home=runtime.agent_skills_dir,
            run_active=runtime._chat_run_manager.is_running,
        )
        runtime._session_title_service = SessionTitleService(
            runtime, usage_recorder=runtime._usage_recorder
        )
        assert runtime._agent_resolver is not None
        assert runtime._projects is not None
        assert runtime._providers is not None
        assert runtime._models is not None
        assert runtime._provider_credentials is not None
        assert runtime._chat_sessions is not None
        assert runtime._chat_run_manager is not None
        assert runtime._tools is not None
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
            usage_recorder=runtime._usage_recorder,
            deliver_background_completions=lambda run, session: (
                runtime._trigger_service.deliver_background_completions(run, session)
                if runtime._trigger_service is not None
                else False
            ),
            get_terminal_manager=lambda: runtime._terminal_manager,
            subagent_taken_over=lambda address: (
                runtime._subagent_coordinator.subagent_taken_over(address)
                if runtime._subagent_coordinator is not None
                else None
            ),
        )
        runtime._chat_loop = ChatLoop(
            chat_dependencies,
            attachment_resolver=resolver,
            compaction_service=compaction_service,
            reflection_service=runtime._reflection_service,
            session_title_service=runtime._session_title_service,
        )
        runtime._trigger_service = TriggerService(
            runtime._chat_loop,
            runtime._chat_run_manager,
            runtime,
            sessions=runtime._chat_sessions,
        )
        runtime._trigger_service.set_owned_completion_starter(runtime._start_owned_completion)
        runtime._trigger_service.set_owned_completion_validator(runtime._validate_owned_completion)
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
        runtime._cron_service = CronService(
            runtime._trigger_service,
            runtime._storage.data_dir,
            agent_resolver=runtime._agent_resolver,
            sessions=runtime._chat_sessions,
            tz=timezone_name,
        )
        runtime._calendar_service = CalendarService(runtime._storage.data_dir, tz=timezone_name)
        runtime._calendar_service.actions.configure(
            runtime._trigger_service, runtime._agent_resolver, runtime._chat_sessions
        )
        runtime._automation_references = AutomationReferences(
            bootstrap=runtime._bootstrap_service,
            cron=runtime._cron_service,
            calendar=runtime._calendar_service,
        )
        runtime._librarian_service = _librarian_service(runtime)
        runtime._archive = ArchiveService(_archive_services(runtime))
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
            reasoning_render_describer=runtime.describe_agent_reasoning_render,
            wire_profile_describer=runtime.describe_agent_wire_profile,
            automation_references=runtime._automation_references,
            snapshot_barrier=runtime._snapshot_barrier,
            stop_all=lambda address: runtime.subagents.stop_tree(address),
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
        if pending_rename is not None:
            complete_pending_rename(runtime._agent_rename_services(), pending_rename)
        # After a settled rename, before Channels, Cron and Runs: finish or undo
        # interrupted archive operations.
        runtime._archive.recover()
        if runtime.safe_startup_mode is None:
            runtime._start_channel_service()
        runtime._sync_channel_tool_registration()
        if runtime.safe_startup_mode is None:
            runtime._start_cron_service()
            runtime._start_calendar_service()
        register_cron_tool(
            runtime._tools,
            runtime._cron_service,
            reference_lock=runtime._automation_references.lock,
        )
        register_calendar_tool(
            runtime._tools,
            runtime._calendar_service,
            reference_lock=runtime._automation_references.lock,
        )
        register_shell_tool(
            runtime._tools,
            runtime._terminal_manager,
            credential_resolver=runtime.resolve_environment_credential,
            prompt_blocks=runtime._tool_prompt_blocks,
            update_handoffs=runtime._update_handoffs,
            projects=runtime._projects,
        )
        runtime._subagent_coordinator = SubAgentCoordinator(runtime, runtime._trigger_service)
        runtime._subagent_coordinator.install(runtime._chat_run_manager)
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
            runtime.describe_agent_reasoning_render,
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
            vbot_version=str(runtime._config.get("VBOT_VERSION") or build.version),
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

        runtime._startup_summary = _summarize_startup(runtime)
        runtime._started = True
        # Measurement only observes; safe modes are measured like normal serving.
        runtime._start_performance_service()
        if runtime.safe_startup_mode is None:
            runtime._start_provider_usage_service()
            runtime._start_recall_indexing()
            runtime._start_archive_retention()
            runtime._start_config_backups()
            runtime._start_librarian()
        runtime.logger.debug("Runtime started (%s)", runtime._startup_summary.describe())
    except Exception as error:
        _log_startup_failure(runtime)
        startup_error = error
    else:
        return
    # Outside the handler, a failed cleanup step's log entry does not repeat the
    # startup traceback as its context.
    clean_up_failed_startup(runtime)
    raise startup_error


def _agent_default_project(agents: AgentStore, agent_id: str) -> str | None:
    """Return the default Project of an Identity Agent, ``None`` for none or no Agent."""
    agent = agents.find(agent_id)
    return None if agent is None else agent.root_project_id


def _librarian_service(runtime: Runtime) -> LibrarianService:
    """The Librarian over the private Skill homes, Skill use and live automations."""
    authoring = runtime._skill_authoring
    automation = runtime._automation_references
    sessions = runtime._chat_sessions
    agents = runtime._agents
    if authoring is None or automation is None or sessions is None or agents is None:
        raise RuntimeError("Librarian services are not available")
    # The Agent and Project stores satisfy the directory protocols structurally.
    usage = StatisticsService(
        sessions,
        cast(Any, agents),
        cast(Any, runtime._projects),
        index=runtime._statistics_index,
        usage_recorder=runtime._usage_recorder,
    )

    def skills_changed(agent_id: str) -> None:
        runtime.invalidate_agent_skills(agent_id)
        runtime._notify_skills_changed()

    return LibrarianService(
        runtime,
        authoring=authoring,
        skills_dir=runtime.agent_skills_dir,
        skill_usage=usage.skill_usage_async,
        triggered_skill_names=automation.agent_triggered_skill_names,
        shared_skill_receivers=runtime.shared_skill_receivers,
        skills_changed=skills_changed,
        # The Skill manager shows the Librarian; its observers reload on Skill changes.
        status_changed=runtime._notify_skills_changed,
        reviewing=lambda agent_id: (
            runtime._reflection_service is not None
            and runtime._reflection_service.reviewing(agent_id)
        ),
    )


def _archive_services(runtime: Runtime) -> ArchiveServices:
    """The owners archive operations compose; startup builds them before this."""
    storage = runtime._storage
    sessions = runtime._chat_sessions
    agents = runtime._agents
    projects = runtime._projects
    resolver = runtime._agent_resolver
    runs = runtime._chat_run_manager
    automation = runtime._automation_references
    terminals = runtime._terminal_manager
    if (
        storage is None
        or sessions is None
        or agents is None
        or projects is None
        or resolver is None
        or runs is None
        or automation is None
        or terminals is None
    ):
        raise RuntimeError("Archive services are not available")

    def import_usage() -> None:
        # A purge deletes Sessions only after their usage reached the usage ledger.
        if runtime._usage_recorder is None:
            raise RuntimeError("the usage ledger is not available")
        runtime._usage_recorder.import_session_history(sessions)

    async def remove_agent_from_recall(agent_id: str) -> None:
        await runtime.recall.remove_agent_from_recall(agent_id)

    def invalidate_project(project_id: str) -> None:
        resolver.invalidate_team_cache(project_id)
        runtime.invalidate_project_skills(project_id)

    def retention_days() -> int | None:
        # Strict: a degraded settings file must never widen retention to the default.
        try:
            days: int | None = storage.load_archive_settings(strict=True)["retention_days"]
        except StorageError as error:
            raise ArchiveRetentionUnknownError(str(error)) from error
        return days

    return ArchiveServices(
        data_dir=storage.data_dir,
        sessions=sessions,
        agents=agents,
        projects=projects,
        agent_resolver=resolver,
        runs=runs,
        automation=automation,
        terminals=terminals,
        snapshot_barrier=runtime._snapshot_barrier,
        agent_references=runtime.agent_references,
        import_usage=import_usage,
        remove_agent_from_recall=remove_agent_from_recall,
        remove_session_from_recall=runtime.remove_session_from_recall,
        invalidate_agent_skills=runtime.invalidate_agent_skills,
        invalidate_project=invalidate_project,
        retention_days=retention_days,
    )


def _build_performance_service(
    storage: StorageManager, run_manager: ChatRunManager
) -> PerformanceService:
    """Build the process measurement owner with the Run gauges it samples."""
    return PerformanceService(
        storage.layout.performance,
        samplers={
            "runs.active": lambda: len(run_manager.active_runs()),
            "runs.queued": lambda: len(run_manager.all_queued()),
        },
    )


def _log_startup_failure(runtime: Runtime) -> None:
    """Record a failed startup with its traceback before cleanup closes logging.

    The server host reports the re-raised error only after cleanup has closed the
    managed handlers, so this is the failure's single entry in the log file. A
    missing data root is never created just to hold the log.
    """
    if not runtime._data_dir.exists():
        return
    with suppress(Exception):
        runtime._open_log_manager().get_logger("core").exception("Runtime startup failed")


@dataclass(frozen=True)
class RuntimeStartupSummary:
    """What a Runtime served when its startup finished."""

    tools: int
    skills: int
    usable_providers: int
    providers: int
    usable_connections: int
    connections: int
    extensions: int
    keep_awake: bool

    def describe(self) -> str:
        """Return the summary as ``key=value`` log fields."""
        return (
            f"tools={self.tools} skills={self.skills}"
            f" providers={self.usable_providers}/{self.providers}"
            f" connections={self.usable_connections}/{self.connections}"
            f" extensions={self.extensions} keep_awake={'on' if self.keep_awake else 'off'}"
        )


def _summarize_startup(runtime: Runtime) -> RuntimeStartupSummary:
    providers = runtime._providers
    provider_credentials = runtime._provider_credentials
    provider_ids: list[str] = []
    usable_provider_count = 0
    total_connection_count = 0
    usable_connection_count = 0

    if providers is not None and provider_credentials is not None:
        provider_ids = providers.list_ids()
        for provider_id in provider_ids:
            provider_is_usable = False
            for connection in providers.get(provider_id).connections:
                total_connection_count += 1
                if provider_credentials.is_usable(provider_id, f"{provider_id}:{connection.id}"):
                    usable_connection_count += 1
                    provider_is_usable = True
            if provider_is_usable:
                usable_provider_count += 1

    return RuntimeStartupSummary(
        tools=len(runtime._tools.list_tools()) if runtime._tools is not None else 0,
        skills=len(runtime._skills.list_all()) if runtime._skills is not None else 0,
        usable_providers=usable_provider_count,
        providers=len(provider_ids),
        usable_connections=usable_connection_count,
        connections=total_connection_count,
        extensions=len(_loaded_extension_names(runtime._extensions)),
        keep_awake=runtime._keep_awake is not None and runtime._keep_awake.active,
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
