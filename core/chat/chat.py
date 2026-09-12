"""Chat admission and Queue coordination."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, cast

from core.chat._agentic_progression import AgenticProgression
from core.chat._request_builder import RequestBuilder
from core.chat._run_execution import RunExecution
from core.chat._run_state import _RunRequest
from core.chat._step_outcomes import MAX_TOOL_ITERATIONS
from core.chat._workers import _CHAT_TRANSFORM_WORKERS
from core.chat.block_resolver import ContentBlockResolver
from core.chat.compaction_host import ChatCompactionHost
from core.chat.content_blocks import ContentBlock
from core.chat.errors import (
    ChatError,
    ChatSessionError,
    CompactionUnavailableError,
)
from core.chat.messages import (
    ChatMessage,
    InputOrigin,
    JsonObject,
    MessageSender,
    ReplySurface,
    _display_content_preview,
    queue_content_is_editable,
)
from core.chat.model_resolution import (
    _ensure_provider_exists,
    _resolve_agent_connection,
)
from core.chat.request_runner import WireRequestRunner
from core.compaction.run_coordination import CompactionRunCoordinator
from core.projects import resolve_working_project_id
from core.runs import (
    ActiveRunError,
    QueuedRunItem,
    Run,
    RunAdmission,
    RunExecutor,
    RunKind,
    WaitingWorkAdmission,
)
from core.sessions import (
    ChatSession,
    SessionAddress,
    TemporarySessionBinding,
    editable_session_message_index,
)
from core.sessions.errors import SessionNotFoundError

if TYPE_CHECKING:
    from core.chat._run_state import ChatLoopDependencies, ReflectionNotifier, SessionTitleNotifier
    from core.compaction import CompactionService
    from core.projects import AgentRunOverrides


@dataclass(frozen=True)
class _QueuedRunExecutor:
    """Editable queued executor retaining every immutable admission input."""

    loop: ChatLoop
    request: _RunRequest

    async def __call__(self, run: Run) -> ChatMessage:
        return await self.loop._execution._execute_run(run, self.request)

    def with_edited_content(
        self,
        content: str | list[ContentBlock],
        input_origin: InputOrigin | None,
    ) -> _QueuedRunExecutor:
        return replace(
            self,
            request=replace(
                self.request,
                content=content,
                input_origin=input_origin,
            ),
        )


class ChatLoop:
    """Admit and queue Chat Runs through the shared Run manager."""

    def __init__(
        self,
        dependencies: ChatLoopDependencies,
        *,
        max_tool_iterations: int = MAX_TOOL_ITERATIONS,
        streaming: bool = False,
        attachment_resolver: ContentBlockResolver | None = None,
        compaction_service: CompactionService | None = None,
        reflection_service: ReflectionNotifier | None = None,
        session_title_service: SessionTitleNotifier | None = None,
    ) -> None:
        if max_tool_iterations < 0:
            raise ChatError("max tool iterations must not be negative")
        self._dependencies = dependencies
        self._max_tool_iterations = max_tool_iterations
        self._streaming = streaming
        self._attachment_resolver = attachment_resolver
        self._compaction_service = compaction_service
        self._reflection_service = reflection_service
        self._session_title_service = session_title_service
        wire_requests = WireRequestRunner(dependencies=dependencies, streaming=streaming)
        self._requests = RequestBuilder(dependencies, wire_requests, attachment_resolver)
        self._compaction_runs = CompactionRunCoordinator(
            host=ChatCompactionHost(dependencies, self._requests, compaction_service)
        )
        progression = AgenticProgression(
            dependencies,
            self._requests,
            wire_requests,
            self._compaction_runs,
            compaction_service,
            max_tool_iterations=max_tool_iterations,
            streaming=streaming,
        )
        self._execution = RunExecution(
            dependencies,
            self._requests,
            progression,
            self._compaction_runs,
            compaction_service,
            reflection_service,
            session_title_service,
        )

    def child_loop(self, *, nesting_depth: int) -> ChatLoop:
        """Create a sub-agent child loop sharing this loop's wiring.

        The child reuses the attachment resolver and compaction service so
        child runs behave like normal live runs; only the nesting depth
        differs.
        """
        child = ChatLoop(
            self._dependencies,
            max_tool_iterations=self._max_tool_iterations,
            streaming=self._streaming,
            attachment_resolver=self._attachment_resolver,
            compaction_service=self._compaction_service,
            reflection_service=self._reflection_service,
            session_title_service=self._session_title_service,
        )
        child._requests.nesting_depth = nesting_depth
        return child

    @property
    def compaction_service(self) -> CompactionService | None:
        """The loop's Compaction service; ``None`` disables Compaction."""
        return self._compaction_service

    def run_executor(
        self,
        content: str | list[ContentBlock],
        *,
        reply_surface: ReplySurface | None = None,
        agent_overrides: AgentRunOverrides | None = None,
        temporary_parent_binding: TemporarySessionBinding | None = None,
    ) -> RunExecutor:
        """Return a run-manager executor that runs *content* through this loop.

        The run's project anchor rides ``run.project_id`` (set by the run manager
        from the ``project_id`` passed to ``start``/``enqueue``), not this
        closure: an identity run keeps ``run.project_id is None`` and today's
        behavior; a project run executes project-scoped (session under the
        project anchor, tool cwd = repo). The public way for other domains
        (sub-agents) to hand the run manager an executor.
        """
        request = _RunRequest(
            content=content,
            reply_surface=reply_surface,
            agent_overrides=agent_overrides,
            temporary_parent_binding=temporary_parent_binding,
        )
        return lambda run: self._execution._execute_run(run, request)

    async def send(
        self,
        agent_id: str,
        content: str | list[ContentBlock],
        *,
        session_id: str | None = None,
        input_origin: InputOrigin | None = None,
        project_id: str | None = None,
    ) -> ChatMessage:
        """Run one persisted non-streaming chat turn and return the final assistant message.

        ``project_id=None`` is the global identity session (today's behavior,
        exactly unchanged); a set ``project_id`` opens the session under the
        project anchor and resolves tool cwd to the project repo.
        """
        run = await self._start_run(
            agent_id,
            content,
            session_id=session_id,
            create_missing=True,
            input_origin=input_origin,
            project_id=project_id,
        )
        return cast(ChatMessage, await run.wait())

    async def start_run(
        self,
        agent_id: str,
        content: str | list[ContentBlock],
        *,
        session_id: str,
        internal: bool = False,
        input_origin: InputOrigin | None = None,
        sender: MessageSender | None = None,
        reply_surface: ReplySurface | None = None,
        project_id: str | None = None,
        tool_restriction: Sequence[str] | None = None,
        tool_denial_resolver: Callable[[str], str | None] | None = None,
        input_persisted_hook: Callable[[], None] | None = None,
        run_kind: RunKind = RunKind.USER,
        contributes_to_agent_activity: bool = True,
        resume_process_restart: bool = False,
    ) -> Run:
        """Start one chat run against an existing session for server-facing callers.

        ``project_id=None`` keeps today's identity behavior; a set ``project_id``
        opens the session under the project anchor and keys the run to it.

        ``tool_restriction`` limits which tools this run may actually *dispatch*
        (an intersection with the agent's effective allowlist); it deliberately
        does not touch the provider tool definitions or the system prompt, so a
        restricted run keeps a byte-identical prompt prefix (the prompt-cache
        invariant). ``None`` is the unrestricted default.

        """
        return await self._start_run(
            agent_id,
            content,
            session_id=session_id,
            create_missing=False,
            internal=internal,
            input_origin=input_origin,
            sender=sender,
            reply_surface=reply_surface,
            project_id=project_id,
            tool_restriction=tool_restriction,
            tool_denial_resolver=tool_denial_resolver,
            input_persisted_hook=input_persisted_hook,
            run_kind=run_kind,
            contributes_to_agent_activity=contributes_to_agent_activity,
            resume_process_restart=resume_process_restart,
        )

    async def edit_run(
        self,
        agent_id: str,
        content: str,
        *,
        session_id: str,
        message_id: str,
        reply_surface: ReplySurface | None = None,
        project_id: str | None = None,
    ) -> Run:
        """Start one idle-only Run from an earlier active plain-text User message."""
        return await self._start_run(
            agent_id,
            content,
            session_id=session_id,
            create_missing=False,
            reply_surface=reply_surface,
            project_id=project_id,
            edit_message_id=message_id,
        )

    async def start_run_in_new_session(
        self,
        agent_id: str,
        content: str | list[ContentBlock],
        *,
        internal: bool = False,
        input_origin: InputOrigin | None = None,
        sender: MessageSender | None = None,
        reply_surface: ReplySurface | None = None,
        project_id: str | None = None,
        tool_restriction: Sequence[str] | None = None,
        tool_denial_resolver: Callable[[str], str | None] | None = None,
        input_persisted_hook: Callable[[], None] | None = None,
        run_kind: RunKind = RunKind.USER,
        contributes_to_agent_activity: bool = True,
        resume_process_restart: bool = False,
    ) -> Run:
        """Validate a target, create its Session, and start one Run.

        Automation entry points use this instead of creating a Session before
        target/model/provider validation. A rejected trigger therefore leaves no
        empty Session behind, while the server-facing :meth:`start_run` contract
        still requires an explicitly existing Session.
        """
        return await self._start_run(
            agent_id,
            content,
            session_id=None,
            create_missing=True,
            internal=internal,
            input_origin=input_origin,
            sender=sender,
            reply_surface=reply_surface,
            project_id=project_id,
            tool_restriction=tool_restriction,
            tool_denial_resolver=tool_denial_resolver,
            input_persisted_hook=input_persisted_hook,
            run_kind=run_kind,
            contributes_to_agent_activity=contributes_to_agent_activity,
            resume_process_restart=resume_process_restart,
        )

    async def queue_run(
        self,
        agent_id: str,
        content: str | list[ContentBlock],
        *,
        session_id: str,
        internal: bool = False,
        input_origin: InputOrigin | None = None,
        sender: MessageSender | None = None,
        reply_surface: ReplySurface | None = None,
        project_id: str | None = None,
        tool_restriction: Sequence[str] | None = None,
        tool_denial_resolver: Callable[[str], str | None] | None = None,
        waiting_work_admission: WaitingWorkAdmission | None = None,
        input_persisted_hook: Callable[[], None] | None = None,
        run_kind: RunKind = RunKind.USER,
        contributes_to_agent_activity: bool = True,
        resume_process_restart: bool = False,
    ) -> QueuedRunItem:
        """Queue one chat run for a busy session or start it immediately when idle.

        ``project_id`` scopes the session/run to a project anchor; ``None`` keeps
        today's identity behavior.
        """
        await self._reject_owner_managed_session(project_id, agent_id, session_id)
        agent = self._dependencies.agent_resolver.resolve_agent(project_id, agent_id)
        working_project_id = resolve_working_project_id(project_id, agent)
        provider_id, _connection_id = _resolve_agent_connection(self._dependencies, agent)
        _ensure_provider_exists(self._dependencies.providers, provider_id)
        session = await self._get_session_async(
            agent_id, session_id, create_missing=False, project_id=project_id
        )
        manager = self._dependencies.run_manager
        request = _RunRequest(
            content=content,
            internal=internal,
            input_origin=input_origin,
            sender=sender,
            reply_surface=reply_surface,
            tool_restriction=(tuple(tool_restriction) if tool_restriction is not None else None),
            tool_denial_resolver=tool_denial_resolver,
            input_persisted_hook=input_persisted_hook,
            resume_process_restart=resume_process_restart,
        )
        return await manager.enqueue(
            SessionAddress(project_id=project_id, agent_id=agent_id, session_id=session.id),
            _QueuedRunExecutor(self, request),
            display_content=_display_content_preview(content),
            editable=queue_content_is_editable(content),
            internal=internal,
            waiting_work_admission=waiting_work_admission,
            admission=RunAdmission(
                working_project_id=working_project_id,
                run_kind=run_kind,
                contributes_to_agent_activity=contributes_to_agent_activity,
            ),
        )

    def build_queue_update(
        self,
        agent_id: str,
        session_id: str,
        content: str | list[ContentBlock],
        queued_item: QueuedRunItem,
        input_origin: InputOrigin | None = None,
        project_id: str | None = None,
    ) -> tuple[str, RunExecutor, str]:
        """Build replacement data for a queued run without mutating queue state."""
        agent = self._dependencies.agent_resolver.resolve_agent(project_id, agent_id)
        provider_id, _connection_id = _resolve_agent_connection(self._dependencies, agent)
        _ensure_provider_exists(self._dependencies.providers, provider_id)
        session = self._get_session(
            agent_id, session_id, create_missing=False, project_id=project_id
        )
        executor = queued_item.executor
        if not isinstance(executor, _QueuedRunExecutor) or executor.loop is not self:
            raise ChatError("queued item is not an editable chat request")
        return (
            session.id,
            executor.with_edited_content(content, input_origin),
            _display_content_preview(content),
        )

    async def start_compaction_run(
        self,
        agent_id: str,
        session_id: str,
        instruction: str | None = None,
        *,
        project_id: str | None = None,
    ) -> Run:
        """Start manual Compaction as the Session's active observable Run."""
        compaction_service = self._compaction_service
        if compaction_service is None:
            raise CompactionUnavailableError("Compaction is not available.")

        agent = self._dependencies.agent_resolver.resolve_agent(project_id, agent_id)
        working_project_id = resolve_working_project_id(project_id, agent)
        session = await self._get_session_async(
            agent_id, session_id, create_missing=False, project_id=project_id
        )
        return await self._dependencies.run_manager.start(
            SessionAddress(project_id=project_id, agent_id=agent_id, session_id=session.id),
            lambda run: self._compaction_runs.execute_manual_run(
                run,
                agent,
                session,
                compaction_service,
                instruction=instruction,
            ),
            admission=RunAdmission(working_project_id=working_project_id),
        )

    async def compact_session(
        self,
        agent_id: str,
        session_id: str,
        instruction: str | None = None,
        *,
        project_id: str | None = None,
    ) -> str:
        """Run manual Compaction to completion for synchronous accessors."""
        try:
            run = await self.start_compaction_run(
                agent_id,
                session_id,
                instruction,
                project_id=project_id,
            )
        except CompactionUnavailableError:
            return "Compaction is not available."
        except ActiveRunError:
            return "Cannot compact while a run is active for this session."

        try:
            await run.wait()
        except Exception as exc:
            return f"Compaction failed: {exc}"
        return "Context compacted."

    async def _start_run(
        self,
        agent_id: str,
        content: str | list[ContentBlock] | None = None,
        *,
        session_id: str | None,
        create_missing: bool,
        internal: bool = False,
        input_origin: InputOrigin | None = None,
        sender: MessageSender | None = None,
        reply_surface: ReplySurface | None = None,
        project_id: str | None = None,
        tool_restriction: Sequence[str] | None = None,
        tool_denial_resolver: Callable[[str], str | None] | None = None,
        input_persisted_hook: Callable[[], None] | None = None,
        run_kind: RunKind = RunKind.USER,
        contributes_to_agent_activity: bool = True,
        resume_process_restart: bool = False,
        edit_message_id: str | None = None,
    ) -> Run:
        if session_id is not None:
            await self._reject_owner_managed_session(project_id, agent_id, session_id)
        agent = self._dependencies.agent_resolver.resolve_agent(project_id, agent_id)
        working_project_id = resolve_working_project_id(project_id, agent)
        provider_id, _connection_id = _resolve_agent_connection(self._dependencies, agent)
        _ensure_provider_exists(self._dependencies.providers, provider_id)
        session = await self._get_session_async(
            agent_id, session_id, create_missing=create_missing, project_id=project_id
        )
        if edit_message_id is not None:
            if internal or not isinstance(content, str):
                raise ChatError("history edits require visible plain-text content")
            editable_session_message_index(await session.load_active_async(), edit_message_id)
        manager = self._dependencies.run_manager
        request = _RunRequest(
            content=content,
            internal=internal,
            input_origin=input_origin,
            sender=sender,
            reply_surface=reply_surface,
            tool_restriction=(tuple(tool_restriction) if tool_restriction is not None else None),
            tool_denial_resolver=tool_denial_resolver,
            input_persisted_hook=input_persisted_hook,
            resume_process_restart=resume_process_restart,
            edit_message_id=edit_message_id,
        )
        return await manager.start(
            SessionAddress(project_id=project_id, agent_id=agent_id, session_id=session.id),
            lambda run: self._execution._execute_run(run, request),
            admission=RunAdmission(
                working_project_id=working_project_id,
                run_kind=run_kind,
                contributes_to_agent_activity=contributes_to_agent_activity,
            ),
        )

    async def start_temporary_run(
        self,
        binding: TemporarySessionBinding,
        content: str,
        *,
        owner: Any = None,
        input_id: str | None = None,
        input_already_persisted: bool = False,
    ) -> Run:
        """Start one owner-authorized temporary Session through the normal Chat engine."""

        current = await _CHAT_TRANSFORM_WORKERS.run(
            self._dependencies.sessions.temporary_binding, binding.address
        )
        if current != binding:
            raise ChatError(
                "This Session is no longer available. Check its state through its Extension."
            )
        agent = self._dependencies.agent_resolver.resolve_temporary_agent(
            binding.address, generation_id=binding.generation_id
        )
        provider_id, _connection_id = _resolve_agent_connection(self._dependencies, agent)
        _ensure_provider_exists(self._dependencies.providers, provider_id)
        await self._get_session_async(
            binding.address.agent_id,
            binding.address.session_id,
            create_missing=False,
            project_id=binding.address.project_id,
        )
        request = _RunRequest(
            content=content,
            temporary_binding=binding,
            input_already_persisted=input_already_persisted,
        )
        return await self._dependencies.run_manager.start(
            binding.address,
            lambda run: self._execution._execute_run(run, request),
            admission=RunAdmission(
                contributes_to_agent_activity=False, owner=owner, input_id=input_id
            ),
        )

    async def start_owned_continuation(
        self,
        address: SessionAddress,
        owner: Any,
        content: str,
        input_id: str,
        input_persisted_hook: Callable[[], None] | None = None,
    ) -> Run:
        """Continue a normal Session under an Extension-owned descendant Run."""
        if not isinstance(content, str) or not content:
            raise ChatError("owned continuation content is required")
        if not isinstance(input_id, str) or not input_id:
            raise ChatError("owned continuation input id is required")
        parent = await _CHAT_TRANSFORM_WORKERS.run(
            self._dependencies.sessions.temporary_binding_by_participant,
            owner_name=owner.extension,
            group_id=owner.group_id,
            participant_id=owner.participant_id,
        )
        if parent is not None and (
            parent.address.agent_id != address.agent_id
            or parent.address.project_id != address.project_id
        ):
            parent = None
        agent = (
            self._dependencies.agent_resolver.resolve_temporary_agent(
                parent.address, generation_id=parent.generation_id
            )
            if parent is not None
            else self._dependencies.agent_resolver.resolve_agent(
                address.project_id, address.agent_id
            )
        )
        working_project_id = resolve_working_project_id(address.project_id, agent)
        provider_id, _connection_id = _resolve_agent_connection(self._dependencies, agent)
        _ensure_provider_exists(self._dependencies.providers, provider_id)
        await self._get_session_async(
            address.agent_id,
            address.session_id,
            create_missing=False,
            project_id=address.project_id,
        )
        request = _RunRequest(
            content=content,
            internal=True,
            input_persisted_hook=input_persisted_hook,
            temporary_parent_binding=parent,
        )
        return await self._dependencies.run_manager.start(
            address,
            lambda run: self._execution._execute_run(run, request),
            admission=RunAdmission(
                working_project_id=working_project_id,
                run_kind=RunKind.SYSTEM,
                contributes_to_agent_activity=False,
                owner=owner,
                input_id=input_id,
            ),
        )

    async def _reject_owner_managed_session(
        self, project_id: str | None, agent_id: str, session_id: str
    ) -> None:
        address = SessionAddress(project_id=project_id, agent_id=agent_id, session_id=session_id)
        try:
            binding = await _CHAT_TRANSFORM_WORKERS.run(
                self._dependencies.sessions.temporary_binding, address
            )
        except SessionNotFoundError:
            return
        if binding is not None:
            raise ChatError(
                "This Session is managed by an Extension. Use that Extension to resume it."
            )

    def _get_session(
        self,
        agent_id: str,
        session_id: str | None,
        *,
        create_missing: bool,
        project_id: str | None = None,
    ) -> ChatSession:
        session_manager = self._dependencies.sessions
        if session_id is None:
            if not create_missing:
                raise ChatSessionError("session id is required")
            return session_manager.create(agent_id, project_id=project_id)
        try:
            return session_manager.get(
                SessionAddress(project_id=project_id, agent_id=agent_id, session_id=session_id)
            )
        except ChatSessionError:
            if not create_missing:
                raise
            return session_manager.create(agent_id, session_id=session_id, project_id=project_id)

    async def _get_session_async(
        self,
        agent_id: str,
        session_id: str | None,
        *,
        create_missing: bool,
        project_id: str | None = None,
    ) -> ChatSession:
        session_manager = self._dependencies.sessions
        if session_id is None:
            if not create_missing:
                raise ChatSessionError("session id is required")
            return await session_manager.create_async(agent_id, project_id=project_id)
        try:
            return await session_manager.get_async(
                SessionAddress(project_id=project_id, agent_id=agent_id, session_id=session_id)
            )
        except ChatSessionError:
            if not create_missing:
                raise
            return await session_manager.create_async(
                agent_id,
                session_id=session_id,
                project_id=project_id,
            )

    async def preview_tool_definitions(
        self, agent: Any, *, session_tool_grants: Sequence[str] = ()
    ) -> list[JsonObject]:
        """Inspect production Tool visibility without starting a Run or calling a Model."""
        return await self._requests.preview_tool_definitions(
            agent, session_tool_grants=session_tool_grants
        )
