"""Chat admission and Queue coordination."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

from core.agents import is_live_agent
from core.chat._agentic_progression import AgenticProgression
from core.chat._external_run import ExternalRun, start_external_run
from core.chat._queued_input import QueuedChatInput as _QueuedRunExecutor
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
from core.projects import AgentOverrides
from core.runs import (
    ActiveRunError,
    QueuedRunItem,
    Run,
    RunAdmission,
    RunCancelledError,
    RunExecutor,
    RunKind,
    WaitingWorkAdmission,
)
from core.sessions import (
    AGENT_DEFAULT_PROJECT,
    ChatSession,
    SessionAddress,
    TemporarySessionBinding,
    WorkingProjectChoice,
    editable_session_message_index,
)
from core.sessions.errors import SessionNotFoundError
from core.utils.logging import get_logger

if TYPE_CHECKING:
    from core.chat._run_state import ChatLoopDependencies, ReflectionNotifier, SessionTitleNotifier
    from core.compaction import CompactionService

_LOGGER = get_logger("chat")


@dataclass(frozen=True)
class _NewSession:
    """The Session a Run start creates once its target validated.

    ``session_id`` is the id it gets (the store allocates one when ``None``),
    ``agent_overrides`` are stored with it in the creating write, ``actor``
    names who asked for it (see ``ChatSessionManager.create``), and
    ``working_project_id`` is the working Project a Session of an Identity
    Agent starts in (a Project id, ``None`` for the Workspace, or the Agent's
    default Project).
    """

    session_id: str | None = None
    agent_overrides: AgentOverrides = field(default_factory=AgentOverrides)
    actor: str | None = None
    working_project_id: WorkingProjectChoice = AGENT_DEFAULT_PROJECT


class ChatLoop:
    """Admit and queue Chat Runs through the shared Run manager."""

    def __init__(
        self,
        dependencies: ChatLoopDependencies,
        *,
        max_tool_iterations: int = MAX_TOOL_ITERATIONS,
        attachment_resolver: ContentBlockResolver | None = None,
        compaction_service: CompactionService | None = None,
        reflection_service: ReflectionNotifier | None = None,
        session_title_service: SessionTitleNotifier | None = None,
    ) -> None:
        if max_tool_iterations < 0:
            raise ChatError("max tool iterations must not be negative")
        self._dependencies = dependencies
        self._max_tool_iterations = max_tool_iterations
        self._attachment_resolver = attachment_resolver
        self._compaction_service = compaction_service
        self._reflection_service = reflection_service
        self._session_title_service = session_title_service
        wire_requests = WireRequestRunner(dependencies=dependencies)
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

    @property
    def compaction_service(self) -> CompactionService | None:
        """The loop's Compaction service; ``None`` disables Compaction."""
        return self._compaction_service

    def run_executor(
        self,
        content: str | list[ContentBlock],
        *,
        reply_surface: ReplySurface | None = None,
        temporary_parent_binding: TemporarySessionBinding | None = None,
        parent_agent_input: bool = False,
    ) -> RunExecutor:
        """Return a run-manager executor that runs *content* through this loop.

        The run's project anchor rides ``run.project_id`` (set by the run manager
        from the ``project_id`` passed to ``start``/``enqueue``), not this
        closure: an identity run keeps ``run.project_id is None`` and today's
        behavior; a project run executes project-scoped (session under the
        project anchor, tool cwd = repo). The public way for other domains
        (sub-agents) to hand the run manager an executor. ``parent_agent_input``
        marks *content* as written by the Parent Agent of a Sub-Agent Session.
        The executor can steer a running Run when the request allows it.
        """
        request = _RunRequest(
            content=content,
            reply_surface=reply_surface,
            temporary_parent_binding=temporary_parent_binding,
            parent_agent_input=parent_agent_input,
        )
        return _QueuedRunExecutor(self, request)

    async def send(
        self,
        agent_id: str,
        content: str | list[ContentBlock],
        *,
        session_id: str | None = None,
        input_origin: InputOrigin | None = None,
        project_id: str | None = None,
    ) -> ChatMessage:
        """Run one persisted chat turn and return the final assistant message.

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
        max_tool_iterations: int | None = None,
        input_persisted_hook: Callable[[], None] | None = None,
        run_kind: RunKind = RunKind.USER,
        contributes_to_agent_activity: bool = True,
        source_session_id: str | None = None,
        context_note: str | None = None,
    ) -> Run:
        """Start one chat run against an existing session for server-facing callers.

        ``project_id=None`` keeps today's identity behavior; a set ``project_id``
        opens the session under the project anchor and keys the run to it.

        ``tool_restriction`` limits which tools this run may actually *dispatch*
        (an intersection with the agent's effective allowlist); it deliberately
        does not touch the provider tool definitions or the system prompt, so a
        restricted run keeps a byte-identical prompt prefix (the prompt-cache
        invariant). ``None`` is the unrestricted default.

        ``max_tool_iterations`` narrows the loop's dispatched Tool-iteration
        limit for this run only (it never raises it); reaching it fails further
        Tool Calls with ``tool_iteration_limit`` and asks for the final answer.
        ``None`` keeps the loop's limit.

        ``source_session_id`` attributes a review Run executing in a fork to the
        Session it examines; it is accessor-only provenance on the Run.

        ``context_note`` is stored as a note right before the input, for context
        the Model reads with it, such as what happened since the previous Run.
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
            max_tool_iterations=max_tool_iterations,
            input_persisted_hook=input_persisted_hook,
            run_kind=run_kind,
            contributes_to_agent_activity=contributes_to_agent_activity,
            source_session_id=source_session_id,
            context_note=context_note,
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
        session_id: str | None = None,
        agent_overrides: AgentOverrides | None = None,
        working_project_id: WorkingProjectChoice = AGENT_DEFAULT_PROJECT,
        actor: str | None = None,
        internal: bool = False,
        input_origin: InputOrigin | None = None,
        sender: MessageSender | None = None,
        reply_surface: ReplySurface | None = None,
        project_id: str | None = None,
        tool_restriction: Sequence[str] | None = None,
        tool_denial_resolver: Callable[[str], str | None] | None = None,
        max_tool_iterations: int | None = None,
        input_persisted_hook: Callable[[], None] | None = None,
        run_kind: RunKind = RunKind.USER,
        contributes_to_agent_activity: bool = True,
        context_note: str | None = None,
    ) -> Run:
        """Validate a target, create its Session, and start one Run.

        A new conversation and automation entry points use this instead of
        creating a Session before target/model/provider validation: the Agent,
        its Model (including an override Model) and Provider are checked first,
        then the Session is created with ``agent_overrides`` in the same write,
        so the first Run already runs with them. A rejected start leaves no
        Session behind: a start the Run manager refuses after the creation
        removes the Session again. ``session_id`` is the id the new Session gets
        (an existing Session at that id fails the start); ``actor`` names who
        asked for the Session (``rpc``, ``command``) and makes its creation log
        at INFO. A Session of an Identity Agent works in ``working_project_id``
        for its whole life: a Project id (it must exist), ``None`` for the
        Agent's Workspace, or by default the Agent's default Project; a Project
        Agent's Session works in its address Project and takes no other. The
        server-facing :meth:`start_run` contract still requires an explicitly
        existing Session. ``context_note`` is as in :meth:`start_run`.
        """
        return await self._start_run(
            agent_id,
            content,
            session_id=None,
            create_missing=True,
            new_session=_NewSession(
                session_id=session_id,
                agent_overrides=agent_overrides or AgentOverrides(),
                actor=actor,
                working_project_id=working_project_id,
            ),
            internal=internal,
            input_origin=input_origin,
            sender=sender,
            reply_surface=reply_surface,
            project_id=project_id,
            tool_restriction=tool_restriction,
            tool_denial_resolver=tool_denial_resolver,
            max_tool_iterations=max_tool_iterations,
            input_persisted_hook=input_persisted_hook,
            run_kind=run_kind,
            contributes_to_agent_activity=contributes_to_agent_activity,
            context_note=context_note,
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
        max_tool_iterations: int | None = None,
        waiting_work_admission: WaitingWorkAdmission | None = None,
        input_persisted_hook: Callable[[], None] | None = None,
        run_kind: RunKind = RunKind.USER,
        contributes_to_agent_activity: bool = True,
        context_note: str | None = None,
    ) -> QueuedRunItem:
        """Queue one chat run for a busy session or start it immediately when idle.

        ``project_id`` scopes the session/run to a project anchor; ``None`` keeps
        today's identity behavior. ``max_tool_iterations`` and ``context_note``
        are as in :meth:`start_run`.
        """
        _validate_run_tool_iteration_limit(max_tool_iterations)
        await self._reject_owner_managed_session(project_id, agent_id, session_id)
        resolver = self._dependencies.agent_resolver
        agent = await resolver.resolve_agent_async(project_id, agent_id, session_id=session_id)
        working_project_id = await resolver.resolve_working_project_async(
            project_id, agent, session_id=session_id
        )
        _reject_live_input(agent, run_kind)
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
            max_tool_iterations=max_tool_iterations,
            input_persisted_hook=input_persisted_hook,
            context_note=context_note,
        )
        return await manager.enqueue(
            SessionAddress(project_id=project_id, agent_id=agent_id, session_id=session.id),
            _QueuedRunExecutor(self, request),
            display_content=_display_content_preview(content),
            editable=queue_content_is_editable(content),
            steerable=request.supports_steering and run_kind == RunKind.USER,
            internal=internal,
            waiting_work_admission=waiting_work_admission,
            admission=RunAdmission(
                working_project_id=working_project_id,
                run_kind=run_kind,
                contributes_to_agent_activity=contributes_to_agent_activity,
                expected_session_generation_id=session.generation_id,
            ),
        )

    async def build_queue_update(
        self,
        agent_id: str,
        session_id: str,
        content: str | list[ContentBlock],
        queued_item: QueuedRunItem,
        input_origin: InputOrigin | None = None,
        project_id: str | None = None,
    ) -> tuple[str, RunExecutor, str]:
        """Build replacement data for a queued run without mutating queue state."""
        agent = await self._dependencies.agent_resolver.resolve_agent_async(
            project_id, agent_id, session_id=session_id
        )
        provider_id, _connection_id = _resolve_agent_connection(self._dependencies, agent)
        _ensure_provider_exists(self._dependencies.providers, provider_id)
        session = await self._get_session_async(
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

        resolver = self._dependencies.agent_resolver
        agent = await resolver.resolve_agent_async(project_id, agent_id, session_id=session_id)
        working_project_id = await resolver.resolve_working_project_async(
            project_id, agent, session_id=session_id
        )
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
            admission=RunAdmission(
                working_project_id=working_project_id,
                expected_session_generation_id=session.generation_id,
            ),
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
        except RunCancelledError:
            # A Stop during the commit leaves the checkpoint stored.
            if "checkpoint_id" not in run.terminal_payload_extras:
                return "Compaction cancelled."
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
        new_session: _NewSession | None = None,
        internal: bool = False,
        input_origin: InputOrigin | None = None,
        sender: MessageSender | None = None,
        reply_surface: ReplySurface | None = None,
        project_id: str | None = None,
        tool_restriction: Sequence[str] | None = None,
        tool_denial_resolver: Callable[[str], str | None] | None = None,
        max_tool_iterations: int | None = None,
        input_persisted_hook: Callable[[], None] | None = None,
        run_kind: RunKind = RunKind.USER,
        contributes_to_agent_activity: bool = True,
        edit_message_id: str | None = None,
        source_session_id: str | None = None,
        context_note: str | None = None,
    ) -> Run:
        _validate_run_tool_iteration_limit(max_tool_iterations)
        if session_id is None and create_missing and new_session is None:
            new_session = _NewSession()
        if session_id is not None:
            await self._reject_owner_managed_session(project_id, agent_id, session_id)
        resolver = self._dependencies.agent_resolver
        if new_session is None:
            agent = await resolver.resolve_agent_async(project_id, agent_id, session_id=session_id)
            # A Session that does not exist yet (``create_missing``) starts in the
            # Agent's default Project.
            working_project_id = await resolver.resolve_working_project_async(
                project_id, agent, session_id=session_id
            )
        else:
            agent = await resolver.resolve_agent_async(
                project_id, agent_id, new_session_overrides=new_session.agent_overrides
            )
            working_project_id = await resolver.resolve_working_project_async(
                project_id, agent, requested=new_session.working_project_id
            )
        _reject_live_input(agent, run_kind)
        provider_id, _connection_id = _resolve_agent_connection(self._dependencies, agent)
        _ensure_provider_exists(self._dependencies.providers, provider_id)
        if new_session is not None:
            session = await self._dependencies.sessions.create_async(
                agent_id,
                session_id=new_session.session_id,
                project_id=project_id,
                actor=new_session.actor,
                metadata=new_session.agent_overrides.session_metadata() or None,
                working_project_id=_stored_working_project(project_id, working_project_id),
            )
        else:
            session = await self._get_session_async(
                agent_id,
                session_id,
                create_missing=create_missing,
                project_id=project_id,
                working_project_id=_stored_working_project(project_id, working_project_id),
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
            max_tool_iterations=max_tool_iterations,
            input_persisted_hook=input_persisted_hook,
            edit_message_id=edit_message_id,
            context_note=context_note,
        )
        address = SessionAddress(project_id=project_id, agent_id=agent_id, session_id=session.id)
        try:
            return await manager.start(
                address,
                lambda run: self._execution._execute_run(run, request),
                admission=RunAdmission(
                    working_project_id=working_project_id,
                    run_kind=run_kind,
                    contributes_to_agent_activity=contributes_to_agent_activity,
                    source_session_id=source_session_id,
                    expected_session_generation_id=(
                        session.generation_id if not create_missing else None
                    ),
                ),
            )
        except BaseException:
            # The Run manager admits nothing when it raises, so the new Session is unused.
            if new_session is not None:
                await self._discard_new_session(address)
            raise

    async def _discard_new_session(self, address: SessionAddress) -> None:
        """Remove a Session this start created for a Run it could not start (best effort)."""
        sessions = self._dependencies.sessions
        try:
            await sessions.run_async(sessions.delete, address)
        except Exception:
            _LOGGER.warning(
                "Could not remove the unused new Session (agent=%s session=%s)",
                address.agent_id,
                address.session_id,
                exc_info=True,
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
        agent = await self._dependencies.agent_resolver.resolve_temporary_agent_async(
            binding.address, generation_id=binding.generation_id, session=binding.address
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
            await self._dependencies.agent_resolver.resolve_temporary_agent_async(
                parent.address, generation_id=parent.generation_id, session=address
            )
            if parent is not None
            else await self._dependencies.agent_resolver.resolve_agent_async(
                address.project_id, address.agent_id, session_id=address.session_id
            )
        )
        working_project_id = await self._dependencies.agent_resolver.resolve_working_project_async(
            address.project_id, agent, session_id=address.session_id
        )
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

    async def _get_session_async(
        self,
        agent_id: str,
        session_id: str | None,
        *,
        create_missing: bool,
        project_id: str | None = None,
        working_project_id: str | None = None,
    ) -> ChatSession:
        """Return the Session, creating a missing one in *working_project_id* on request."""
        session_manager = self._dependencies.sessions
        if session_id is None:
            raise ChatSessionError("session id is required")
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
                working_project_id=working_project_id,
            )

    async def preview_tool_definitions(
        self, agent: Any, *, session_tool_grants: Sequence[str] = ()
    ) -> list[JsonObject]:
        """Inspect production Tool visibility without starting a Run or calling a Model."""
        return await self._requests.preview_tool_definitions(
            agent, session_tool_grants=session_tool_grants
        )

    async def start_external_run(
        self,
        agent_id: str,
        *,
        model: str,
        title: str,
        run_kind: RunKind = RunKind.LIVE,
        extra_tools: Sequence[str] = (),
        on_cancel: Callable[[], None] | None = None,
    ) -> ExternalRun:
        """Start a Run in a new Session whose turns a Model outside the loop produces.

        The Session is titled *title*; *model* is recorded on its Assistant
        messages. The Model can call the Agent's Tools and *extra_tools*
        (:attr:`ExternalRun.tool_definitions`). The Run does not count as Agent
        activity. *on_cancel* is called when the user cancels the Run, so the
        owner can end the conversation.
        """
        return await start_external_run(
            self._dependencies,
            self._requests,
            agent_id,
            model=model,
            title=title,
            run_kind=run_kind,
            extra_tools=extra_tools,
            on_cancel=on_cancel,
        )


def _stored_working_project(project_id: str | None, working_project_id: str | None) -> str | None:
    """Return the working Project a new Session stores: none for a Project Session."""
    return None if project_id is not None else working_project_id


def _reject_live_input(agent: Any, run_kind: RunKind) -> None:
    """Refuse typed input to a Session that records a Live voice call."""
    if is_live_agent(agent) and run_kind != RunKind.LIVE:
        raise ChatError("This Session records a Live voice call; it takes no typed messages.")


def _validate_run_tool_iteration_limit(max_tool_iterations: int | None) -> None:
    if max_tool_iterations is not None and max_tool_iterations < 0:
        raise ChatError("max tool iterations must not be negative")
