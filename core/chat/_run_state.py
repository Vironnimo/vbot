"""Chat Run input, resolved state, and context preparation."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol

from core.chat._workers import _CHAT_TRANSFORM_WORKERS
from core.chat.content_blocks import ContentBlock
from core.chat.continuation import (
    ContinuationState,
    ContinuationTracker,
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
from core.runs import Run
from core.sessions import (
    ChatSession,
    SessionAddress,
    SessionReadCursor,
    TemporarySessionBinding,
    active_session_messages,
    editable_session_message_index,
    latest_project_tool_context_id,
)
from core.tools import ToolContract

if TYPE_CHECKING:
    from core.chat._request_builder import RequestBuilder
    from core.extensions import ExtensionRegistry
    from core.models.models import ModelRegistry
    from core.projects import AgentResolver, AgentRunOverrides, ProjectStore
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
    replay_reasoning: bool = True


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


@dataclass(frozen=True)
class _ModelTarget:
    """One resolved Provider target used for Model steps in a Run."""

    provider_id: str
    connection_id: str
    model_id: str
    model_reference: str
    adapter: ProviderAdapter
    replay_policy: ReasoningReplayPolicy
    input_modalities: frozenset[str]
    wire_media_types: frozenset[str]
    chunk_timeout_seconds: float | None


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


@dataclass
class _SessionSnapshot:
    """Run-local canonical Session state refreshed through append-only deltas."""

    messages: list[ChatMessage]
    cursor: SessionReadCursor
    pending_edit_message_id: str | None = None

    @property
    def active_messages(self) -> list[ChatMessage]:
        """Return current lineage, including an admitted edit not yet persisted."""
        active = active_session_messages(self.messages)
        if self.pending_edit_message_id is None:
            return active
        target_index = editable_session_message_index(active, self.pending_edit_message_id)
        return active[:target_index]

    def begin_edit(self, message_id: str) -> None:
        editable_session_message_index(active_session_messages(self.messages), message_id)
        self.pending_edit_message_id = message_id

    def commit_edit(self) -> None:
        self.pending_edit_message_id = None

    @classmethod
    async def load(cls, session: ChatSession) -> _SessionSnapshot:
        batch = await session.load_since_async()
        if batch is None:
            raise AssertionError("A full Session snapshot must always produce a cursor")
        return cls(messages=list(batch.messages), cursor=batch.cursor)

    async def refresh(self, session: ChatSession) -> None:
        batch = await session.load_since_async(self.cursor)
        if batch is None:
            replacement = await self.load(session)
            self.messages = replacement.messages
            self.cursor = replacement.cursor
            return
        self.messages.extend(batch.messages)
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
        agent = dependencies.agent_resolver.resolve_temporary_agent(
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
        agent = dependencies.agent_resolver.resolve_temporary_agent(
            parent.address,
            generation_id=parent.generation_id,
            run_overrides=request.agent_overrides,
        )
    elif request.agent_overrides is None:
        agent = dependencies.agent_resolver.resolve_agent(project_id, run.agent_id)
    else:
        agent = dependencies.agent_resolver.resolve_agent(
            project_id,
            run.agent_id,
            run_overrides=request.agent_overrides,
        )
    provider_id, connection_id = _resolve_agent_connection(dependencies, agent)
    _ensure_provider_exists(dependencies.providers, provider_id)
    _model_provider_id, model_id = _split_agent_model(agent.model)
    target = requests._create_model_target(provider_id, connection_id, model_id)
    run.add_cancel_callback(lambda: _close_adapter(target.adapter))
    run.add_cancel_callback(lambda: dependencies.process_manager.cancel_scope_async(run.id))
    if temporary_source is not None:
        temporary_cwd = getattr(agent, "cwd", None)
        if not isinstance(temporary_cwd, Path):
            raise ChatError(
                "This Session has an invalid configuration. "
                "Ask the user to check it through its Extension."
            )
        project_cwd = temporary_cwd
    else:
        project_cwd = requests.resolve_project_cwd(working_project_id)
    prompt_project = resolve_prompt_project(dependencies.projects, working_project_id)
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
