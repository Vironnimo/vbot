"""vBot runtime bootstrap.

The ``Runtime`` class is the single entry point that wires together
all core services and manages the application lifecycle.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from core.agents.agents import AgentStore
from core.agents.temporary import TemporaryAgentRegistry
from core.attachments import AttachmentStore
from core.automation import BootstrapService, CronService, ReflectionService, TriggerService
from core.calendar import CalendarService
from core.channels import ChannelService
from core.chat import ChatLoop, CommandDispatcher
from core.extensions import (
    ExtensionRegistry,
    InteractionEvent,
    InteractionResponder,
)
from core.extensions.operations import ExtensionHost
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
)
from core.prompts import (
    SkillPromptRegistry,
    SystemPromptManager,
)
from core.providers.accounts import ConnectionRef
from core.providers.adapter import ProviderAdapter
from core.providers.providers import ProviderRegistry
from core.providers.reasoning import ReasoningIntent
from core.providers.runtime import ProviderRuntime
from core.providers.token_getter import TokenGetter
from core.providers.token_store import TokenStore
from core.providers.usage import ProviderUsageService
from core.recall import RecallBackend
from core.runs import ChatRunManager
from core.runtime._bootstrap import bootstrap, start_event_loop_service
from core.runtime._configuration import _resolve_data_dir, _resolve_resources_path
from core.runtime._extension_host import ExtensionHostFactory
from core.runtime._prompt_blocks import refresh_prompt_blocks
from core.runtime._recall import RecallIntegration
from core.runtime._service_access import _StartedService
from core.runtime._workers import _RUNTIME_WORKERS
from core.runtime.interfaces import (
    ConfigProtocol,
    LoggerProtocol,
    ProviderCredentialResolverProtocol,
)
from core.runtime.keep_awake import KeepAwakeController
from core.sessions import ChatSessionManager
from core.sessions.titles import SessionTitleService
from core.settings.paths import DEFAULT_SPEECH_UPLOAD_MAX_SIZE_BYTES
from core.settings.settings import effective_timezone_name
from core.skills.authoring import SkillAuthoringService
from core.skills.policy import SkillPolicyService
from core.skills.runtime import SkillRuntime
from core.skills.skills import SkillMetadata, SkillRegistry
from core.storage.storage import StorageManager
from core.subagents import SubAgentCoordinator
from core.tools import (
    ChangeTracker,
    FileReadState,
    register_skill_manage_tool,
    register_skill_tool,
)
from core.tools.process_manager import ProcessManager
from core.tools.terminal_manager import TerminalManager, TerminalManagerError
from core.tools.tools import ToolPromptBlockRegistry, ToolRegistry
from core.utils.logging import LogManager


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
        """Prepare logging and empty service state from the injected configuration."""
        self._config = config
        self._data_dir = _resolve_data_dir(config)
        self._log_manager = LogManager(
            level=config.get("LOG_LEVEL", "INFO"), data_dir=self._data_dir
        )
        self.logger: LoggerProtocol | None = None
        self._speech_upload_max_size_bytes = DEFAULT_SPEECH_UPLOAD_MAX_SIZE_BYTES
        self._extension_change_publisher: Callable[[str, str, Sequence[str], int], None] | None = (
            None
        )
        self._clear_service_references()

    def _clear_service_references(self) -> None:
        """Initialize or release every service reference in one lifecycle-owned place."""
        self._extension_host_factory: ExtensionHostFactory | None = None
        self._started: bool = False
        self._started_at: datetime | None = None
        self._startup_id: str | None = None
        self._provider_runtime: ProviderRuntime | None = None
        self._providers: ProviderRegistry | None = None
        self._provider_credentials: ProviderCredentialResolverProtocol | None = None
        self._provider_usage: ProviderUsageService | None = None
        self._token_store: TokenStore | None = None
        self._fallback_environment: dict[str, str] = {}
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
        self._agents: AgentStore | None = None
        self._tools: ToolRegistry | None = None
        self._memory_service: MemoryService | None = None
        self._file_state: FileReadState | None = None
        self._process_manager: ProcessManager | None = None
        self._terminal_manager: TerminalManager | None = None
        self._skills: SkillRegistry | None = None
        self._skill_authoring: SkillAuthoringService | None = None
        self._skill_policy: SkillPolicyService | None = None
        self._skill_runtime: SkillRuntime | None = None
        self._extensions: ExtensionRegistry | None = None
        self._extension_runtime: ExtensionRuntime | None = None
        self._chat_sessions: ChatSessionManager | None = None
        self._projects: ProjectStore | None = None
        self._agent_resolver: AgentResolver | None = None
        self._temporary_agents: TemporaryAgentRegistry | None = None
        self._recall: RecallIntegration | None = None
        self._channel_service: ChannelService | None = None
        self._cron_service: CronService | None = None
        self._calendar_service: CalendarService | None = None
        self._bootstrap_service: BootstrapService | None = None
        self._trigger_service: TriggerService | None = None
        self._reflection_service: ReflectionService | None = None
        self._session_title_service: SessionTitleService | None = None
        self._subagent_coordinator: SubAgentCoordinator | None = None
        self._chat_loop: ChatLoop | None = None
        self._streaming_chat_loop: ChatLoop | None = None
        self._command_dispatcher: CommandDispatcher | None = None
        self._chat_run_manager: ChatRunManager | None = None
        self.chat_runs: ChatRunManager | None = None
        self._system_prompts: SystemPromptManager | None = None
        self._change_tracker: ChangeTracker | None = None
        self._tool_prompt_blocks: ToolPromptBlockRegistry | None = None

    def start(self) -> None:
        """Start the dependency-ordered service graph, cleaning up a failed bootstrap."""
        bootstrap(self)

    async def fire_extension_startup(self) -> None:
        """Fire extension startup handlers once bootstrap is complete and serving.

        Called by the server from inside its async lifespan, so startup handlers
        run on the live serving loop (they may schedule background tasks there).
        No-op before ``start()`` / after shutdown.
        """
        if self._extensions is not None:
            self._extensions.bind_host(self._extension_host())
            await self._extensions.fire_startup()

    def _host_operations(self) -> ExtensionHostFactory:
        self._ensure_started()
        if self._extension_host_factory is None:
            if self._temporary_agents is None:
                raise RuntimeError("temporary execution is unavailable")
            self._extension_host_factory = ExtensionHostFactory(
                host=ExtensionHost(
                    data_dir=self.storage.data_dir,
                    sample=self._sample_extension,
                    resolve_agent=self.agent_resolver.resolve_agent,
                    store_attachment=self.attachment_store.store,
                    resolve_credential=self.resolve_environment_credential,
                    set_credential=self._set_extension_credential,
                    resolve_cwd=self._extension_cwd,
                ),
                ensure_started=self._ensure_started,
                agent_resolver=self.agent_resolver,
                chat_loop=self.chat_loop,
                chat_run_manager=self.chat_run_manager,
                temporary_agents=self._temporary_agents,
                projects=self.projects,
                agents=self.agents,
                sessions=self.chat_sessions,
                tools=self.tools,
                models=self.models,
                provider_credentials=self.provider_credentials,
                system_prompts=self.system_prompts,
                get_registry=lambda: self._extensions,
                get_skills=lambda: self.skills,
                skills_for=self.skills_for,
                project_skill_names=self.project_skill_names,
                resources=(self.process_manager, self.terminal_manager, self.trigger_service),
                get_change_publisher=lambda: self._extension_change_publisher,
                logger=self.logger,
            )
        return self._extension_host_factory

    def _extension_host(self) -> ExtensionHost:
        return self._host_operations().make_host()

    def _validate_temporary_admission(self, address: Any, admission: Any) -> None:
        if admission.owner is not None:
            self._host_operations()._validate_temporary_admission(address, admission)

    async def _start_owned_completion(
        self,
        address: Any,
        owner: Any,
        content: str,
        notice_ids: tuple[str, ...],
        on_persisted: Any,
    ) -> Any:
        return await self._host_operations()._start_owned_completion(
            address, owner, content, notice_ids, on_persisted
        )

    def set_extension_change_publisher(
        self,
        publisher: Callable[[str, str, Sequence[str], int], None] | None,
    ) -> None:
        """Install the server-owned generic resource invalidation publisher."""
        self._extension_change_publisher = publisher

    def _extension_cwd(self, project_id: str | None, agent_id: str) -> Path:
        from core.projects.resolver import resolve_working_project_id

        agent = self.agent_resolver.resolve_agent(project_id, agent_id)
        working_project_id = resolve_working_project_id(project_id, agent)
        if working_project_id is not None:
            return Path(self.projects.get(working_project_id).cwd)
        return Path(agent.workspace)

    def _set_extension_credential(self, key: str, value: str) -> None:
        if value:
            self.storage.set_data_dir_credential(key, value)
        else:
            self.storage.remove_data_dir_credential(key)
        self.reload_environment_credentials()

    async def _sample_extension(self, context: Any, request: dict[str, Any]) -> dict[str, Any]:
        from core.chat.model_resolution import resolve_agent_model_target

        agent = self.agent_resolver.resolve_agent(context.project_id, context.agent_id)
        provider_id, model_id, connection_id = resolve_agent_model_target(self, agent)
        adapter = self.get_adapter(ConnectionRef(provider_id, connection_id))
        options = {
            key: request[key]
            for key in ("stop_sequences", "tool_choice")
            if request.get(key) is not None
        }
        try:
            response = await adapter.send(
                request["messages"],
                model_id=model_id,
                tools=request.get("tools"),
                max_tokens=request["max_tokens"],
                temperature=request.get("temperature")
                if request.get("temperature") is not None
                else agent.temperature,
                **options,
                **adapter.request_context_kwargs(
                    agent_id=context.agent_id,
                    session_id=context.session_id,
                    project_id=context.project_id,
                ),
            )
            normalized = adapter.normalize_response(response, model_id=model_id)
        finally:
            await adapter.aclose()
        return {"model": f"{provider_id}/{model_id}", **normalized}

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
        if self._calendar_service is not None:
            self._calendar_service.actions.stop()
        if self._bootstrap_service is not None:
            self._bootstrap_service.stop()
        if self._provider_usage is not None:
            self._provider_usage.stop()
        if self._process_manager is not None:
            self._process_manager.stop()
        terminal_error: TerminalManagerError | None = None
        if self._terminal_manager is not None:
            try:
                self._terminal_manager.stop()
            except TerminalManagerError as error:
                terminal_error = error
        if self._keep_awake is not None:
            self._keep_awake.close()
        if self._storage is not None:
            self._storage.temporary_files.stop()
        if self._chat_sessions is not None:
            self._chat_sessions.close()

        if self._speech is not None:
            self._speech.close()
        self._clear_service_references()
        self._log_manager.close()
        if terminal_error is not None:
            raise terminal_error

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
        if self._calendar_service is not None:
            await self._calendar_service.actions.aclose()
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
        if self._speech is not None:
            await self._speech.aclose()
        if self._provider_usage is not None:
            await self._provider_usage.aclose()
        if self._process_manager is not None:
            await self._process_manager.aclose()
        terminal_error: TerminalManagerError | None = None
        if self._terminal_manager is not None:
            try:
                await self._terminal_manager.aclose()
            except TerminalManagerError as error:
                terminal_error = error
        if self._keep_awake is not None:
            self._keep_awake.close()
        if self._storage is not None:
            await self._storage.temporary_files.aclose()
        if self._chat_sessions is not None:
            self._chat_sessions.close()

        self._clear_service_references()
        self._log_manager.close()
        if terminal_error is not None:
            raise terminal_error

    def _log_shutdown(self) -> None:
        if self.logger is not None:
            self.logger.info("Runtime stopped")

    def _cleanup_failed_startup(self) -> None:
        """Release every started resource after a failed synchronous bootstrap."""
        if self._calendar_service is not None:
            self._calendar_service.actions.stop()
        cleanup_actions = (
            (self._speech, "close"),
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
        start_event_loop_service(self._process_manager, "Process manager service not available")

    def _start_terminal_manager(self) -> None:
        if self._terminal_manager is None:
            raise RuntimeError("Terminal manager service not available")
        self._terminal_manager.start()

    def _start_cron_service(self) -> None:
        start_event_loop_service(self._cron_service, "Cron service not available")

    def _start_calendar_service(self) -> None:
        assert self._calendar_service is not None
        self._calendar_service.actions.start()

    def _start_provider_usage_service(self) -> None:
        start_event_loop_service(self._provider_usage, "Provider usage service not available")

    def _start_channel_service(self) -> None:
        start_event_loop_service(self._channel_service, "Channel service not available")

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

    def inspect_skill(self, entry_id: str) -> dict[str, Any]:
        return self._skill_operations().inspect_skill(entry_id)

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

    def _recall_operations(self) -> RecallIntegration:
        self._ensure_started()
        if self._recall is None:
            raise RuntimeError("Recall backend is not available")
        return self._recall

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
        """Reload Session Recall Tools from persisted settings and current Extensions."""
        self._recall_operations().reload_recall_backend()

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
        if self._recall is not None:
            self._recall._recover_recall_backend_if_deactivated(removed_backend_names)

    def available_recall_backends(self) -> list[str]:
        """Return selectable Recall backends from built-ins and live Extensions."""
        return self._recall_operations().available_recall_backends()

    async def remove_session_from_recall(
        self, agent_id: str, session_id: str, project_id: str | None = None
    ) -> None:
        """Best-effort eviction of a deleted Session from the active Recall index."""
        await self._recall_operations().remove_session_from_recall(agent_id, session_id, project_id)

    def reload_skills(self) -> None:
        """Reload the runtime skill registry from current persisted settings."""
        self._apply_reloaded_skills(self._load_reloaded_skills())

    async def reload_skills_async(self) -> None:
        """Reload Skills without scanning their files on the Event Loop."""
        skills = await _RUNTIME_WORKERS.run(self._load_reloaded_skills)
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

    def _refresh_prompt_block_definitions(self) -> None:
        refresh_prompt_blocks(self._system_prompts, self._tool_prompt_blocks, self._extensions)

    def reload_environment_credentials(self) -> None:
        """Reload shared credential fallback values from the data-dir `.env`."""

        self._ensure_started()
        data_dir_credentials = self.storage.load_environment()
        self._fallback_environment = dict(data_dir_credentials)
        self.provider_credentials.reload_fallback_credentials(data_dir_credentials)

    def reload_custom_providers(self) -> None:
        """Reload Settings-owned Provider and Model overlays in place."""

        self._ensure_started()
        resources_path = _resolve_resources_path(self.config)
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

    providers: _StartedService[ProviderRegistry] = _StartedService(
        lambda runtime: runtime._providers, "Provider registry not available"
    )

    models: _StartedService[ModelRegistry] = _StartedService(
        lambda runtime: runtime._models, "Model registry not available"
    )

    provider_credentials: _StartedService[ProviderCredentialResolverProtocol] = _StartedService(
        lambda runtime: runtime._provider_credentials, "Provider credential service not available"
    )

    model_tasks: _StartedService[TaskModelService] = _StartedService(
        lambda runtime: runtime._model_tasks, "Task-model service not available"
    )

    speech: _StartedService[SpeechService] = _StartedService(
        lambda runtime: runtime._speech, "Speech service not available"
    )

    image: _StartedService[ImageService] = _StartedService(
        lambda runtime: runtime._image, "Image service not available"
    )

    video: _StartedService[VideoService] = _StartedService(
        lambda runtime: runtime._video, "Video service not available"
    )

    music: _StartedService[MusicService] = _StartedService(
        lambda runtime: runtime._music, "Music service not available"
    )

    embeddings: _StartedService[EmbeddingService] = _StartedService(
        lambda runtime: runtime._embeddings, "Embedding service not available"
    )

    token_store: _StartedService[TokenStore] = _StartedService(
        lambda runtime: runtime._token_store, "Token store not available"
    )

    storage: _StartedService[StorageManager] = _StartedService(
        lambda runtime: runtime._storage, "Storage service not available"
    )

    attachment_store: _StartedService[AttachmentStore] = _StartedService(
        lambda runtime: runtime._attachment_store, "Attachment store not available"
    )

    @property
    def speech_upload_max_size_bytes(self) -> int:
        """Maximum accepted uploaded audio size for speech transcription."""

        self._ensure_started()
        return self._speech_upload_max_size_bytes

    agents: _StartedService[AgentStore] = _StartedService(
        lambda runtime: runtime._agents, "Agent service not available"
    )

    memory: _StartedService[MemoryService] = _StartedService(
        lambda runtime: runtime._memory_service, "Memory service not available"
    )

    tools: _StartedService[ToolRegistry] = _StartedService(
        lambda runtime: runtime._tools, "Tool service not available"
    )

    process_manager: _StartedService[ProcessManager] = _StartedService(
        lambda runtime: runtime._process_manager, "Process manager service not available"
    )

    terminal_manager: _StartedService[TerminalManager] = _StartedService(
        lambda runtime: runtime._terminal_manager, "Terminal manager service not available"
    )

    file_read_state: _StartedService[FileReadState] = _StartedService(
        lambda runtime: runtime._file_state, "File read state service not available"
    )

    change_tracker: _StartedService[ChangeTracker] = _StartedService(
        lambda runtime: runtime._change_tracker, "Change tracker service not available"
    )

    skills: _StartedService[SkillRegistry] = _StartedService(
        lambda runtime: runtime._skills, "Skill service not available"
    )

    skill_authoring: _StartedService[SkillAuthoringService] = _StartedService(
        lambda runtime: runtime._skill_authoring, "Skill authoring service not available"
    )

    skill_policy: _StartedService[SkillPolicyService] = _StartedService(
        lambda runtime: runtime._skill_policy, "Skill policy service not available"
    )

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

    chat_sessions: _StartedService[ChatSessionManager] = _StartedService(
        lambda runtime: runtime._chat_sessions, "Chat session service not available"
    )

    subagents: _StartedService[SubAgentCoordinator] = _StartedService(
        lambda runtime: runtime._subagent_coordinator, "Sub-agent coordinator is not available"
    )

    projects: _StartedService[ProjectStore] = _StartedService(
        lambda runtime: runtime._projects, "Project service not available"
    )

    agent_resolver: _StartedService[AgentResolver] = _StartedService(
        lambda runtime: runtime._agent_resolver, "Agent resolver service not available"
    )

    recall_backend: _StartedService[RecallBackend] = _StartedService(
        lambda runtime: runtime._recall.backend if runtime._recall is not None else None,
        "Recall backend is not available",
    )

    chat_run_manager: _StartedService[ChatRunManager] = _StartedService(
        lambda runtime: runtime._chat_run_manager, "Chat run manager service not available"
    )

    command_dispatcher: _StartedService[CommandDispatcher] = _StartedService(
        lambda runtime: runtime._command_dispatcher, "Command dispatcher service not available"
    )

    chat_loop: _StartedService[ChatLoop] = _StartedService(
        lambda runtime: runtime._chat_loop, "Chat loop service not available"
    )

    trigger_service: _StartedService[TriggerService] = _StartedService(
        lambda runtime: runtime._trigger_service, "Trigger service not available"
    )

    reflection: _StartedService[ReflectionService] = _StartedService(
        lambda runtime: runtime._reflection_service, "Reflection service not available"
    )

    streaming_chat_loop: _StartedService[ChatLoop] = _StartedService(
        lambda runtime: runtime._streaming_chat_loop, "Streaming chat loop is not available"
    )

    channel_service: _StartedService[ChannelService] = _StartedService(
        lambda runtime: runtime._channel_service, "Channel service not available"
    )

    cron_service: _StartedService[CronService] = _StartedService(
        lambda runtime: runtime._cron_service, "Cron service not available"
    )

    calendar_service: _StartedService[CalendarService] = _StartedService(
        lambda runtime: runtime._calendar_service, "Calendar service not available"
    )

    bootstrap_service: _StartedService[BootstrapService] = _StartedService(
        lambda runtime: runtime._bootstrap_service, "Bootstrap service not available"
    )

    provider_usage: _StartedService[ProviderUsageService] = _StartedService(
        lambda runtime: runtime._provider_usage, "Provider usage service not available"
    )

    system_prompts: _StartedService[SystemPromptManager] = _StartedService(
        lambda runtime: runtime._system_prompts, "System prompt service not available"
    )

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
