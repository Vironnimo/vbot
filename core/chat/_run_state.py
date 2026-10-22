"""Chat Run input, resolved state, and context preparation."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol

from core.chat._effort_epoch import EffortState
from core.chat._prompt_block_epoch import PromptBlockPin
from core.chat._run_permissions import (
    RunPermissions,
    permitted_project_context,
    run_permission_boundary,
)
from core.chat._step_outcomes import _ToolProgress
from core.chat._stream_draft import StreamDraft
from core.chat._tool_epoch import ToolChange, ToolEpochView
from core.chat._workers import _CHAT_TRANSFORM_WORKERS
from core.chat.content_blocks import ContentBlock
from core.chat.errors import ChatError
from core.chat.events import _close_adapter
from core.chat.messages import (
    ChatMessage,
    InputOrigin,
    JsonObject,
    MessageSender,
    ReplySurface,
)
from core.chat.recovery import RecoveryBudget
from core.chat.usage import RequestContextUsage
from core.chat.wire_shaping import RequestImageBudget
from core.permissions import PermissionBoundary
from core.projects import (
    FolderInstructions,
    resolve_prompt_project,
    resolve_skill_scope,
    runtime_agent_body,
)
from core.prompts import PinnedSkillCatalog, ProjectPromptContext
from core.prompts.pinned_context import (
    pinned_agent_body,
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
from core.secrets.agent_secrets import EMPTY_MASKER
from core.sessions import (
    SESSION_GOAL_META_KEY,
    SESSION_GOAL_OPEN_STATUSES,
    SUBAGENT_PARENT_META_KEY,
    SUBAGENT_REPLY_TARGET_META_KEY,
    ChatSession,
    PromptEpoch,
    SeenSkillsUpdate,
    SessionAddress,
    SessionReadBatch,
    SessionReadCursor,
    TemporarySessionBinding,
    ToolResultFacts,
    editable_session_message_index,
    latest_project_tool_context_id,
)
from core.tools import ToolContract
from core.utils.logging import get_logger

if TYPE_CHECKING:
    from core.chat._request_builder import RequestBuilder
    from core.extensions import ExtensionRegistry
    from core.models.models import ModelRegistry
    from core.projects import AgentResolver, Project, ProjectStore
    from core.prompts import SystemPromptManager
    from core.providers.adapter import ProviderAdapter
    from core.providers.providers import ProviderRegistry
    from core.runs import ChatRunManager
    from core.runtime.interfaces import ProviderCredentialResolverProtocol
    from core.secrets.agent_secrets import AgentSecrets, SecretMasker
    from core.sessions import ChatSessionManager
    from core.skills.skills import SkillRegistry
    from core.storage import StorageManager
    from core.tools.change_tracker import ChangeTracker
    from core.tools.command_manager import CommandManager
    from core.tools.file_state import FileReadState
    from core.tools.scratch_areas import ScratchAreas
    from core.tools.tools import ToolRegistry
    from core.usage import UsageRecorder

_LOGGER = get_logger("chat")


@dataclass(frozen=True)
class RequestState:
    """One Provider request of a Run and the Tool state its Tool Calls dispatch with.

    ``tools`` is the request's Tool list; ``allowed_tool_names`` and
    ``tool_contracts`` follow ``tool_epoch``, the pinned Tools plus the Tool
    changes announced to the Model (``None`` only for a state assembled without
    a Session request build). ``prompt_blocks`` holds the dynamic block texts
    the System Prompt shows. ``tool_loads`` holds, per On-demand Tool the
    Model may load now, the ``loaded`` change a ``load_tools`` call records:
    its definition as the Run's primary route shows it (``None`` until a Tool
    catalog was measured for this state). ``effort`` holds the prompt epoch's
    pinned thinking effort, which the System Prompt shows, and the effort the
    requests use now (``None`` only without a Session request build).
    """

    messages: list[JsonObject]
    tools: list[JsonObject]
    allowed_tool_names: tuple[str, ...]
    session_tool_grants: tuple[str, ...]
    tool_contracts: Mapping[str, ToolContract] = field(default_factory=dict)
    tool_epoch: ToolEpochView | None = None
    prompt_blocks: PromptBlockPin | None = None
    tool_loads: Mapping[str, ToolChange] | None = None
    effort: EffortState | None = None

    @property
    def loadable_tools(self) -> dict[str, JsonObject]:
        """The definitions a ``load_tools`` call may return now, by registry name."""
        if self.tool_epoch is None or not self.tool_loads:
            return {}
        return {
            name: change.definition
            for name in self.tool_epoch.loadable_names
            if (change := self.tool_loads.get(name)) is not None and change.definition is not None
        }

    @property
    def unloadable_tools(self) -> frozenset[str]:
        """The On-demand Tools the Model was told it can load that cannot be loaded now.

        They are not ready or not offered on the Run's primary route. Empty until
        a Tool catalog was measured for this state.
        """
        if self.tool_epoch is None or self.tool_loads is None:
            return frozenset()
        loadable = self.loadable_tools
        return frozenset(name for name in self.tool_epoch.loadable_names if name not in loadable)


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
    deliver_background_completions: Callable[[Run, ChatSession], Awaitable[ChatMessage | None]]
    # The shell's commands; created after the Chat Loop.
    get_command_manager: Callable[[], CommandManager | None]
    usage_recorder: UsageRecorder | None = None
    scratch_areas: ScratchAreas | None = None
    # Agent Secrets: masking of results, input and requests.
    secrets: AgentSecrets | None = None

    @property
    def secret_masker(self) -> SecretMasker:
        """Masks every known Secret value; empty without Agent Secrets."""
        return self.secrets.masker if self.secrets is not None else EMPTY_MASKER

    # Called when a persisted user input switches a Sub-Agent's answer destination.
    subagent_reply_to_user: Callable[[SessionAddress, str], None] | None = None
    # The vBot server's own port, which restricted Runs may not reach on loopback;
    # ``None`` outside a server.
    server_port: Callable[[], int | None] | None = None
    # Blocking: the further names and addresses that reach it on that port when it
    # listens beyond loopback (``BoundaryContext.own_hosts``).
    server_hosts: Callable[[], tuple[str, ...]] | None = None
    # Whether a Sub-Agent Session has a use for the ``subagent`` Tool (one at the
    # nesting limit has none); asked only for Sub-Agent Sessions. Blocking.
    # ``None`` offers the Tool to every Session whose Agent allows it.
    subagent_tool_usable: Callable[[SessionAddress], bool] | None = None


def is_subagent_session(sessions: ChatSessionManager, address: SessionAddress) -> bool:
    """Return whether *address* is linked to a Parent Agent. Blocking."""
    return isinstance(sessions.metadata_value(address, SUBAGENT_PARENT_META_KEY), dict)


def withholds_subagent_tool(dependencies: ChatLoopDependencies, address: SessionAddress) -> bool:
    """Return whether the Sub-Agent Session at *address* is not offered ``subagent``. Blocking."""
    usable = dependencies.subagent_tool_usable
    return usable is not None and not usable(address)


def has_open_goal(sessions: ChatSessionManager, address: SessionAddress) -> bool:
    """Return whether *address* has a goal that grants ``update_goal``. Blocking."""
    goal = sessions.metadata_value(address, SESSION_GOAL_META_KEY)
    return isinstance(goal, dict) and goal.get("status") in SESSION_GOAL_OPEN_STATUSES


def subagent_input_reply_target(context: _RunExecutionContext, request: _RunRequest) -> str | None:
    """Destination selected by this input; internal continuations keep the current one."""
    if not context.subagent_session or request.internal or request.input_already_persisted:
        return None
    return "parent" if request.parent_agent_input else "user"


async def switches_subagent_reply_to_user(
    context: _RunExecutionContext, target: str | None, sessions: ChatSessionManager
) -> bool:
    """Read under the Session write lock, before persisting the input and its destination."""
    return (
        target == "user"
        and await sessions.metadata_value_async(
            context.session.address, SUBAGENT_REPLY_TARGET_META_KEY
        )
        != "user"
    )


def report_subagent_user_input(
    dependencies: ChatLoopDependencies, address: SessionAddress, message_id: str
) -> None:
    """Tell the Parent after the input and its answer destination committed together."""
    if dependencies.subagent_reply_to_user is not None:
        try:
            dependencies.subagent_reply_to_user(address, message_id)
        except Exception:
            _LOGGER.warning(
                "Sub-Agent reply destination report failed for %s", address, exc_info=True
            )


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
    # Narrows the loop's dispatched Tool-iteration limit for this Run only.
    max_tool_iterations: int | None = None
    input_persisted_hook: Callable[[], None] | None = None
    edit_message_id: str | None = None
    temporary_binding: TemporarySessionBinding | None = None
    input_already_persisted: bool = False
    temporary_parent_binding: TemporarySessionBinding | None = None
    # Input the Parent Agent of this Sub-Agent Session wrote, not the user.
    parent_agent_input: bool = False
    # A note stored right before the input, such as what happened since the last Run.
    context_note: str | ChatMessage | None = None

    @property
    def supports_steering(self) -> bool:
        """Only ordinary user input can share the current execution policy."""
        return (
            not self.internal
            and self.sender is None
            and (self.reply_surface is None or self.reply_surface.kind in {"webui", "tui"})
            and self.tool_restriction is None
            and self.tool_denial_resolver is None
            and self.max_tool_iterations is None
            and self.input_persisted_hook is None
            and self.context_note is None
            and self.temporary_binding is None
            and self.temporary_parent_binding is None
            and not self.input_already_persisted
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
    # Documented image-count limit of one request on this route, if known.
    max_request_images: int | None = None
    # Whether the route returns calls to Tools outside the request's Tool list
    # (the Adapter's ``list_announced_tools`` is false).
    unlisted_tool_calls: bool = True
    # Whether the route serves On-demand Tools (the Adapter's ``on_demand_tools``);
    # on the primary route false lists every Tool a new Tool pin offers.
    on_demand_tools: bool = True
    # The Model's family from the Model catalog ("" when unknown); on the primary
    # route it decides the file edit Tools a new Tool pin offers.
    model_family: str = ""


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
    session_snapshot: _SessionSnapshot
    # The Session is linked to a Parent Agent (a Sub-Agent Session).
    subagent_session: bool = False
    # The Session leaves ``subagent`` out of its Tool list for this Run (a
    # Sub-Agent at the nesting limit).
    subagent_tool_withheld: bool = False
    # The Session has an open goal, which grants ``update_goal`` for this Run.
    goal_session: bool = False
    # Every Tool Call of this Run checks its accesses against this boundary.
    permissions: PermissionBoundary = PermissionBoundary.UNRESTRICTED
    # What ``permissions`` was built from; ``None`` keeps it for the whole Run.
    permission_sources: RunPermissions | None = None
    request_state: _RequestState | None = None
    # Finds the folder instruction files of the Working Project's subfolders;
    # ``None`` without a Working Project or with its folder instructions off.
    folder_instructions: FolderInstructions | None = None
    image_budget: RequestImageBudget = field(default_factory=RequestImageBudget)
    context_usage: RequestContextUsage = field(default_factory=RequestContextUsage)
    recovery: RecoveryBudget = field(default_factory=RecoveryBudget)
    tool_progress: _ToolProgress = field(default_factory=_ToolProgress)
    interruption_chain: list[ChatMessage] = field(default_factory=list)
    # perf_counter() at which the first step's request assembly began; the
    # progression consumes it for the ``chat.request_build`` measurement.
    request_build_started: float | None = None
    stream_draft: StreamDraft = field(init=False)

    def __post_init__(self) -> None:
        self.stream_draft = StreamDraft(self.session)


class _HistoryWrite(Protocol):
    def __call__(self, *, since: SessionReadCursor) -> Awaitable[SessionReadBatch | None]: ...


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

    async def refresh(self, session: ChatSession) -> list[ChatMessage] | None:
        """Advance to the Session's newest entry.

        Returns the active messages appended since the previous cursor, or
        ``None`` when the cursor could not continue (an edit or takeover) and
        the whole snapshot was replaced.
        """
        batch = await session.load_since_async(self.cursor)
        await self._apply(session, batch)
        return None if batch is None else list(batch.active_messages)

    async def append(
        self,
        session: ChatSession,
        messages: list[ChatMessage],
        *,
        seen_skills: SeenSkillsUpdate | None = None,
        tool_results: Mapping[str, ToolResultFacts] | None = None,
        subagent_reply_target: str | None = None,
    ) -> None:
        """Persist *messages* and advance past them in the same transaction.

        *tool_results* reports how each appended Tool Result's call ended.
        *seen_skills* commits in that transaction too, so a Skill is marked
        seen exactly when the note announcing it persists, and so does
        *subagent_reply_target*, the destination for the Sub-Agent's next answers.
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
                subagent_reply_target=subagent_reply_target,
            ),
        )

    async def apply_edit(
        self,
        session: ChatSession,
        messages: list[ChatMessage],
        *,
        seen_skills: SeenSkillsUpdate | None = None,
        subagent_reply_target: str | None = None,
    ) -> None:
        """Commit the admitted edit with its replacement *messages* in one transaction.

        *subagent_reply_target* commits in that transaction too. The snapshot is
        replaced by the Session's state after the edit.
        """
        target = self.pending_edit_message_id
        if target is None:
            raise ValueError("no history edit was admitted")
        batch = await session.apply_edit_async(
            target, messages, seen_skills=seen_skills, subagent_reply_target=subagent_reply_target
        )
        self.pending_edit_message_id = None
        self.messages = list(batch.messages)
        self.active_lineage = list(batch.active_messages)
        self.cursor = batch.cursor

    async def commit_checkpoint(
        self,
        session: ChatSession,
        checkpoint: ChatMessage,
        *,
        epoch: PromptEpoch,
    ) -> bool:
        """Persist a Compaction *checkpoint* and its prompt *epoch* while this snapshot is current.

        Advances past the checkpoint and returns ``True``, or returns ``False``
        without writing or advancing when another writer changed the Session
        since this snapshot's cursor.
        """
        committed = await session.commit_compaction_async(
            checkpoint, since=self.cursor, epoch=epoch
        )
        if committed is None:
            return False
        await self._apply(session, committed)
        return True

    async def commit(
        self,
        session: ChatSession,
        write: _HistoryWrite,
    ) -> None:
        """Run one history *write* of this Session and advance to its result.

        *write* returns every record after *since* from its own transaction,
        including other writers' appends, exactly as :meth:`refresh` would.
        """
        committed = await write(since=self.cursor)
        if committed is not None:
            session.acknowledge_deferred_notes(committed.messages)
        await self._apply(session, committed)

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
    available_skill_names: tuple[str, ...]
    memory_prompt_mode: str | None = None
    # Whether ``agent_body`` is a prompt-epoch pin (a Workspace-less Agent's body).
    pins_agent_body: bool = False


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
    max_request_images: int | None = None
    # The Run's primary route, whose gates decide the Tools a new Tool pin
    # offers, also while a fallback target serves this request. ``None`` uses
    # this request's own route.
    tool_route_input_modalities: frozenset[str] | None = None
    tool_route_wire_media_types: frozenset[str] | None = None
    tool_route_model_family: str | None = None
    tool_route_on_demand_tools: bool = True
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
    # The Session is linked to a Parent Agent: the System Prompt shows the
    # Sub-Agent role and the Session grants the Sub-Agent's own Tools.
    subagent_session: bool = False
    # The Session leaves ``subagent`` out of its Tool list (a Sub-Agent at the
    # nesting limit).
    subagent_tool_withheld: bool = False
    # The Session has an open goal and grants ``update_goal``.
    goal_session: bool = False
    # The Run's Permission Boundary; a restricted one shows the ``core:permissions``
    # block, pinned per prompt epoch like the temporary Session blocks.
    permissions: PermissionBoundary = PermissionBoundary.UNRESTRICTED
    # Start a new prompt epoch's Tool pin and dynamic block pin from the current
    # Tools and blocks instead of reading the Session's pins; the caller persists
    # them (Compaction commit).
    fresh_prompt_epoch: bool = False
    # List announced Tool additions in the request's Tool list: for a route
    # that drops calls to unlisted Tools.
    list_announced_tools: bool = False
    # List every Tool the Model may call, On-demand Tools included: for every
    # fallback route, so its Tool list never changes while it serves the Run.
    list_all_tools: bool = False
    # With ``fresh_prompt_epoch``: On-demand Tools the new Tool pin lists anyway
    # (those the Agent called in the epoch a Compaction ended).
    keep_listed_tools: frozenset[str] = frozenset()
    # With ``fresh_prompt_epoch``: the thinking effort the new epoch pins (the
    # effort the Run's requests use now); ``None`` pins the Agent's.
    fresh_thinking_effort: str | None = None

    @classmethod
    def from_context(
        cls,
        context: _RunExecutionContext,
        target: _ModelTarget,
    ) -> RequestBuildInputs:
        """Collect the context's pinned epoch plus one target's wire inputs.

        Tools are always measured on the Run's primary route, so a Compaction
        on a fallback target pins what the primary route offers.
        """
        return cls(
            replay_policy=target.replay_policy,
            reasoning_scope_model=target.model_reference,
            input_modalities=target.input_modalities,
            wire_media_types=target.wire_media_types,
            max_image_bytes=target.max_image_bytes,
            max_request_images=target.max_request_images,
            tool_route_input_modalities=context.primary_target.input_modalities,
            tool_route_wire_media_types=context.primary_target.wire_media_types,
            tool_route_model_family=context.primary_target.model_family,
            tool_route_on_demand_tools=context.primary_target.on_demand_tools,
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
            subagent_session=context.subagent_session,
            subagent_tool_withheld=context.subagent_tool_withheld,
            goal_session=context.goal_session,
            permissions=context.permissions,
            list_announced_tools=not target.unlisted_tool_calls,
            list_all_tools=target is not context.primary_target,
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


async def refresh_run_permissions(
    dependencies: ChatLoopDependencies, context: _RunExecutionContext, *, force: bool = False
) -> None:
    """Rebuild the Run's boundary when one of its rule sources changed, or on *force*.

    Called before each Tool round, so a rule edit applies to the next Tool
    Calls; the ``core:permissions`` block follows at the next Run as a
    ``[prompt-block-change]`` note. A rebuild that fails ends the Run.
    """
    current = context.permission_sources
    if current is None or not (force or await current.changed()):
        return
    agent = await resolve_run_agent(dependencies, context.run, context.request)
    rebuilt = await run_permission_boundary(
        dependencies,
        context.run,
        agent,
        address=current.address,
        project_root=current.project_root,
        skill_registry=context.skill_registry,
    )
    context.permission_sources = rebuilt
    context.permissions = rebuilt.boundary


async def resolve_run_agent(
    dependencies: ChatLoopDependencies, run: Run, request: _RunRequest
) -> Any:
    """Resolve the Agent as the Run's Session runs it now, overrides included.

    Raises :class:`ChatError` when a temporary Session no longer matches the Run.
    """
    project_id = run.project_id
    temporary_binding = request.temporary_binding
    if temporary_binding is not None:
        if temporary_binding.address != SessionAddress(
            project_id=project_id, agent_id=run.agent_id, session_id=run.session_id
        ):
            raise ChatError(
                "This Session no longer matches this Run. "
                "Ask the user to resume it through its Extension."
            )
        return await dependencies.agent_resolver.resolve_temporary_agent_async(
            temporary_binding.address,
            generation_id=temporary_binding.generation_id,
            session=temporary_binding.address,
        )
    if request.temporary_parent_binding is not None:
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
        return await dependencies.agent_resolver.resolve_temporary_agent_async(
            parent.address,
            generation_id=parent.generation_id,
            session=SessionAddress(
                project_id=project_id, agent_id=run.agent_id, session_id=run.session_id
            ),
        )
    return await dependencies.agent_resolver.resolve_agent_async(
        project_id, run.agent_id, session_id=run.session_id
    )


async def create_run_execution_context(
    dependencies: ChatLoopDependencies,
    requests: RequestBuilder,
    run: Run,
    request: _RunRequest,
    *,
    session: ChatSession,
    session_snapshot: _SessionSnapshot | None = None,
) -> _RunExecutionContext:
    """Resolve all stable execution inputs once at the Run boundary."""
    if session_snapshot is None:
        session_snapshot = await _SessionSnapshot.load(session)
    project_id = run.project_id
    working_project_id = run.working_project_id
    temporary_binding = request.temporary_binding
    temporary_source = temporary_binding or request.temporary_parent_binding
    project_cwd: Path | None
    agent = await resolve_run_agent(dependencies, run, request)
    target = await requests.targets.for_agent(agent)
    try:
        run.add_cancel_callback(lambda: _close_adapter(target.adapter))
        commands = dependencies.get_command_manager()
        if commands is not None:
            # A cancelled Run stops the shell commands still inside their call;
            # commands already in the background keep running and deliver.
            run.add_cancel_callback(lambda: commands.cancel_run(run.id))

            async def release_command_scope(_status: RunStatus) -> None:
                # Completion observers run after the executor, every Tool task, and
                # all cancellation callbacks have settled, so no shell command for
                # this Run can race the release of its cancellation marker.
                commands.release_run(run.id)

            run.add_completion_observer(release_command_scope)
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
        folder_instructions = (
            await _CHAT_TRANSFORM_WORKERS.run(prompt_project.folder_instructions_finder)
            if prompt_project is not None
            else None
        )
        agent_body = await _CHAT_TRANSFORM_WORKERS.run(
            pinned_agent_body,
            dependencies,
            run.agent_id,
            run.session_id,
            agent,
            project_id,
            runtime_agent_body(agent),
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
                project_id, prompt_project, agent
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
        session_address = SessionAddress(
            project_id=project_id, agent_id=run.agent_id, session_id=run.session_id
        )
        prompt_cache_affinity_id = await _CHAT_TRANSFORM_WORKERS.run(
            dependencies.sessions.prompt_cache_affinity_id, session_address
        )
        subagent_session = await _CHAT_TRANSFORM_WORKERS.run(
            is_subagent_session, dependencies.sessions, session_address
        )
        subagent_tool_withheld = subagent_session and await _CHAT_TRANSFORM_WORKERS.run(
            withholds_subagent_tool, dependencies, session_address
        )
        goal_session = await _CHAT_TRANSFORM_WORKERS.run(
            has_open_goal, dependencies.sessions, session_address
        )
        permissions = await run_permission_boundary(
            dependencies,
            run,
            agent,
            address=session_address,
            project_root=Path(prompt_project.cwd) if prompt_project is not None else None,
            skill_registry=skill_registry,
        )
        # The frame carries only the instruction files the Run may read.
        project_prompt_context = (
            await _CHAT_TRANSFORM_WORKERS.run(
                permitted_project_context, prompt_project, permissions.boundary
            )
            if prompt_project is not None
            else None
        )
        working_project_context = await _CHAT_TRANSFORM_WORKERS.run(
            pinned_working_project_context,
            dependencies,
            run.agent_id,
            run.session_id,
            agent,
            prompt_project,
            project_prompt_context,
            project_id,
        )
        session.activated_skill_contents(session_snapshot.active_messages)
        context = _RunExecutionContext(
            run=run,
            request=request,
            session=session,
            agent=agent,
            agent_body=agent_body,
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
            session_snapshot=session_snapshot,
            subagent_session=subagent_session,
            subagent_tool_withheld=subagent_tool_withheld,
            goal_session=goal_session,
            permissions=permissions.boundary,
            permission_sources=permissions,
            folder_instructions=folder_instructions,
            # The Session's newest measurement still anchors this Run's Context.
            context_usage=RequestContextUsage.resume(
                session_snapshot.active_messages, dependencies.usage_recorder
            ),
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
