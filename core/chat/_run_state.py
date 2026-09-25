"""Chat Run input, resolved state, and context preparation."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol

from core.chat._step_outcomes import _ToolProgress
from core.chat._workers import _CHAT_TRANSFORM_WORKERS
from core.chat.content_blocks import ContentBlock
from core.chat.continuation import (
    ContinuationState,
    ContinuationTracker,
    JournalBoundary,
)
from core.chat.errors import ChatError
from core.chat.events import _close_adapter
from core.chat.messages import (
    ChatMessage,
    InputOrigin,
    JsonObject,
    MessageSender,
    ReplySurface,
)
from core.chat.model_resolution import (
    _ensure_provider_exists,
    _resolve_agent_connection,
    _split_agent_model,
)
from core.chat.recovery import RecoveryBudget
from core.chat.usage import RequestContextUsage
from core.chat.wire_shaping import RequestImageBudget
from core.projects import (
    resolve_prompt_project,
    resolve_skill_scope,
    runtime_agent_body,
)
from core.prompts import PinnedSkillCatalog, ProjectPromptContext
from core.prompts.pinned_context import (
    pinned_memory_files,
    pinned_skill_catalog,
    pinned_soul_context,
    pinned_working_project_context,
)
from core.providers.accounts import ConnectionRef
from core.providers.adapter import TerminalOutcome
from core.providers.reasoning import (
    DEFAULT_REASONING_REPLAY_POLICY,
    ReasoningReplayPolicy,
)
from core.runs import Run, RunStatus
from core.sessions import (
    ChatSession,
    PromptEpoch,
    SeenSkillsUpdate,
    SessionAddress,
    SessionEditResult,
    SessionReadBatch,
    SessionReadCursor,
    TemporarySessionBinding,
    ToolResultFacts,
    editable_session_message_index,
    latest_project_tool_context_id,
)
from core.tools import ToolContract

if TYPE_CHECKING:
    from core.chat._request_builder import RequestBuilder
    from core.extensions import ExtensionRegistry
    from core.models.models import ModelRegistry
    from core.projects import AgentResolver, AgentRunOverrides, Project, ProjectStore
    from core.prompts import SystemPromptManager
    from core.providers.adapter import ProviderAdapter
    from core.providers.providers import ProviderRegistry
    from core.runs import ChatRunManager
    from core.runtime.interfaces import ProviderCredentialResolverProtocol
    from core.sessions import ChatSessionManager
    from core.skills.skills import SkillRegistry
    from core.storage import StorageManager
    from core.tools.change_tracker import ChangeTracker
    from core.tools.file_state import FileReadState
    from core.tools.process_manager import ProcessManager
    from core.tools.tools import ToolRegistry


@dataclass(frozen=True)
class RequestState:
    messages: list[JsonObject]
    tools: list[JsonObject]
    allowed_tool_names: tuple[str, ...]
    session_tool_grants: tuple[str, ...]
    tool_contracts: Mapping[str, ToolContract] = field(default_factory=dict)


_RequestState = RequestState


@dataclass(frozen=True)
class _AssistantStep:
    """One canonical Assistant message plus its Provider terminal meaning."""

    message: ChatMessage
    terminal_outcome: TerminalOutcome | None
    recovery: Literal["none", "continue", "interrupt"] = "none"
    recovery_note: str | None = None
    failure: Exception | None = None
    recovery_error: Exception | None = None


@dataclass(frozen=True)
class ChatLoopDependencies:
    """Explicit collaborators required by the Chat domain.

    Runtime owns construction of this contract. Chat owns its shape, so adding a
    Runtime service does not silently make that service available inside the
    Agentic Loop. Callbacks keep intentionally live reload seams and the
    bootstrap-late System Prompt manager explicit without reopening Runtime.
    """

    agent_resolver: AgentResolver
    projects: ProjectStore
    providers: ProviderRegistry
    models: ModelRegistry
    provider_credentials: ProviderCredentialResolverProtocol
    sessions: ChatSessionManager
    run_manager: ChatRunManager
    tools: ToolRegistry
    process_manager: ProcessManager
    file_read_state: FileReadState
    change_tracker: ChangeTracker
    storage: StorageManager
    get_extension_registry: Callable[[], ExtensionRegistry | None]
    get_system_prompts: Callable[[], SystemPromptManager]
    get_adapter: Callable[[ConnectionRef], ProviderAdapter]
    resolve_skills: Callable[[str | None, str | None], SkillRegistry]
    refresh_skills: Callable[[str | None, str | None], SkillRegistry]
    get_local_context_windows: Callable[[], Mapping[str, Any]]
    image_understanding_available: Callable[[], Awaitable[bool]]
    deliver_background_completions: Callable[[Run, ChatSession], bool]


@dataclass(frozen=True)
class _RunRequest:
    """Immutable input captured by one admitted Run executor."""

    content: str | list[ContentBlock] | None
    internal: bool = False
    input_origin: InputOrigin | None = None
    sender: MessageSender | None = None
    reply_surface: ReplySurface | None = None
    tool_restriction: tuple[str, ...] | None = None
    tool_denial_resolver: Callable[[str], str | None] | None = None
    input_persisted_hook: Callable[[], None] | None = None
    agent_overrides: AgentRunOverrides | None = None
    resume_process_restart: bool = False
    edit_message_id: str | None = None
    temporary_binding: TemporarySessionBinding | None = None
    input_already_persisted: bool = False
    temporary_parent_binding: TemporarySessionBinding | None = None

    @property
    def supports_steering(self) -> bool:
        """Only ordinary user input can share the current execution policy."""
        return (
            not self.internal
            and self.sender is None
            and (self.reply_surface is None or self.reply_surface.kind == "webui")
            and self.tool_restriction is None
            and self.tool_denial_resolver is None
            and self.input_persisted_hook is None
            and self.agent_overrides is None
            and self.temporary_binding is None
            and self.temporary_parent_binding is None
            and not self.input_already_persisted
            and not self.resume_process_restart
        )


@dataclass(frozen=True)
class _ModelTarget:
    """One resolved Provider target used for Model steps in a Run."""

    provider_id: str
    connection_id: str
    model_id: str
    model_reference: str
    # User-facing Model string recorded on this route's Assistant messages.
    public_model: str
    adapter: ProviderAdapter
    replay_policy: ReasoningReplayPolicy
    input_modalities: frozenset[str]
    wire_media_types: frozenset[str]
    chunk_timeout_seconds: float | None
    max_image_bytes: int | None = None


@dataclass
class _RunExecutionContext:
    """Resolved, Run-local state shared by progression, Tools, and Compaction."""

    run: Run
    request: _RunRequest
    session: ChatSession
    agent: Any
    agent_body: str
    primary_target: _ModelTarget
    project_id: str | None
    project_cwd: Path | None
    project_prompt_context: ProjectPromptContext | None
    working_project_context: str | None
    soul_context: str | None
    memory_files_context: str | None
    skill_project_id: str | None
    skill_registry: SkillRegistry
    skill_catalog: PinnedSkillCatalog
    prompt_cache_affinity_id: str
    prior_continuation: ContinuationState | None
    continuation_tracker: ContinuationTracker | None
    continuation_reminder: str | None
    session_snapshot: _SessionSnapshot
    request_state: _RequestState | None = None
    image_budget: RequestImageBudget = field(default_factory=RequestImageBudget)
    context_usage: RequestContextUsage = field(default_factory=RequestContextUsage)
    recovery: RecoveryBudget = field(default_factory=RecoveryBudget)
    tool_progress: _ToolProgress = field(default_factory=_ToolProgress)
    interruption_chain: list[ChatMessage] = field(default_factory=list)
    # perf_counter() at which the first step's request assembly began; the
    # progression consumes it for the ``chat.request_build`` measurement.
    request_build_started: float | None = None


class _HistoryWrite(Protocol):
    def __call__(
        self, *, continuation_records: list[JsonObject], since: SessionReadCursor
    ) -> Awaitable[SessionReadBatch | None]: ...


@dataclass
class _SessionSnapshot:
    """Run-local canonical Session state refreshed through append-only deltas."""

    messages: list[ChatMessage]
    cursor: SessionReadCursor
    active_lineage: list[ChatMessage]
    pending_edit_message_id: str | None = None

    @property
    def active_messages(self) -> list[ChatMessage]:
        """Return current lineage, including an admitted edit not yet persisted."""
        active = self.active_lineage
        if self.pending_edit_message_id is None:
            return active
        target_index = editable_session_message_index(active, self.pending_edit_message_id)
        return active[:target_index]

    def begin_edit(self, message_id: str) -> None:
        editable_session_message_index(self.active_lineage, message_id)
        self.pending_edit_message_id = message_id

    @classmethod
    async def load(cls, session: ChatSession) -> _SessionSnapshot:
        batch = await session.load_since_async()
        if batch is None:
            raise AssertionError("A full Session snapshot must always produce a cursor")
        return cls(
            messages=list(batch.messages),
            cursor=batch.cursor,
            active_lineage=list(batch.active_messages),
        )

    async def refresh(self, session: ChatSession) -> None:
        await self._apply(session, await session.load_since_async(self.cursor))

    async def append(
        self,
        session: ChatSession,
        messages: list[ChatMessage],
        *,
        journal: JournalBoundary | None = None,
        seen_skills: SeenSkillsUpdate | None = None,
        tool_results: Mapping[str, ToolResultFacts] | None = None,
    ) -> None:
        """Persist *messages* and advance past them in the same transaction.

        *tool_results* reports how each appended Tool Result's call ended.
        *seen_skills* commits in that transaction too, so a Skill is marked
        seen exactly when the note announcing it persists.
        """
        if not messages:
            raise ValueError("a snapshot append requires Messages")
        await self.commit(
            session,
            partial(
                session.append_many_async,
                messages,
                seen_skills=seen_skills,
                tool_results=tool_results,
            ),
            journal=journal,
        )

    async def apply_edit(
        self,
        session: ChatSession,
        messages: list[ChatMessage],
        *,
        journal: JournalBoundary | None = None,
        seen_skills: SeenSkillsUpdate | None = None,
    ) -> SessionEditResult:
        """Commit the admitted edit with its replacement *messages* in one transaction.

        The Continuation restarts from *journal*'s records. The snapshot is
        replaced by the Session's state after the edit.
        """
        target = self.pending_edit_message_id
        if target is None:
            raise ValueError("no history edit was admitted")

        async def write(records: list[JsonObject]) -> SessionEditResult:
            return await session.apply_edit_async(
                target, messages, seen_skills=seen_skills, continuation_records=records
            )

        result = await (write([]) if journal is None else journal.commit(write))
        self.pending_edit_message_id = None
        self.messages = list(result.batch.messages)
        self.active_lineage = list(result.batch.active_messages)
        self.cursor = result.batch.cursor
        return result

    async def commit_checkpoint(
        self,
        session: ChatSession,
        checkpoint: ChatMessage,
        *,
        epoch: PromptEpoch,
    ) -> str | None:
        """Persist a Compaction *checkpoint* and its prompt *epoch* while this snapshot is current.

        Returns the rotated prompt-cache affinity id and advances past the
        checkpoint, or returns ``None`` without writing or advancing when
        another writer changed the Session since this snapshot's cursor.
        """
        committed = await session.commit_compaction_async(
            checkpoint, since=self.cursor, epoch=epoch
        )
        if committed is None:
            return None
        batch, affinity_id = committed
        await self._apply(session, batch)
        return affinity_id

    async def commit(
        self,
        session: ChatSession,
        write: _HistoryWrite,
        *,
        journal: JournalBoundary | None = None,
    ) -> None:
        """Run one history *write* of this Session and advance to its result.

        *write* persists the given Continuation records in its own transaction
        and returns every record after *since* from that transaction, including
        other writers' appends, exactly as :meth:`refresh` would.
        """
        if journal is None:
            batch = await write(continuation_records=[], since=self.cursor)
        else:
            batch = await journal.commit(
                lambda records: write(continuation_records=records, since=self.cursor)
            )
        await self._apply(session, batch)

    async def flush_deferred_notes(self, session: ChatSession) -> None:
        """Persist deferred notes, then include every newer record."""
        notes = session.take_deferred_notes()
        if notes:
            await self.append(session, notes)
        else:
            await self.refresh(session)

    async def _apply(self, session: ChatSession, batch: SessionReadBatch | None) -> None:
        if batch is None:
            replacement = await self.load(session)
            self.messages = replacement.messages
            self.cursor = replacement.cursor
            self.active_lineage = replacement.active_lineage
            return
        self.messages.extend(batch.messages)
        self.active_lineage.extend(batch.active_messages)
        self.cursor = batch.cursor


@dataclass(frozen=True)
class _CompactionPromptRefresh:
    """Fresh prompt-only inputs prepared for one Compaction checkpoint commit."""

    agent_body: str
    project_prompt_context: ProjectPromptContext | None
    working_project_context: str | None
    soul_context: str | None
    memory_files_context: str | None
    skill_registry: SkillRegistry
    skill_catalog: PinnedSkillCatalog
    # The Project whose Skill pool the refreshed catalog advertises (qualifies its pin).
    skill_project_id: str | None
    prompt_read_paths: tuple[Path, ...]
    available_skill_names: tuple[str, ...] | None
    memory_prompt_mode: str | None = None


@dataclass(frozen=True)
class RequestBuildInputs:
    """One immutable bundle of everything that shapes a provider request build.

    Groups the Model-target-derived wire inputs and the pinned prompt-epoch
    state so call sites pass one value instead of thirteen field-by-field
    kwargs, and so a Compaction prompt refresh derives the next epoch's inputs
    in one step instead of re-fusing each field.
    """

    # Derived from the resolved model target serving this request.
    replay_policy: ReasoningReplayPolicy = DEFAULT_REASONING_REPLAY_POLICY
    reasoning_scope_model: str | None = None
    input_modalities: frozenset[str] | None = None
    wire_media_types: frozenset[str] = frozenset()
    max_image_bytes: int | None = None
    # Pinned prompt-epoch state; replaced wholesale by a Compaction refresh.
    agent_body: str = ""
    project_context: ProjectPromptContext | None = None
    working_project_context: str | None = None
    soul_context: str | None = None
    memory_files_context: str | None = None
    agent_project_id: str | None = None
    skill_registry: SkillRegistry | None = None
    skill_catalog: PinnedSkillCatalog | None = None
    # Request-only message source; ``None`` loads the Session transcript.
    session_messages_override: list[ChatMessage] | None = None
    image_budget: RequestImageBudget | None = None
    temporary_binding: TemporarySessionBinding | None = None

    @classmethod
    def from_context(
        cls,
        context: _RunExecutionContext,
        target: _ModelTarget,
    ) -> RequestBuildInputs:
        """Collect the context's pinned epoch plus one target's wire inputs."""
        return cls(
            replay_policy=target.replay_policy,
            reasoning_scope_model=target.model_reference,
            input_modalities=target.input_modalities,
            wire_media_types=target.wire_media_types,
            max_image_bytes=target.max_image_bytes,
            agent_body=context.agent_body,
            project_context=context.project_prompt_context,
            working_project_context=context.working_project_context,
            soul_context=context.soul_context,
            memory_files_context=context.memory_files_context,
            agent_project_id=context.project_id,
            skill_registry=context.skill_registry,
            skill_catalog=context.skill_catalog,
            image_budget=context.image_budget,
            temporary_binding=context.request.temporary_binding,
        )

    def with_session_messages(self, messages: list[ChatMessage]) -> RequestBuildInputs:
        """Return the same bundle reading its request messages from *messages*."""
        return replace(self, session_messages_override=messages)

    def merged_with_refresh(
        self,
        refresh: _CompactionPromptRefresh | None,
    ) -> RequestBuildInputs:
        """Apply one prepared Compaction prompt refresh, or return unchanged."""
        if refresh is None:
            return self
        return replace(
            self,
            agent_body=refresh.agent_body,
            project_context=refresh.project_prompt_context,
            working_project_context=refresh.working_project_context,
            soul_context=refresh.soul_context,
            memory_files_context=refresh.memory_files_context,
            skill_registry=refresh.skill_registry,
            skill_catalog=refresh.skill_catalog,
        )


class ReflectionNotifier(Protocol):
    """Run-end hook of the background reflection service.

    The chat loop only reports that a run ended; cadence policy, exclusions
    beyond the cheap inline gates, and the review itself live in
    ``core/automation/reflection.py``. The callable must be non-blocking —
    anything with I/O belongs in a task the service schedules itself.
    """

    def notify_run_end(self, run: Run, agent: Any, *, internal: bool, outcome: str) -> None:
        """Account one finished run (``outcome``: success/error/cancelled)."""
        ...


class SessionTitleNotifier(Protocol):
    """Non-blocking first-message hook for automatic Session titles."""

    def notify_user_message(
        self,
        *,
        agent_id: str,
        session_id: str,
        project_id: str | None,
        agent: Any,
        content: str | list[ContentBlock],
        run_id: str,
    ) -> None:
        """Persist the local title and optionally schedule its Model replacement."""
        ...


def _resolve_working_project(
    requests: RequestBuilder,
    dependencies: ChatLoopDependencies,
    working_project_id: str,
    resolve_cwd: bool,
) -> tuple[Path | None, Project | None]:
    """Read the Working Project's cwd and prompt Project. Blocking."""
    project_cwd = requests.resolve_project_cwd(working_project_id) if resolve_cwd else None
    return project_cwd, resolve_prompt_project(dependencies.projects, working_project_id)


async def create_run_execution_context(
    dependencies: ChatLoopDependencies,
    requests: RequestBuilder,
    run: Run,
    request: _RunRequest,
    *,
    session: ChatSession,
    session_snapshot: _SessionSnapshot | None = None,
    prior_continuation: ContinuationState | None,
    continuation_reminder: str | None,
    continuation_tracker: ContinuationTracker | None,
) -> _RunExecutionContext:
    """Resolve all stable execution inputs once at the Run boundary."""
    if session_snapshot is None:
        session_snapshot = await _SessionSnapshot.load(session)
    project_id = run.project_id
    working_project_id = run.working_project_id
    temporary_binding = request.temporary_binding
    temporary_source = temporary_binding or request.temporary_parent_binding
    project_cwd: Path | None
    if temporary_binding is not None:
        if temporary_binding.address != SessionAddress(
            project_id=project_id, agent_id=run.agent_id, session_id=run.session_id
        ):
            raise ChatError(
                "This Session no longer matches this Run. "
                "Ask the user to resume it through its Extension."
            )
        agent = await dependencies.agent_resolver.resolve_temporary_agent_async(
            temporary_binding.address, generation_id=temporary_binding.generation_id
        )
    elif request.temporary_parent_binding is not None:
        parent = request.temporary_parent_binding
        owner = run.execution_owner
        if (
            owner is None
            or owner.extension != parent.owner_name
            or owner.group_id != parent.group_id
            or owner.participant_id != parent.participant_id
            or owner.generation_id != parent.generation_id
            or run.agent_id != parent.address.agent_id
            or run.project_id != parent.address.project_id
        ):
            raise ChatError(
                "This Session no longer matches this Run. "
                "Ask the user to resume it through its Extension."
            )
        agent = await dependencies.agent_resolver.resolve_temporary_agent_async(
            parent.address,
            generation_id=parent.generation_id,
            run_overrides=request.agent_overrides,
        )
    elif request.agent_overrides is None:
        agent = await dependencies.agent_resolver.resolve_agent_async(project_id, run.agent_id)
    else:
        agent = await dependencies.agent_resolver.resolve_agent_async(
            project_id,
            run.agent_id,
            run_overrides=request.agent_overrides,
        )
    provider_id, connection_id = _resolve_agent_connection(dependencies, agent)
    _ensure_provider_exists(dependencies.providers, provider_id)
    _model_provider_id, model_id = _split_agent_model(agent.model)
    target = requests._create_model_target(
        provider_id, connection_id, model_id, public_model=agent.model
    )
    try:
        run.add_cancel_callback(lambda: _close_adapter(target.adapter))
        run.add_cancel_callback(lambda: dependencies.process_manager.cancel_scope_async(run.id))

        async def release_process_scope(_status: RunStatus) -> None:
            # Completion observers run after the executor, every Tool task, and
            # all cancellation callbacks have settled, so no Bash launch for this
            # Run can race the release of its closed-scope marker.
            dependencies.process_manager.release_scope(run.id)

        run.add_completion_observer(release_process_scope)
        temporary_cwd: Path | None = None
        if temporary_source is not None:
            temporary_cwd = getattr(agent, "cwd", None)
            if not isinstance(temporary_cwd, Path):
                raise ChatError(
                    "This Session has an invalid configuration. "
                    "Ask the user to check it through its Extension."
                )
        if working_project_id is None:
            project_cwd, prompt_project = None, None
        else:
            # The Working Project's anchor and repository are read off the Event Loop.
            project_cwd, prompt_project = await _CHAT_TRANSFORM_WORKERS.run(
                _resolve_working_project,
                requests,
                dependencies,
                working_project_id,
                temporary_source is None,
            )
        if temporary_cwd is not None:
            project_cwd = temporary_cwd
        project_prompt_context = (
            ProjectPromptContext.from_project(
                prompt_project.project_id,
                prompt_project.display_name,
                prompt_project.cwd,
                prompt_project.auto_load,
            )
            if prompt_project is not None
            else None
        )
        working_project_context = await _CHAT_TRANSFORM_WORKERS.run(
            pinned_working_project_context,
            dependencies,
            run.agent_id,
            run.session_id,
            prompt_project,
            project_prompt_context,
            project_id,
        )
        if temporary_source is None:
            soul_context = await _CHAT_TRANSFORM_WORKERS.run(
                pinned_soul_context,
                dependencies,
                run.agent_id,
                run.session_id,
                agent,
                project_id,
            )
            memory_files_context = await _CHAT_TRANSFORM_WORKERS.run(
                pinned_memory_files,
                dependencies,
                run.agent_id,
                run.session_id,
                agent,
                project_id,
            )
            skill_project_id, identity_agent_id = resolve_skill_scope(
                project_id, prompt_project, run.agent_id
            )
        else:
            soul_context = memory_files_context = None
            # A temporary participant has no identity-owned Skills, but an
            # explicitly selected Project still supplies its shared Skill pool.
            skill_project_id = working_project_id
            identity_agent_id = None
        skill_registry = await _CHAT_TRANSFORM_WORKERS.run(
            dependencies.resolve_skills,
            skill_project_id,
            identity_agent_id,
        )
        skill_catalog = await _CHAT_TRANSFORM_WORKERS.run(
            pinned_skill_catalog,
            dependencies,
            run.agent_id,
            run.session_id,
            agent,
            skill_registry,
            project_id,
            skill_project_id=skill_project_id,
        )
        prompt_cache_affinity_id = await _CHAT_TRANSFORM_WORKERS.run(
            dependencies.sessions.prompt_cache_affinity_id,
            SessionAddress(project_id=project_id, agent_id=run.agent_id, session_id=run.session_id),
        )
        session.activated_skill_contents(session_snapshot.active_messages)
        context = _RunExecutionContext(
            run=run,
            request=request,
            session=session,
            agent=agent,
            agent_body=runtime_agent_body(agent),
            primary_target=target,
            project_id=project_id,
            project_cwd=project_cwd,
            project_prompt_context=project_prompt_context,
            working_project_context=working_project_context,
            soul_context=soul_context,
            memory_files_context=memory_files_context,
            skill_project_id=skill_project_id,
            skill_registry=skill_registry,
            skill_catalog=skill_catalog,
            prompt_cache_affinity_id=prompt_cache_affinity_id,
            prior_continuation=prior_continuation,
            continuation_tracker=continuation_tracker,
            continuation_reminder=continuation_reminder,
            session_snapshot=session_snapshot,
        )
        if project_id is None and temporary_source is None:
            loaded_project_id = latest_project_tool_context_id(session_snapshot.active_messages)
            if loaded_project_id is not None:
                await requests._apply_project_skill_context(context, loaded_project_id)
        return context
    except BaseException:
        # Execution takes ownership only once the complete context is returned.
        await _close_adapter(target.adapter)
        raise
