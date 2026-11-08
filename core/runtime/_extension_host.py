"""Runtime integration for owner-bound Extension operations and temporary Runs."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import asdict, replace
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

from core.agents import AgentStore, normalize_agent_tools
from core.agents.temporary import TemporaryAgentConfig, TemporaryAgentRegistry
from core.agents.temporary_execution import (
    ExecutionResources,
    TemporaryExecutionGroups,
    release_temporary_group,
)
from core.automation.cron import CronJobNotFoundError, CronService
from core.chat import ChatLoop
from core.extensions import ExtensionRegistry
from core.extensions.databases import ExtensionDatabases
from core.extensions.notifications import (
    MAX_NOTIFICATION_BODY,
    MAX_NOTIFICATION_KEY,
    MAX_NOTIFICATION_TITLE,
    PAGE_BADGES_RESOURCE,
    notification_text,
    validate_page_route,
)
from core.extensions.operations import ExtensionHost
from core.model_tasks.decision_types import DecisionError
from core.model_tasks.decisions import DecisionService
from core.model_tasks.task_execution import TaskUsageContext
from core.models.models import ModelRegistry
from core.models.query import ModelQuery
from core.permissions import PermissionBoundary, PermissionCeiling
from core.projects import AgentResolver, ProjectStore, RuntimeAgent
from core.projects.resolver import effective_project_allowed_skills
from core.prompts import SystemPromptManager
from core.prompts.prompts import ProjectPromptContext
from core.providers.providers import ProviderRegistry
from core.runs import (
    ChatRunManager,
    Run,
    RunAdmissionBlockedError,
    RunExecutionOwner,
    RunNotFoundError,
)
from core.runtime._shell_review_context import current_input_authority, shell_review_context
from core.runtime._workers import _RUNTIME_WORKERS
from core.runtime.interfaces import (
    LoggerProtocol,
    ProviderCredentialResolverProtocol,
)
from core.sessions import ChatSessionManager, SessionAddress
from core.sessions.titles import SessionTitleService
from core.skills.skills import SkillRegistry
from core.statistics import StatisticsIndex, StatisticsService
from core.tools import (
    resolve_tool_access,
    tool_is_ready,
)
from core.tools.availability import (
    BASH_ALLOWED_ENV_KEY,
    BASH_TOOL_SETTINGS_KEY,
    SUBAGENT_ALLOWED_AGENTS_KEY,
    SUBAGENT_TOOL_SETTINGS_KEY,
    normalize_tool_access,
)
from core.tools.command_manager import CommandManager
from core.tools.portal_registry import PortalRegistry
from core.tools.scratch_areas import ScratchAreas
from core.tools.terminal_manager import TerminalManager, TerminalOwner
from core.tools.tools import ToolContext, ToolRegistry
from core.usage import UsageRecorder
from core.utils.ids import is_safe_id
from core.utils.timestamps import utc_now_timestamp

if TYPE_CHECKING:
    from core.automation.automation import TriggerService


def _temporary_config_from_binding(binding: Any) -> TemporaryAgentConfig:
    """Decode one retained temporary descriptor through its canonical DTO."""

    raw = getattr(binding, "config", None)
    if not isinstance(raw, Mapping):
        raise RuntimeError("temporary execution configuration is invalid")
    try:
        return TemporaryAgentConfig(
            model=raw["model"],
            cwd=Path(raw["cwd"]),
            tool_access=replace(
                normalize_tool_access(raw["tool_access"]), fixed=raw.get("fixed_tool_access", False)
            ),
            allowed_skills=raw["allowed_skills"],
            tools=raw["tools"],
            name=raw["name"],
            temperature=raw.get("temperature"),
            top_p=raw.get("top_p"),
            thinking_effort=raw.get("thinking_effort"),
            fallback_models=raw.get("fallback_models"),
            instructions=raw.get("instructions", ""),
            prompt_blocks=raw.get("prompt_blocks"),
            project_memory_enabled=raw.get("project_memory_enabled", True),
            permission_rules=raw.get("permission_rules"),
            permission_ceiling=raw.get("permission_ceiling"),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise RuntimeError("temporary execution configuration is invalid") from error


def _validate_temporary_tool_configuration(
    config: TemporaryAgentConfig,
    ordinary_tools: Mapping[str, Any],
) -> None:
    """Apply the shared Identity-Agent settings grammar to temporary profiles."""

    try:
        normalize_agent_tools(config.tools)
    except ValueError as error:
        raise RuntimeError("temporary Tool settings are invalid") from error

    requested = set(config.tool_access.denied) | set(config.tool_access.granted)
    if config.tool_access.mode == "selected":
        requested.update(config.tool_access.allowed)
    requested.update(config.tools)
    unknown = requested - set(ordinary_tools)
    if unknown:
        raise RuntimeError("temporary Tool configuration refers to an unavailable Tool")


# Blocking: the Permission Boundary of an Agent's work outside a Run, from the
# Agent, its Working Project's directory, the Session's stored ceiling and the
# Session whose temporary files it may read (Runtime._session_boundary).
type SessionBoundaryBuilder = Callable[
    [RuntimeAgent, Path | None, PermissionCeiling | None, SessionAddress | None],
    PermissionBoundary,
]


class ExtensionHostFactory:
    """Own temporary execution groups and Extension catalog/inspection capabilities."""

    def __init__(
        self,
        *,
        host: ExtensionHost,
        ensure_started: Callable[[], None],
        agent_resolver: AgentResolver,
        chat_loop: ChatLoop,
        chat_run_manager: ChatRunManager,
        temporary_agents: TemporaryAgentRegistry,
        projects: ProjectStore,
        agents: AgentStore,
        sessions: ChatSessionManager,
        statistics_index: StatisticsIndex,
        usage_recorder: UsageRecorder,
        tools: ToolRegistry,
        models: ModelRegistry,
        providers: ProviderRegistry,
        provider_credentials: ProviderCredentialResolverProtocol,
        system_prompts: SystemPromptManager,
        get_registry: Callable[[], ExtensionRegistry | None],
        get_skills: Callable[[], SkillRegistry],
        skills_for: Callable[[str | None, str | None], SkillRegistry],
        project_skill_names: Callable[[str | None], frozenset[str]],
        resources: Sequence[ExecutionResources],
        terminals: TerminalManager,
        commands: CommandManager,
        triggers: TriggerService,
        portals: PortalRegistry | None,
        scratch_areas: ScratchAreas,
        get_change_publisher: Callable[[], Callable[[str, str, Sequence[str], int], None] | None],
        get_notification_publisher: Callable[[], Callable[[dict[str, Any]], None] | None],
        get_title_service: Callable[[], SessionTitleService | None],
        session_boundary: SessionBoundaryBuilder,
        logger: LoggerProtocol | None,
        decisions: DecisionService | None = None,
        cron: CronService | None = None,
    ) -> None:
        self._host = host
        self._ensure_started = ensure_started
        self.agent_resolver = agent_resolver
        self.chat_loop = chat_loop
        self.chat_run_manager = chat_run_manager
        self._temporary_agents = temporary_agents
        self.projects = projects
        self.agents = agents
        self.chat_sessions = sessions
        self._statistics_index = statistics_index
        self._usage_recorder = usage_recorder
        self.tools = tools
        self.models = models
        self.providers = providers
        self.provider_credentials = provider_credentials
        self.system_prompts = system_prompts
        self._get_registry = get_registry
        self._get_skills = get_skills
        self.skills_for = skills_for
        self.project_skill_names = project_skill_names
        self._resources = resources
        self._terminals = terminals
        self._commands = commands
        self._triggers = triggers
        self._deferred_runs: dict[tuple[Any, str], Run] = {}
        self._portals = portals
        self._scratch_areas = scratch_areas
        self._get_change_publisher = get_change_publisher
        self._get_notification_publisher = get_notification_publisher
        self._get_title_service = get_title_service
        self._session_boundary = session_boundary
        self.logger = logger
        self._decisions = decisions
        self._cron = cron
        self._temporary_groups: list[TemporaryExecutionGroups] = []
        self._statistics_service: StatisticsService | None = None
        self.databases = ExtensionDatabases(host.data_dir)

    @property
    def _extensions(self) -> ExtensionRegistry | None:
        return self._get_registry()

    def make_host(self) -> ExtensionHost:
        self._ensure_started()
        return replace(
            self._host,
            for_owner=self._extension_owner_host,
            release_owner=self._release_owner,
        )

    async def _release_owner(self, identity: Any) -> None:
        for key in tuple(self._deferred_runs):
            if key[0] == identity:
                self._deferred_runs.pop(key)
        await self.databases.release(identity)

    def _extension_owner_host(self, identity: Any) -> ExtensionHost:
        if self._extensions is None:
            raise RuntimeError("temporary execution is unavailable")
        self._ensure_started()
        groups = TemporaryExecutionGroups(
            self._temporary_agents,
            self.chat_loop,
            lambda candidate: (
                self._extensions is not None and self._extensions.is_registration_current(candidate)
            ),
            identity,
            run_manager=self.chat_run_manager,
            resources=self._resources,
            validate_binding=self._validate_extension_session_binding,
            usage=lambda group_id, query: self._extension_group_usage(
                identity.name, group_id, query
            ),
            title=lambda group_id, source_text: self._extension_group_title(
                identity.name, group_id, source_text
            ),
            close_session_work=self._close_session_work,
            scratch_areas=self._scratch_areas,
        )
        self._temporary_groups.append(groups)
        state_dir = self._host.data_dir / "extension-data" / identity.name
        state_dir.mkdir(parents=True, exist_ok=True)
        return replace(
            self._host,
            decide=lambda context, request: self._decide(identity, context, request),
            review_context=lambda context: self._review_context(identity, context),
            input_authority=lambda context: self._input_authority(identity, context),
            sample=self._guard_host_async_call(identity, self._host.sample),
            resolve_agent=self._guard_host_call(identity, self._host.resolve_agent),
            resolve_tool_agent=self._guard_host_call(identity, self._host.resolve_tool_agent),
            resolve_tool_agent_async=(
                self._guard_host_async_call(identity, self._host.resolve_tool_agent_async)
                if self._host.resolve_tool_agent_async is not None
                else None
            ),
            store_attachment=self._guard_host_call(identity, self._host.store_attachment),
            resolve_credential=self._guard_host_call(identity, self._host.resolve_credential),
            set_credential=self._guard_host_call(identity, self._host.set_credential),
            resolve_cwd=(
                self._guard_host_call(identity, self._host.resolve_cwd)
                if self._host.resolve_cwd is not None
                else None
            ),
            resolve_permissions=(
                self._guard_host_async_call(identity, self._host.resolve_permissions)
                if self._host.resolve_permissions is not None
                else None
            ),
            for_owner=self._guard_host_call(
                identity, lambda _identity: self._extension_owner_host(identity)
            ),
            temporary_agents=groups,
            state_dir=state_dir,
            open_database=self.databases.opener(identity),
            capture_tool_call=lambda context: self._capture_tool_call(identity, context),
            defer_tool_call=lambda context: self._capture_tool_call(
                identity, context, deferred=True
            ),
            restore_tool_call=lambda receipt: self._restore_tool_call(identity, receipt),
            deliver_tool_outcome=lambda receipt, text: self._deliver_tool_outcome(
                identity, receipt, text
            ),
            catalog=lambda: self._extension_catalog(identity),
            inspect_prompt=lambda config, project_id: self._inspect_extension_prompt(
                identity, config, project_id
            ),
            publish_change=lambda resource, ids, revision: self._publish_extension_change(
                identity, resource, ids, revision
            ),
            notify=partial(self._notify, identity),
            set_page_badge=lambda page_id, count: self._set_page_badge(identity, page_id, count),
            execution_usage=partial(self._execution_usage, identity),
            pending_approvals=lambda group_id, run_ids: self._pending_approvals(
                identity, group_id, run_ids
            ),
            load_result_payload=lambda context, payload_id: self._load_result_payload(
                identity, context, payload_id
            ),
        )

    def _guard_host_call[**P, T](self, identity: Any, handler: Callable[P, T]) -> Callable[P, T]:
        """Refuse new service calls from a cached host after its registration retires."""

        def call(*args: P.args, **kwargs: P.kwargs) -> T:
            self._require_registration(identity)
            return handler(*args, **kwargs)

        return call

    async def _decide(
        self, identity: Any, context: ToolContext | None, request: dict[str, Any]
    ) -> dict[str, Any]:
        """One native Decision request, attributed to its declaring Extension."""
        self._require_registration(identity)
        if self._decisions is None:
            raise DecisionError("The Decision Model is unavailable.", code="not_configured")
        if set(request) - {"state", "questions", "context"} or "state" not in request:
            raise DecisionError("Supply state, questions and optional context.")
        outcomes = await self._decisions.classify(
            [request["state"]],
            request.get("questions"),
            context=request.get("context"),
            usage_context=TaskUsageContext(
                owner_name=identity.name,
                agent_id=context.agent_id if context else None,
                project_id=context.project_id if context else None,
                session_id=context.session_id if context else None,
                run_id=context.run_id if context else None,
                group_id=(
                    context.execution_owner.group_id
                    if context and context.execution_owner
                    else None
                ),
            ),
        )
        self._require_registration(identity)
        result = outcomes[0]
        if isinstance(result, DecisionError):
            raise result
        return result

    async def _review_context(self, identity: Any, context: ToolContext) -> dict[str, Any]:
        self._require_registration(identity)
        result = await self.chat_sessions.run_async(
            shell_review_context, self.chat_sessions, context, self.tools.secret_masker
        )
        # A stored job is authority only when the operator approved its current
        # scope. Unknown/Tool-authored jobs remain evidence for the human task.
        if self._cron is not None:
            unverified = []
            for task in result["evidence"]["automated_requests"]:
                job = None
                if task["source"] == "cron" and task["schedule_id"]:
                    with suppress(CronJobNotFoundError):
                        job = self._cron.get_job(task["schedule_id"])
                if (
                    job is not None
                    and job.operator_approved
                    and job.agent_id == task["agent_id"]
                    and job.project_id == task["project_id"]
                    and self.tools.secret_masker.mask(job.prompt) == task["text"]
                ):
                    result["authorized_tasks"].append(json.dumps(task, ensure_ascii=False))
                else:
                    unverified.append(task)
            result["evidence"]["automated_requests"] = unverified
        self._require_registration(identity)
        return result

    async def _input_authority(
        self, identity: Any, context: ToolContext
    ) -> Literal["user", "authorized_task", "none"]:
        self._require_registration(identity)
        try:
            run = self.chat_run_manager.get(context.run_id)
        except RunNotFoundError:
            run = None
        authority = await current_input_authority(
            self.chat_sessions, context, run=run, cron=self._cron, masker=self.tools.secret_masker
        )
        self._require_registration(identity)
        return authority

    def _guard_host_async_call[**P, T](
        self, identity: Any, handler: Callable[P, Awaitable[T]]
    ) -> Callable[P, Awaitable[T]]:
        async def call(*args: P.args, **kwargs: P.kwargs) -> T:
            # Check when awaited, not when an Extension constructs the coroutine.
            self._require_registration(identity)
            return await handler(*args, **kwargs)

        return call

    async def _close_session_work(self, address: SessionAddress) -> None:
        await self._terminals.close_scope(
            TerminalOwner(address.project_id, address.agent_id, address.session_id)
        )
        await self._commands.close_scope(address)
        if self._portals is not None:
            self._portals.close_sessions([address])

    async def archive_uninstalled_owner_groups(self, installed: frozenset[str]) -> None:
        """Archive the live owner-managed Sessions of every owner not in *installed*.

        A group with active or queued work stays live until the next load.
        """
        for owner_name in await self.chat_sessions.temporary_owners_async():
            if owner_name in installed:
                continue
            groups = sessions = 0
            after = ""
            while True:
                group_ids = await self.chat_sessions.temporary_groups_async(
                    owner_name=owner_name, after=after, limit=100
                )
                for group_id in group_ids:
                    try:
                        archived = await release_temporary_group(
                            self.chat_sessions,
                            self.chat_run_manager,
                            owner_name,
                            group_id,
                            delete=False,
                            close_session_work=self._close_session_work,
                            scratch_areas=self._scratch_areas,
                            archive_reason="extension_removed",
                        )
                    except RunAdmissionBlockedError:
                        if self.logger is not None:
                            self.logger.warning(
                                "Owner-managed Sessions kept while they have work "
                                "(owner=%s group=%s)",
                                owner_name,
                                group_id,
                            )
                        continue
                    groups += 1
                    sessions += archived
                if len(group_ids) < 100:
                    break
                after = group_ids[-1]
            if self.logger is not None and sessions:
                self.logger.warning(
                    "Archived owner-managed Sessions of a removed Extension "
                    "(owner=%s groups=%d sessions=%d)",
                    owner_name,
                    groups,
                    sessions,
                )

    async def _load_result_payload(
        self, identity: Any, context: ToolContext, payload_id: str
    ) -> Any:
        """Load one owned payload whose Tool Result the calling Session can see."""
        if self._extensions is None or not self._extensions.is_registration_current(identity):
            raise ValueError("Extension registration is no longer current")
        # A call outside a Session (management, direct invocation) names no view.
        if not context.result_payloads_available or not is_safe_id(payload_id):
            return None
        address = SessionAddress(context.project_id, context.agent_id, context.session_id)
        return await self.chat_sessions.tool_result_payload_async(
            address, payload_id, owner_name=identity.name
        )

    def _validate_temporary_admission(self, address: Any, admission: Any) -> None:
        if admission.owner is None:
            return
        matching = [groups for groups in self._temporary_groups if groups.owns(admission.owner)]
        if len(matching) != 1:
            raise RunAdmissionBlockedError(
                "This Session is no longer available. Check its state through its Extension."
            )
        matching[0].validate(address, admission)

    async def _validate_extension_session_binding(self, binding: Any) -> None:
        registry = self._extensions
        if registry is None:
            raise RuntimeError("temporary execution is unavailable")
        capability = registry.session_capability(binding, self.tools)
        if capability is None:
            raise RuntimeError("temporary execution is unavailable")
        try:
            await _RUNTIME_WORKERS.run(
                self._validate_extension_session_binding_blocking,
                binding,
                capability.tool_names,
            )
        except Exception as error:
            if self.logger is not None:
                self.logger.warning(
                    "Temporary Session preflight failed owner=%s group=%s participant=%s: %s",
                    getattr(binding, "owner_name", ""),
                    getattr(binding, "group_id", ""),
                    getattr(binding, "participant_id", ""),
                    error,
                )
            raise RuntimeError("temporary execution is unavailable") from error
        if (
            self._extensions is not registry
            or not registry.is_registration_current(capability.identity)
            or registry.session_capability(binding, self.tools) is None
        ):
            raise RuntimeError("temporary execution is unavailable")

    def _validate_extension_session_binding_blocking(
        self,
        binding: Any,
        session_tool_grants: Sequence[str],
    ) -> None:
        config = _temporary_config_from_binding(binding)
        if not config.cwd.is_dir():
            raise RuntimeError("temporary working directory is unavailable")
        self.agent_resolver.require_model_configured(config.model)
        for fallback in config.fallback_models or ():
            self.agent_resolver.require_model_configured(fallback)

        catalog = self.tools.list_tools(include_internal=False)
        ordinary_tools = {tool.name: tool for tool in catalog if not tool.session_scoped}
        private_selections = {tool.name for tool in catalog if tool.session_scoped}
        # Private capabilities are granted by the owner; profiles may only deny
        # them. The ordinary selection applies as configured, also inside a
        # Project.
        assert self._extensions is not None
        record = next(
            item for item in self._extensions.records() if item.name == binding.owner_name
        )
        private_names = {tool.name for tool in record.declarations.tools if tool.session_scoped}
        ordinary_config = replace(
            config,
            tool_access=replace(
                config.tool_access,
                # A broad repository profile materializes the complete catalog.
                # Selecting a private name does not grant its capability.
                allowed=tuple(
                    name for name in config.tool_access.allowed if name not in private_selections
                ),
                denied=tuple(
                    name for name in config.tool_access.denied if name not in private_names
                ),
            ),
        )
        _validate_temporary_tool_configuration(ordinary_config, ordinary_tools)
        project_id = getattr(binding.address, "project_id", None)
        if project_id is not None:
            project = self.projects.get(project_id)
            if config.cwd.resolve() != Path(project.cwd).resolve():
                raise RuntimeError("temporary working directory is outside its Project")

        resolved = self.agent_resolver.resolve_temporary_agent(
            binding.address,
            generation_id=binding.generation_id,
        )
        available = self.tools.list_tools(include_internal=False)
        effective = resolve_tool_access(
            resolved.tool_access,
            available,
            resolved.memory_prompt_mode,
            workspace=resolved.workspace,
            project_memory_enabled=resolved.project_memory_enabled,
            session_tool_grants=session_tool_grants,
        )
        effective_by_name = {tool.name: tool for tool in available}
        # Ordinary Tools follow request-time readiness, just like other Agents;
        # an unrelated unconfigured integration cannot prevent this participant
        # from starting. The owner's private handoff capabilities must be ready.
        if any(
            not tool_is_ready(effective_by_name[name]) for name in effective.session_tool_grants
        ) or set(effective.session_tool_grants) != set(session_tool_grants):
            raise RuntimeError("temporary execution is unavailable")

    async def _execution_usage(
        self, identity: Any, group_id: str, run_ids: list[str] | None = None
    ) -> dict[str, Any]:
        self._require_registration(identity)
        if self._usage_recorder is None:
            raise RuntimeError("Model usage accounting is unavailable")
        result = await self._usage_recorder.execution_totals(identity.name, group_id, run_ids)
        self._require_registration(identity)
        return result

    async def _extension_group_usage(
        self,
        owner_name: str,
        group_id: str,
        query: dict[str, Any],
    ) -> dict[str, Any]:
        if self._statistics_service is None:
            self._ensure_started()
            self._statistics_service = StatisticsService(
                self.chat_sessions,
                cast(Any, self.agents),
                cast(Any, self.projects),
                pricing_lookup=self.models.pricing_for,
                index=self._statistics_index,
                usage_recorder=self._usage_recorder,
            )
        return await self._statistics_service.group_usage(
            owner_name=owner_name,
            group_id=group_id,
            query=query,
        )

    async def _extension_group_title(
        self, owner_name: str, group_id: str, source_text: str
    ) -> str | None:
        title_service = self._get_title_service()
        if title_service is None:
            raise RuntimeError("Extension group titles are unavailable")
        participants = await self.chat_sessions.list_owned_session_summaries_async(
            owner_name=owner_name, group_id=group_id
        )
        return await title_service.generate_group_title(
            owner_name=owner_name,
            group_id=group_id,
            source_text=source_text,
            participants=participants,
        )

    def _publish_extension_change(
        self,
        identity: Any,
        resource: str,
        ids: Sequence[str],
        revision: int,
    ) -> None:
        if (
            self._extensions is None
            or not self._extensions.is_registration_current(identity)
            or not isinstance(resource, str)
            or not resource
            or not isinstance(revision, int)
            or revision < 0
            or not all(isinstance(item, str) and item for item in ids)
        ):
            raise ValueError("extension change is unavailable")
        publisher = self._get_change_publisher()
        if publisher is not None:
            publisher(identity.name, resource, tuple(ids), revision)

    def _require_registration(self, identity: Any) -> None:
        if self._extensions is None or not self._extensions.is_registration_current(identity):
            raise ValueError("Extension registration is no longer current")

    async def _notify(
        self,
        identity: Any,
        title: str,
        body: str,
        *,
        page_id: str,
        route: str = "",
        key: str | None = None,
    ) -> None:
        self._require_registration(identity)
        assert self._extensions is not None
        if not isinstance(page_id, str) or self._extensions.current_page(identity, page_id) is None:
            raise ValueError("Extension page is unavailable")
        title = notification_text(title, name="title", maximum=MAX_NOTIFICATION_TITLE)
        body = notification_text(body, name="body", maximum=MAX_NOTIFICATION_BODY, multiline=True)
        validate_page_route(route)
        if key is not None:
            key = notification_text(key, name="key", maximum=MAX_NOTIFICATION_KEY)
        publisher = self._get_notification_publisher()
        if publisher is not None:
            publisher(
                {
                    "extension": identity.name,
                    "page_id": page_id,
                    "route": route,
                    "title": title,
                    "body": body,
                    "key": key,
                    "at": utc_now_timestamp(),
                }
            )

    def _set_page_badge(self, identity: Any, page_id: str, count: int) -> None:
        self._require_registration(identity)
        assert self._extensions is not None
        if not isinstance(page_id, str):
            raise ValueError("Extension page is unavailable")
        revision = self._extensions.set_page_badge(identity, page_id, count)
        if revision is not None:
            self._publish_extension_change(identity, PAGE_BADGES_RESOURCE, (page_id,), revision)

    async def _capture_tool_call(
        self, identity: Any, context: ToolContext, *, deferred: bool = False
    ) -> str | None:
        self._require_registration(identity)
        if self.tools.get(context.tool_name).extension != identity.name:
            raise ValueError("Extension does not own this Tool")
        receipt = await self.chat_sessions.capture_tool_receipt_async(
            SessionAddress(context.project_id, context.agent_id, context.session_id),
            owner_name=identity.name,
            run_id=context.run_id,
            tool_call_id=context.tool_call_id,
            tool_name=context.tool_name,
            execution_owner=asdict(context.execution_owner) if context.execution_owner else None,
            deferred=deferred,
        )
        self._require_registration(identity)
        if deferred and receipt is not None:
            # Keep the origin independently of the Run manager's bounded history.
            # It also retains cancellation and temporary continuation settings.
            with suppress(RunNotFoundError):
                self._deferred_runs.setdefault(
                    (identity, receipt), self.chat_run_manager.get(context.run_id)
                )
        return receipt

    async def _pending_approvals(
        self, identity: Any, group_id: str, run_ids: Sequence[str]
    ) -> dict[str, Any]:
        self._require_registration(identity)
        if (
            not isinstance(group_id, str)
            or not group_id
            or isinstance(run_ids, str)
            or len(run_ids) > 1000
            or not all(isinstance(run_id, str) and run_id for run_id in run_ids)
        ):
            raise ValueError("Supply a group id and at most 1000 Run ids")
        records = await self.chat_sessions.owned_runs_by_id_async(
            owner_name=identity.name, group_id=group_id, run_ids=run_ids
        )
        self._require_registration(identity)
        snapshot = self.chat_run_manager.approvals_snapshot()
        pending = {request["run_id"] for request in snapshot["requests"]}
        return {
            "revision": snapshot["revision"],
            "requests": [
                {
                    "run_id": record.run_id,
                    "project_id": record.address.project_id,
                    "agent_id": record.address.agent_id,
                    "session_id": record.address.session_id,
                }
                for record in records.values()
                if record.run_id in pending
            ],
        }

    async def _restore_tool_call(self, identity: Any, receipt: str) -> ToolContext | None:
        self._require_registration(identity)
        record = await self.chat_sessions.resolve_tool_receipt_async(
            receipt, owner_name=identity.name
        )
        while record is not None:
            context = None
            error = None
            try:
                context = await self._restored_tool_context(record)
            except Exception as failure:
                # A move or Agent rename can invalidate an intermediate lookup.
                # Only retry it when the canonical invocation actually moved.
                error = failure
            current = await self.chat_sessions.resolve_tool_receipt_async(
                receipt, owner_name=identity.name
            )
            self._require_registration(identity)
            if current == record:
                if error is not None:
                    raise error
                return context
            # A missing original ends restoration; a move of the same generation
            # rebuilds its Agent, cwd and Permission Boundary at the new address.
            record = current
        self._deferred_runs.pop((identity, receipt), None)
        return None

    async def _restored_tool_context(self, record: dict[str, Any]) -> ToolContext:
        """Resolve a receipt snapshot; the caller revalidates it after this async work."""
        context = ToolContext(
            agent_id=record["agent_id"],
            session_id=record["session_id"],
            session_generation_id=record["generation_id"],
            project_id=record["project_id"],
            run_id=record["run_id"],
            tool_call_id=record["tool_call_id"],
            tool_name=record["tool_name"],
            tool_call_index=0,
            workspace=self._host.data_dir,
            data_root=self._host.data_dir,
            vbot_root=Path(__file__).resolve().parents[2],
            execution_owner=RunExecutionOwner(**record["execution_owner"])
            if record["execution_owner"]
            else None,
            # Placeholder until the Session's own boundary replaces it below;
            # the context leaves this method only with that boundary.
            permissions=PermissionBoundary.UNRESTRICTED,
        )
        assert self._host.resolve_tool_agent_async is not None
        agent = await self._host.resolve_tool_agent_async(context)
        address = SessionAddress(context.project_id, context.agent_id, context.session_id)
        working_project = await self.agent_resolver.resolve_working_project_async(
            context.project_id, agent, session_id=context.session_id
        )
        project = (
            await _RUNTIME_WORKERS.run(self.projects.get, working_project)
            if working_project
            else None
        )
        # The Session's boundary as its Runs compose it, so a recovered call
        # (MCP roots, for example) reaches no more than the original call could.
        ceiling = (await self.agent_resolver.session_overrides_async(address)).ceiling()
        permissions = await _RUNTIME_WORKERS.run(
            self._session_boundary,
            agent,
            Path(project.cwd) if project else None,
            ceiling,
            address,
        )
        return replace(
            context,
            workspace=Path(agent.workspace),
            cwd=Path(project.cwd) if project else Path(agent.workspace),
            working_project_id=working_project,
            skill_project_id=working_project,
            permissions=permissions,
        )

    async def _deliver_tool_outcome(self, identity: Any, receipt: str, content: str) -> bool:
        self._require_registration(identity)
        record = await self.chat_sessions.resolve_tool_receipt_async(
            receipt, owner_name=identity.name
        )
        if record is None:
            self._deferred_runs.pop((identity, receipt), None)
            return False
        if not record["deferred"]:
            return await self.chat_sessions.deliver_tool_outcome_async(
                receipt, content, owner_name=identity.name
            )
        outcome = await self.chat_sessions.prepare_tool_outcome_async(
            receipt, content, owner_name=identity.name
        )
        if outcome is None:
            return False
        if outcome["delivered"]:
            self._deferred_runs.pop((identity, receipt), None)
            return True
        self._require_registration(identity)
        # A restart cannot revive an old execution epoch. Its receipt still
        # permits the outcome Note in the same live generation, as for recovery
        # of a captured call, but never a replay or an owned continuation.
        origin = self._deferred_runs.get((identity, receipt))
        wake = origin is not None and origin.cancel_reason != "user"
        owner = (
            RunExecutionOwner(**outcome["execution_owner"]) if outcome["execution_owner"] else None
        )
        if origin is None:
            owner = None
        source = origin.capture_continuation_source() if origin else None
        if source is not None:
            # Receipt lookup already followed the same generation through a
            # move; preserve its continuation settings at that current address.
            source = replace(
                source,
                project_id=outcome["project_id"],
                agent_id=outcome["agent_id"],
                session_id=outcome["session_id"],
            )
        await self._triggers.submit_completion(
            SessionAddress(outcome["project_id"], outcome["agent_id"], outcome["session_id"]),
            notice_id=f"extension:{identity.name}/task:{receipt}",
            origin_run_id=outcome["run_id"],
            body=outcome["content"],
            execution_owner=owner,
            wake=wake,
            continuation_source=source,
            expected_session_generation_id=outcome["generation_id"],
        )
        # Records the durable acknowledgement; a crash before this write is
        # recovered by the canonical Note check in prepare_tool_outcome.
        completed = await self.chat_sessions.prepare_tool_outcome_async(
            receipt, content, owner_name=identity.name
        )
        delivered = completed is not None and completed["delivered"]
        if delivered:
            self._deferred_runs.pop((identity, receipt), None)
        return delivered

    async def _extension_catalog(self, identity: Any) -> dict[str, Any]:
        if self._extensions is None or not self._extensions.is_registration_current(identity):
            raise ValueError("Extension registration is no longer current")
        catalog = await _RUNTIME_WORKERS.run(self._extension_catalog_projection)
        if self._extensions is None or not self._extensions.is_registration_current(identity):
            raise ValueError("Extension registration is no longer current")
        return catalog

    async def _inspect_extension_prompt(
        self, identity: Any, config: TemporaryAgentConfig, project_id: str | None
    ) -> dict[str, Any]:
        if self._extensions is None or not self._extensions.is_registration_current(identity):
            raise ValueError("Extension registration is no longer current")
        config = await _RUNTIME_WORKERS.run(
            self.agent_resolver.prepare_temporary_config, config, project_id
        )
        agent = await _RUNTIME_WORKERS.run(
            self.agent_resolver.preview_temporary_agent,
            replace(config, repository_profile=None, identity_profile=None),
            project_id,
        )
        record = next(item for item in self._extensions.records() if item.name == identity.name)
        grants = tuple(
            tool.name
            for tool in record.declarations.tools
            if tool.session_scoped and tool.name not in config.tool_access.denied
        )
        definitions = await self.chat_loop.preview_tool_definitions(
            agent, session_tool_grants=grants
        )
        result = await _RUNTIME_WORKERS.run(
            self._extension_prompt_projection, config, project_id, agent, definitions, grants
        )
        if self._extensions is None or not self._extensions.is_registration_current(identity):
            raise ValueError("Extension registration is no longer current")
        return result

    def _extension_prompt_projection(
        self,
        config: TemporaryAgentConfig,
        project_id: str | None,
        agent: Any,
        definitions: list[dict[str, Any]],
        grants: tuple[str, ...],
    ) -> dict[str, Any]:
        self._ensure_started()
        project = self.projects.get(project_id) if project_id else None
        context = (
            ProjectPromptContext.from_project(
                project.project_id,
                project.display_name,
                project.cwd,
                project.instruction_files,
                memory_workspace=self.projects.memory_workspace(project.project_id),
                memory_enabled=project.memory_enabled,
            )
            if project is not None
            else None
        )
        blocks: list[dict[str, Any]] = []
        text = self.system_prompts.build_system_prompt(
            agent,
            agent_body=config.instructions,
            project_context=context,
            agent_project_id=project_id,
            skill_registry=self.skills_for(project_id, None),
            effective_tool_definitions=definitions,
            session_tool_grants=grants,
            block_details=blocks,
        )
        return {
            "text": text,
            "blocks": blocks,
            "tools": definitions,
            "model": agent.model,
            "thinking_effort": agent.thinking_effort,
        }

    def _extension_catalog_projection(self) -> dict[str, Any]:
        self._ensure_started()
        projects = self.projects.list()
        skill_choices = [
            {"name": skill.name, "description": skill.description}
            for skill in self._get_skills().list_all()
        ]
        return {
            # Identity Agents in roster order; built-in Agents stay hidden.
            "agents": [
                {
                    "id": agent.id,
                    "name": agent.name,
                    "description": agent.description,
                    "placement": agent.placement.kind if agent.placement is not None else "chat",
                }
                for agent in self.agents.list()
            ],
            "projects": [
                {
                    "id": project.project_id,
                    "name": project.display_name,
                    "cwd": project.cwd,
                    "agent_profiles": [
                        {
                            "id": member.agent_id,
                            "name": member.display_name,
                            "source": member.source,
                            "status": member.status,
                        }
                        for member in self.agent_resolver.cached_scan(project).team
                        if not member.unavailable_reason
                    ],
                    "allowed_skills": effective_project_allowed_skills(
                        project, self.project_skill_names(project.project_id)
                    ),
                }
                for project in projects
            ],
            "tools": [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                    "constraints": list(tool.constraints),
                    "requires_opt_in": tool.requires_opt_in,
                    "family": tool.family,
                    "family_label": tool.family_label,
                    "activation": tool.activation,
                    "activation_source": (
                        list(tool.activation_source)
                        if isinstance(tool.activation_source, tuple)
                        else tool.activation_source
                    ),
                }
                for tool in self.tools.list_tools()
                if tool.catalog_visible and not tool.session_scoped
            ],
            "skills": skill_choices,
            "tool_settings": {
                BASH_TOOL_SETTINGS_KEY: {
                    "type": "object",
                    "properties": {
                        BASH_ALLOWED_ENV_KEY: {
                            "type": "array",
                            "items": {"type": "string"},
                            "uniqueItems": True,
                        }
                    },
                    "additionalProperties": False,
                },
                SUBAGENT_TOOL_SETTINGS_KEY: {
                    "type": "object",
                    "properties": {
                        SUBAGENT_ALLOWED_AGENTS_KEY: {
                            "type": "array",
                            "items": {"type": "string"},
                            "uniqueItems": True,
                        }
                    },
                    "additionalProperties": False,
                },
            },
            "models": self._extension_catalog_models(),
            "prompt_blocks": self.system_prompts.list_blocks(),
        }

    def _extension_catalog_models(self) -> list[dict[str, Any]]:
        usable_connections_by_provider: dict[str, list[str]] = {}

        def usable_connections(provider_id: str) -> list[str]:
            cached = usable_connections_by_provider.get(provider_id)
            if cached is not None:
                return cached
            try:
                provider = self.providers.get(provider_id)
            except KeyError:
                connection_ids: list[str] = []
            else:
                connection_ids = [
                    connection.id
                    for connection in provider.connections
                    if self.provider_credentials.is_usable(
                        provider_id, f"{provider_id}:{connection.id}"
                    )
                ]
            usable_connections_by_provider[provider_id] = connection_ids
            return connection_ids

        models: list[dict[str, Any]] = []
        for provider_id, model in self.models.query(ModelQuery()):
            # An empty ``connections`` allowlist lets the Model run on every Connection.
            connections = [
                connection_id
                for connection_id in usable_connections(provider_id)
                if model.allows_connection(connection_id)
            ]
            if connections:
                models.append(
                    {
                        "id": f"{provider_id}/{model.model_id}",
                        "name": model.name,
                        "connections": connections,
                        # The window of the Connection a bare binding resolves
                        # to: the first usable one the Model allows.
                        "context_window": model.context_window_for(connections[0]),
                        "capabilities": {
                            "tools": model.capabilities.tools,
                            "reasoning": {
                                "supported": model.capabilities.reasoning.supported,
                                "control": model.capabilities.reasoning.control,
                                "levels": list(model.capabilities.reasoning.levels),
                                "budget_max": model.capabilities.reasoning.budget_max,
                                "mandatory": model.capabilities.reasoning.mandatory,
                            },
                        },
                    }
                )
        return models

    def _validate_owned_completion(self, address: Any, owner: Any) -> None:
        """Raise unless the owner's work may still be admitted to this Session."""
        from core.runs import RunAdmission

        self._validate_temporary_admission(address, RunAdmission(owner=owner))

    async def _start_owned_completion(
        self,
        address: Any,
        owner: Any,
        content: str,
        notice_ids: tuple[str, ...],
        on_persisted: Any,
        *,
        expected_session_generation_id: str | None = None,
    ) -> Any:
        self._validate_owned_completion(address, owner)
        groups = next(groups for groups in self._temporary_groups if groups.owns(owner))
        return await groups.continue_completion(
            address,
            owner,
            content,
            notice_ids,
            on_persisted,
            expected_session_generation_id=expected_session_generation_id,
        )
