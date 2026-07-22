"""Chat message primitives and chat loop execution."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol, cast

from core.chat.content_blocks import ContentBlock, MediaBlock, content_block_to_dict
from core.chat.continuation import (
    ContinuationCause,
    ContinuationState,
    ContinuationTracker,
    fold_continuation_records,
    inject_continuation_reminder,
    normalize_interruption_cause,
    recover_continuation,
    render_continuation_reminder,
)
from core.chat.errors import ChatError, ChatSessionError, ToolIterationLimitError
from core.chat.events import (
    _close_adapter,
    _emit_assistant_events,
    _emit_message_event,
    _emit_streaming_assistant_events,
    _persist_run_error,
    _timing_payload,
)
from core.chat.events import (
    _exception_to_error_kind as _exception_to_error_kind,
)
from core.chat.messages import (
    ERROR_KIND_AUTH as ERROR_KIND_AUTH,
)
from core.chat.messages import (
    ERROR_KIND_CONFIG as ERROR_KIND_CONFIG,
)
from core.chat.messages import (
    ERROR_KIND_NETWORK as ERROR_KIND_NETWORK,
)
from core.chat.messages import (
    ERROR_KIND_PROVIDER_ERROR as ERROR_KIND_PROVIDER_ERROR,
)
from core.chat.messages import (
    ERROR_KIND_PROVIDER_FATAL as ERROR_KIND_PROVIDER_FATAL,
)
from core.chat.messages import (
    ERROR_KIND_PROVIDER_OVERLOAD as ERROR_KIND_PROVIDER_OVERLOAD,
)
from core.chat.messages import (
    ERROR_KIND_RATE_LIMIT as ERROR_KIND_RATE_LIMIT,
)
from core.chat.messages import (
    ERROR_KIND_TIMEOUT as ERROR_KIND_TIMEOUT,
)
from core.chat.messages import (
    ERROR_KIND_TOOL_ITERATIONS as ERROR_KIND_TOOL_ITERATIONS,
)
from core.chat.messages import (
    INPUT_ORIGIN_SPEECH_TRANSCRIPTION as INPUT_ORIGIN_SPEECH_TRANSCRIPTION,
)
from core.chat.messages import (
    ChatMessage,
    JsonObject,
    ReplySurface,
    _append_input_origin_note,
    _append_reply_surface_note,
    _apply_usage_estimation,
    _assistant_continuation_dict,
    _assistant_message_from_response,
    _display_content_preview,
    _effective_compaction_messages,
    _embed_notes_into_request,
    _last_user_message,
    _last_user_message_with_content_blocks,
    _latest_compaction_checkpoint,
    _message_to_request_dict,
    _notes_to_request_messages,
    _restore_in_run_assistant_reasoning,
    _session_has_any_content_blocks,
    _strip_assistant_reasoning_fields,
    checkpoint_ordinal,
    finalize_checkpoint_history_guidance,
    history_available,
)
from core.chat.messages import (
    InputOrigin as InputOrigin,
)
from core.chat.messages import (
    MessageSender as MessageSender,
)
from core.chat.messages import (
    ToolCall as ToolCall,
)
from core.chat.messages import (
    _validate_assistant_message as _validate_assistant_message,
)
from core.chat.messages import (
    error_kind_llm_visible as error_kind_llm_visible,
)
from core.chat.model_resolution import (
    _ensure_provider_exists,
    _first_usable_connection_id,
    _model_connection_allowlist,
    _model_input_modalities,
    _resolve_agent_connection,
    _resolve_fallback,
    _split_agent_model,
)
from core.chat.model_resolution import (
    parse_bare_model as parse_bare_model,
)
from core.chat.model_resolution import (
    parse_model_with_connection as parse_model_with_connection,
)
from core.chat.streaming import (
    STREAM_CHUNK_TIMEOUT_SECONDS,
    StreamingAccumulator,
    StreamingChunkTimeoutError,
    StreamRecoveryAction,
    decide_stream_recovery,
    is_local_provider_base_url,
    iter_with_chunk_timeout,
)
from core.chat.streaming import (
    _is_model_fallback_trigger as _is_model_fallback_trigger,
)
from core.chat.tool_dispatch import (
    ToolDispatchContext,
    _activate_triggered_skills,
    _dispatch_tool_calls,
)
from core.chat.usage import add_session_turn_usage, aggregate_session_usage
from core.debug import DebugContext
from core.extensions import HookContext
from core.projects import (
    resolve_prompt_project,
    resolve_skill_scope,
    resolve_working_project_id,
    runtime_agent_body,
)
from core.prompts import PinnedSkillCatalog, ProjectPromptContext
from core.providers.errors import NetworkError
from core.providers.providers import resolve_effective_context_window
from core.providers.reasoning import REASONING_REPLAY_CURRENT_RUN, ReasoningReplayPolicy
from core.runs import (
    COMPACTION_COMPLETED_EVENT,
    MODEL_FALLBACK_ACTIVATED_EVENT,
    MODEL_STEP_USAGE_EVENT,
    USER_MESSAGE_EVENT,
    QueuedRunItem,
    Run,
    RunExecutor,
    WaitingWorkAdmission,
)
from core.sessions import SKILL_AVAILABLE_NOTE_PREFIX, ChatSession, skill_activation_names
from core.tools import HISTORY_TOOL_NAME
from core.utils.errors import ConfigError, ProviderError, VBotError
from core.utils.logging import get_logger

if TYPE_CHECKING:
    from core.chat.block_resolver import ContentBlockResolver
    from core.compaction import CompactionService, CompactionSettings
    from core.extensions import ExtensionRegistry
    from core.models.models import ModelRegistry
    from core.projects import AgentResolver, ProjectStore
    from core.prompts import SystemPromptManager
    from core.providers.adapter import ProviderAdapter
    from core.providers.providers import ProviderRegistry
    from core.runs import ChatRunManager
    from core.runtime.interfaces import ProviderCredentialResolverProtocol
    from core.sessions import ChatSessionManager
    from core.skills.skills import SkillRegistry
    from core.storage import StorageManager
    from core.tools.file_state import FileReadState
    from core.tools.process_manager import ProcessManager
    from core.tools.tools import ToolRegistry

_LOGGER = get_logger("chat")

MAX_TOOL_ITERATIONS = 1000

# Session-pinned skill catalog snapshot (the rendered ``<available_skills>`` text),
# stored in session metadata so a mid-session skill write never shifts the session's
# system prompt (the prompt-cache invariant).
PINNED_SKILL_CATALOG_META_KEY = "pinned_skill_catalog"


@dataclass(frozen=True)
class _RequestState:
    messages: list[JsonObject]
    tools: list[JsonObject]
    allowed_tool_names: tuple[str, ...]
    session_tool_grants: tuple[str, ...]


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
    storage: StorageManager
    get_extension_registry: Callable[[], ExtensionRegistry | None]
    get_system_prompts: Callable[[], SystemPromptManager]
    get_adapter: Callable[[str, str], ProviderAdapter]
    resolve_skills: Callable[[str | None, str | None], SkillRegistry]
    get_local_context_windows: Callable[[], Mapping[str, Any]]


@dataclass(frozen=True)
class _RunRequest:
    """Immutable input captured by one admitted Run executor."""

    content: str | list[ContentBlock] | None
    internal: bool = False
    input_origin: InputOrigin | None = None
    sender: MessageSender | None = None
    reply_surface: ReplySurface | None = None
    tool_restriction: tuple[str, ...] | None = None
    input_persisted_hook: Callable[[], None] | None = None


@dataclass(frozen=True)
class _ModelTarget:
    """One resolved Provider target used for Model steps in a Run."""

    provider_id: str
    connection_id: str
    model_id: str
    adapter: ProviderAdapter
    replay_policy: ReasoningReplayPolicy
    wire_media_types: frozenset[str]
    chunk_timeout_seconds: float | None


@dataclass
class _RunExecutionContext:
    """Resolved, Run-local state shared by progression, Tools, and Compaction."""

    run: Run
    request: _RunRequest
    session: ChatSession
    agent: Any
    primary_target: _ModelTarget
    project_id: str | None
    project_cwd: Path | None
    project_prompt_context: ProjectPromptContext | None
    skill_project_id: str | None
    skill_registry: SkillRegistry
    skill_catalog: PinnedSkillCatalog
    prior_continuation: ContinuationState | None
    continuation_tracker: ContinuationTracker | None
    continuation_reminder: str | None
    request_state: _RequestState | None = None


# Skill names the session has already surfaced to the model: the pinned catalog at
# first build, plus any later additions already announced. Diffed each run against the
# agent's currently available+allowed skills so a newly available one is announced once.
SEEN_SKILLS_META_KEY = "seen_skills"
# Header of the mid-session "new skills available" reminder. The pinned prompt catalog
# cannot grow without breaking the prompt cache, so additions reach the model here.
SKILL_AVAILABLE_NEW_SKILLS_HEADER = (
    "New skills are now available to you. Load one by name with the `skill` tool when relevant:"
)

# How often a streaming attempt may be restarted from scratch after a transient
# drop that occurred before any visible output. Each restart re-issues the whole
# request (the adapter's own connect-level retry still applies per attempt), so
# this bounds only the post-connect mid-stream replays.
MAX_STREAM_RESTARTS = 2


class _StreamRestartNeeded(Exception):  # noqa: N818 — control-flow signal, not an error
    """Internal signal: a streaming attempt dropped before any visible output.

    Raised by ``_consume_stream_attempt`` and caught by
    ``_send_streaming_assistant_request`` to replay the stream. It never escapes
    the chat loop — the final attempt cannot restart and re-raises the real
    error instead.
    """

    def __init__(self, cause: Exception) -> None:
        super().__init__(str(cause))
        self.cause = cause


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


def _resolve_reasoning_replay_policy(adapter: Any, model_id: str) -> ReasoningReplayPolicy:
    """Resolve the adapter's reasoning replay policy for one request build.

    Mirrors the ``set_debug_context`` probe: adapters and test doubles that do
    not expose the hook get the historical ``current_run`` shaping.
    """
    if hasattr(adapter, "reasoning_replay_policy"):
        return cast(ReasoningReplayPolicy, adapter.reasoning_replay_policy(model_id))
    return REASONING_REPLAY_CURRENT_RUN


def _resolve_wire_media_support(adapter: Any, model_id: str) -> frozenset[str]:
    """Resolve the media types the adapter's wire can carry for one request build.

    Mirrors ``_resolve_reasoning_replay_policy``: adapters and test doubles that
    do not expose the hook carry nothing, so the resolver degrades every
    attachment rather than emitting media the wire cannot encode.
    """
    if hasattr(adapter, "wire_media_support"):
        return frozenset(adapter.wire_media_support(model_id))
    return frozenset()


def _resolve_request_context_kwargs(adapter: Any, run: Run) -> dict[str, Any]:
    """Resolve per-request conversation-context kwargs for one request build.

    Mirrors ``_resolve_reasoning_replay_policy``: adapters and test doubles that
    do not expose the hook contribute nothing, so the provider call is unchanged
    for every adapter that has no use for the conversation identity.
    """
    if hasattr(adapter, "request_context_kwargs"):
        return dict(
            adapter.request_context_kwargs(
                agent_id=run.agent_id,
                session_id=run.session_id,
                project_id=run.project_id,
            )
        )
    return {}


def _connection_local_id(provider_id: str, connection_id: str) -> str | None:
    """Extract the provider-local connection id from a ``<provider>:<conn>[:<account>]`` id.

    Returns ``None`` when *connection_id* does not carry the expected provider
    prefix, so callers fall back to the provider-level base URL.
    """
    prefix = f"{provider_id}:"
    if not connection_id.startswith(prefix):
        return None
    remainder = connection_id[len(prefix) :]
    return remainder.split(":", 1)[0] or None


def _usage_token_count(usage: Any, key: str) -> int:
    """Return one non-negative token count from a usage payload, else 0."""
    if not isinstance(usage, dict):
        return 0
    value = usage.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


def _read_media_text_note(filename: str, media_type: str) -> JsonObject:
    """Plain-text fallback when a read-media image cannot be shown to the model."""
    return {
        "role": "user",
        "content": (
            f"[Loaded media {filename} ({media_type}) from disk, but it cannot be "
            "shown to this model directly.]"
        ),
    }


def _serialize_continuation_request(
    content: str | list[ContentBlock] | None,
) -> str | list[JsonObject] | None:
    """Return the canonical JSON form stored by the continuation journal."""
    if isinstance(content, list):
        return [content_block_to_dict(block) for block in content]
    return content


class ChatLoop:
    """Minimal agentic chat loop."""

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
        self._nesting_depth = 0

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
        child._nesting_depth = nesting_depth
        return child

    def run_executor(
        self,
        content: str | list[ContentBlock],
        *,
        reply_surface: ReplySurface | None = None,
    ) -> RunExecutor:
        """Return a run-manager executor that runs *content* through this loop.

        The run's project anchor rides ``run.project_id`` (set by the run manager
        from the ``project_id`` passed to ``start``/``enqueue``), not this
        closure: an identity run keeps ``run.project_id is None`` and today's
        behavior; a project run executes project-scoped (session under the
        project anchor, tool cwd = repo). The public way for other domains
        (sub-agents) to hand the run manager an executor.
        """
        request = _RunRequest(content=content, reply_surface=reply_surface)
        return lambda run: self._execute_run(run, request)

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
        input_persisted_hook: Callable[[], None] | None = None,
        contributes_to_agent_activity: bool = True,
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
            input_persisted_hook=input_persisted_hook,
            contributes_to_agent_activity=contributes_to_agent_activity,
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
        input_persisted_hook: Callable[[], None] | None = None,
        contributes_to_agent_activity: bool = True,
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
            input_persisted_hook=input_persisted_hook,
            contributes_to_agent_activity=contributes_to_agent_activity,
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
        waiting_work_admission: WaitingWorkAdmission | None = None,
        input_persisted_hook: Callable[[], None] | None = None,
        contributes_to_agent_activity: bool = True,
    ) -> QueuedRunItem:
        """Queue one chat run for a busy session or start it immediately when idle.

        ``project_id`` scopes the session/run to a project anchor; ``None`` keeps
        today's identity behavior.
        """
        agent = self._dependencies.agent_resolver.resolve_agent(project_id, agent_id)
        working_project_id = resolve_working_project_id(project_id, agent)
        provider_id, _connection_id = _resolve_agent_connection(self._dependencies, agent)
        _ensure_provider_exists(self._dependencies.providers, provider_id)
        session = self._get_session(
            agent_id, session_id, create_missing=False, project_id=project_id
        )
        manager = self._dependencies.run_manager
        request = _RunRequest(
            content=content,
            internal=internal,
            input_origin=input_origin,
            sender=sender,
            reply_surface=reply_surface,
            input_persisted_hook=input_persisted_hook,
        )
        return await manager.enqueue(
            agent_id=agent_id,
            session_id=session.id,
            executor=lambda run: self._execute_run(run, request),
            display_content=_display_content_preview(content),
            internal=internal,
            project_id=project_id,
            working_project_id=working_project_id,
            waiting_work_admission=waiting_work_admission,
            contributes_to_agent_activity=contributes_to_agent_activity,
        )

    def build_queue_update(
        self,
        agent_id: str,
        session_id: str,
        content: str | list[ContentBlock],
        input_origin: InputOrigin | None = None,
        reply_surface: ReplySurface | None = None,
        project_id: str | None = None,
    ) -> tuple[str, RunExecutor, str]:
        """Build replacement data for a queued run without mutating queue state."""
        agent = self._dependencies.agent_resolver.resolve_agent(project_id, agent_id)
        provider_id, _connection_id = _resolve_agent_connection(self._dependencies, agent)
        _ensure_provider_exists(self._dependencies.providers, provider_id)
        session = self._get_session(
            agent_id, session_id, create_missing=False, project_id=project_id
        )
        return (
            session.id,
            lambda run: self._execute_run(
                run,
                _RunRequest(
                    content=content,
                    input_origin=input_origin,
                    reply_surface=reply_surface,
                ),
            ),
            _display_content_preview(content),
        )

    async def compact_session(
        self,
        agent_id: str,
        session_id: str,
        instruction: str | None = None,
        *,
        project_id: str | None = None,
    ) -> str:
        """Manually compact a session and return a user-facing command reply.

        Refuses while a run is active for the session. On success one
        compaction checkpoint is appended to the session; failures inside
        the compaction itself are converted into a reply string instead of
        raising, matching the `/compact` command contract. ``instruction`` is the
        optional free-text argument from `/compact <instruction>` and is woven
        into the summarization prompt. ``project_id`` scopes the agent and session
        to a project anchor (``None`` = the identity agent and its session).
        """
        if self._compaction_service is None:
            return "Compaction is not available."

        manager = self._dependencies.run_manager
        if (
            manager.active_run(agent_id=agent_id, session_id=session_id, project_id=project_id)
            is not None
        ):
            return "Cannot compact while a run is active for this session."

        # Resolve the agent and load the session in the caller's scope: a project
        # chat compacts its project session and the project agent, an identity chat
        # (``project_id=None``) the identity session — both through the one
        # resolver/session seam.
        agent = self._dependencies.agent_resolver.resolve_agent(project_id, agent_id)
        working_project_id = resolve_working_project_id(project_id, agent)
        session = self._get_session(
            agent_id, session_id, create_missing=False, project_id=project_id
        )
        messages = session.load()
        settings = self._load_compaction_settings(
            agent, agent_id=agent_id, session_id=session_id, project_id=project_id
        )

        adapter: Any | None = None
        summary_adapter: Any | None = None
        try:
            self._resolve_project_cwd(working_project_id)
            provider_id, connection_id = _resolve_agent_connection(self._dependencies, agent)
            adapter = self._dependencies.get_adapter(provider_id, connection_id)
            _model_provider_id, model_id = _split_agent_model(agent.model)
            summary_adapter, summary_model_id = self._resolve_summary_adapter(
                agent,
                adapter,
                model_id,
                settings,
            )
            prompt_project = resolve_prompt_project(self._dependencies.projects, working_project_id)
            prompt_context = (
                ProjectPromptContext.from_project(prompt_project.cwd, prompt_project.auto_load)
                if prompt_project is not None
                else None
            )
            skill_project_id, identity_agent_id = resolve_skill_scope(
                project_id, prompt_project, agent_id
            )
            skill_registry = self._dependencies.resolve_skills(skill_project_id, identity_agent_id)
            request_state = await self._build_request_state(
                agent,
                session,
                replay_policy=_resolve_reasoning_replay_policy(adapter, model_id),
                wire_media_types=_resolve_wire_media_support(adapter, model_id),
                agent_body=runtime_agent_body(agent),
                project_context=prompt_context,
                agent_project_id=project_id,
                skill_registry=skill_registry,
                skill_catalog=self._pinned_skill_catalog(
                    agent_id, session_id, agent, skill_registry, project_id
                ),
            )
            checkpoint = await self._compaction_service.compact(
                messages,
                agent=agent,
                summary_adapter=summary_adapter,
                summary_model_id=summary_model_id,
                storage=self._dependencies.storage,
                settings=settings,
                instruction=instruction,
                request_messages=request_state.messages,
                active_adapter=adapter,
                active_model_id=model_id,
                active_tools=request_state.tools,
            )
            ordinal = (
                len([message for message in messages if message.role == "compaction_checkpoint"])
                + 1
            )
            checkpoint = finalize_checkpoint_history_guidance(checkpoint, ordinal=ordinal)
            session.append(checkpoint)
        except Exception as exc:
            return f"Compaction failed: {exc}"
        finally:
            if adapter is not None:
                await _close_adapter(adapter)
            if summary_adapter is not None and summary_adapter is not adapter:
                await _close_adapter(summary_adapter)

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
        input_persisted_hook: Callable[[], None] | None = None,
        contributes_to_agent_activity: bool = True,
    ) -> Run:
        agent = self._dependencies.agent_resolver.resolve_agent(project_id, agent_id)
        working_project_id = resolve_working_project_id(project_id, agent)
        provider_id, _connection_id = _resolve_agent_connection(self._dependencies, agent)
        _ensure_provider_exists(self._dependencies.providers, provider_id)
        session = self._get_session(
            agent_id, session_id, create_missing=create_missing, project_id=project_id
        )
        manager = self._dependencies.run_manager
        request = _RunRequest(
            content=content,
            internal=internal,
            input_origin=input_origin,
            sender=sender,
            reply_surface=reply_surface,
            tool_restriction=(tuple(tool_restriction) if tool_restriction is not None else None),
            input_persisted_hook=input_persisted_hook,
        )
        return await manager.start(
            agent_id=agent_id,
            session_id=session.id,
            executor=lambda run: self._execute_run(run, request),
            project_id=project_id,
            working_project_id=working_project_id,
            contributes_to_agent_activity=contributes_to_agent_activity,
        )

    async def _execute_run(
        self,
        run: Run,
        request: _RunRequest,
    ) -> ChatMessage:
        project_id = run.project_id
        session = self._dependencies.sessions.get(
            run.agent_id,
            run.session_id,
            project_id,
        )
        prior_continuation: ContinuationState | None = None
        continuation_reminder: str | None = None
        continuation_tracker: ContinuationTracker | None = None
        if not request.internal:
            prior_continuation = recover_continuation(session, active_run_id=run.id)
            if prior_continuation is not None and not prior_continuation.active:
                continuation_reminder = render_continuation_reminder(
                    prior_continuation,
                    context_window=None,
                )
            continuation_tracker = ContinuationTracker(
                session,
                run_id=run.id,
                request=_serialize_continuation_request(request.content),
                prior_state=prior_continuation,
            )
        try:
            context = self._create_run_execution_context(
                run,
                request,
                session=session,
                prior_continuation=prior_continuation,
                continuation_reminder=continuation_reminder,
                continuation_tracker=continuation_tracker,
            )
            return await self._execute_run_impl(context)
        except BaseException as exc:
            if continuation_tracker is not None and not continuation_tracker.closed:
                cause: ContinuationCause = (
                    "user"
                    if run.cancel_requested and run.cancel_reason == "user"
                    else normalize_interruption_cause(exc)
                )
                await continuation_tracker.interrupt(cause)
            raise

    def _create_run_execution_context(
        self,
        run: Run,
        request: _RunRequest,
        *,
        session: ChatSession,
        prior_continuation: ContinuationState | None,
        continuation_reminder: str | None,
        continuation_tracker: ContinuationTracker | None,
    ) -> _RunExecutionContext:
        """Resolve all stable execution inputs once at the Run boundary."""
        project_id = run.project_id
        working_project_id = run.working_project_id
        agent = self._dependencies.agent_resolver.resolve_agent(project_id, run.agent_id)
        provider_id, connection_id = _resolve_agent_connection(self._dependencies, agent)
        _ensure_provider_exists(self._dependencies.providers, provider_id)
        _model_provider_id, model_id = _split_agent_model(agent.model)
        target = self._create_model_target(provider_id, connection_id, model_id)
        run.add_cancel_callback(lambda: _close_adapter(target.adapter))
        run.add_cancel_callback(lambda: self._dependencies.process_manager.cancel_scope(run.id))
        project_cwd = self._resolve_project_cwd(working_project_id)
        prompt_project = resolve_prompt_project(self._dependencies.projects, working_project_id)
        project_prompt_context = (
            ProjectPromptContext.from_project(prompt_project.cwd, prompt_project.auto_load)
            if prompt_project is not None
            else None
        )
        skill_project_id, identity_agent_id = resolve_skill_scope(
            project_id, prompt_project, run.agent_id
        )
        skill_registry = self._dependencies.resolve_skills(skill_project_id, identity_agent_id)
        skill_catalog = self._pinned_skill_catalog(
            run.agent_id,
            run.session_id,
            agent,
            skill_registry,
            project_id,
        )
        return _RunExecutionContext(
            run=run,
            request=request,
            session=session,
            agent=agent,
            primary_target=target,
            project_id=project_id,
            project_cwd=project_cwd,
            project_prompt_context=project_prompt_context,
            skill_project_id=skill_project_id,
            skill_registry=skill_registry,
            skill_catalog=skill_catalog,
            prior_continuation=prior_continuation,
            continuation_tracker=continuation_tracker,
            continuation_reminder=continuation_reminder,
        )

    def _create_model_target(
        self,
        provider_id: str,
        connection_id: str,
        model_id: str,
    ) -> _ModelTarget:
        adapter = self._dependencies.get_adapter(provider_id, connection_id)
        return _ModelTarget(
            provider_id=provider_id,
            connection_id=connection_id,
            model_id=model_id,
            adapter=adapter,
            replay_policy=_resolve_reasoning_replay_policy(adapter, model_id),
            wire_media_types=_resolve_wire_media_support(adapter, model_id),
            chunk_timeout_seconds=self._resolve_chunk_timeout(provider_id, connection_id),
        )

    async def _execute_run_impl(
        self,
        context: _RunExecutionContext,
    ) -> ChatMessage:
        run = context.run
        request = context.request
        session = context.session
        agent = context.agent
        target = context.primary_target
        project_id = context.project_id
        internal = request.internal
        run_timing_started_at = datetime.now(UTC)
        run_timing_started_perf = time.perf_counter()
        _run_succeeded = True
        run_error: BaseException | None = None
        completed_assistant: ChatMessage | None = None
        start_line_extras = ""
        if project_id is not None:
            start_line_extras += f" project={project_id}"
        if internal:
            start_line_extras += " internal"
        _LOGGER.info(
            "Run %s started (agent=%s session=%s model=%s connection=%s%s)",
            run.id,
            run.agent_id,
            run.session_id,
            agent.model,
            target.connection_id,
            start_line_extras,
        )

        try:
            extension_registry = self._dependencies.get_extension_registry()
            if extension_registry is not None:
                extension_ctx = HookContext(
                    session_id=run.session_id,
                    agent_id=run.agent_id,
                    run_id=run.id,
                    add_note=session.add_note,
                )
                await extension_registry.dispatch_run_start(
                    extension_ctx,
                    session_id=run.session_id,
                    agent_id=run.agent_id,
                )

            run.raise_if_cancelled()
            self._announce_newly_available_skills(
                run.agent_id,
                run.session_id,
                session,
                agent,
                context.skill_registry,
                project_id,
            )
            async with self._dependencies.sessions.write_lock(
                run.agent_id, run.session_id, project_id
            ):
                if internal:
                    if not isinstance(request.content, str):
                        raise ChatError("internal runs require string content")
                    _append_reply_surface_note(session, request.reply_surface)
                    session.add_note(request.content)
                else:
                    if request.content is None:
                        raise ChatError("content is required for non-retry runs")
                    _append_input_origin_note(session, request.input_origin)
                    _append_reply_surface_note(session, request.reply_surface)
                    user_message = ChatMessage.user(request.content, sender=request.sender)
                    session.append(user_message)
                    _emit_message_event(run, USER_MESSAGE_EVENT, user_message)
                if request.input_persisted_hook is not None:
                    try:
                        request.input_persisted_hook()
                    except Exception:
                        _LOGGER.warning(
                            "Input-persistence callback failed for run %s",
                            run.id,
                            exc_info=True,
                        )
            if not internal:
                if self._session_title_service is not None:
                    self._session_title_service.notify_user_message(
                        agent_id=run.agent_id,
                        session_id=run.session_id,
                        project_id=project_id,
                        agent=agent,
                        content=cast(str | list[ContentBlock], request.content),
                        run_id=run.id,
                    )
                if isinstance(request.content, str):
                    _activate_triggered_skills(
                        agent,
                        session,
                        request.content,
                        context.skill_registry,
                    )
            run.raise_if_cancelled()
            context.request_state = await self._build_request_state(
                agent,
                session,
                replay_policy=target.replay_policy,
                wire_media_types=target.wire_media_types,
                agent_body=runtime_agent_body(agent),
                project_context=context.project_prompt_context,
                agent_project_id=context.project_id,
                skill_registry=context.skill_registry,
                skill_catalog=context.skill_catalog,
            )
            if context.continuation_reminder is not None:
                assert context.prior_continuation is not None
                context.continuation_reminder = render_continuation_reminder(
                    context.prior_continuation,
                    context_window=self._resolve_context_window(agent),
                )
                context.request_state = _RequestState(
                    inject_continuation_reminder(
                        context.request_state.messages,
                        context.continuation_reminder,
                    ),
                    context.request_state.tools,
                    context.request_state.allowed_tool_names,
                    context.request_state.session_tool_grants,
                )

            try:
                completed_assistant = await self._send_until_final(context, target)
                return completed_assistant
            except ProviderError as primary_exc:
                if _is_model_fallback_trigger(primary_exc):
                    fallback = _resolve_fallback(self._dependencies, agent)
                    if fallback is not None:
                        fallback_model_str, fb_provider_id, fb_connection_id = fallback
                        _, fallback_model_id = _split_agent_model(fallback_model_str)
                        try:
                            fallback_target = self._create_model_target(
                                fb_provider_id,
                                fb_connection_id,
                                fallback_model_id,
                            )
                        except (ConfigError, VBotError) as construction_exc:
                            _run_succeeded = False
                            _persist_run_error(run, session, construction_exc)
                            raise
                        run.add_cancel_callback(lambda: _close_adapter(fallback_target.adapter))
                        _LOGGER.info(
                            "Model fallback activated (run=%s from=%s to=%s)",
                            run.id,
                            agent.model,
                            fallback_model_str,
                        )
                        run.emit(
                            MODEL_FALLBACK_ACTIVATED_EVENT,
                            {"from_model": agent.model, "to_model": fallback_model_str},
                        )
                        session.add_note(
                            "Primary model unavailable. Switched to "
                            f"{fallback_model_str} for this run."
                        )
                        # The reused messages list may carry current-turn
                        # reasoning/reasoning_meta from the primary provider;
                        # stale meta must never reach the fallback provider.
                        assert context.request_state is not None
                        _strip_assistant_reasoning_fields(context.request_state.messages)
                        try:
                            completed_assistant = await self._send_until_final(
                                context, fallback_target
                            )
                            return completed_assistant
                        except (ProviderError, ChatError, ConfigError, VBotError) as fallback_exc:
                            _run_succeeded = False
                            run_error = fallback_exc
                            _persist_run_error(run, session, fallback_exc)
                            raise fallback_exc
                        finally:
                            await _close_adapter(fallback_target.adapter)

                _run_succeeded = False
                run_error = primary_exc
                _persist_run_error(run, session, primary_exc)
                raise
            except (ChatError, ConfigError, VBotError) as exc:
                _run_succeeded = False
                run_error = exc
                _persist_run_error(run, session, exc)
                raise
            except asyncio.CancelledError:
                run_error = asyncio.CancelledError()
                raise
            except BaseException as exc:
                _run_succeeded = False
                run_error = exc
                raise
        finally:
            outcome: Literal["success", "error", "cancelled"]
            if run.cancel_requested:
                outcome = "cancelled"
            elif _run_succeeded:
                outcome = "success"
            else:
                outcome = "error"
            run_status = {"success": "completed", "error": "failed", "cancelled": "cancelled"}[
                outcome
            ]
            run_timing = _timing_payload(run_timing_started_at, run_timing_started_perf)
            _LOGGER.info(
                "Run %s %s (agent=%s session=%s duration_ms=%s model_steps=%d "
                "tool_calls=%d input_tokens=%d output_tokens=%d)",
                run.id,
                run_status,
                run.agent_id,
                run.session_id,
                run_timing["duration_ms"],
                run.model_step_count,
                run.tool_call_count,
                run.input_token_total,
                run.output_token_total,
            )
            run_summary = ChatMessage.run_summary(
                run_id=run.id,
                status=run_status,
                timing=run_timing,
            )
            session.append(run_summary)
            if run.contributes_to_agent_activity:
                try:
                    self._dependencies.sessions.record_terminal_run(
                        run.agent_id,
                        run.session_id,
                        run.id,
                        run_status,
                        run_summary.timestamp,
                        run.project_id,
                    )
                except Exception:
                    # The canonical Run result is already durable in the transcript.
                    # A damaged activity sidecar must not turn successful agent work
                    # into a failed Run, but the missing notification is diagnosable.
                    _LOGGER.warning(
                        "Failed to record unread completion for run %s", run.id, exc_info=True
                    )
            if context.continuation_tracker is not None:
                if (
                    outcome == "success"
                    and completed_assistant is not None
                    and not completed_assistant.interrupted
                ):
                    await context.continuation_tracker.resolve()
                else:
                    if outcome == "cancelled":
                        cause: ContinuationCause = (
                            "user" if run.cancel_reason == "user" else "internal"
                        )
                    else:
                        cause = (
                            context.continuation_tracker.interruption_cause
                            or normalize_interruption_cause(run_error)
                        )
                    await context.continuation_tracker.interrupt(cause)
            # Session usage totals ride every terminal event so accessors can
            # keep their session-level token/cache display current without
            # re-fetching history. Diagnostics only — never mask the outcome.
            try:
                run.terminal_payload_extras["session_usage"] = aggregate_session_usage(
                    session.load()
                )
            except Exception:
                _LOGGER.warning(
                    "Failed to aggregate session usage for run %s", run.id, exc_info=True
                )

            extension_registry = self._dependencies.get_extension_registry()
            if extension_registry is not None:
                extension_ctx = HookContext(
                    session_id=run.session_id,
                    agent_id=run.agent_id,
                    run_id=run.id,
                    add_note=session.add_note,
                )
                await extension_registry.dispatch_run_end(
                    extension_ctx,
                    session_id=run.session_id,
                    agent_id=run.agent_id,
                    outcome=outcome,
                )

            # Background reflection accounting. Fire-and-forget on the service's
            # side; a failure here must never mask the run outcome.
            if self._reflection_service is not None:
                try:
                    self._reflection_service.notify_run_end(
                        run, agent, internal=internal, outcome=outcome
                    )
                except Exception:
                    _LOGGER.warning(
                        "Reflection run-end notification failed (run=%s)", run.id, exc_info=True
                    )

            await _close_adapter(target.adapter)

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
            return session_manager.get(agent_id, session_id, project_id)
        except ChatSessionError:
            if not create_missing:
                raise
            return session_manager.create(agent_id, session_id=session_id, project_id=project_id)

    def _resolve_project_cwd(self, project_id: str | None) -> Path | None:
        """Resolve a working Project cwd, failing closed when unavailable."""
        if project_id is None:
            return None
        cwd = Path(self._dependencies.projects.get(project_id).cwd)
        if not cwd.is_dir():
            raise ChatError(f"Project repository is unavailable: {cwd}")
        return cwd

    def _pinned_skill_catalog(
        self,
        agent_id: str,
        session_id: str,
        agent: Any,
        skill_registry: SkillRegistry,
        project_id: str | None,
    ) -> PinnedSkillCatalog:
        """Return the session's pinned skill catalog, snapshotting it on first build.

        The catalog text and the skill-tool-presence gate are fixed for the
        session's lifetime (persisted in session metadata under the session's own
        ``project_id`` anchor), so a skill written mid-session leaves the session's
        system prompt and tool list byte-identical and the provider prompt cache
        stays intact. Skill activation and ``/``-``$`` triggers still resolve the
        live registry, so a newly written skill is loadable immediately even though
        the catalog is frozen. A new session pins a fresh snapshot, so it sees the
        new skill.
        """
        metadata = self._dependencies.sessions.get_metadata(agent_id, session_id, project_id)
        pinned = metadata.get(PINNED_SKILL_CATALOG_META_KEY)
        if isinstance(pinned, dict) and isinstance(pinned.get("catalog_text"), str):
            return PinnedSkillCatalog(catalog_text=pinned["catalog_text"])
        snapshot = self._dependencies.get_system_prompts().render_skill_catalog(
            agent, skill_registry
        )
        metadata[PINNED_SKILL_CATALOG_META_KEY] = {"catalog_text": snapshot.catalog_text}
        self._dependencies.sessions.set_metadata(agent_id, session_id, metadata, project_id)
        return snapshot

    def _announce_newly_available_skills(
        self,
        agent_id: str,
        session_id: str,
        session: ChatSession,
        agent: Any,
        skill_registry: SkillRegistry,
        project_id: str | None,
    ) -> None:
        """Tell the model about skills that became available to it since this session began.

        The session's ``<available_skills>`` prompt block is pinned for prompt-cache
        stability, so a skill authored or activated mid-session never appears in the
        prompt. This appends a one-time ``<system-reminder>`` note at the conversation
        tail for any newly available+allowed skill, leaving the cached prompt prefix
        untouched. Additions only — a skill that becomes unavailable is not announced.
        The first run seeds the baseline (the skills already in the pinned catalog)
        without announcing them. The diff runs against the registry already resolved for
        this run, so it is a small in-memory set comparison, not a fresh scan.
        """
        # Minimal/degraded skill registries (e.g. some test doubles) may not expose
        # ``filter_allowed``; the announcement is an optional enhancement, so skip it
        # cleanly rather than break the run — the real ``SkillRegistry`` always has it.
        filter_allowed = getattr(skill_registry, "filter_allowed", None)
        if not callable(filter_allowed):
            return
        allowed_skills = getattr(agent, "allowed_skills", None)
        allowed = ["*"] if allowed_skills is None else allowed_skills
        available = {skill.name: skill.description for skill in filter_allowed(allowed)}
        metadata = self._dependencies.sessions.get_metadata(agent_id, session_id, project_id)
        seen = metadata.get(SEEN_SKILLS_META_KEY)
        if not isinstance(seen, list):
            metadata[SEEN_SKILLS_META_KEY] = sorted(available)
            self._dependencies.sessions.set_metadata(agent_id, session_id, metadata, project_id)
            return
        new_names = sorted(set(available) - set(seen))
        if not new_names:
            return
        lines = [SKILL_AVAILABLE_NEW_SKILLS_HEADER]
        lines.extend(f"- {name}: {available[name]}" for name in new_names)
        session.add_note(SKILL_AVAILABLE_NOTE_PREFIX + "\n".join(lines))
        metadata[SEEN_SKILLS_META_KEY] = sorted(set(seen) | set(new_names))
        self._dependencies.sessions.set_metadata(agent_id, session_id, metadata, project_id)

    def _stamp_prompt_files_read(self, session_id: str, paths: list[Path]) -> None:
        """Register auto-injected prompt files as read-before-write for a session.

        Files whose content the System Prompt places into the model's context — SOUL,
        pinned-memory files, a Project's auto-load files, and workspace includes —
        are treated as already read, so the agent can edit one directly with
        ``write``/``edit`` without a redundant ``read`` call.
        The guard still forces a re-read if such a file changes on disk afterwards
        (its ``(mtime, size)`` no longer matches), so the "only while unchanged"
        contract holds. ``paths`` is the resolved-absolute-path list the prompt build
        reported; empty is a no-op. The explicit Project Tool stamps its own result
        files directly through the same ``FileReadState`` instance.
        """
        if not paths:
            return
        file_state = self._dependencies.file_read_state
        for path in paths:
            file_state.record_read(session_id, path)

    async def _build_request_messages(
        self,
        agent: Any,
        session: ChatSession,
        *,
        replay_policy: ReasoningReplayPolicy = REASONING_REPLAY_CURRENT_RUN,
        wire_media_types: frozenset[str] = frozenset(),
        agent_body: str = "",
        project_context: ProjectPromptContext | None = None,
        agent_project_id: str | None = None,
        skill_registry: SkillRegistry | None = None,
        skill_catalog: PinnedSkillCatalog | None = None,
    ) -> list[JsonObject]:
        state = await self._build_request_state(
            agent,
            session,
            replay_policy=replay_policy,
            wire_media_types=wire_media_types,
            agent_body=agent_body,
            project_context=project_context,
            agent_project_id=agent_project_id,
            skill_registry=skill_registry,
            skill_catalog=skill_catalog,
        )
        return state.messages

    async def _build_request_state(
        self,
        agent: Any,
        session: ChatSession,
        *,
        replay_policy: ReasoningReplayPolicy = REASONING_REPLAY_CURRENT_RUN,
        wire_media_types: frozenset[str] = frozenset(),
        agent_body: str = "",
        project_context: ProjectPromptContext | None = None,
        agent_project_id: str | None = None,
        skill_registry: SkillRegistry | None = None,
        skill_catalog: PinnedSkillCatalog | None = None,
    ) -> _RequestState:
        # For a project-born session the project files land in the system prompt;
        # for an identity session both are empty and the prompt is unchanged. The
        # config-agent body is inserted verbatim (never re-expanded) by the builder.
        # ``skill_registry`` scopes the skills block to the project pool (``None`` =
        # the global registry); ``skill_catalog`` is the session-pinned snapshot the
        # skills block renders from, so a mid-session skill write never shifts it.
        session_messages = session.load()
        session_tool_grants = (HISTORY_TOOL_NAME,) if history_available(session_messages) else ()
        system_prompts = self._dependencies.get_system_prompts()
        tools = system_prompts.provider_tool_definitions(
            agent,
            session_tool_grants=session_tool_grants,
        )
        allowed_tool_names = tuple(
            str(definition["name"])
            for definition in tools
            if isinstance(definition.get("name"), str)
        )
        prompt_read_paths: list[Path] = []
        system_prompt = system_prompts.build_system_prompt(
            agent,
            agent_body=agent_body,
            project_context=project_context,
            agent_project_id=agent_project_id,
            skill_registry=skill_registry,
            skill_catalog=skill_catalog,
            read_paths=prompt_read_paths,
            effective_tool_names=allowed_tool_names,
            session_tool_grants=session_tool_grants,
        )
        # Auto-injected prompt files (SOUL, pinned memory, project auto-load files,
        # workspace includes) count as read for this session, so the agent can edit
        # one directly without a redundant read call. Rebuilt every request, so the
        # stamp always reflects what the model currently sees; a later on-disk change
        # still trips the stale guard and forces a re-read.
        self._stamp_prompt_files_read(session.id, prompt_read_paths)
        system_messages = (
            [ChatMessage.system(system_prompt, agent.model).to_dict()]
            if system_prompt.strip()
            else []
        )
        checkpoint = _latest_compaction_checkpoint(session_messages)
        effective_messages = _effective_compaction_messages(session_messages)
        history = _embed_notes_into_request(
            effective_messages,
            replay_policy=replay_policy,
            agent_model=agent.model,
        )
        skill_context_messages: list[JsonObject] = []
        if checkpoint is not None:
            projected_skills = skill_activation_names(effective_messages)
            skill_context_messages = [
                {"role": "user", "content": content}
                for name, content in session.activated_skill_contents(session_messages).items()
                if name not in projected_skills
            ]
        request_messages = [*system_messages, *skill_context_messages, *history]

        session.drain_pending_notes()

        if self._attachment_resolver is None:
            return _RequestState(
                request_messages,
                tools,
                allowed_tool_names,
                session_tool_grants,
            )
        if not _session_has_any_content_blocks(effective_messages):
            return _RequestState(
                request_messages,
                tools,
                allowed_tool_names,
                session_tool_grants,
            )

        # Use the most recently appended user turn as the current-turn marker.
        # If that turn is plain text, all content blocks resolve as historical.
        current_user_message = _last_user_message_with_content_blocks(
            effective_messages
        ) or _last_user_message(effective_messages)
        if current_user_message is None:
            return _RequestState(
                request_messages,
                tools,
                allowed_tool_names,
                session_tool_grants,
            )

        return _RequestState(
            await self._attachment_resolver.resolve_messages(
                request_messages,
                current_user_message_id=current_user_message.id,
                input_modalities=_model_input_modalities(self._dependencies, agent),
                wire_media_types=wire_media_types,
            ),
            tools,
            allowed_tool_names,
            session_tool_grants,
        )

    async def _send_until_final(
        self,
        context: _RunExecutionContext,
        target: _ModelTarget,
    ) -> ChatMessage:
        if context.request_state is None:
            raise AssertionError("Run request state must be built before Model progression")
        run = context.run
        session = context.session
        agent = context.agent
        project_id = context.project_id
        state = context.request_state
        messages = state.messages
        tools = state.tools
        replay_policy = target.replay_policy
        session_usage = run.terminal_payload_extras.get("session_usage")
        if not isinstance(session_usage, dict):
            session_usage = aggregate_session_usage(session.load())
            run.terminal_payload_extras["session_usage"] = session_usage
        tool_iteration_count = 0
        iteration_number = 1
        for _ in range(self._max_tool_iterations + 1):
            run.raise_if_cancelled()
            pending_notes = session.drain_pending_notes()
            if pending_notes:
                messages.extend(_notes_to_request_messages(pending_notes))
            extension_registry = self._dependencies.get_extension_registry()
            messages_for_request = [dict(message) for message in messages]
            if extension_registry is not None:
                extension_ctx = HookContext(
                    session_id=run.session_id,
                    agent_id=run.agent_id,
                    run_id=run.id,
                    add_note=session.add_note,
                )
                messages_for_request = await extension_registry.dispatch_context(
                    extension_ctx,
                    messages=messages_for_request,
                )

            if hasattr(target.adapter, "set_debug_context"):
                target.adapter.set_debug_context(
                    DebugContext(
                        run_id=run.id,
                        agent_id=run.agent_id,
                        session_id=run.session_id,
                        provider_id=target.provider_id,
                        connection_id=target.connection_id,
                        model_id=target.model_id,
                        streaming=self._streaming,
                        iteration_number=iteration_number,
                    )
                )
            run.model_step_count += 1
            _LOGGER.debug(
                "Model step %d requested (run=%s model=%s messages=%d)",
                iteration_number,
                run.id,
                target.model_id,
                len(messages_for_request),
            )
            step_started_perf = time.perf_counter()
            assistant_message = await self._send_assistant_request(
                agent,
                target.adapter,
                target.model_id,
                messages_for_request,
                tools,
                run,
                chunk_timeout_seconds=target.chunk_timeout_seconds,
                continuation_tracker=context.continuation_tracker,
            )
            # A user cancel after visible streamed output returns the preserved
            # partial as an interrupted turn — it must reach the persist block
            # below before the cancel is honored, or the answer the user
            # already saw would vanish from history.
            preserve_cancelled_partial = run.cancel_requested and assistant_message.interrupted
            if not preserve_cancelled_partial:
                run.raise_if_cancelled()
            if assistant_message.usage is None:
                assistant_message = _apply_usage_estimation(assistant_message, messages)
            run.input_token_total += _usage_token_count(assistant_message.usage, "input_tokens")
            run.output_token_total += _usage_token_count(assistant_message.usage, "output_tokens")
            _LOGGER.debug(
                "Model step %d completed (run=%s duration_ms=%d input_tokens=%d "
                "output_tokens=%d tool_calls=%d)",
                iteration_number,
                run.id,
                round((time.perf_counter() - step_started_perf) * 1000),
                _usage_token_count(assistant_message.usage, "input_tokens"),
                _usage_token_count(assistant_message.usage, "output_tokens"),
                len(assistant_message.tool_calls or ()),
            )
            # Hold the per-session append lock from the assistant tool-call
            # message through its tool results so a writer on another accessor
            # (a channel observed note, session.link_channel) cannot land between
            # them and break the tool-cycle ordering invariant.
            async with self._dependencies.sessions.write_lock(
                run.agent_id, run.session_id, project_id
            ):
                session.append(assistant_message)
                assert isinstance(assistant_message.usage, dict)
                session_usage = add_session_turn_usage(session_usage, assistant_message.usage)
                run.terminal_payload_extras["session_usage"] = session_usage
                run.emit(
                    MODEL_STEP_USAGE_EVENT,
                    {
                        "usage": dict(assistant_message.usage),
                        "session_usage": dict(session_usage),
                    },
                    allow_after_cancel=preserve_cancelled_partial,
                )
                if context.continuation_tracker is not None:
                    context.continuation_tracker.record_assistant_boundary(
                        message_id=assistant_message.id,
                        reasoning=assistant_message.reasoning,
                        content=(
                            assistant_message.content
                            if isinstance(assistant_message.content, str)
                            else None
                        ),
                        interrupted=assistant_message.interrupted,
                        tool_calls=assistant_message.tool_calls,
                    )
                if not self._streaming:
                    _emit_assistant_events(run, assistant_message)
                messages.append(
                    _assistant_continuation_dict(assistant_message, replay_policy=replay_policy)
                )

                if not assistant_message.tool_calls:
                    if preserve_cancelled_partial:
                        # The preserved partial is persisted; end the turn
                        # without new provider work (no auto-compaction). The
                        # run manager sees ``cancel_requested`` and marks the
                        # Run cancelled despite the normal return.
                        return assistant_message
                    if self._compaction_service is not None:
                        await self._maybe_auto_compact_state(
                            context,
                            target,
                            usage=assistant_message.usage,
                            continuation_request_messages=[
                                *messages_for_request,
                                _assistant_continuation_dict(
                                    assistant_message, replay_policy=replay_policy
                                ),
                            ],
                        )
                    return assistant_message

                if tool_iteration_count >= self._max_tool_iterations:
                    raise ToolIterationLimitError("maximum tool iterations exceeded")
                tool_iteration_count += 1  # noqa: SIM113 - paired with iteration_number; enumerate would obscure the pre-increment limit check.
                iteration_number += 1

                session.begin_defer_notes()
                try:
                    if context.continuation_tracker is not None:
                        context.continuation_tracker.record_tool_starts(
                            assistant_message.tool_calls
                        )
                    tool_dispatch_context = ToolDispatchContext(
                        registry=self._dependencies.tools,
                        extension_registry=self._dependencies.get_extension_registry(),
                        agent=agent,
                        session=session,
                        run=run,
                        nesting_depth=self._nesting_depth,
                        app_root=Path(self._dependencies.get_system_prompts().app_dir),
                        data_root=Path(self._dependencies.storage.data_dir),
                        project_cwd=context.project_cwd,
                        project_id=project_id,
                        skill_project_id=context.skill_project_id,
                        tool_restriction=context.request.tool_restriction,
                        base_allowed_tools=state.allowed_tool_names,
                        session_tool_grants=state.session_tool_grants,
                    )
                    tool_messages, media_injections = await _dispatch_tool_calls(
                        tool_dispatch_context,
                        assistant_message.tool_calls,
                    )
                    for tool_message in tool_messages:
                        session.append(tool_message)
                        assert tool_message.tool_call_id is not None
                        tool_dispatch_context.notify_result_persisted(tool_message.tool_call_id)
                        messages.append(_message_to_request_dict(tool_message))
                    if context.continuation_tracker is not None:
                        context.continuation_tracker.record_tool_results(tool_messages)
                    # A tool may ask to show media (e.g. read on an image): inject it
                    # as a synthetic current-turn user message after the tool results
                    # so the tool-cycle invariant (results before any non-tool message)
                    # is preserved.
                    for injection in media_injections:
                        await self._inject_read_media(
                            agent,
                            session,
                            messages,
                            injection,
                            target.wire_media_types,
                        )
                    # Honored only after every sibling tool result is persisted, so
                    # this cooperative stop never itself dangles the assistant turn.
                    # It is not a full JSONL guarantee, though: the forceful
                    # task.cancel() in Run.request_cancel (and a process kill) can
                    # still interrupt the dispatch above with tool_calls left
                    # unanswered on disk. That persisted state is not corruption —
                    # request assembly repairs it via _repair_dangling_tool_calls,
                    # synthesizing the missing results before any provider sees it.
                    run.raise_if_cancelled()
                finally:
                    session.flush_deferred_notes()

            if self._compaction_service is not None:
                compacted_state = await self._maybe_auto_compact_state(
                    context,
                    target,
                    usage=None,
                )
                context.request_state = compacted_state
                state = compacted_state
                messages = compacted_state.messages
                tools = compacted_state.tools

        raise ToolIterationLimitError("maximum tool iterations exceeded")

    async def _inject_read_media(
        self,
        agent: Any,
        session: ChatSession,
        messages: list[JsonObject],
        injection: JsonObject,
        wire_media_types: frozenset[str],
    ) -> None:
        """Inject a tool-loaded media file as a synthetic current-turn user message.

        Only the small ``MediaBlock`` reference is persisted to the session, so a
        later run degrades it to a path note through the once-at-start resolver
        and context stays small. The base64-resolved request dict is appended to
        the in-flight ``messages`` so the model sees the image this turn — the
        resolver does not run again inside the tool loop. A non-vision model (or a
        missing resolver) gets a plain text note instead of a hard error, so the
        run never aborts.
        """
        media_type = injection["media_type"]
        filename = injection["filename"]
        media_block = MediaBlock(
            type="media",
            attachment_id=injection["attachment_id"],
            filename=filename,
            media_type=media_type,
        )
        user_message = ChatMessage.user([media_block])
        session.append(user_message)

        input_modalities = _model_input_modalities(self._dependencies, agent)
        vision_unavailable = media_type.startswith("image/") and "image" not in input_modalities
        if self._attachment_resolver is None or vision_unavailable:
            messages.append(_read_media_text_note(filename, media_type))
            return

        resolved = await self._attachment_resolver.resolve_messages(
            [_message_to_request_dict(user_message)],
            current_user_message_id=user_message.id,
            input_modalities=input_modalities,
            wire_media_types=wire_media_types,
        )
        messages.append(resolved[0])

    async def _maybe_auto_compact_state(
        self,
        context: _RunExecutionContext,
        target: _ModelTarget,
        usage: JsonObject | None,
        *,
        continuation_request_messages: list[JsonObject] | None = None,
    ) -> _RequestState:
        """Auto-compact when configured token thresholds are exceeded."""
        if context.request_state is None:
            raise AssertionError("Run request state must exist before Compaction")
        current_state = context.request_state
        run = context.run
        agent = context.agent
        session = context.session
        messages = current_state.messages
        tools = current_state.tools
        if self._compaction_service is None:
            return current_state

        settings = self._load_compaction_settings(
            agent,
            agent_id=run.agent_id,
            session_id=run.session_id,
            project_id=run.project_id,
        )
        if not settings.auto:
            return current_state
        if settings.strategy == "continuation" and continuation_request_messages is None:
            return current_state

        context_window = self._resolve_context_window(agent)
        if context_window is None:
            return current_state

        if isinstance(usage, dict):
            input_tokens_raw = usage.get("input_tokens")
            input_tokens = (
                input_tokens_raw
                if isinstance(input_tokens_raw, int) and not isinstance(input_tokens_raw, bool)
                else 0
            )
        else:
            input_tokens = self._compaction_service.estimate_messages_tokens(messages)

        if settings.trigger == "context_ratio":
            should_compact = self._compaction_service.should_auto_compact(
                input_tokens,
                context_window,
                settings.threshold,
            )
        else:
            should_compact = self._compaction_service.should_auto_compact(
                input_tokens,
                context_window,
                settings.threshold,
                settings=settings,
            )
        if not should_compact:
            return current_state

        _LOGGER.info(
            "Auto-compaction triggered (run=%s agent=%s session=%s input_tokens=%d "
            "context_window=%d threshold=%s)",
            run.id,
            run.agent_id,
            run.session_id,
            input_tokens,
            context_window,
            settings.threshold,
        )
        summary_adapter, summary_model_id = self._resolve_summary_adapter(
            agent,
            target.adapter,
            target.model_id,
            settings,
        )
        close_summary_adapter = summary_adapter is not target.adapter
        try:
            checkpoint = await self._compaction_service.compact(
                session.load(),
                agent=agent,
                summary_adapter=summary_adapter,
                summary_model_id=summary_model_id,
                storage=self._dependencies.storage,
                settings=settings,
                request_messages=continuation_request_messages or messages,
                active_adapter=target.adapter,
                active_model_id=target.model_id,
                active_tools=tools,
            )
        except Exception:
            _LOGGER.warning("Compaction failed; continuing without compaction", exc_info=True)
            return current_state
        finally:
            if close_summary_adapter:
                await _close_adapter(summary_adapter)

        session_messages = session.load()
        ordinal = (
            len(
                [message for message in session_messages if message.role == "compaction_checkpoint"]
            )
            + 1
        )
        checkpoint = finalize_checkpoint_history_guidance(checkpoint, ordinal=ordinal)
        session.append(checkpoint)
        if context.continuation_tracker is not None:
            context.continuation_tracker.record_compaction_boundary()
        persisted_ordinal = checkpoint_ordinal(session.load(), checkpoint.id)
        run.emit(
            COMPACTION_COMPLETED_EVENT,
            {
                "message": checkpoint.to_dict(),
                "checkpoint": persisted_ordinal,
                "checkpoint_id": checkpoint.id,
                "history_available": True,
            },
        )
        # Identity runs only, exactly like the run-start resolution: a config
        # agent's slug must not resolve a same-named identity agent's private home.
        compaction_skill_registry = self._dependencies.resolve_skills(
            context.skill_project_id,
            run.agent_id if run.project_id is None else None,
        )
        rebuilt_state = await self._build_request_state(
            agent,
            session,
            replay_policy=target.replay_policy,
            wire_media_types=target.wire_media_types,
            agent_body=runtime_agent_body(agent),
            project_context=context.project_prompt_context,
            agent_project_id=context.project_id,
            skill_registry=compaction_skill_registry,
            # Reuse the session's pinned snapshot so the rebuilt prompt's catalog is
            # byte-identical across the compaction checkpoint.
            skill_catalog=context.skill_catalog,
        )
        rebuilt_messages = rebuilt_state.messages
        if context.continuation_reminder is not None:
            if context.continuation_tracker is not None:
                active_continuation = fold_continuation_records(session.load_continuation_records())
                if active_continuation is not None:
                    context.continuation_reminder = render_continuation_reminder(
                        active_continuation,
                        context_window=self._resolve_context_window(agent),
                    )
            rebuilt_messages = inject_continuation_reminder(
                rebuilt_messages,
                context.continuation_reminder,
            )
        _LOGGER.info(
            "Auto-compaction completed (run=%s session=%s estimated_tokens_after=%d)",
            run.id,
            run.session_id,
            self._compaction_service.estimate_messages_tokens(rebuilt_messages),
        )
        return _RequestState(
            _restore_in_run_assistant_reasoning(rebuilt_messages, messages),
            rebuilt_state.tools,
            rebuilt_state.allowed_tool_names,
            rebuilt_state.session_tool_grants,
        )

    def _load_compaction_settings(
        self,
        agent: Any,
        *,
        agent_id: str,
        session_id: str,
        project_id: str | None,
    ) -> CompactionSettings:
        """Resolve Session override → Agent default → global Compaction Policy."""
        # Local import: core.compaction imports core.chat at module load, so
        # chat must not import it back at module level (runtime cycle).
        from core.compaction import COMPACTION_POLICY_META_KEY, CompactionSettings
        from core.settings.normalizers import normalize_compaction_policy

        metadata = self._dependencies.sessions.get_metadata(agent_id, session_id, project_id)
        session_policy = metadata.get(COMPACTION_POLICY_META_KEY)
        agent_policy = getattr(agent, "compaction_policy", None)
        raw_settings = normalize_compaction_policy(
            session_policy
            if isinstance(session_policy, dict)
            else agent_policy
            if isinstance(agent_policy, dict)
            else self._dependencies.storage.load_compaction_settings(),
            use_defaults=True,
        )
        trigger = raw_settings["trigger"]
        strategy = raw_settings["strategy"]
        return CompactionSettings(
            auto=bool(raw_settings["enabled"]),
            trigger=str(trigger["type"]),
            threshold=float(trigger.get("threshold", 0.8)),
            trigger_tokens=int(trigger.get("tokens", 100_000)),
            strategy=str(strategy["type"]),
            tail_tokens=int(strategy.get("tail_tokens", 15_000)),
            summary_model=strategy.get("summary_model"),
        )

    def _resolve_context_window(self, agent: Any) -> int | None:
        """Resolve the usable context window for the active agent model.

        Returns ``None`` only when the model string is unusable (no
        ``provider/model`` form). Otherwise the value always resolves through the
        shared effective chain (user-set/capped window for flagged-local models,
        else model window → provider-config default → global floor, see
        :func:`resolve_effective_context_window`), so a model whose window is
        ``None`` still gets a usable budget and auto-compaction keeps working
        instead of silently disabling itself.
        """
        bare_model = parse_bare_model(agent.model)
        if "/" not in bare_model:
            return None

        provider_id, _, resolved_model_id = bare_model.partition("/")
        if not provider_id or not resolved_model_id:
            return None

        try:
            model_entry = self._dependencies.models.get(provider_id, resolved_model_id)
        except (KeyError, AttributeError):
            return None

        return resolve_effective_context_window(
            model_entry.context_window,
            self._lookup_provider_config(provider_id),
            model_metadata=model_entry.metadata,
            model_key=f"{provider_id}/{resolved_model_id}",
            # Live read through the runtime's single source of truth (no reload
            # hook, StorageError-tolerant), so a settings change applies to the
            # next request without re-implementing the storage read here.
            local_context_windows=self._dependencies.get_local_context_windows(),
        )

    def _lookup_provider_config(self, provider_id: str) -> Any:
        """Return the ProviderConfig for the read-side window default, or None.

        Tolerant of a missing/partial runtime (the registry may be absent for a
        custom provider): the resolver treats ``None`` as "no provider default"
        and falls back to the global floor.
        """
        try:
            return self._dependencies.providers.get(provider_id)
        except (KeyError, AttributeError):
            return None

    def _resolve_summary_adapter(
        self,
        agent: Any,
        adapter: Any,
        model_id: str,
        settings: Any,
    ) -> tuple[Any, str]:
        """Resolve compaction summary adapter/model, defaulting to active run target."""
        del agent

        summary_model = settings.summary_model
        if not isinstance(summary_model, str) or not summary_model:
            return adapter, model_id

        try:
            provider_id, summary_model_id, connection_suffix = parse_model_with_connection(
                summary_model
            )
            if connection_suffix:
                connection_id = f"{provider_id}:{connection_suffix}"
            else:
                connection_id = _first_usable_connection_id(
                    self._dependencies,
                    provider_id,
                    _model_connection_allowlist(self._dependencies, provider_id, summary_model_id),
                )
            summary_adapter = self._dependencies.get_adapter(provider_id, connection_id)
        except (ChatError, ConfigError, VBotError, KeyError):
            _LOGGER.warning(
                "Invalid compaction summary model %r; using active run model instead.",
                summary_model,
                exc_info=True,
            )
            return adapter, model_id

        return summary_adapter, summary_model_id

    async def _send_assistant_request(
        self,
        agent: Any,
        adapter: Any,
        model_id: str,
        messages: list[JsonObject],
        tools: list[JsonObject],
        run: Run,
        chunk_timeout_seconds: float | None = STREAM_CHUNK_TIMEOUT_SECONDS,
        continuation_tracker: ContinuationTracker | None = None,
    ) -> ChatMessage:
        request_context = _resolve_request_context_kwargs(adapter, run)
        if self._streaming:
            return await self._send_streaming_assistant_request(
                agent,
                adapter,
                model_id,
                messages,
                tools,
                run,
                chunk_timeout_seconds=chunk_timeout_seconds,
                request_context=request_context,
                continuation_tracker=continuation_tracker,
            )

        return await self._send_non_streaming_assistant_request(
            agent, adapter, model_id, messages, tools, request_context=request_context
        )

    async def _send_non_streaming_assistant_request(
        self,
        agent: Any,
        adapter: Any,
        model_id: str,
        messages: list[JsonObject],
        tools: list[JsonObject],
        *,
        request_context: dict[str, Any],
    ) -> ChatMessage:
        response = await adapter.send(
            messages,
            model_id=model_id,
            temperature=agent.temperature,
            thinking_effort=agent.thinking_effort,
            tools=tools,
            **request_context,
        )
        normalized = adapter.normalize_response(response, model_id=model_id)
        return _assistant_message_from_response(agent.model, normalized)

    async def _send_streaming_assistant_request(
        self,
        agent: Any,
        adapter: Any,
        model_id: str,
        messages: list[JsonObject],
        tools: list[JsonObject],
        run: Run,
        chunk_timeout_seconds: float | None = STREAM_CHUNK_TIMEOUT_SECONDS,
        request_context: dict[str, Any] | None = None,
        continuation_tracker: ContinuationTracker | None = None,
    ) -> ChatMessage:
        # A transient drop before any visible output is replayed as a full stream
        # restart (the not-yet-visible analogue of the non-streaming fallback).
        # Once anything visible has been emitted, the failure propagates instead —
        # partial output cannot be replayed cleanly.
        for attempt in range(MAX_STREAM_RESTARTS + 1):
            try:
                return await self._consume_stream_attempt(
                    agent,
                    adapter,
                    model_id,
                    messages,
                    tools,
                    run,
                    can_restart=attempt < MAX_STREAM_RESTARTS,
                    chunk_timeout_seconds=chunk_timeout_seconds,
                    request_context=request_context or {},
                    continuation_tracker=continuation_tracker,
                )
            except _StreamRestartNeeded as restart:
                _LOGGER.warning(
                    "Streaming attempt %d/%d dropped before any visible output "
                    "(%s: %s); restarting stream",
                    attempt + 1,
                    MAX_STREAM_RESTARTS + 1,
                    type(restart.cause).__name__,
                    restart.cause,
                )
        # Unreachable: the final attempt runs with can_restart=False, so it either
        # returns a message or re-raises the underlying error.
        raise AssertionError("stream restart loop exited without returning")

    async def _consume_stream_attempt(
        self,
        agent: Any,
        adapter: Any,
        model_id: str,
        messages: list[JsonObject],
        tools: list[JsonObject],
        run: Run,
        *,
        can_restart: bool,
        chunk_timeout_seconds: float | None = STREAM_CHUNK_TIMEOUT_SECONDS,
        request_context: dict[str, Any] | None = None,
        continuation_tracker: ContinuationTracker | None = None,
    ) -> ChatMessage:
        accumulator = StreamingAccumulator()
        emitted_visible_delta = False
        stream = adapter.stream(
            messages,
            model_id=model_id,
            temperature=agent.temperature,
            thinking_effort=agent.thinking_effort,
            tools=tools,
            **(request_context or {}),
        )

        try:
            async for delta in iter_with_chunk_timeout(
                stream,
                timeout_seconds=chunk_timeout_seconds,
            ):
                run.raise_if_cancelled()
                visible_deltas = accumulator.add_delta(delta)
                for visible_delta in visible_deltas:
                    run.emit(visible_delta.event_type, visible_delta.payload)
                    if continuation_tracker is not None:
                        continuation_tracker.record_stream_delta(
                            reasoning=str(visible_delta.payload.get("reasoning_delta", "")),
                            content=str(visible_delta.payload.get("content_delta", "")),
                        )
                    emitted_visible_delta = True
                run.raise_if_cancelled()
            if accumulator.finish_reason is None:
                raise NetworkError("Provider stream ended without finish delta")
            assistant_fields = accumulator.finalize_assistant_fields()
        except (ProviderError, NetworkError, StreamingChunkTimeoutError) as exc:
            # One provider-agnostic owner decides what a stream break means; the
            # action stays here (the chat loop owns side effects, not the policy).
            action = decide_stream_recovery(
                exc,
                emitted_visible_delta=emitted_visible_delta,
                can_restart=can_restart,
                has_partial_content=accumulator.partial_content is not None,
                finish_received=accumulator.finish_reason is not None,
            )
            if action is StreamRecoveryAction.ACCEPT_COMPLETE:
                _LOGGER.warning(
                    "Provider stream transport failed after a finish delta; "
                    "accepting the completed response (%s: %s)",
                    type(exc).__name__,
                    exc,
                )
                assistant_fields = accumulator.finalize_assistant_fields()
            elif action is StreamRecoveryAction.FALLBACK:
                assistant_message = await self._send_non_streaming_assistant_request(
                    agent,
                    adapter,
                    model_id,
                    messages,
                    tools,
                    request_context=request_context or {},
                )
                _emit_assistant_events(run, assistant_message)
                return assistant_message
            elif action is StreamRecoveryAction.RESTART:
                raise _StreamRestartNeeded(exc) from exc
            elif action is StreamRecoveryAction.PRESERVE_PARTIAL:
                if continuation_tracker is not None:
                    continuation_tracker.mark_interruption_cause(normalize_interruption_cause(exc))
                return self._finalize_interrupted_partial(agent, accumulator, run)
            else:
                raise
        except asyncio.CancelledError:
            # User cancel mid-stream. Output the user already saw must not
            # vanish (GLOSSARY → Cancel), so accumulated visible content is
            # finalized like a stream break after visible output; the caller
            # persists it and the Run still ends as cancelled. Readable
            # reasoning-only state already belongs to the Continuation Checkpoint.
            if run.cancel_requested and accumulator.partial_content is not None:
                return self._finalize_interrupted_partial(agent, accumulator, run)
            raise

        assistant_message = _assistant_message_from_response(
            agent.model,
            assistant_fields.to_response_dict(),
        )
        _emit_streaming_assistant_events(run, assistant_message)
        return assistant_message

    def _finalize_interrupted_partial(
        self,
        agent: Any,
        accumulator: StreamingAccumulator,
        run: Run,
    ) -> ChatMessage:
        """Preserve a stream broken after visible output as an interrupted turn.

        The visible answer streamed so far is finalized into an assistant message
        flagged ``interrupted`` (no finish reason; any in-flight tool call is
        dropped — it was never executed, so dropping it is side-effect-free).
        The run ends as a normal turn-less assistant turn instead of failing or
        re-running, so the next turn sees the truncated answer in history and
        continues it naturally — no auto-retry, no duplicate output.

        Also the finalize path for a user cancel after visible output: there the
        run ends as *cancelled*, and ``allow_after_cancel`` lets the settled
        assistant-output event through the cancel suppression — it re-publishes
        text the user already saw streaming, not a late result.
        """
        assistant_message = _assistant_message_from_response(
            agent.model,
            accumulator.finalize_partial_fields().to_response_dict(),
            interrupted=True,
        )
        _emit_streaming_assistant_events(run, assistant_message, allow_after_cancel=True)
        return assistant_message

    def _resolve_chunk_timeout(self, provider_id: str, connection_id: str) -> float | None:
        """Return the per-chunk stall timeout for this connection, or None to disable it.

        Local/loopback inference servers (Ollama, llama.cpp, vLLM) can stay
        silent for minutes during prompt prefill, so the stall guard is disabled
        for them; every remote provider keeps the default timeout. Detection is
        owned by :func:`is_local_provider_base_url` so the policy has one home.
        """
        base_url = self._resolve_connection_base_url(provider_id, connection_id)
        if is_local_provider_base_url(base_url):
            return None
        return STREAM_CHUNK_TIMEOUT_SECONDS

    def _resolve_connection_base_url(self, provider_id: str, connection_id: str) -> str | None:
        """Resolve the effective base URL for a provider connection, if known.

        Tolerant of a missing/partial provider registry and of connections
        without their own base URL: returns ``None`` when nothing is resolvable
        (treated as "not local", so the stall guard stays on).
        """
        try:
            provider_config = self._dependencies.providers.get(provider_id)
        except (KeyError, AttributeError):
            return None
        local_id = _connection_local_id(provider_id, connection_id)
        get_connection = getattr(provider_config, "get_connection", None)
        if local_id is not None and callable(get_connection):
            try:
                connection = get_connection(local_id)
            except KeyError:
                connection = None
            connection_base_url = getattr(connection, "base_url", None) if connection else None
            if isinstance(connection_base_url, str) and connection_base_url:
                return connection_base_url
        provider_base_url = getattr(provider_config, "base_url", None)
        return provider_base_url if isinstance(provider_base_url, str) else None
