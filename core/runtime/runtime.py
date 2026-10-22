"""vBot runtime bootstrap.

The ``Runtime`` class is the single entry point that wires together
all core services and manages the application lifecycle.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, suppress
from pathlib import Path
from typing import Any, Literal

from core.chat.errors import ChatError
from core.chat.status_report import StatusWireProfile
from core.database import SnapshotBarrier, UnregisteredDatabase
from core.debug import ProviderDebugRecorder
from core.extensions import (
    ExtensionRegistry,
    InteractionEvent,
    InteractionResponder,
)
from core.extensions.oauth_redirects import OAuthRedirects
from core.extensions.operations import ExtensionHost
from core.extensions.runtime import ExtensionRuntime
from core.models.models import Model
from core.providers._wire_profile_files import WireProfileFile
from core.providers.accounts import ConnectionRef, split_connection_id
from core.providers.adapter import ProviderAdapter
from core.providers.reasoning import ReasoningIntent
from core.providers.runtime import ProviderRuntime
from core.providers.token_getter import TokenGetter
from core.providers.wire_observations import ObservedFacts
from core.providers.wire_profile import ProfileStatus, Verification, WireProfile
from core.runs import RunNotFoundError
from core.runtime import _settings
from core.runtime._agent_description import suggest_agent_description
from core.runtime._agent_rename import (
    AgentRenameOutcome,
    AgentRenameServices,
    rename_identity_agent,
)
from core.runtime._bootstrap import bootstrap, start_event_loop_service
from core.runtime._config_backups import ConfigBackupSchedule
from core.runtime._configuration import _resolve_data_dir
from core.runtime._extension_host import ExtensionHostFactory
from core.runtime._extension_sampling import sample_extension
from core.runtime._prompt_blocks import refresh_prompt_blocks
from core.runtime._recall import RecallIntegration
from core.runtime._service_graph import RuntimeServiceGraph
from core.runtime._settings import SettingsChangeEffects, apply_settings_change
from core.runtime._shutdown import run_shutdown, run_shutdown_async
from core.runtime._skill_merge import SkillMergeServices, follow_skill_merge
from core.runtime.interfaces import (
    ConfigProtocol,
    LoggerProtocol,
)
from core.settings.paths import DEFAULT_SPEECH_UPLOAD_MAX_SIZE_BYTES
from core.settings.settings import effective_timezone_name
from core.skills.authoring import (
    ArchivedSkill,
    SkillReference,
    SkillWriter,
    SkillWriteResult,
)
from core.skills.runtime import SkillRuntime
from core.skills.skills import SkillMetadata, SkillRegistry
from core.utils.errors import ConfigError
from core.utils.logging import LogManager

# Windows environment variable names are case-insensitive. The data-directory
# credential fallback (the Secret Store) follows the same rule there, matching
# Skill `env` requirements.
_ENVIRONMENT_NAMES_IGNORE_CASE = os.name == "nt"


class Runtime(RuntimeServiceGraph):
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

    def __init__(
        self,
        config: ConfigProtocol,
        *,
        safe_startup_mode: Literal["verification", "test"] | None = None,
        operational_producers: Literal["enabled", "disabled"] = "enabled",
    ) -> None:
        """Prepare logging and empty service state from the injected configuration."""
        if safe_startup_mode not in {None, "verification", "test"}:
            raise ValueError("safe_startup_mode must be verification, test, or None")
        if operational_producers not in {"enabled", "disabled"}:
            raise ValueError("operational_producers must be enabled or disabled")
        self._operational_producers = operational_producers
        self._config = config
        self.safe_startup_mode = safe_startup_mode
        self._data_dir = _resolve_data_dir(config)
        # Opened by startup and closed by shutdown; constructing a Runtime
        # leaves the process logging configuration alone.
        self._log_manager: LogManager | None = None
        self.logger: LoggerProtocol | None = None
        self._speech_upload_max_size_bytes = DEFAULT_SPEECH_UPLOAD_MAX_SIZE_BYTES
        self._extension_change_publisher: Callable[[str, str, Sequence[str], int], None] | None = (
            None
        )
        # The serving port, which restricted Runs may not reach on loopback.
        self._server_port: int | None = None
        self._server_hosts: Callable[[], tuple[str, ...]] | None = None
        self._close_task: asyncio.Task[None] | None = None
        # Lives as long as the Runtime: a restart keeps coordinating with a
        # data snapshot that is still copying.
        self._snapshot_barrier = SnapshotBarrier()
        # Lives as long as the Runtime: the server binds its callback URL once.
        self.oauth_redirects = OAuthRedirects()
        self._clear_service_references()

    def start(self) -> None:
        """Start the dependency-ordered service graph, cleaning up a failed bootstrap."""
        if self._close_task is not None and not self._close_task.done():
            raise RuntimeError("Runtime is closing")
        if not self._started:
            self._close_task = None
        bootstrap(self)

    async def fire_extension_startup(self) -> None:
        """Fire extension startup handlers once bootstrap is complete and serving.

        Called by the server from inside its async lifespan, so startup handlers
        run on the live serving loop (they may schedule background tasks there).
        No-op before ``start()`` / after shutdown.
        """
        if self._extension_runtime is not None:
            await self._extension_runtime.startup()

    def _host_operations(self) -> ExtensionHostFactory:
        self._ensure_started()
        if self._extension_host_factory is None:
            assert self._temporary_agents is not None
            self._extension_host_factory = ExtensionHostFactory(
                host=ExtensionHost(
                    data_dir=self.storage.data_dir,
                    sample=self._sample_extension,
                    resolve_agent=self.agent_resolver.resolve_agent,
                    resolve_tool_agent=self._extension_tool_agent,
                    resolve_tool_agent_async=self._extension_tool_agent_async,
                    store_attachment=self.attachment_store.store,
                    resolve_credential=self.resolve_environment_credential,
                    set_credential=self._set_extension_credential,
                    resolve_cwd=self._extension_cwd,
                    oauth_redirects=self.oauth_redirects,
                ),
                ensure_started=self._ensure_started,
                agent_resolver=self.agent_resolver,
                chat_loop=self.chat_loop,
                chat_run_manager=self.chat_run_manager,
                temporary_agents=self._temporary_agents,
                projects=self.projects,
                agents=self.agents,
                sessions=self.chat_sessions,
                statistics_index=self.statistics_index,
                usage_recorder=self.usage_recorder,
                tools=self.tools,
                models=self.models,
                providers=self.providers,
                provider_credentials=self.provider_credentials,
                system_prompts=self.system_prompts,
                get_registry=lambda: self._extensions,
                get_skills=lambda: self.skills,
                skills_for=self.skills_for,
                project_skill_names=self.project_skill_names,
                resources=(self.terminal_manager, self.command_manager, self.trigger_service),
                terminals=self.terminal_manager,
                commands=self.command_manager,
                portals=self._portals,
                scratch_areas=self.scratch_areas,
                get_change_publisher=lambda: self._extension_change_publisher,
                get_title_service=lambda: self._session_title_service,
                logger=self.logger,
            )
        return self._extension_host_factory

    def _extension_host(self) -> ExtensionHost:
        return self._host_operations().make_host()

    async def _archive_uninstalled_extension_groups(self, installed: frozenset[str]) -> None:
        await self._host_operations().archive_uninstalled_owner_groups(installed)

    def _validate_temporary_admission(self, address: Any, admission: Any) -> None:
        if admission.owner is not None:
            self._host_operations()._validate_temporary_admission(address, admission)

    def _validate_owned_completion(self, address: Any, owner: Any) -> None:
        self._host_operations()._validate_owned_completion(address, owner)

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

    def set_server_port(
        self, port: int | None, *, own_hosts: Callable[[], tuple[str, ...]] | None = None
    ) -> None:
        """Record the port the vBot server listens on (``None`` outside a server).

        ``own_hosts`` returns the further names and addresses that reach it on that
        port (blocking; empty while it listens on loopback only).
        """
        self._server_port = port
        self._server_hosts = own_hosts

    def _own_server_hosts(self) -> tuple[str, ...]:
        """Blocking: what reaches the server besides loopback, for restricted boundaries."""
        return self._server_hosts() if self._server_hosts is not None else ()

    def _extension_cwd(self, project_id: str | None, agent_id: str) -> Path:
        # No Session is at hand: the Agent works where its new Sessions would.
        agent = self.agent_resolver.resolve_agent(project_id, agent_id)
        working_project_id = self.agent_resolver.new_session_working_project(project_id, agent)
        if working_project_id is not None:
            return Path(self.projects.get(working_project_id).cwd)
        return Path(agent.workspace)

    def _set_extension_credential(self, key: str, value: str) -> None:
        if value:
            self.storage.set_data_dir_credential(key, value)
        else:
            self.storage.remove_data_dir_credential(key)
        self.reload_environment_credentials()

    def _extension_tool_agent(self, context: Any) -> Any:
        from core.sessions import SessionAddress, SessionNotFoundError

        address = SessionAddress(context.project_id, context.agent_id, context.session_id)
        try:
            binding = self.chat_sessions.temporary_binding(address)
        except SessionNotFoundError:
            # A temporary invocation needs its live Session. A call outside any
            # Session (Extension management such as MCP ``invoke``) runs as the
            # addressed Agent, which no Session overrides.
            if context.execution_owner is not None:
                raise ValueError("Temporary Tool invocation no longer owns this Session") from None
            return self.agent_resolver.resolve_agent(context.project_id, context.agent_id)
        if binding is not None:
            owner = context.execution_owner
            if owner is None or (
                owner.extension != binding.owner_name
                or owner.group_id != binding.group_id
                or owner.participant_id != binding.participant_id
                or owner.generation_id != binding.generation_id
            ):
                raise ValueError("Temporary Tool invocation no longer owns this Session")
            return self.agent_resolver.resolve_temporary_agent(
                address, generation_id=binding.generation_id, session=address
            )
        return self.agent_resolver.resolve_agent(
            context.project_id, context.agent_id, session_id=context.session_id
        )

    async def _extension_tool_agent_async(self, context: Any) -> Any:
        return await self.chat_sessions.run_async(self._extension_tool_agent, context)

    async def _sample_extension(self, context: Any, request: dict[str, Any]) -> dict[str, Any]:
        return await sample_extension(self, context, request)

    async def suggest_agent_description(self, agent_id: str) -> str:
        """Draft a description of an Identity Agent with its own Model; nothing is saved."""
        return await suggest_agent_description(self, agent_id)

    def activate_bootstrap(self) -> None:
        """Start eligible Bootstrap Runs after the serving lifespan is ready."""
        if self.safe_startup_mode is None and self._operational_producers == "enabled":
            self.bootstrap_service.activate()

    def stop(self) -> None:
        """Shut the Runtime down synchronously; safe before ``start()``.

        Runs every shutdown step even when an earlier one fails, then clears
        service references and closes logging. A single failure is re-raised
        unchanged; several are raised together as an ``ExceptionGroup``.
        """
        run_shutdown(self)

    async def aclose(self) -> None:
        """Shut down like ``stop()``, awaiting drains; concurrent callers share one close.

        Caller cancellation propagates only after the shared cleanup settled. A
        later call reports the same outcome without running any step again.
        """
        if self._close_task is None:
            self._close_task = asyncio.create_task(
                run_shutdown_async(self), name="runtime-shutdown"
            )
        task = self._close_task
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as cancellation:
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue
                except BaseException:
                    break
            with suppress(BaseException):
                task.result()
            raise cancellation

    def _close_extension_databases(self) -> None:
        """Close Extension databases a shutdown handler did not release."""
        if self._extension_host_factory is not None:
            self._extension_host_factory.databases.close()

    def _open_log_manager(self) -> LogManager:
        """Return the Runtime's log manager, activating it on first use."""
        if self._log_manager is None:
            self._log_manager = LogManager(
                level=self._config.get("LOG_LEVEL", "INFO"), data_dir=self._data_dir
            )
        return self._log_manager

    def _close_log_manager(self) -> None:
        manager, self._log_manager = self._log_manager, None
        if manager is not None:
            manager.close()

    def _log_shutdown(self) -> None:
        if self.logger is not None:
            self.logger.debug("Runtime stopped")

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

    def _start_terminal_manager(self) -> None:
        if self._terminal_manager is None:
            raise RuntimeError("Terminal manager service not available")
        self._terminal_manager.start()

    def _start_cron_service(self) -> None:
        start_event_loop_service(self._cron_service, "Cron service not available")

    def _start_provider_usage_service(self) -> None:
        start_event_loop_service(self._provider_usage, "Provider usage service not available")

    def _start_performance_service(self) -> None:
        start_event_loop_service(self._performance, "Performance service not available")

    def _start_recall_indexing(self) -> None:
        start_event_loop_service(self._recall, "Recall is not available")

    def _start_archive_retention(self) -> None:
        start_event_loop_service(self._archive, "Archive is not available")

    def _start_scratch_areas(self) -> None:
        start_event_loop_service(self._scratch_areas, "Scratch Areas are not available")

    def _start_config_backups(self) -> None:
        """Back up the configuration files while serving (``core.database.config_backups``)."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        if self._config_backups is None:
            self._config_backups = ConfigBackupSchedule(self._data_dir, self._snapshot_barrier)
        self._config_backups.start()

    def _start_librarian(self) -> None:
        start_event_loop_service(self._librarian_service, "Librarian service not available")

    def _start_channel_service(self) -> None:
        start_event_loop_service(self._channel_service, "Channel service not available")

    def resolve_environment_credential(self, key: str) -> str:
        """Resolve one environment credential using runtime precedence rules."""
        if key in os.environ:
            return os.environ[key]
        fallback = self._fallback_credential(key)
        return "" if fallback is None else fallback

    def environment_credential_source(self, key: str) -> str | None:
        """Return the effective source for one environment credential key."""
        if key in os.environ:
            return "process_environment"
        if self._fallback_credential(key) is not None:
            return "data_dir"
        return None

    def _fallback_credential(self, key: str) -> str | None:
        """Return the data-directory credential for *key* under host naming rules.

        On Windows, names differing only in case match, and every spelling of
        *key* resolves the same entry: the all-uppercase one, otherwise the
        first in sorted order (``core.secrets.fold_credential_names``, which
        already folds the snapshot Storage loads).
        """
        if not _ENVIRONMENT_NAMES_IGNORE_CASE:
            return self._fallback_environment.get(key)
        folded = key.upper()
        matches = sorted(name for name in self._fallback_environment if name.upper() == folded)
        if not matches:
            return None
        return self._fallback_environment[folded if folded in matches else matches[0]]

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

    def archived_skill(self, agent_id: str | None, name: str) -> ArchivedSkill | None:
        return self._skill_operations().archived_skill(agent_id, name)

    def background_skill_protection(self, agent_id: str, names: Iterable[str]) -> dict[str, str]:
        return self._skill_operations().background_protection(agent_id, names)

    def shared_skill_receivers(self, owner_id: str) -> dict[str, frozenset[str]]:
        """Map each Skill the Identity Agent ``owner_id`` shares to its receiver Agents."""
        return self._skill_operations().shared_skill_receivers(owner_id)

    async def follow_skill_merge(
        self,
        owner_id: str,
        name: str,
        target: str | None,
        delete: Callable[[tuple[SkillReference, ...]], bool],
    ) -> tuple[SkillReference, ...]:
        """Delete an Identity Agent's Skill and move what named it to ``target``, if any.

        ``delete(followed)`` deletes the Skill and records ``followed``; returns
        what did not move (``core/runtime/_skill_merge.py``).
        """
        if self._skill_policy is None:
            raise RuntimeError("Skill policy service not available")
        services = SkillMergeServices(
            policy=self._skill_policy,
            automation=self.automation_references,
            agent_name=self._agent_name,
            shares_changed=lambda: self.invalidate_agent_skills(None),
            mutation_guard=self._skill_operations().merge_guard,
            prepare_access=self._skill_operations().prepare_skill_merge,
            preserve_access=self._skill_operations().preserve_merged_skill_access,
        )
        return await follow_skill_merge(services, owner_id, name, target, delete)

    def _agent_name(self, agent_id: str) -> str:
        agent = self.agents.find(agent_id)
        return agent.name if agent is not None and agent.name else agent_id

    def run_started_at(self, run_id: str) -> str | None:
        """When the Run ``run_id`` was created, while the Run manager still holds it."""
        if self._chat_run_manager is None:
            return None
        try:
            return self._chat_run_manager.get(run_id).created_at
        except RunNotFoundError:
            return None

    def project_context_skills(self, project_id: str) -> list[SkillMetadata]:
        return self._skill_operations().project_context_skills(project_id)

    def inspect_skill(self, entry_id: str) -> dict[str, Any]:
        return self._skill_operations().inspect_skill(entry_id)

    def skill_inventory(self) -> dict[str, Any]:
        return self._skill_operations().skill_inventory()

    def set_skill_package_disabled(self, entry_id: str, *, disabled: bool) -> dict[str, Any]:
        return self._skill_operations().set_package_disabled(entry_id, disabled=disabled)

    def profile_skill_content(self, project_id: str, name: str, allowed: list[str]) -> str | None:
        from core.skills.skill_validator import split_skill_document
        from core.tools.skill import SKILL_BASE_DIR_MARKER

        registry = self.skills_for(project_id)
        try:
            metadata = registry.get(name)
            if name not in {skill.name for skill in registry.filter_allowed(allowed)}:
                return None
            with metadata.path.open("rb") as stream:
                content = stream.read(128 * 1024 + 1)
            if len(content) > 128 * 1024:
                return None
            body = split_skill_document(content.decode("utf-8-sig"))[1]
            directory = metadata.path.parent.as_posix()
            body = body.replace(SKILL_BASE_DIR_MARKER, directory)
            return f"Skill directory: {directory}\n\n{body}"
        except KeyError, OSError, ValueError:
            return None

    def project_skill_names(self, project_id: str | None) -> frozenset[str]:
        return self._skill_operations().project_skill_names(project_id)

    def project_skill_pool(self, project_id: str) -> dict[str, list[SkillMetadata]]:
        return self._skill_operations().project_skill_pool(project_id)

    def add_skill_changed_callback(self, callback: Callable[[], None]) -> Callable[[], None]:
        """Subscribe to Skill changes an Agent makes; return an unsubscribe function."""
        return self._skill_operations().add_changed_callback(callback)

    def _notify_skills_changed(self) -> None:
        self._skill_operations().notify_changed()

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

    def _publish_agent_skill(
        self, agent_id: str, name: str, writer: SkillWriter
    ) -> SkillWriteResult:
        return self._skill_operations().publish_agent_skill(agent_id, name, writer=writer)

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

    async def apply_settings_change(
        self,
        previous: Mapping[str, Any],
        current: Mapping[str, Any],
        *,
        refresh_sections: Collection[str] = (),
    ) -> SettingsChangeEffects:
        """Apply all live effects of a successfully persisted Settings change.

        Call after validation and the atomic write, with its before/after raw
        Settings snapshots. Keep the mutation serialized through this await.
        Explicit section saves may request a Skills/Recall refresh even when
        their values are unchanged. Return whether the Command catalog and the
        Skill layer may have changed.
        """
        self._ensure_started()
        return await apply_settings_change(
            self, previous, current, refresh_sections=refresh_sections
        )

    def reload_keep_awake(self, settings: Mapping[str, Any] | None = None) -> None:
        """Apply the persisted ``keep_awake`` setting to the running process."""
        _settings.reload_keep_awake(self, settings)

    def timezone_name(self) -> str:
        """Return the effective persisted application timezone."""
        if self._storage is None:
            return "UTC"
        return effective_timezone_name(self._storage.load_settings())

    def reload_timezone(self, settings: Mapping[str, Any] | None = None) -> None:
        """Apply the persisted timezone to all live wall-clock services."""
        _settings.reload_timezone(self, settings)

    def reload_recall_backend(self, settings: Mapping[str, Any] | None = None) -> None:
        """Reload Session Recall Tools from persisted settings and current Extensions."""
        self._recall_operations().reload_recall_backend(settings)

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

    def _recover_recall_backend_if_deactivated(
        self, removed_backend_names: set[str], settings: Mapping[str, Any] | None = None
    ) -> None:
        if self._recall is not None:
            self._recall._recover_recall_backend_if_deactivated(removed_backend_names, settings)

    def available_recall_backends(self) -> list[str]:
        """Return selectable Recall backends from built-ins and live Extensions."""
        return self._recall_operations().available_recall_backends()

    async def remove_session_from_recall(
        self, agent_id: str, session_id: str, project_id: str | None = None
    ) -> None:
        """Best-effort eviction of a deleted Session from the active Recall index."""
        await self._recall_operations().remove_session_from_recall(agent_id, session_id, project_id)

    async def rename_agent(self, agent_id: str, new_agent_id: str) -> AgentRenameOutcome:
        """Rename one Identity Agent and every reference to it as one recoverable change.

        The Agent's tree, config, Sessions and Sub-Agent links move together with
        the Channels, Cron and Bootstrap jobs, delegation
        allow-lists and live Terminal Session, Portal and command scopes that
        name it. A failure
        reverts all of it; an interrupted rename completes on the next start.
        While a Channel, Cron or Bootstrap job still names
        ``new_agent_id``, the rename is refused with ``AgentReferencedError``
        before anything changes; a delegation allow-list entry naming the unused
        id is a leftover the rename removes instead. The caller holds the Run
        admission guards of both ids. Afterwards Skills of both ids are
        invalidated and the Recall indexes forget the moved Sessions' old
        addresses.
        """
        self._ensure_started()
        outcome = await rename_identity_agent(self._agent_rename_services(), agent_id, new_agent_id)
        self.invalidate_agent_skills(agent_id)
        self.invalidate_agent_skills(new_agent_id)
        for session_id in outcome.session_ids:
            try:
                await self.remove_session_from_recall(agent_id, session_id, None)
            except Exception as error:
                if self.logger is not None:
                    self.logger.warning(
                        "Recall cleanup failed after Agent rename (agent=%s session=%s): %s",
                        agent_id,
                        session_id,
                        error,
                    )
        return outcome

    async def agent_references(self, agent_id: str) -> tuple[str, ...]:
        """Name the Channels and live automations that keep an Identity Agent from deletion.

        Labels are ``channel:<id>`` and the ``<kind>:<id>`` labels of
        :meth:`AutomationReferences.agent_references`, sorted; automation history
        that never starts another Run does not count, nor do delegation
        allow-lists, which the deletion itself removes the Agent from. The Channel
        configs are read off the Event Loop.
        """
        self._ensure_started()
        references = [
            f"channel:{channel.id}"
            for channel in await self.channel_service.list_channels_async()
            if channel.agent_id == agent_id
        ]
        references.extend(
            reference.label for reference in self.automation_references.agent_references(agent_id)
        )
        return tuple(sorted(references))

    def _agent_rename_services(self) -> AgentRenameServices:
        """The owners an Identity Agent rename changes, also while startup builds them."""
        agents = self._agents
        sessions = self._chat_sessions
        channels = self._channel_service
        cron = self._cron_service
        bootstrap_jobs = self._bootstrap_service
        if (
            agents is None
            or sessions is None
            or channels is None
            or cron is None
            or bootstrap_jobs is None
        ):
            raise RuntimeError("Agent rename services are not available")
        return AgentRenameServices(
            agents=agents,
            sessions=sessions,
            channels=channels,
            cron=cron,
            bootstrap=bootstrap_jobs,
            snapshot_barrier=self._snapshot_barrier,
            terminals=self._terminal_manager,
            commands=self._command_manager,
            portals=self._portals,
            secrets=self._agent_secrets,
        )

    def reload_skills(self) -> None:
        """Reload the runtime skill registry from current persisted settings."""
        _settings.reload_skills(self)

    async def reload_skills_async(self, *, only_if_changed: bool = False) -> None:
        """Read Skills off the loop; an editor refresh can preserve unchanged scopes."""
        await _settings.reload_skills_async(self, only_if_changed=only_if_changed)

    def _refresh_secret_sources(self) -> None:
        """Offer Agent Secrets the Secret Sources of the Extensions loaded now."""
        if self._agent_secrets is not None:
            self._agent_secrets.replace_sources(
                self._extensions.secret_sources() if self._extensions is not None else []
            )

    def _refresh_prompt_block_definitions(self) -> None:
        refresh_prompt_blocks(self._system_prompts, self._tool_prompt_blocks, self._extensions)

    def reload_environment_credentials(self) -> None:
        """Reload shared credential fallback values from the Secret Store."""
        _settings.reload_environment_credentials(self)

    async def reload_environment_credentials_async(self) -> None:
        """Read credentials off-loop before updating the live resolver and Skill snapshots."""
        await _settings.reload_environment_credentials_async(self)

    def reload_custom_providers(self) -> None:
        """Reload Settings-owned Provider, Model and wire profile overlays in place."""
        _settings.reload_custom_providers(self)

    async def reload_custom_providers_async(self) -> None:
        """Read persisted Provider overlays off-loop and publish registries on the caller."""
        await _settings.reload_custom_providers_async(self)

    # ------------------------------------------------------------------
    # Read-only registry access
    # ------------------------------------------------------------------

    @property
    def config(self) -> ConfigProtocol:
        """The injected configuration. Available before ``start()``."""
        return self._config

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

    async def unregister_extension_database(self, name: str) -> UnregisteredDatabase:
        """Release a registered Extension database whose Extension was removed.

        Runs through the one Extension database coordinator, so it is refused
        while an Extension has the database open (see
        ``ExtensionDatabases.unregister``).
        """
        return await self._host_operations().databases.unregister(name)

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

    async def close_session_transports(self, sessions: Iterable[tuple[str, str]]) -> None:
        """Close the Provider transports kept open for these ``(agent_id, session_id)``.

        A Codex WebSocket outlives its Run for the Session's next one; an
        archived Session no longer needs it.
        """
        if self._provider_runtime is not None:
            await self._provider_runtime.close_session_transports(sessions)

    async def close_connection_transports(
        self, provider_id: str, connection_id: str | None = None
    ) -> None:
        """Close the Provider transports kept open on a Connection whose credentials changed.

        ``connection_id`` is the local id (``subscription``); without it every
        Connection of the Provider closes.
        """
        if self._provider_runtime is not None:
            await self._provider_runtime.close_connection_transports(provider_id, connection_id)

    def get_adapter(self, connection: ConnectionRef) -> ProviderAdapter:
        """Return a fully wired Adapter for one exact Provider Connection."""
        return self._provider_operations().get_adapter(connection)

    def get_connection_token_getter(self, connection: ConnectionRef) -> TokenGetter:
        """Return the refresh-capable token getter for one Provider Connection."""
        return self._provider_operations().get_connection_token_getter(connection)

    def provider_debug_recorder(
        self, *, body_limit: int | None = None
    ) -> ProviderDebugRecorder | None:
        """Return a new Provider trace recorder while debug mode is on, else None."""
        return self._provider_operations().debug_recorder(body_limit=body_limit)

    def get_connection_token_extra(self, connection: ConnectionRef) -> Mapping[str, str]:
        """Return persisted OAuth metadata for one Provider Connection."""
        return self._provider_operations().get_connection_token_extra(connection)

    def wire_profile(self, provider_id: str, connection_id: str, model_id: str) -> WireProfile:
        """Return the wire profile requests use for one Model on one local Connection id."""
        return self._provider_operations().wire_profile(provider_id, connection_id, model_id)

    def wire_status(
        self, provider_id: str, connection_id: str, model_id: str
    ) -> tuple[ProfileStatus, Verification | None]:
        """Return the wire profile ``(status, verification)`` without resolving the profile."""
        return self._provider_operations().wire_status(provider_id, connection_id, model_id)

    def forget_wire_facts(
        self, provider_id: str, connection_id: str | None = None, model_id: str | None = None
    ) -> int:
        """Drop what live traffic taught about a Provider, a Connection, or one Model."""
        return self._provider_operations().forget_wire_facts(provider_id, connection_id, model_id)

    def wire_profile_files(self) -> Mapping[str, WireProfileFile]:
        """Return the wire profile data by Provider id, Custom Provider blocks included."""
        return self._provider_operations().wire_profile_files

    def learned_wire_facts(
        self, provider_id: str, connection_id: str, model_id: str
    ) -> ObservedFacts:
        """Return what live traffic showed for one Model on one local Connection id."""
        return self._provider_operations().learned_wire_facts(provider_id, connection_id, model_id)

    def describe_agent_wire_profile(self, agent: Any) -> StatusWireProfile | None:
        """Describe the wire profile of the Connection the Agent's Model resolves to.

        Returns ``None`` when the Model cannot resolve to a Connection.
        """
        target = self._agent_wire_target(agent)
        if target is None:
            return None
        provider_id, connection_id, local_connection_id, model_id = target
        profile = self.wire_profile(provider_id, local_connection_id, model_id)
        return StatusWireProfile(
            connection_id=connection_id,
            status=profile.status,
            verified_at=profile.verification.date if profile.verification is not None else None,
            learned=self.learned_wire_facts(provider_id, local_connection_id, model_id),
        )

    def describe_agent_reasoning_render(self, agent: Any) -> ReasoningIntent | None:
        """Describe the reasoning a request with the Agent's thinking effort carries.

        Describes the wire profile of the Connection the Agent's Model resolves
        to, learned facts included; ``None`` when the Model cannot resolve to a
        Connection.
        """
        target = self._agent_wire_target(agent)
        if target is None:
            return None
        provider_id, _connection_id, local_connection_id, model_id = target
        return self._provider_operations().describe_reasoning_render(
            provider_id, local_connection_id, model_id, agent.thinking_effort
        )

    def _agent_wire_target(self, agent: Any) -> tuple[str, str, str, str] | None:
        """Return ``(provider, connection, local connection, model)`` of the Agent's Model.

        Resolves the Connection as chat does (a pinned ``::connection`` suffix,
        else the first usable Connection); ``None`` when the Model cannot
        resolve to one.
        """
        from core.chat.model_resolution import resolve_agent_model_target

        try:
            provider_id, model_id, connection_id = resolve_agent_model_target(self, agent)
            local_connection_id, _account = split_connection_id(provider_id, connection_id)
        except ChatError, ConfigError, KeyError:
            return None
        return provider_id, connection_id, local_connection_id, model_id

    def model_database_refresh(self) -> AbstractAsyncContextManager[None]:
        """Coordinate manual and automatic Model DB refresh transactions."""
        return self._provider_operations().model_database_refresh()

    async def maybe_refresh_local_catalogs(self, *, force: bool = False) -> None:
        """Refresh enabled local Provider catalogs without disrupting live registries."""
        if self._provider_runtime is None:
            return
        await self._provider_operations().maybe_refresh_local_catalogs(force=force)

    def add_model_catalog_changed_callback(
        self, callback: Callable[[], None]
    ) -> Callable[[], None]:
        """Subscribe to Model catalog changes a local catalog sweep publishes.

        Returns an unsubscribe function.
        """
        return self._provider_operations().add_catalog_changed_callback(callback)

    def connection_reachability(self, connection_id: str) -> bool | None:
        """Return the latest local catalog probe outcome for one Connection."""
        if self._provider_runtime is None:
            return None
        return self._provider_operations().connection_reachability(connection_id)

    def local_context_windows(self) -> Mapping[str, Any]:
        """Return live context windows for local Models.

        The user's Settings, overridden by the window LM Studio reports for a loaded instance.
        """
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
