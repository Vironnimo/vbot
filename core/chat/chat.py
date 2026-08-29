"""Chat message primitives and chat loop execution."""

from __future__ import annotations

import asyncio
import inspect
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol, cast

from core.chat.content_blocks import ContentBlock, MediaBlock, content_block_to_dict
from core.chat.continuation import (
    ContinuationCause,
    ContinuationState,
    ContinuationTracker,
    inject_continuation_reminder,
    normalize_interruption_cause,
    recover_continuation,
    render_continuation_reminder,
)
from core.chat.errors import (
    ChatError,
    ChatSessionError,
    CompactionUnavailableError,
)
from core.chat.errors import (
    ToolIterationLimitError as ToolIterationLimitError,
)
from core.chat.events import (
    _close_adapter,
    _emit_assistant_events,
    _emit_message_event,
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
    _display_content_preview,
    _effective_compaction_messages,
    _last_user_message,
    _last_user_message_with_content_blocks,
    _session_has_any_content_blocks,
    finalize_checkpoint_history_guidance,
    history_available,
    queue_content_is_editable,
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
    ToolCallRejection as ToolCallRejection,
)
from core.chat.messages import (
    _latest_compaction_checkpoint as _latest_compaction_checkpoint,
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
    _model_input_modalities_for_target,
    _resolve_agent_connection,
    _resolve_fallback_chain,
    _split_agent_model,
)
from core.chat.model_resolution import (
    parse_bare_model as parse_bare_model,
)
from core.chat.model_resolution import (
    parse_model_with_connection as parse_model_with_connection,
)
from core.chat.output_files import resolve_assistant_file_references
from core.chat.streaming import (
    should_advance_model_fallback_chain as should_advance_model_fallback_chain,
)
from core.chat.tool_dispatch import (
    ToolDispatchContext,
    _activate_triggered_skills,
    _dispatch_tool_calls,
    _fail_tool_calls_without_dispatch,
    _read_media_outputs,
)
from core.chat.usage import (
    add_session_turn_usage,
    aggregate_session_usage,
    build_model_step_context_usage,
    latest_session_context_usage,
)
from core.chat.wire_shaping import (
    _assistant_continuation_dict,
    _complete_usage_with_estimates,
    _embed_notes_into_request,
    _message_to_request_dict,
    _notes_to_request_messages,
    _strip_assistant_reasoning_fields,
)
from core.debug import DebugContext
from core.extensions import HookContext
from core.projects import (
    ProjectError,
    resolve_prompt_project,
    resolve_skill_scope,
    resolve_working_project_id,
    runtime_agent_body,
)
from core.prompts import PinnedSkillCatalog, ProjectPromptContext
from core.prompts.pinned_context import (
    pinned_memory_files,
    pinned_skill_catalog,
    pinned_soul_context,
    pinned_working_project_context,
    stamp_prompt_files_read,
)
from core.providers.accounts import (
    DEFAULT_ACCOUNT_ID,
    ConnectionRef,
    split_connection_id,
)
from core.providers.adapter import (
    TERMINAL_OUTCOME_OUTPUT_TRUNCATED,
    TERMINAL_OUTCOME_STOP,
    TERMINAL_OUTCOME_TOOL_CALLS,
    TERMINAL_OUTCOME_UNKNOWN,
    TOOL_RESULT_CONTENT_BLOCKS_FIELD,
    TerminalOutcome,
    estimate_wire_request_input_tokens,
)
from core.providers.providers import resolve_effective_context_window
from core.providers.reasoning import (
    DEFAULT_REASONING_REPLAY_POLICY,
    ReasoningReplayPolicy,
)
from core.runs import (
    MODEL_FALLBACK_ACTIVATED_EVENT,
    MODEL_STEP_USAGE_EVENT,
    RUN_CHANGE_STATS_EVENT,
    USER_MESSAGE_EVENT,
    ActiveRunError,
    QueuedRunItem,
    Run,
    RunAdmission,
    RunExecutor,
    RunInterruptedError,
    RunKind,
    WaitingWorkAdmission,
)
from core.sessions import (
    SKILL_AVAILABLE_NOTE_PREFIX,
    ChatSession,
    SessionAddress,
    SessionReadCursor,
    active_session_messages,
    editable_session_message_index,
    latest_project_tool_context_id,
    project_tool_context_id,
)
from core.tools import (
    ANALYZE_IMAGE_TOOL_NAME,
    HISTORY_TOOL_NAME,
    ToolContract,
    ToolNotFoundError,
    project_bash_tool_definitions,
)
from core.utils.errors import ConfigError, ProviderError, VBotError
from core.utils.logging import get_logger
from core.utils.workers import BoundedWorkerPool

if TYPE_CHECKING:
    from core.chat.block_resolver import ContentBlockResolver
    from core.chat.request_runner import WireRequestRunner
    from core.compaction import CompactionService
    from core.compaction.run_coordination import CompactionRunCoordinator
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

_LOGGER = get_logger("chat")

CHAT_TRANSFORM_WORKER_LIMIT = 4
_CHAT_TRANSFORM_WORKERS = BoundedWorkerPool(
    name="chat-transform",
    max_workers=CHAT_TRANSFORM_WORKER_LIMIT,
)

MAX_TOOL_ITERATIONS = 1000
MAX_IDENTICAL_FAILED_TOOL_CALLS = 8
MAX_TOOL_FINALIZATION_VIOLATIONS = 2
TOOL_ITERATION_LIMIT_FAILURE_CODE = "tool_iteration_limit"
TOOL_FINALIZATION_DISABLED_FAILURE_CODE = "tool_calls_disabled"
TOOL_FINALIZATION_NOTE = (
    "Tool execution is disabled for the remainder of this Run because {reason}. "
    "Do not issue further Tool Calls. Explain the blocker and provide the best final answer "
    "possible using the information already available."
)


@dataclass(frozen=True)
class _RequestState:
    messages: list[JsonObject]
    tools: list[JsonObject]
    allowed_tool_names: tuple[str, ...]
    session_tool_grants: tuple[str, ...]
    tool_contracts: Mapping[str, ToolContract] = field(default_factory=dict)


@dataclass(frozen=True)
class _AssistantStep:
    """One canonical Assistant message plus its Provider terminal meaning."""

    message: ChatMessage
    terminal_outcome: TerminalOutcome | None
    recovery: Literal["none", "continue", "interrupt"] = "none"


@dataclass(frozen=True)
class _PreparedRequestMessages:
    """CPU-built provider request projection plus its effective canonical source."""

    messages: list[JsonObject]
    effective_messages: list[ChatMessage]


async def _run_prompt_method(
    manager: Any,
    async_name: str,
    sync_name: str,
    *arguments: Any,
    **keyword_arguments: Any,
) -> Any:
    """Prefer a prompt-owned async boundary and preserve sync test doubles."""
    async_method = getattr(manager, async_name, None)
    if callable(async_method) and inspect.iscoroutinefunction(async_method):
        return await async_method(*arguments, **keyword_arguments)
    return await _CHAT_TRANSFORM_WORKERS.run(
        getattr(manager, sync_name),
        *arguments,
        **keyword_arguments,
    )


def _prepare_request_messages(
    *,
    system_prompt: str,
    agent_model: str,
    session_messages: list[ChatMessage],
    replay_policy: ReasoningReplayPolicy,
    reasoning_scope_model: str,
) -> _PreparedRequestMessages:
    """Project canonical history without consuming Event-Loop time on large Sessions."""
    system_messages = (
        [ChatMessage.system(system_prompt, agent_model).to_dict()] if system_prompt.strip() else []
    )
    effective_messages = _effective_compaction_messages(session_messages)
    history = _embed_notes_into_request(
        effective_messages,
        replay_policy=replay_policy,
        agent_model=reasoning_scope_model,
    )
    return _PreparedRequestMessages(
        messages=[
            *system_messages,
            *history,
        ],
        effective_messages=effective_messages,
    )


def _finalize_compaction_checkpoint(
    checkpoint: ChatMessage,
    session_messages: list[ChatMessage],
) -> ChatMessage:
    ordinal = sum(message.role == "compaction_checkpoint" for message in session_messages) + 1
    return finalize_checkpoint_history_guidance(checkpoint, ordinal=ordinal)


def _prepare_completed_assistant(
    assistant_message: ChatMessage,
    request_messages: list[JsonObject],
    output_cwd: Path | None,
) -> ChatMessage:
    """Fill estimated Usage and resolve output-file references off the Event Loop."""
    completed = _complete_usage_with_estimates(assistant_message, request_messages)
    return _with_assistant_output_files(completed, cwd=output_cwd)


def _request_content_resolution_inputs(
    effective_messages: list[ChatMessage],
    session_messages: list[ChatMessage],
) -> tuple[ChatMessage | None, list[JsonObject]]:
    """Find attachment boundaries and Run-local media without loop-bound scans."""
    current_user_message: ChatMessage | None = None
    if _session_has_any_content_blocks(effective_messages):
        current_user_message = _last_user_message_with_content_blocks(
            effective_messages
        ) or _last_user_message(effective_messages)
    return current_user_message, _current_run_read_media_outputs(session_messages)


def _assign_session_image_references(
    content: str | list[ContentBlock],
    session_messages: Sequence[ChatMessage],
) -> str | list[ContentBlock]:
    """Give incoming images their next durable, Session-local reference number."""

    if isinstance(content, str):
        return content

    existing_images = 0
    highest_reference = 0
    for message in session_messages:
        if not isinstance(message.content, list):
            continue
        for block in message.content:
            if not isinstance(block, MediaBlock) or not block.media_type.startswith("image/"):
                continue
            existing_images += 1
            highest_reference = max(highest_reference, block.image_reference or 0)

    next_reference = max(existing_images, highest_reference) + 1
    assigned: list[ContentBlock] = []
    for block in content:
        if isinstance(block, MediaBlock) and block.media_type.startswith("image/"):
            assigned.append(replace(block, image_reference=next_reference))
            next_reference += 1
        else:
            assigned.append(block)
    return assigned


@dataclass
class _FailedToolCallCircuitBreaker:
    """Stop one Run after repeated identical failed Tool Calls make no progress."""

    limit: int = MAX_IDENTICAL_FAILED_TOOL_CALLS
    _last_signature: tuple[str, str, str, str, str] | None = None
    _consecutive_count: int = 0

    def observe(
        self,
        tool_calls: Sequence[ToolCall],
        tool_messages: Sequence[ChatMessage],
        registry: Any | None = None,
    ) -> str | None:
        """Return the Tool name when the failure threshold is reached."""

        for tool_call, tool_message in zip(tool_calls, tool_messages, strict=True):
            error_code = _tool_message_failure_code(tool_message)
            if error_code is None:
                self._reset()
                continue
            fingerprint = ""
            fingerprint_resolver = getattr(registry, "schema_fingerprint", None)
            if callable(fingerprint_resolver):
                try:
                    fingerprint = str(fingerprint_resolver(tool_call.name))
                except (KeyError, ToolNotFoundError, ValueError):
                    fingerprint = ""
            signature = (
                tool_call.name,
                json.dumps(
                    tool_call.arguments,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                error_code,
                fingerprint,
                tool_call.rejection.fingerprint if tool_call.rejection is not None else "",
            )
            if signature == self._last_signature:
                self._consecutive_count += 1
            else:
                self._last_signature = signature
                self._consecutive_count = 1
            if self._consecutive_count >= self.limit:
                return tool_call.name
        return None

    def _reset(self) -> None:
        self._last_signature = None
        self._consecutive_count = 0


def _tool_message_is_failure(message: ChatMessage) -> bool:
    """Return whether one canonical Tool message carries a failure envelope."""
    return _tool_message_failure_code(message) is not None


def _tool_message_failure_code(message: ChatMessage) -> str | None:
    """Return one stable Tool failure code, or ``None`` for non-failures."""

    if not isinstance(message.content, str):
        return None
    try:
        result = json.loads(message.content)
    except (TypeError, ValueError):
        return None
    if not isinstance(result, dict) or result.get("ok") is not False:
        return None
    error = result.get("error")
    if not isinstance(error, dict):
        return "unknown_failure"
    code = error.get("code")
    return code if isinstance(code, str) and code else "unknown_failure"


def _terminal_outcome_error(
    terminal_outcome: TerminalOutcome | None,
    *,
    has_tool_calls: bool,
) -> ProviderError | None:
    """Return a fail-closed error for an unsafe or inconsistent terminal state."""

    if terminal_outcome is None:
        return None
    if terminal_outcome == TERMINAL_OUTCOME_OUTPUT_TRUNCATED:
        return None
    if (
        terminal_outcome == TERMINAL_OUTCOME_STOP
        and not has_tool_calls
        or terminal_outcome == TERMINAL_OUTCOME_TOOL_CALLS
        and has_tool_calls
    ):
        return None
    return ProviderError(
        f"Provider ended the Assistant turn with unsafe terminal outcome {terminal_outcome!r}",
        retryable=False,
    )


def _combined_interrupted_result(
    messages: list[ChatMessage],
    *,
    output_cwd: Path | None,
) -> ChatMessage:
    """Return one consumer-facing view of every visible recovery fragment."""
    if not messages:
        raise AssertionError("interrupted result requires at least one Assistant message")
    if len(messages) == 1:
        return messages[0]

    latest = messages[-1]
    if latest.model is None:
        raise AssertionError("interrupted Assistant result requires a model")
    content = "".join(message.content for message in messages if isinstance(message.content, str))
    reasoning = "".join(message.reasoning for message in messages if message.reasoning)
    message = ChatMessage.assistant(
        model=latest.model,
        content=content or None,
        reasoning=reasoning or None,
        reasoning_scope=latest.reasoning_scope,
        phase=latest.phase,
        usage=latest.usage,
        interrupted=True,
        interruption_cause=latest.interruption_cause,
    )
    return _with_assistant_output_files(message, cwd=output_cwd)


def _terminal_tool_failure(
    terminal_outcome: TerminalOutcome | None,
) -> tuple[str, str]:
    """Return the canonical Tool failure used when dispatch is forbidden."""

    if terminal_outcome == TERMINAL_OUTCOME_OUTPUT_TRUNCATED:
        return (
            "tool_call_truncated",
            "The Provider reached its output-token limit before completing this Tool "
            "Call. The Tool was not executed. Reissue the complete Tool Call.",
        )
    return (
        "tool_call_rejected",
        f"The Provider ended this turn with terminal outcome "
        f"{terminal_outcome or TERMINAL_OUTCOME_UNKNOWN!r}. The Tool was not executed.",
    )


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


@dataclass(frozen=True)
class _QueuedRunExecutor:
    """Editable queued executor retaining every immutable admission input."""

    loop: ChatLoop
    request: _RunRequest

    async def __call__(self, run: Run) -> ChatMessage:
        return await self.loop._execute_run(run, self.request)

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


# Skill names the session has already surfaced to the model: the pinned catalog at
# first build, plus any later additions already announced. Diffed each run against the
# agent's currently available+allowed skills so a newly available one is announced once.
SEEN_SKILLS_META_KEY = "seen_skills"
# Header of the mid-session "new skills available" reminder. The pinned prompt catalog
# cannot grow without breaking the prompt cache, so additions reach the model here.
SKILL_AVAILABLE_NEW_SKILLS_HEADER = (
    "New skills are now available to you. Load one by name with the `skill` tool when relevant:"
)

# Answer text cannot be replayed without duplication, so a broken visible step
# is persisted and followed by a fresh continuation request in the same Run.
# Keep this budget separate from stream replay and Tool iterations: it bounds
# only consecutive broken continuations after visible answer text.
MAX_STREAM_CONTINUATIONS = 2
STREAM_RECOVERY_NOTE = (
    "The previous Model response stream ended unexpectedly after producing visible "
    "answer text. The partial Assistant response is already part of this conversation. "
    "Continue the same task from exactly where it stopped without repeating that visible "
    "text. No Tool Call from the interrupted Model step was executed; if tools are still "
    "needed, emit every intended Tool Call again as a complete call."
)


def _with_assistant_output_files(
    message: ChatMessage,
    *,
    cwd: Path | None,
) -> ChatMessage:
    """Attach resolved output files once at the canonical Assistant boundary."""
    if message.output_files is not None or not isinstance(message.content, str):
        return message
    output_files = resolve_assistant_file_references(message.content, cwd=cwd)
    return replace(message, output_files=output_files) if output_files is not None else message


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
    not expose the hook receive the system ``full_history`` default.
    """
    if hasattr(adapter, "reasoning_replay_policy"):
        return cast(ReasoningReplayPolicy, adapter.reasoning_replay_policy(model_id))
    return DEFAULT_REASONING_REPLAY_POLICY


def _resolve_wire_media_support(adapter: Any, model_id: str) -> frozenset[str]:
    """Resolve the media types the adapter's wire can carry for one request build.

    Mirrors ``_resolve_reasoning_replay_policy``: adapters and test doubles that
    do not expose the hook carry nothing, so the resolver degrades every
    attachment rather than emitting media the wire cannot encode.
    """
    if hasattr(adapter, "wire_media_support"):
        return frozenset(adapter.wire_media_support(model_id))
    return frozenset()


def _resolved_model_reference(
    dependencies: ChatLoopDependencies,
    provider_id: str,
    connection_id: str,
    model_id: str,
) -> str:
    """Return the exact Provider/Model/Connection/Account scope for one request."""

    local_connection_id, explicit_account_id = split_connection_id(provider_id, connection_id)
    resolved_account_id = dependencies.provider_credentials.resolve_account_id(
        provider_id,
        local_connection_id,
        explicit_account_id,
    )
    connection_suffix = local_connection_id
    if resolved_account_id != DEFAULT_ACCOUNT_ID:
        connection_suffix = f"{connection_suffix}:{resolved_account_id}"
    bare_reference = f"{provider_id}/{model_id}"
    return f"{bare_reference}::{connection_suffix}"


def _usage_token_count(usage: Any, key: str) -> int:
    """Return one non-negative token count from a usage payload, else 0."""
    if not isinstance(usage, dict):
        return 0
    value = usage.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


def _current_run_read_media_outputs(
    messages: list[ChatMessage],
) -> list[JsonObject]:
    """Recover compact media references from the active Run's Tool Results."""

    tail_start = 0
    for index, message in enumerate(messages):
        if message.role == "run_summary":
            tail_start = index + 1

    outputs: list[JsonObject] = []
    for message in messages[tail_start:]:
        if (
            message.role != "tool"
            or not isinstance(message.tool_call_id, str)
            or not isinstance(message.content, str)
        ):
            continue
        try:
            result = json.loads(message.content)
        except (TypeError, ValueError):
            continue
        if isinstance(result, dict):
            outputs.extend(
                _read_media_outputs(
                    result,
                    tool_call_id=message.tool_call_id,
                )
            )
    return outputs


def _restore_in_run_tool_result_content(
    rebuilt_messages: list[JsonObject],
    live_messages: list[JsonObject],
) -> list[JsonObject]:
    """Restore request-only rich Tool Results after in-Run Compaction."""

    from core.compaction import is_compacted_tool_result_content

    rich_content_by_call_id = {
        message["tool_call_id"]: message[TOOL_RESULT_CONTENT_BLOCKS_FIELD]
        for message in live_messages
        if message.get("role") == "tool"
        and isinstance(message.get("tool_call_id"), str)
        and isinstance(message.get(TOOL_RESULT_CONTENT_BLOCKS_FIELD), list)
    }
    for message in rebuilt_messages:
        tool_call_id = message.get("tool_call_id")
        if (
            message.get("role") == "tool"
            and isinstance(tool_call_id, str)
            and tool_call_id in rich_content_by_call_id
            and not is_compacted_tool_result_content(message.get("content"))
        ):
            message[TOOL_RESULT_CONTENT_BLOCKS_FIELD] = rich_content_by_call_id[tool_call_id]
    return rebuilt_messages


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

    @property
    def compaction_service(self) -> CompactionService | None:
        """The loop's Compaction service; ``None`` disables Compaction."""
        return self._compaction_service

    @property
    def _compaction_runs(self) -> CompactionRunCoordinator:
        from core.compaction.run_coordination import CompactionRunCoordinator  # runtime cycle

        return CompactionRunCoordinator(dependencies=self._dependencies, host=self)

    @property
    def _wire_requests(self) -> WireRequestRunner:
        from core.chat.request_runner import WireRequestRunner  # runtime cycle

        return WireRequestRunner(dependencies=self._dependencies, streaming=self._streaming)

    def run_executor(
        self,
        content: str | list[ContentBlock],
        *,
        reply_surface: ReplySurface | None = None,
        agent_overrides: AgentRunOverrides | None = None,
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
        )
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
            lambda run: self._execute_run(run, request),
            admission=RunAdmission(
                working_project_id=working_project_id,
                run_kind=run_kind,
                contributes_to_agent_activity=contributes_to_agent_activity,
            ),
        )

    async def _execute_run(
        self,
        run: Run,
        request: _RunRequest,
    ) -> ChatMessage:
        project_id = run.project_id
        session_address = SessionAddress(
            project_id=project_id, agent_id=run.agent_id, session_id=run.session_id
        )
        session = await self._dependencies.sessions.get_async(session_address)
        await _CHAT_TRANSFORM_WORKERS.run(
            self._dependencies.sessions.record_run_kind,
            session_address,
            run.run_kind,
        )
        async with self._dependencies.sessions.write_lock(session_address):
            session_snapshot = await _SessionSnapshot.load(session)
            if request.edit_message_id is not None:
                session_snapshot.begin_edit(request.edit_message_id)
        prior_continuation: ContinuationState | None = None
        continuation_reminder: str | None = None
        continuation_tracker: ContinuationTracker | None = None
        if not request.internal or request.resume_process_restart:
            if request.edit_message_id is not None:
                recovered = None
            else:
                recovered = await recover_continuation(
                    session,
                    active_run_id=run.id,
                    canonical_messages=session_snapshot.active_messages,
                )
            if request.internal and recovered is not None and recovered.cause != "process_restart":
                recovered = None
            prior_continuation = recovered
            if prior_continuation is not None and not prior_continuation.active:
                continuation_reminder = render_continuation_reminder(
                    prior_continuation,
                    context_window=None,
                )
            if not request.internal or prior_continuation is not None:
                continuation_tracker = ContinuationTracker(
                    session,
                    run_id=run.id,
                    request=_serialize_continuation_request(request.content),
                    prior_state=prior_continuation,
                )
                await continuation_tracker.start()
        try:
            context = await self._create_run_execution_context(
                run,
                request,
                session=session,
                session_snapshot=session_snapshot,
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

    async def _create_run_execution_context(
        self,
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
        if request.agent_overrides is None:
            agent = self._dependencies.agent_resolver.resolve_agent(project_id, run.agent_id)
        else:
            agent = self._dependencies.agent_resolver.resolve_agent(
                project_id,
                run.agent_id,
                run_overrides=request.agent_overrides,
            )
        provider_id, connection_id = _resolve_agent_connection(self._dependencies, agent)
        _ensure_provider_exists(self._dependencies.providers, provider_id)
        _model_provider_id, model_id = _split_agent_model(agent.model)
        target = self._create_model_target(provider_id, connection_id, model_id)
        run.add_cancel_callback(lambda: _close_adapter(target.adapter))
        run.add_cancel_callback(
            lambda: self._dependencies.process_manager.cancel_scope_async(run.id)
        )
        project_cwd = self.resolve_project_cwd(working_project_id)
        prompt_project = resolve_prompt_project(self._dependencies.projects, working_project_id)
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
            self._dependencies,
            run.agent_id,
            run.session_id,
            prompt_project,
            project_prompt_context,
            project_id,
        )
        soul_context = await _CHAT_TRANSFORM_WORKERS.run(
            pinned_soul_context,
            self._dependencies,
            run.agent_id,
            run.session_id,
            agent,
            project_id,
        )
        memory_files_context = await _CHAT_TRANSFORM_WORKERS.run(
            pinned_memory_files,
            self._dependencies,
            run.agent_id,
            run.session_id,
            agent,
            project_id,
        )
        skill_project_id, identity_agent_id = resolve_skill_scope(
            project_id, prompt_project, run.agent_id
        )
        skill_registry = await _CHAT_TRANSFORM_WORKERS.run(
            self._dependencies.resolve_skills,
            skill_project_id,
            identity_agent_id,
        )
        skill_catalog = await _CHAT_TRANSFORM_WORKERS.run(
            pinned_skill_catalog,
            self._dependencies,
            run.agent_id,
            run.session_id,
            agent,
            skill_registry,
            project_id,
        )
        prompt_cache_affinity_id = await _CHAT_TRANSFORM_WORKERS.run(
            self._dependencies.sessions.prompt_cache_affinity_id,
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
        if project_id is None:
            loaded_project_id = latest_project_tool_context_id(session_snapshot.active_messages)
            if loaded_project_id is not None:
                await self._apply_project_skill_context(context, loaded_project_id)
        return context

    async def _apply_project_skill_context(
        self,
        context: _RunExecutionContext,
        project_id: str,
    ) -> None:
        """Make one explicitly loaded Project's Skills active for an Identity Run.

        The Project Tool Result remains the sole persisted context carrier. This
        updates only the Run-local Skill resolver used by activation and Tool
        dispatch; the current prompt-epoch Skill catalog and therefore the System Prompt
        remain unchanged.
        """
        try:
            registry = await _CHAT_TRANSFORM_WORKERS.run(
                self._dependencies.resolve_skills,
                project_id,
                context.run.agent_id,
            )
        except (ProjectError, OSError) as error:
            _LOGGER.warning(
                "Loaded Project Skill context is unavailable (agent=%s session=%s project=%s): %s",
                context.run.agent_id,
                context.run.session_id,
                project_id,
                error,
            )
            return
        context.skill_project_id = project_id
        context.skill_registry = registry

    def _create_model_target(
        self,
        provider_id: str,
        connection_id: str,
        model_id: str,
    ) -> _ModelTarget:
        connection = ConnectionRef(provider_id, connection_id)
        adapter = self._dependencies.get_adapter(connection)
        return _ModelTarget(
            provider_id=provider_id,
            connection_id=connection_id,
            model_id=model_id,
            model_reference=_resolved_model_reference(
                self._dependencies,
                provider_id,
                connection_id,
                model_id,
            ),
            adapter=adapter,
            replay_policy=_resolve_reasoning_replay_policy(adapter, model_id),
            input_modalities=_model_input_modalities_for_target(
                self._dependencies,
                provider_id,
                model_id,
            ),
            wire_media_types=_resolve_wire_media_support(adapter, model_id),
            chunk_timeout_seconds=self._wire_requests.resolve_chunk_timeout(connection),
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
        session_address = SessionAddress(
            project_id=project_id, agent_id=run.agent_id, session_id=run.session_id
        )
        internal = request.internal
        run_timing_started_at = datetime.now(UTC)
        run_timing_started_perf = time.perf_counter()
        _run_succeeded = True
        _run_interrupted = False
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
            session.begin_defer_notes()
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
                await _CHAT_TRANSFORM_WORKERS.run(
                    self._announce_newly_available_skills,
                    run.agent_id,
                    run.session_id,
                    session,
                    agent,
                    context.skill_registry,
                    project_id,
                )
                async with self._dependencies.sessions.write_lock(session_address):
                    # Another admitted Run may have appended while this Run was
                    # queued for the Session lock. Refresh before assigning image
                    # references so each persisted image stays unique.
                    await context.session_snapshot.refresh(session)
                    reset_auto_title = False
                    if request.edit_message_id is not None:
                        editable_session_message_index(
                            active_session_messages(context.session_snapshot.messages),
                            request.edit_message_id,
                        )
                        reset_auto_title = not any(
                            message.role == "user"
                            for message in context.session_snapshot.active_messages
                        )
                    if internal:
                        if not isinstance(request.content, str):
                            raise ChatError("internal runs require string content")
                        _append_reply_surface_note(
                            session,
                            request.reply_surface,
                            messages=context.session_snapshot.active_messages,
                        )
                        session.add_note(request.content)
                        persisted_messages = session.take_deferred_notes()
                    else:
                        if request.content is None:
                            raise ChatError("content is required for non-retry runs")
                        _append_input_origin_note(session, request.input_origin)
                        _append_reply_surface_note(
                            session,
                            request.reply_surface,
                            messages=context.session_snapshot.active_messages,
                        )
                        user_message = ChatMessage.user(
                            _assign_session_image_references(
                                request.content,
                                context.session_snapshot.active_messages,
                            ),
                            sender=request.sender,
                        )
                        history_edit = (
                            ChatMessage.history_edit(request.edit_message_id)
                            if request.edit_message_id is not None
                            else None
                        )
                        persisted_messages = [
                            *([history_edit] if history_edit is not None else []),
                            *session.take_deferred_notes(),
                            user_message,
                        ]
                    await session.append_many_async(persisted_messages)
                    if request.edit_message_id is not None:
                        context.session_snapshot.commit_edit()
                        await session.clear_continuation_async()
                        context.prompt_cache_affinity_id = await _CHAT_TRANSFORM_WORKERS.run(
                            self._dependencies.sessions.rotate_prompt_cache_affinity_id,
                            session_address,
                        )
                        if reset_auto_title:
                            try:
                                await _CHAT_TRANSFORM_WORKERS.run(
                                    self._dependencies.sessions.reset_auto_title,
                                    session_address,
                                )
                            except Exception:
                                _LOGGER.warning(
                                    "Failed to reset generated Session title after history edit",
                                    exc_info=True,
                                )
                    await context.session_snapshot.refresh(session)
                    if not internal:
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
            finally:
                await session.flush_deferred_notes_async()
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
                    session.activated_skill_contents(context.session_snapshot.active_messages)
                    session.begin_defer_notes()
                    try:
                        await _CHAT_TRANSFORM_WORKERS.run(
                            _activate_triggered_skills,
                            agent,
                            session,
                            request.content,
                            context.skill_registry,
                        )
                        async with self._dependencies.sessions.write_lock(session_address):
                            await session.flush_deferred_notes_async()
                            await context.session_snapshot.refresh(session)
                    finally:
                        await session.flush_deferred_notes_async()
            run.raise_if_cancelled()
            context.request_state = await self.build_request_state(
                agent,
                session,
                inputs=RequestBuildInputs.from_context(context, target).with_session_messages(
                    context.session_snapshot.active_messages
                ),
            )
            if context.continuation_reminder is not None:
                assert context.prior_continuation is not None
                context.continuation_reminder = render_continuation_reminder(
                    context.prior_continuation,
                    context_window=self.resolve_context_window(agent),
                )
                context.request_state = _RequestState(
                    inject_continuation_reminder(
                        context.request_state.messages,
                        context.continuation_reminder,
                    ),
                    context.request_state.tools,
                    context.request_state.allowed_tool_names,
                    context.request_state.session_tool_grants,
                    context.request_state.tool_contracts,
                )

            try:
                completed_assistant = await self._send_until_final(context, target)
                return completed_assistant
            except ProviderError as primary_exc:
                try:
                    (
                        completed_assistant,
                        chain_error,
                    ) = await self._advance_fallback_chain(context, run, session, primary_exc)
                except RunInterruptedError as exc:
                    _run_succeeded = False
                    _run_interrupted = True
                    run_error = exc
                    if isinstance(exc.result, ChatMessage):
                        completed_assistant = exc.result
                    raise
                if completed_assistant is not None:
                    return completed_assistant
                _run_succeeded = False
                run_error = chain_error
                await _persist_run_error(run, session, chain_error)
                raise chain_error from primary_exc
            except RunInterruptedError as exc:
                _run_succeeded = False
                _run_interrupted = True
                run_error = exc
                if isinstance(exc.result, ChatMessage):
                    completed_assistant = exc.result
                raise
            except (ChatError, ConfigError, VBotError) as exc:
                _run_succeeded = False
                run_error = exc
                await _persist_run_error(run, session, exc)
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
            run_status = (
                "interrupted"
                if _run_interrupted and outcome != "cancelled"
                else {"success": "completed", "error": "failed", "cancelled": "cancelled"}[outcome]
            )
            run_timing = _timing_payload(run_timing_started_at, run_timing_started_perf)
            _LOGGER.info(
                "Run %s %s (agent=%s session=%s duration_ms=%s iterations=%d "
                "tool_calls=%d input_tokens=%d output_tokens=%d)",
                run.id,
                run_status,
                run.agent_id,
                run.session_id,
                run_timing["duration_ms"],
                run.iteration_count,
                run.tool_call_count,
                run.input_token_total,
                run.output_token_total,
            )
            run_summary = ChatMessage.run_summary(
                run_id=run.id,
                work_id=run.work_id,
                status=run_status,
                timing=run_timing,
                iteration_count=run.iteration_count,
            )
            # Git-style change statistics for this run, computed from the
            # session-scoped content tracker (real before/after line diffs).
            # Peek first so an all-zero outcome persists explicitly and matches
            # the totals the live stream last showed; take consumes the deltas.
            # Best-effort: untracked files (too large, non-UTF-8) simply mean
            # the UI falls back to its per-tool-call counts.
            try:
                change_stats = self._dependencies.change_tracker.peek_run_stats(run.session_id)
                if change_stats is not None:
                    run.terminal_payload_extras["change_stats"] = change_stats
                    object.__setattr__(run_summary, "change_stats", change_stats)
                self._dependencies.change_tracker.take_run_stats(run.session_id)
            except Exception:
                _LOGGER.warning(
                    "Failed to compute change statistics for run %s", run.id, exc_info=True
                )
            await session.append_async(run_summary)
            await context.session_snapshot.refresh(session)
            if run.contributes_to_agent_activity:
                try:
                    await _CHAT_TRANSFORM_WORKERS.run(
                        self._dependencies.sessions.record_terminal_run,
                        session_address,
                        run.id,
                        run_status,
                        run_summary.timestamp,
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
                await context.session_snapshot.refresh(session)
                terminal_messages = context.session_snapshot.messages
                run.terminal_payload_extras["session_usage"] = aggregate_session_usage(
                    terminal_messages
                )
                terminal_context_usage = latest_session_context_usage(
                    context.session_snapshot.active_messages
                )
                if terminal_context_usage is not None:
                    run.terminal_payload_extras["context_usage"] = terminal_context_usage
            except Exception:
                _LOGGER.warning(
                    "Failed to aggregate Session Usage for run %s", run.id, exc_info=True
                )

            extension_registry = self._dependencies.get_extension_registry()
            if extension_registry is not None:
                session.begin_defer_notes()
                extension_ctx = HookContext(
                    session_id=run.session_id,
                    agent_id=run.agent_id,
                    run_id=run.id,
                    add_note=session.add_note,
                )
                try:
                    await extension_registry.dispatch_run_end(
                        extension_ctx,
                        session_id=run.session_id,
                        agent_id=run.agent_id,
                        outcome=outcome,
                    )
                finally:
                    async with self._dependencies.sessions.write_lock(session_address):
                        await session.flush_deferred_notes_async()
                        await context.session_snapshot.refresh(session)

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
            return session_manager.get(
                SessionAddress(project_id=project_id, agent_id=agent_id, session_id=session_id)
            )
        except ChatSessionError:
            if not create_missing:
                raise
            return session_manager.create(agent_id, session_id=session_id, project_id=project_id)

    async def _advance_fallback_chain(
        self,
        context: _RunExecutionContext,
        run: Run,
        session: ChatSession,
        first_failure: ProviderError,
    ) -> tuple[ChatMessage | None, ProviderError]:
        """Advance through the resolved fallback chain after a provider failure.

        Returns ``(assistant, error_to_report)``. ``assistant`` is set when a
        chain candidate completed the Run. Otherwise ``error_to_report`` is the
        last actual send failure (the primary failure when no candidate ever
        ran) and the caller owns persisting it. Raises directly — already
        persisted — when a candidate fails in a way that must replace the
        reported outcome: a non-qualifying ProviderError (auth, billing,
        permission, content policy, context limits) or any Chat/Config/VBot
        error during a candidate Run. Candidate construction failures only log
        a warning and skip that candidate — one broken binding must never take
        down the whole escape route.
        """
        agent = context.agent
        chain = list(_resolve_fallback_chain(self._dependencies, agent))
        if not chain or not should_advance_model_fallback_chain(first_failure):
            return None, first_failure

        from_binding = agent.model
        last_failure: ProviderError = first_failure
        for binding, provider_id, connection_id in chain:
            _, candidate_model_id = _split_agent_model(binding)
            try:
                candidate_target = self._create_model_target(
                    provider_id,
                    connection_id,
                    candidate_model_id,
                )
            except (ConfigError, VBotError) as construction_exc:
                _LOGGER.warning(
                    "Skipping fallback candidate %s for agent %s (%s)",
                    binding,
                    getattr(agent, "id", "?"),
                    construction_exc,
                )
                continue

            def _close_candidate_adapter(
                _adapter: Any = candidate_target.adapter,
            ) -> Any:
                return _close_adapter(_adapter)

            run.add_cancel_callback(_close_candidate_adapter)
            _LOGGER.info(
                "Model fallback activated (run=%s from=%s to=%s)",
                run.id,
                from_binding,
                binding,
            )
            run.emit(
                MODEL_FALLBACK_ACTIVATED_EVENT,
                {"from_model": from_binding, "to_model": binding},
            )
            await session.add_note_async(
                f"Model {from_binding} unavailable. Switched to {binding} for this run."
            )
            await context.session_snapshot.refresh(session)
            context.request_state = await self.build_request_state(
                agent,
                session,
                inputs=RequestBuildInputs.from_context(
                    context, candidate_target
                ).with_session_messages(context.session_snapshot.active_messages),
            )
            if context.continuation_reminder is not None:
                assert context.prior_continuation is not None
                context.request_state = _RequestState(
                    inject_continuation_reminder(
                        context.request_state.messages,
                        context.continuation_reminder,
                    ),
                    context.request_state.tools,
                    context.request_state.allowed_tool_names,
                    context.request_state.session_tool_grants,
                    context.request_state.tool_contracts,
                )
            # Rebuilding applies the fallback route's media and Tool
            # capabilities. The persisted previous tool cycle may still carry
            # Provider-specific reasoning, which must never cross the Provider
            # boundary.
            _strip_assistant_reasoning_fields(context.request_state.messages)
            try:
                completed = await self._send_until_final(context, candidate_target)
                return completed, last_failure
            except RunInterruptedError:
                raise
            except ProviderError as candidate_failure:
                if not should_advance_model_fallback_chain(candidate_failure):
                    await _persist_run_error(run, session, candidate_failure)
                    raise
                last_failure = candidate_failure
                from_binding = binding
                continue
            except (ChatError, ConfigError, VBotError) as candidate_failure:
                await _persist_run_error(run, session, candidate_failure)
                raise
            finally:
                await _close_adapter(candidate_target.adapter)
        return None, last_failure

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

    def resolve_project_cwd(self, project_id: str | None) -> Path | None:
        """Resolve a working Project cwd, failing closed when unavailable."""
        if project_id is None:
            return None
        cwd = Path(self._dependencies.projects.get(project_id).cwd)
        if not cwd.is_dir():
            raise ChatError(f"Project repository is unavailable: {cwd}")
        return cwd

    @staticmethod
    def available_skill_names(agent: Any, skill_registry: SkillRegistry) -> list[str] | None:
        """Return the currently advertised Skill names, or ``None`` for a degraded registry."""
        filter_allowed = getattr(skill_registry, "filter_allowed", None)
        if not callable(filter_allowed):
            return None
        allowed_skills = getattr(agent, "allowed_skills", None)
        allowed = ["*"] if allowed_skills is None else allowed_skills
        return sorted(str(skill.name) for skill in filter_allowed(allowed))

    def _announce_newly_available_skills(
        self,
        agent_id: str,
        session_id: str,
        session: ChatSession,
        agent: Any,
        skill_registry: SkillRegistry,
        project_id: str | None,
    ) -> None:
        """Tell the model about Skills that became available during this prompt epoch.

        The Session's ``<available_skills>`` block stays pinned between Compactions,
        so a Skill that becomes available mid-epoch does not change the prompt. This
        appends a one-time ``<system-reminder>`` note for each newly available+allowed
        Skill, leaving the cached prefix untouched. Additions only — a Skill that
        becomes unavailable is not announced. The first Run and every successful
        Compaction seed the baseline from the catalog without announcing it. The diff
        uses the registry already resolved for this Run, so it is an in-memory set
        comparison rather than another scan.
        """
        # Minimal/degraded skill registries (e.g. some test doubles) may not expose
        # ``filter_allowed``; the announcement is an optional enhancement, so skip it
        # cleanly rather than break the run — the real ``SkillRegistry`` always has it.
        available_names = self.available_skill_names(agent, skill_registry)
        if available_names is None:
            return
        allowed_skills = getattr(agent, "allowed_skills", None)
        allowed = ["*"] if allowed_skills is None else allowed_skills
        available = {
            str(skill.name): str(skill.description)
            for skill in skill_registry.filter_allowed(allowed)
        }
        address = SessionAddress(project_id=project_id, agent_id=agent_id, session_id=session_id)
        metadata = self._dependencies.sessions.get_metadata(address)
        seen = metadata.get(SEEN_SKILLS_META_KEY)
        if not isinstance(seen, list):
            metadata[SEEN_SKILLS_META_KEY] = available_names
            self._dependencies.sessions.set_metadata(address, metadata)
            return
        new_names = sorted(set(available) - set(seen))
        if not new_names:
            return
        lines = [SKILL_AVAILABLE_NEW_SKILLS_HEADER]
        lines.extend(f"- {name}: {available[name]}" for name in new_names)
        session.add_note(SKILL_AVAILABLE_NOTE_PREFIX + "\n".join(lines))
        metadata[SEEN_SKILLS_META_KEY] = sorted(set(seen) | set(new_names))
        self._dependencies.sessions.set_metadata(address, metadata)

    async def _build_request_messages(
        self,
        agent: Any,
        session: ChatSession,
        *,
        replay_policy: ReasoningReplayPolicy = DEFAULT_REASONING_REPLAY_POLICY,
        reasoning_scope_model: str | None = None,
        input_modalities: frozenset[str] | None = None,
        wire_media_types: frozenset[str] = frozenset(),
        agent_body: str = "",
        project_context: ProjectPromptContext | None = None,
        working_project_context: str | None = None,
        agent_project_id: str | None = None,
        skill_registry: SkillRegistry | None = None,
        skill_catalog: PinnedSkillCatalog | None = None,
    ) -> list[JsonObject]:
        state = await self.build_request_state(
            agent,
            session,
            inputs=RequestBuildInputs(
                replay_policy=replay_policy,
                reasoning_scope_model=reasoning_scope_model,
                input_modalities=input_modalities,
                wire_media_types=wire_media_types,
                agent_body=agent_body,
                project_context=project_context,
                working_project_context=working_project_context,
                agent_project_id=agent_project_id,
                skill_registry=skill_registry,
                skill_catalog=skill_catalog,
            ),
        )
        return state.messages

    async def build_request_state(
        self,
        agent: Any,
        session: ChatSession,
        *,
        inputs: RequestBuildInputs,
    ) -> _RequestState:
        # For a project-born session the Working Project context lands in the system
        # prompt; for an unrooted identity session it is empty. The
        # config-agent body is inserted verbatim (never re-expanded) by the builder.
        # ``skill_registry`` scopes the skills block to the project pool (``None`` =
        # the global registry); ``inputs.skill_catalog`` is the current prompt-epoch
        # snapshot the skills block renders from, so only Compaction replaces it. The
        # ``working_project_context`` / ``soul_context`` / ``memory_files_context``
        # prompt-epoch snapshots behave the same way.
        session_messages = (
            await session.load_active_async()
            if inputs.session_messages_override is None
            else list(inputs.session_messages_override)
        )
        system_prompts = self._dependencies.get_system_prompts()
        base_tools = await _run_prompt_method(
            system_prompts,
            "provider_tool_definitions_async",
            "provider_tool_definitions",
            agent,
        )
        history_grants: tuple[str, ...] = (
            (HISTORY_TOOL_NAME,) if history_available(session_messages) else ()
        )
        session_tool_grants = history_grants
        effective_input_modalities = (
            inputs.input_modalities
            if inputs.input_modalities is not None
            else _model_input_modalities(self._dependencies, agent)
        )
        tools = (
            await _run_prompt_method(
                system_prompts,
                "provider_tool_definitions_async",
                "provider_tool_definitions",
                agent,
                session_tool_grants=session_tool_grants,
            )
            if session_tool_grants
            else base_tools
        )
        tools = await self._route_tool_definitions(
            tools,
            input_modalities=effective_input_modalities,
            wire_media_types=inputs.wire_media_types,
        )
        allowed_tool_names = tuple(
            str(definition["name"])
            for definition in tools
            if isinstance(definition.get("name"), str)
        )
        allowed_tool_name_set = set(allowed_tool_names)
        session_tool_grants = tuple(
            name for name in session_tool_grants if name in allowed_tool_name_set
        )
        tool_contracts = await _CHAT_TRANSFORM_WORKERS.run(
            self._dependencies.tools.contracts_for_provider_definitions,
            tools,
        )
        prompt_read_paths: list[Path] = []
        system_prompt = await _run_prompt_method(
            system_prompts,
            "build_system_prompt_async",
            "build_system_prompt",
            agent,
            agent_body=inputs.agent_body,
            project_context=inputs.project_context,
            working_project_context=inputs.working_project_context,
            soul_context=inputs.soul_context,
            memory_files_context=inputs.memory_files_context,
            agent_project_id=inputs.agent_project_id,
            nesting_depth=self._nesting_depth,
            skill_registry=inputs.skill_registry,
            skill_catalog=inputs.skill_catalog,
            read_paths=prompt_read_paths,
            effective_tool_names=allowed_tool_names,
            session_tool_grants=session_tool_grants,
        )
        # Auto-injected prompt files (SOUL, pinned memory, project auto-load files,
        # workspace includes) count as read for this session, so the agent can edit
        # one directly without a redundant read call. Rebuilt every request, so the
        # stamp always reflects what the model currently sees; a later on-disk change
        # still trips the stale guard and forces a re-read.
        await _CHAT_TRANSFORM_WORKERS.run(
            stamp_prompt_files_read,
            self._dependencies.file_read_state,
            session.id,
            prompt_read_paths,
        )
        prepared_messages = await _CHAT_TRANSFORM_WORKERS.run(
            _prepare_request_messages,
            system_prompt=system_prompt,
            agent_model=agent.model,
            session_messages=session_messages,
            replay_policy=inputs.replay_policy,
            reasoning_scope_model=inputs.reasoning_scope_model or agent.model,
        )
        effective_messages = prepared_messages.effective_messages
        request_messages = prepared_messages.messages

        session.drain_pending_notes()

        if self._attachment_resolver is None:
            return _RequestState(
                request_messages,
                tools,
                allowed_tool_names,
                session_tool_grants,
                tool_contracts,
            )

        current_user_message, read_media_outputs = await _CHAT_TRANSFORM_WORKERS.run(
            _request_content_resolution_inputs,
            effective_messages,
            session_messages,
        )
        # Use the most recently appended user turn as the current-turn marker.
        # If that turn is plain text, all user content blocks resolve as historical.
        if current_user_message is not None:
            request_messages = await self._attachment_resolver.resolve_messages(
                request_messages,
                current_user_message_id=current_user_message.id,
                input_modalities=effective_input_modalities,
                wire_media_types=inputs.wire_media_types,
            )

        await self._attach_tool_result_content(
            [message for message in request_messages if message.get("role") == "tool"],
            read_media_outputs,
            effective_input_modalities,
            inputs.wire_media_types,
        )
        return _RequestState(
            request_messages,
            tools,
            allowed_tool_names,
            session_tool_grants,
            tool_contracts,
        )

    @asynccontextmanager
    async def _assistant_persistence_boundary(
        self,
        run: Run,
        *,
        project_id: str | None,
        preserve_after_cancel: bool,
    ) -> AsyncIterator[None]:
        """Acquire the Session lock without losing already streamed readable output."""
        write_lock = self._dependencies.sessions.write_lock(
            SessionAddress(project_id=project_id, agent_id=run.agent_id, session_id=run.session_id)
        )
        while True:
            try:
                await write_lock.__aenter__()
                break
            except asyncio.CancelledError:
                if not (preserve_after_cancel and run.cancel_requested):
                    raise
                # Run cancellation is forceful, but this completed readable
                # stream is already visible. Defer that cancellation only until
                # the competing writer releases the append boundary.
        try:
            yield
        finally:
            # The concrete Session lock never suppresses body exceptions and
            # needs no exception details to release its ContextVar ownership.
            await write_lock.__aexit__(None, None, None)

    async def _route_tool_definitions(
        self,
        tools: list[JsonObject],
        *,
        input_modalities: frozenset[str],
        wire_media_types: frozenset[str],
    ) -> list[JsonObject]:
        """Apply effective Model-route gates to route-dependent Tools."""

        tools = project_bash_tool_definitions(tools, nesting_depth=self._nesting_depth)
        if not any(definition.get("name") == ANALYZE_IMAGE_TOOL_NAME for definition in tools):
            return tools
        route_can_view_images = "image" in input_modalities and any(
            media_type.startswith("image/") for media_type in wire_media_types
        )
        image_task_available = (
            False
            if route_can_view_images
            else await self._dependencies.image_understanding_available()
        )
        if image_task_available:
            return tools
        return [
            definition for definition in tools if definition.get("name") != ANALYZE_IMAGE_TOOL_NAME
        ]

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
        session_address = SessionAddress(
            project_id=project_id, agent_id=run.agent_id, session_id=run.session_id
        )
        state = context.request_state
        messages = state.messages
        tools = state.tools
        replay_policy = target.replay_policy
        session_usage = run.terminal_payload_extras.get("session_usage")
        if not isinstance(session_usage, dict):
            session_usage = aggregate_session_usage(context.session_snapshot.messages)
            run.terminal_payload_extras["session_usage"] = session_usage
        tool_iteration_count = 0
        stream_continuation_count = 0
        interruption_chain: list[ChatMessage] = []
        emitted_change_stats: dict[str, object] | None = None
        failed_tool_call_breaker = _FailedToolCallCircuitBreaker()
        tool_finalization_reason: str | None = None
        tool_finalization_violation_count = 0
        while True:
            run.raise_if_cancelled()
            async with self._dependencies.sessions.write_lock(session_address):
                session.begin_defer_notes()
                try:
                    self._dependencies.deliver_background_completions(run, session)
                except Exception:
                    _LOGGER.warning(
                        "Background completion injection failed for run %s",
                        run.id,
                        exc_info=True,
                    )
                finally:
                    await session.flush_deferred_notes_async()
                    await context.session_snapshot.refresh(session)
                pending_notes = session.drain_pending_notes()
            if pending_notes:
                messages.extend(_notes_to_request_messages(pending_notes))
            extension_registry = self._dependencies.get_extension_registry()
            messages_for_request = [dict(message) for message in messages]
            if extension_registry is not None:
                session.begin_defer_notes()
                extension_ctx = HookContext(
                    session_id=run.session_id,
                    agent_id=run.agent_id,
                    run_id=run.id,
                    add_note=session.add_note,
                )
                try:
                    messages_for_request = await extension_registry.dispatch_context(
                        extension_ctx,
                        messages=messages_for_request,
                    )
                finally:
                    async with self._dependencies.sessions.write_lock(session_address):
                        await session.flush_deferred_notes_async()
                        await context.session_snapshot.refresh(session)

            # The next ordinal is derived from the canonical completed count.
            # Failed requests therefore do not consume an Iteration number.
            request_iteration_number = run.iteration_count + 1
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
                        iteration_number=request_iteration_number,
                    )
                )
            _LOGGER.debug(
                "Iteration %d requested (run=%s model=%s messages=%d)",
                request_iteration_number,
                run.id,
                target.model_id,
                len(messages_for_request),
            )
            self._raise_if_measured_context_exhausted(
                context.session_snapshot.active_messages,
                messages_for_request,
                [] if tool_finalization_reason is not None else tools,
                agent,
                run,
                target,
            )
            step_started_perf = time.perf_counter()
            workspace = getattr(agent, "workspace", None)
            output_cwd = (
                context.project_cwd
                if context.project_cwd is not None
                else Path(workspace)
                if workspace
                else None
            )
            assistant_step = await self._wire_requests.send_assistant_request(
                agent,
                target.adapter,
                target.model_id,
                target.model_reference,
                messages_for_request,
                [] if tool_finalization_reason is not None else tools,
                run,
                prompt_cache_affinity_id=context.prompt_cache_affinity_id,
                chunk_timeout_seconds=target.chunk_timeout_seconds,
                continuation_tracker=context.continuation_tracker,
                output_cwd=output_cwd,
                provider_id=target.provider_id,
            )
            # This is the sole mutation point for the Iteration count: one
            # completed request/response pair, independent of how many Tool
            # Calls or readable Assistant blocks the response contains.
            run.iteration_count = request_iteration_number
            assistant_message = assistant_step.message
            terminal_outcome = assistant_step.terminal_outcome
            recovery = assistant_step.recovery
            # Both an interrupted partial and a finished readable stream may
            # already be visible when Cancel arrives. The latter can race only
            # while acquiring the append lock; neither may vanish from History.
            preserve_after_cancel = assistant_message.interrupted or (
                self._streaming
                and not assistant_message.tool_calls
                and (
                    (
                        bool(assistant_message.content)
                        if isinstance(assistant_message.content, str)
                        else False
                    )
                    or bool(assistant_message.reasoning)
                )
            )
            if not preserve_after_cancel:
                run.raise_if_cancelled()
            assistant_message = await _CHAT_TRANSFORM_WORKERS.run(
                _prepare_completed_assistant,
                assistant_message,
                messages,
                output_cwd,
            )
            assistant_request_message = await _CHAT_TRANSFORM_WORKERS.run(
                _assistant_continuation_dict,
                assistant_message,
                replay_policy=replay_policy,
            )
            assert isinstance(assistant_message.usage, dict)
            assistant_context_usage = await _CHAT_TRANSFORM_WORKERS.run(
                build_model_step_context_usage,
                assistant_message.usage,
                [*messages_for_request, assistant_request_message],
            )
            run.input_token_total += _usage_token_count(assistant_message.usage, "input_tokens")
            run.output_token_total += _usage_token_count(assistant_message.usage, "output_tokens")
            _LOGGER.debug(
                "Iteration %d completed (run=%s duration_ms=%d input_tokens=%d "
                "output_tokens=%d tool_calls=%d)",
                request_iteration_number,
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
            repeated_failed_tool: str | None = None
            async with self._assistant_persistence_boundary(
                run,
                project_id=project_id,
                preserve_after_cancel=preserve_after_cancel,
            ):
                preserved_cancelled_output = run.cancel_requested and preserve_after_cancel
                await session.append_async(assistant_message)
                await context.session_snapshot.refresh(session)
                run.terminal_payload_extras["context_usage"] = assistant_context_usage
                session_usage = add_session_turn_usage(session_usage, assistant_message.usage)
                run.terminal_payload_extras["session_usage"] = session_usage
                run.emit(
                    MODEL_STEP_USAGE_EVENT,
                    {
                        "usage": dict(assistant_message.usage),
                        "session_usage": dict(session_usage),
                        "context_usage": dict(assistant_context_usage),
                        "iteration_count": run.iteration_count,
                    },
                    allow_after_cancel=preserved_cancelled_output,
                )
                if context.continuation_tracker is not None:
                    await context.continuation_tracker.record_assistant_boundary(
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
                messages.append(assistant_request_message)
                if (recovery != "none" or interruption_chain) and (
                    isinstance(assistant_message.content, str)
                    or assistant_message.reasoning is not None
                ):
                    interruption_chain.append(assistant_message)

                if not assistant_message.tool_calls:
                    if preserved_cancelled_output:
                        # The already visible response is durable; end the turn
                        # without new provider work (no auto-compaction). The Run
                        # manager sees ``cancel_requested`` and marks it cancelled.
                        return assistant_message
                    if recovery == "interrupt":
                        raise RunInterruptedError(
                            assistant_message.interruption_cause or "internal",
                            result=_combined_interrupted_result(
                                interruption_chain,
                                output_cwd=output_cwd,
                            ),
                        )
                    if recovery == "continue":
                        if stream_continuation_count >= MAX_STREAM_CONTINUATIONS:
                            raise RunInterruptedError(
                                assistant_message.interruption_cause or "internal",
                                result=_combined_interrupted_result(
                                    interruption_chain,
                                    output_cwd=output_cwd,
                                ),
                            )
                        await session.add_note_async(STREAM_RECOVERY_NOTE)
                        await context.session_snapshot.refresh(session)
                        stream_continuation_count += 1
                        continue
                    terminal_error = _terminal_outcome_error(
                        terminal_outcome,
                        has_tool_calls=False,
                    )
                    if terminal_error is not None:
                        raise terminal_error
                    break

                stream_continuation_count = 0
                finalization_violation = tool_finalization_reason is not None
                finalization_request_reason: str | None = None
                tool_limit_reached = (
                    not finalization_violation
                    and terminal_outcome == TERMINAL_OUTCOME_TOOL_CALLS
                    and tool_iteration_count >= self._max_tool_iterations
                )

                session.begin_defer_notes()
                try:
                    tool_dispatch_context = ToolDispatchContext(
                        registry=self._dependencies.tools,
                        extension_registry=self._dependencies.get_extension_registry(),
                        agent=agent,
                        session=session,
                        run=run,
                        nesting_depth=self._nesting_depth,
                        vbot_root=Path(self._dependencies.get_system_prompts().vbot_root),
                        data_root=Path(self._dependencies.storage.data_dir),
                        project_cwd=context.project_cwd,
                        project_id=project_id,
                        skill_project_id=context.skill_project_id,
                        skill_registry=context.skill_registry,
                        tool_restriction=context.request.tool_restriction,
                        tool_denial_resolver=context.request.tool_denial_resolver,
                        base_allowed_tools=state.allowed_tool_names,
                        session_tool_grants=state.session_tool_grants,
                        tool_contracts=state.tool_contracts,
                        change_tracker=self._dependencies.change_tracker,
                    )
                    terminal_error = _terminal_outcome_error(
                        terminal_outcome,
                        has_tool_calls=True,
                    )
                    will_dispatch_tools = (
                        terminal_outcome == TERMINAL_OUTCOME_TOOL_CALLS
                        and not finalization_violation
                        and not tool_limit_reached
                    )
                    if not will_dispatch_tools and context.continuation_tracker is not None:
                        await context.continuation_tracker.record_tool_starts(
                            assistant_message.tool_calls
                        )
                    if terminal_outcome == TERMINAL_OUTCOME_TOOL_CALLS:
                        if finalization_violation:
                            tool_messages = _fail_tool_calls_without_dispatch(
                                tool_dispatch_context,
                                assistant_message.tool_calls,
                                code=TOOL_FINALIZATION_DISABLED_FAILURE_CODE,
                                message=(
                                    "Tool execution is disabled for the remainder of this Run. "
                                    "This Tool was not executed; provide the final answer without "
                                    "issuing another Tool Call."
                                ),
                                retryable=False,
                            )
                            media_outputs: list[JsonObject] = []
                        elif tool_limit_reached:
                            finalization_request_reason = (
                                "the Run reached its limit of "
                                f"{self._max_tool_iterations} dispatched Tool iterations"
                            )
                            tool_messages = _fail_tool_calls_without_dispatch(
                                tool_dispatch_context,
                                assistant_message.tool_calls,
                                code=TOOL_ITERATION_LIMIT_FAILURE_CODE,
                                message=(
                                    f"The Run reached its limit of {self._max_tool_iterations} "
                                    "dispatched Tool iterations. This Tool was not executed; "
                                    "provide the final answer without issuing another Tool Call."
                                ),
                                retryable=False,
                            )
                            media_outputs = []
                        else:
                            tool_iteration_count += 1
                            tool_messages, media_outputs = await _dispatch_tool_calls(
                                tool_dispatch_context,
                                assistant_message.tool_calls,
                                continuation_tracker=context.continuation_tracker,
                            )
                    else:
                        failure_code, failure_message = _terminal_tool_failure(terminal_outcome)
                        tool_messages = _fail_tool_calls_without_dispatch(
                            tool_dispatch_context,
                            assistant_message.tool_calls,
                            code=failure_code,
                            message=failure_message,
                        )
                        media_outputs = []
                    tool_request_messages: list[JsonObject] = []
                    for tool_message in tool_messages:
                        assert tool_message.tool_call_id is not None
                        request_message = _message_to_request_dict(tool_message)
                        messages.append(request_message)
                        tool_request_messages.append(request_message)
                    repeated_failed_tool = failed_tool_call_breaker.observe(
                        assistant_message.tool_calls,
                        tool_messages,
                        tool_dispatch_context.registry,
                    )
                    if repeated_failed_tool is not None and finalization_request_reason is None:
                        finalization_request_reason = (
                            f"Tool {repeated_failed_tool!r} repeated the same failed Call "
                            f"{MAX_IDENTICAL_FAILED_TOOL_CALLS} times"
                        )
                    if finalization_request_reason is not None:
                        session.add_note(
                            TOOL_FINALIZATION_NOTE.format(reason=finalization_request_reason)
                        )
                    deferred_notes = session.take_deferred_notes()
                    await session.append_many_async([*tool_messages, *deferred_notes])
                    await context.session_snapshot.refresh(session)
                    for tool_message in tool_messages:
                        assert tool_message.tool_call_id is not None
                        tool_dispatch_context.notify_result_persisted(tool_message.tool_call_id)
                        loaded_project_id = project_tool_context_id(tool_message)
                        if project_id is None and loaded_project_id is not None:
                            await self._apply_project_skill_context(context, loaded_project_id)
                    if context.continuation_tracker is not None:
                        await context.continuation_tracker.record_tool_results(tool_messages)
                    if terminal_error is not None:
                        raise terminal_error
                    await self._attach_tool_result_content(
                        tool_request_messages,
                        media_outputs,
                        target.input_modalities,
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
                    await session.flush_deferred_notes_async()

            # Live git-style change statistics after each dispatched Tool round,
            # so the UI shows the same real totals during the Run that the
            # terminal payload will carry instead of summing per-call estimates.
            # Emitted only when the totals changed; best-effort like every
            # transient projection.
            if self._dependencies.change_tracker is not None:
                current_change_stats = self._dependencies.change_tracker.peek_run_stats(
                    run.session_id
                )
                if current_change_stats != emitted_change_stats:
                    emitted_change_stats = current_change_stats
                    run.emit(RUN_CHANGE_STATS_EVENT, {"change_stats": current_change_stats})

            continuation_request_messages = [
                *messages_for_request,
                assistant_request_message,
                *tool_request_messages,
            ]
            tool_context_usage = await _CHAT_TRANSFORM_WORKERS.run(
                build_model_step_context_usage,
                assistant_message.usage,
                continuation_request_messages,
                estimated_delta_messages=tool_request_messages,
            )
            run.terminal_payload_extras["context_usage"] = tool_context_usage

            if finalization_violation:
                tool_finalization_violation_count += 1
                if tool_finalization_violation_count >= MAX_TOOL_FINALIZATION_VIOLATIONS:
                    # The Model already received one correlated disabled-Tool
                    # failure and ignored the boundary again. Complete gracefully
                    # instead of creating an unbounded recovery loop.
                    return assistant_message
            if finalization_request_reason is not None:
                tool_finalization_reason = finalization_request_reason

            if self._compaction_service is not None:
                compacted_state = await self._compaction_runs.maybe_auto_compact_state(
                    context,
                    target,
                    usage=assistant_message.usage,
                    continuation_request_messages=continuation_request_messages,
                    context_usage=tool_context_usage,
                )
                context.request_state = compacted_state
                state = compacted_state
                messages = compacted_state.messages
                tools = compacted_state.tools

        if self._compaction_service is not None:
            await self._compaction_runs.maybe_auto_compact_state(
                context,
                target,
                usage=assistant_message.usage,
                continuation_request_messages=[
                    *messages_for_request,
                    assistant_request_message,
                ],
                context_usage=assistant_context_usage,
                allow_continuation=True,
                continue_same_run=False,
            )
        return assistant_message

    async def _attach_tool_result_content(
        self,
        tool_messages: list[JsonObject],
        media_outputs: list[JsonObject],
        input_modalities: frozenset[str],
        wire_media_types: frozenset[str],
    ) -> None:
        """Attach resolved media blocks to their correlated Tool Results.

        The persisted Tool message keeps only its compact result envelope. Base64
        blocks live exclusively in the in-flight request, so reading an image does
        not fabricate or persist a user turn.
        """

        if self._attachment_resolver is None or not media_outputs:
            return

        by_tool_call_id: dict[str, list[JsonObject]] = {}
        for media_output in media_outputs:
            tool_call_id = media_output.get("tool_call_id")
            if isinstance(tool_call_id, str):
                by_tool_call_id.setdefault(tool_call_id, []).append(media_output)

        for tool_message in tool_messages:
            tool_call_id = tool_message.get("tool_call_id")
            matching = (
                by_tool_call_id.get(tool_call_id, []) if isinstance(tool_call_id, str) else []
            )
            if not matching:
                continue
            content_blocks = [
                content_block_to_dict(
                    MediaBlock(
                        type="media",
                        attachment_id=media_output["attachment_id"],
                        filename=media_output["filename"],
                        media_type=media_output["media_type"],
                    )
                )
                for media_output in matching
            ]
            transient_message_id = f"tool-result:{tool_call_id}"
            resolved = await self._attachment_resolver.resolve_messages(
                [
                    {
                        "id": transient_message_id,
                        "role": "user",
                        "content": content_blocks,
                    }
                ],
                current_user_message_id=transient_message_id,
                input_modalities=input_modalities,
                wire_media_types=wire_media_types,
            )
            resolved_content = resolved[0].get("content")
            if isinstance(resolved_content, list):
                tool_message[TOOL_RESULT_CONTENT_BLOCKS_FIELD] = resolved_content

    def resolve_context_window(self, agent: Any) -> int | None:
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

        try:
            local_context_windows = self._dependencies.get_local_context_windows()
        except (AttributeError, KeyError):
            # Tolerant of a missing/partial runtime (test doubles may not
            # implement the local-model settings hook): treated as "no
            # user-configured local window overrides".
            local_context_windows = {}

        return resolve_effective_context_window(
            model_entry.context_window,
            self._lookup_provider_config(provider_id),
            model_metadata=model_entry.metadata,
            model_key=f"{provider_id}/{resolved_model_id}",
            # Live read through the runtime's single source of truth (no reload
            # hook, StorageError-tolerant), so a settings change applies to the
            # next request without re-implementing the storage read here.
            local_context_windows=local_context_windows,
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

    def _raise_if_measured_context_exhausted(
        self,
        session_messages: list[ChatMessage],
        request_messages: list[JsonObject],
        tools: list[JsonObject],
        agent: Any,
        run: Run,
        target: _ModelTarget,
    ) -> None:
        """Fail fast when measured Context Usage already fills the Model window.

        The Adapter owns the estimate for the exact wire it renders. A durable
        Provider measurement is an independent stronger signal when it is
        higher, so the guard uses the larger of the two rather than adding
        System Prompt or Tool overhead a second time to a measured request.
        """

        context_window = self.resolve_context_window(agent)
        if context_window is None:
            return
        projection = latest_session_context_usage(session_messages)
        if projection is None or "provider_input_tokens" not in projection:
            return
        wire_tokens = estimate_wire_request_input_tokens(
            target.adapter,
            request_messages,
            model_id=target.model_id,
            tools=tools,
        )
        measured_tokens = int(projection["tokens"])
        projected_tokens = max(measured_tokens, wire_tokens)
        if projected_tokens < context_window:
            return
        _LOGGER.error(
            "Run %s pre-send context guard tripped (agent=%s session=%s "
            "projected_input_tokens=%d context_window=%d)",
            run.id,
            run.agent_id,
            run.session_id,
            projected_tokens,
            context_window,
        )
        raise ProviderError(
            "Context usage leaves no output capacity in the Model "
            f"context window (projected_input_tokens={projected_tokens}, "
            f"context_window={context_window})",
            retryable=False,
        )

    def resolve_summary_adapter(
        self,
        agent: Any,
        adapter: Any,
        model_id: str,
        settings: Any,
        *,
        active_provider_id: str,
    ) -> tuple[Any, str, str]:
        """Resolve compaction summary adapter/model/provider, defaulting to active."""
        del agent

        summary_model = settings.summary_model
        if not isinstance(summary_model, str) or not summary_model:
            return adapter, model_id, active_provider_id

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
            summary_adapter = self._dependencies.get_adapter(
                ConnectionRef(provider_id, connection_id)
            )
        except (ChatError, ConfigError, VBotError, KeyError):
            _LOGGER.warning(
                "Invalid compaction summary model %r; using active run model instead.",
                summary_model,
                exc_info=True,
            )
            return adapter, model_id, active_provider_id

        return summary_adapter, summary_model_id, provider_id
