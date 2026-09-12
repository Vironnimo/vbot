"""Policy-driven compaction engine for provider-neutral chat Context."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from functools import partial
from typing import Any, Literal, Protocol, cast

from core.chat import (
    compaction_projection_without_active_skills,
    compaction_projection_without_provider_state,
    effective_compaction_messages,
    latest_compaction_checkpoint,
)
from core.chat.messages import (
    COMPACTION_SKILL_NOTE_PREFIX,
    COMPACTION_SUMMARY_END_MARKER,
    COMPACTION_SUMMARY_NOTE_PREFIX,
    TOOL_RESULT_COMPACTED_FIELD,
    ChatMessage,
    JsonObject,
)
from core.chat.streaming import StreamingAccumulator
from core.chat.wire_shaping import (
    SYSTEM_REMINDER_CLOSE_TAG,
    SYSTEM_REMINDER_OPEN_TAG,
    _notes_to_request_messages,
)
from core.providers.adapter import TERMINAL_OUTCOME_STOP, estimate_wire_request_input_tokens
from core.sessions import SessionAddress, current_skill_activation_contents, skill_tool_activation
from core.utils.errors import VBotError
from core.utils.tokens import estimate_message_tokens, estimate_request_input_tokens
from core.utils.workers import BoundedWorkerPool

TRIGGER_CONTEXT_RATIO = "context_ratio"
TRIGGER_INPUT_TOKENS = "input_tokens"
STRATEGY_SUMMARY_TAIL = "summary_tail"
STRATEGY_CONTINUATION = "continuation"
COMPACTION_POLICY_META_KEY = "compaction_policy"
_LEGACY_COMPACTION_TAIL_GUIDANCE = (
    "The messages below are the most recent verbatim Session activity retained after this "
    "Compaction checkpoint. They chronologically follow the summary above."
)
COMPACTION_REFERENCE_PREFIX = (
    "[CONTEXT COMPACTION] The summary below records the conversation and task state up to "
    "a cutoff. The historical User quote attached to this checkpoint, if present, belongs "
    "to that summarized history; it is not a new request. The retained conversation "
    "messages after this checkpoint follow the cutoff in their original order and may "
    "advance or correct the summarized state. When continuing unfinished work, resume "
    "from the latest state across the summary and those messages, following any later "
    "User updates. Do not restart the task or repeat completed actions merely because "
    "they appear in the summary or quote:"
)
COMPACTION_USER_QUOTE_PREFIX = (
    "Latest User message in the summarized history "
    "(JSON-quoted; already received, not a new request):\n"
)
COMPACTION_TRIGGER_AUTO = "auto"
COMPACTION_TRIGGER_MANUAL = "manual"
COMPACTION_TRIGGERS = frozenset({COMPACTION_TRIGGER_AUTO, COMPACTION_TRIGGER_MANUAL})
SKILL_COMPACTION_GUIDANCE = (
    "Skills active before this Compaction: {skill_names_json}. Their instructions and "
    "environment access are no longer active after this checkpoint. If a Skill is still "
    "relevant, load it again by name with the `skill` Tool before following it."
)

MIN_AUTO_COMPACTION_RECLAIM_TOKENS = 4_096
COMPACTION_WORKER_LIMIT = 4

_COMPACTION_WORKERS = BoundedWorkerPool(
    name="compaction",
    max_workers=COMPACTION_WORKER_LIMIT,
)

ModelTarget = Literal["active", "summary"]
RequestTokenEstimator = Callable[[Sequence[Mapping[str, Any]]], int]


@dataclass(frozen=True)
class CompactionSettings:
    """Resolved policy settings used by Chat until persisted policy resolution lands."""

    auto: bool = True
    threshold: float = 0.8
    tail_tokens: int = 15_000
    summary_model: str | None = None
    trigger: str = TRIGGER_CONTEXT_RATIO
    trigger_tokens: int = 100_000
    max_input_tokens: int | None = None
    strategy: str = STRATEGY_SUMMARY_TAIL


@dataclass(frozen=True)
class CompactionTriggerContext:
    """Measured input available to a Trigger."""

    input_tokens: int
    context_window: int


class CompactionTrigger(Protocol):
    """Decides whether one resolved Policy should compact now."""

    def should_compact(
        self, context: CompactionTriggerContext, settings: CompactionSettings
    ) -> bool:
        """Return whether the Strategy should execute."""


class ContextRatioTrigger:
    """Trigger at a Model Context fraction or its optional absolute cap."""

    def should_compact(
        self, context: CompactionTriggerContext, settings: CompactionSettings
    ) -> bool:
        if context.context_window <= 0:
            return False
        ratio_reached = (context.input_tokens / context.context_window) >= settings.threshold
        cap_reached = (
            settings.max_input_tokens is not None
            and context.input_tokens >= settings.max_input_tokens
        )
        return ratio_reached or cap_reached


class InputTokensTrigger:
    """Trigger at an absolute input-token count."""

    def should_compact(
        self, context: CompactionTriggerContext, settings: CompactionSettings
    ) -> bool:
        return context.input_tokens >= settings.trigger_tokens


@dataclass(frozen=True)
class CompactionPlan:
    """One Strategy result: zero or one Model call and one ordered projection."""

    model_messages: tuple[JsonObject, ...] | None
    model_target: ModelTarget
    before_summary: tuple[ChatMessage, ...] = ()
    after_summary: tuple[ChatMessage, ...] = ()
    summary_text: str = ""
    user_quote: ChatMessage | None = None
    compacted_token_count: int = 0


@dataclass(frozen=True)
class CompactionContext:
    """Current effective Context supplied to a Strategy."""

    messages: tuple[ChatMessage, ...]
    request_messages: tuple[JsonObject, ...]
    previous_compacted_token_count: int
    instruction: str | None
    storage: Any
    estimate_tail_tokens: RequestTokenEstimator | None = None
    trigger: str = COMPACTION_TRIGGER_AUTO


@dataclass(frozen=True)
class _TailPlan:
    """One bounded working-Tail projection and its canonical suffix boundary."""

    boundary_id: str
    boundary_index: int
    projected_suffix: tuple[ChatMessage, ...]

    @property
    def retained_messages(self) -> tuple[ChatMessage, ...]:
        return self.projected_suffix


@dataclass(frozen=True)
class _PreparedCompaction:
    plan: CompactionPlan
    effective_messages: list[ChatMessage]
    strategy_id: str
    active_skill_names: tuple[str, ...]
    activation_result_names: tuple[tuple[str, str], ...]


class CompactionStrategy(Protocol):
    """Builds one CompactionPlan without performing Model I/O."""

    id: str

    def plan(self, context: CompactionContext, settings: CompactionSettings) -> CompactionPlan:
        """Return the single-call-or-less Context transformation plan."""


class CompactionError(VBotError):
    """Raised when a compaction plan cannot be produced or executed."""


class CompactionInsufficientReclaimError(CompactionError):
    """Raised when an automatic checkpoint would not reclaim enough Context."""


def find_tail_boundary(messages: list[ChatMessage], tail_tokens: int) -> str:
    """Return the canonical boundary of the bounded chronological Tail suffix."""

    return _plan_working_tail(messages, tail_tokens).boundary_id


def _fragment_name_for_trigger(trigger: str) -> str:
    """Return the compaction instruction fragment for one trigger scenario."""
    return "compaction-manual.md" if trigger == COMPACTION_TRIGGER_MANUAL else "compaction.md"


class SummarizationStrategy:
    """Summarize an exact provider-request prefix and retain one safe canonical tail."""

    id = STRATEGY_SUMMARY_TAIL

    def plan(self, context: CompactionContext, settings: CompactionSettings) -> CompactionPlan:
        messages = list(context.messages)
        if not messages:
            raise CompactionError("Cannot compact an empty Context")
        tail_plan = _plan_working_tail(
            messages,
            settings.tail_tokens,
            request_messages=context.request_messages,
            estimate_tail_tokens=context.estimate_tail_tokens,
        )
        head = messages[: tail_plan.boundary_index]
        request_prefix = _request_prefix_before_tail(
            context.request_messages,
            tail_plan.boundary_id,
        )
        prompt = _build_compaction_instruction(
            context.storage.read_prompt_fragment(_fragment_name_for_trigger(context.trigger)),
            context.instruction,
        )
        return CompactionPlan(
            model_messages=(
                *request_prefix,
                _system_reminder_request_message(prompt),
            ),
            model_target="summary",
            after_summary=tail_plan.retained_messages,
            user_quote=_summary_user_quote(head, tail_plan.retained_messages),
            compacted_token_count=(
                context.previous_compacted_token_count
                + _estimate_token_span(
                    [message for message in head if not _is_compaction_checkpoint_note(message)]
                )
            ),
        )


class ContinuationStrategy:
    """Cache-preserving compaction that continues the active request verbatim."""

    id = STRATEGY_CONTINUATION

    def plan(self, context: CompactionContext, settings: CompactionSettings) -> CompactionPlan:
        del settings
        if not context.request_messages:
            raise CompactionError("Continuation compaction requires an active request Context")
        base_instruction = context.storage.read_prompt_fragment(
            "compaction-continuation-manual.md"
            if context.trigger == COMPACTION_TRIGGER_MANUAL
            else "compaction-continuation.md"
        ).strip()
        instruction = (context.instruction or "").strip()
        suffix = base_instruction
        if instruction:
            suffix = f"{suffix}\n\nAdditional instruction: {instruction}"
        reminder = (
            "Create the next compaction checkpoint now. Return only the compacted "
            "context text that the agent should retain for continuing this Session.\n\n"
            f"{suffix}"
        )
        model_messages = (
            *context.request_messages,
            _system_reminder_request_message(reminder),
        )
        return CompactionPlan(
            model_messages=tuple(model_messages),
            model_target="active",
            compacted_token_count=(
                context.previous_compacted_token_count
                + _estimate_token_span(list(context.messages))
            ),
        )


class CompactionService:
    """Registry-backed Engine that executes and validates one Strategy plan."""

    def __init__(
        self,
        strategies: tuple[CompactionStrategy, ...] | CompactionStrategy | None = None,
        triggers: dict[str, CompactionTrigger] | None = None,
    ) -> None:
        if strategies is None:
            resolved_strategies: tuple[CompactionStrategy, ...] = (
                SummarizationStrategy(),
                ContinuationStrategy(),
            )
        elif isinstance(strategies, tuple):
            resolved_strategies = strategies
        else:
            resolved_strategies = (strategies,)
        self._strategies = {strategy.id: strategy for strategy in resolved_strategies}
        self._triggers = triggers or {
            TRIGGER_CONTEXT_RATIO: ContextRatioTrigger(),
            TRIGGER_INPUT_TOKENS: InputTokensTrigger(),
        }

    def should_auto_compact(
        self,
        input_tokens: int,
        context_window: int,
        threshold: float,
        *,
        settings: CompactionSettings | None = None,
    ) -> bool:
        """Evaluate the resolved Policy Trigger."""
        resolved = settings or CompactionSettings(threshold=threshold)
        trigger = self._triggers.get(resolved.trigger)
        if trigger is None:
            raise CompactionError(f"Unknown compaction trigger: {resolved.trigger}")
        return trigger.should_compact(
            CompactionTriggerContext(input_tokens=input_tokens, context_window=context_window),
            resolved,
        )

    def has_new_compactable_context(
        self,
        messages: list[ChatMessage],
        settings: CompactionSettings,
        *,
        request_messages: list[JsonObject] | None = None,
        active_adapter: Any | None = None,
        active_model_id: str | None = None,
    ) -> bool:
        """Return whether automatic Compaction has a non-summary Head to replace."""

        strategy = self._strategies.get(settings.strategy)
        if strategy is None:
            raise CompactionError(f"Unknown compaction strategy: {settings.strategy}")
        if strategy.id != STRATEGY_SUMMARY_TAIL:
            return True

        effective = effective_compaction_messages(messages)
        if not effective:
            return False
        try:
            tail_plan = _plan_working_tail(
                effective,
                settings.tail_tokens,
                request_messages=tuple(request_messages) if request_messages is not None else None,
                estimate_tail_tokens=_tail_estimator(active_adapter, active_model_id),
            )
        except CompactionError:
            return False
        compactable_prefix = effective[: tail_plan.boundary_index]
        return any(not _is_compaction_checkpoint_note(message) for message in compactable_prefix)

    async def compact(
        self,
        messages: list[ChatMessage],
        *,
        session_address: SessionAddress,
        prompt_cache_affinity_id: str,
        summary_adapter: Any,
        summary_model_id: str,
        storage: Any,
        settings: CompactionSettings,
        instruction: str | None = None,
        request_messages: list[JsonObject] | None = None,
        trigger: str = COMPACTION_TRIGGER_AUTO,
        active_adapter: Any | None = None,
        active_model_id: str | None = None,
        active_tools: list[JsonObject] | None = None,
        minimum_reclaim_tokens: int = 0,
        summary_temperature: float | None = None,
        active_temperature: float | None = None,
    ) -> ChatMessage:
        """Execute at most one Model request and persist its assembled projection."""
        if trigger not in COMPACTION_TRIGGERS:
            raise CompactionError(f"Unknown compaction trigger: {trigger}")
        if minimum_reclaim_tokens < 0:
            raise CompactionError("minimum_reclaim_tokens cannot be negative")
        try:
            prepared = await _COMPACTION_WORKERS.run(
                self._prepare_compaction,
                messages,
                storage=storage,
                settings=settings,
                instruction=instruction,
                trigger=trigger,
                request_messages=request_messages,
                estimate_tail_tokens=_tail_estimator(active_adapter, active_model_id),
            )
            plan = prepared.plan
            response: JsonObject | None = None
            if plan.model_messages is not None:
                adapter, model_id = _plan_model_target(
                    plan,
                    summary_adapter=summary_adapter,
                    summary_model_id=summary_model_id,
                    active_adapter=active_adapter,
                    active_model_id=active_model_id,
                )
                # Internal task, not the agent's voice: only the model/provider
                # tiers of the temperature chain apply (None = unspecified, so
                # provider-config defaults or the API default reach the wire).
                request_options: dict[str, Any] = {
                    "model_id": model_id,
                    "temperature": (
                        summary_temperature
                        if plan.model_target == "summary"
                        else active_temperature
                    ),
                    "thinking_effort": "",
                }
                if hasattr(adapter, "request_context_kwargs"):
                    request_options.update(
                        adapter.request_context_kwargs(
                            agent_id=session_address.agent_id,
                            session_id=session_address.session_id,
                            project_id=session_address.project_id,
                            prompt_cache_affinity_id=prompt_cache_affinity_id,
                        )
                    )
                if active_tools is not None:
                    request_options["tools"] = list(active_tools)
                model_messages = await _COMPACTION_WORKERS.run(
                    _prepare_compaction_model_messages,
                    plan,
                    strip_reasoning=(adapter is not active_adapter or model_id != active_model_id),
                )
                response = await _send_streaming_model_request(
                    adapter, model_messages, request_options
                )
            return await _COMPACTION_WORKERS.run(
                _finalize_compaction,
                prepared,
                response=response,
                minimum_reclaim_tokens=minimum_reclaim_tokens,
            )
        except CompactionError:
            raise
        except Exception as exc:
            raise CompactionError(f"Compaction failed: {exc}") from exc

    def _prepare_compaction(
        self,
        messages: list[ChatMessage],
        *,
        storage: Any,
        settings: CompactionSettings,
        instruction: str | None,
        request_messages: list[JsonObject] | None,
        trigger: str = COMPACTION_TRIGGER_AUTO,
        estimate_tail_tokens: RequestTokenEstimator | None = None,
    ) -> _PreparedCompaction:
        """Build and validate the sync Strategy plan inside the Compaction pool."""
        strategy = self._strategies.get(settings.strategy)
        if strategy is None:
            raise CompactionError(f"Unknown compaction strategy: {settings.strategy}")
        effective = effective_compaction_messages(messages)
        checkpoint = latest_compaction_checkpoint(messages)
        context = CompactionContext(
            messages=tuple(effective),
            request_messages=tuple(dict(message) for message in request_messages or []),
            previous_compacted_token_count=_previous_compacted_token_count(checkpoint),
            instruction=instruction,
            storage=storage,
            estimate_tail_tokens=estimate_tail_tokens,
            trigger=trigger,
        )
        plan = strategy.plan(context, settings)
        _validate_plan(plan)
        active_skill_names = tuple(current_skill_activation_contents(messages))
        activation_result_names = tuple(
            (message.id, activation[0])
            for message in messages
            if (activation := skill_tool_activation(message)) is not None
        )
        return _PreparedCompaction(
            plan=plan,
            effective_messages=effective,
            strategy_id=strategy.id,
            active_skill_names=active_skill_names,
            activation_result_names=activation_result_names,
        )

    def estimate_messages_tokens(self, messages: list[dict]) -> int:
        estimated_tokens, _ = estimate_request_input_tokens(messages)
        return estimated_tokens


def _prepare_compaction_model_messages(
    plan: CompactionPlan,
    *,
    strip_reasoning: bool,
) -> list[JsonObject]:
    if plan.model_messages is None:
        raise CompactionError("Compaction plan has no Model request")
    model_messages = [dict(message) for message in plan.model_messages]
    if strip_reasoning:
        _strip_assistant_reasoning_fields(model_messages)
    return model_messages


def _finalize_compaction(
    prepared: _PreparedCompaction,
    *,
    response: JsonObject | None,
    minimum_reclaim_tokens: int,
) -> ChatMessage:
    """Extract the canonical summary, validate the Projection, and estimate reclaim."""
    plan = prepared.plan
    summary = plan.summary_text
    if response is not None:
        summary = _extract_summary_text(response)
    if summary and prepared.strategy_id == STRATEGY_SUMMARY_TAIL:
        summary = _reference_summary(summary)
    projection = [*plan.before_summary]
    if summary:
        projection.append(ChatMessage.note(f"{COMPACTION_SUMMARY_NOTE_PREFIX}{summary}"))
    if plan.user_quote is not None:
        projection.append(plan.user_quote)
    projection.extend(plan.after_summary)
    projection = compaction_projection_without_active_skills(
        [
            message
            for message in projection
            if not (
                message.role == "note"
                and isinstance(message.content, str)
                and message.content.startswith(COMPACTION_SKILL_NOTE_PREFIX)
            )
        ],
        activation_result_names=dict(prepared.activation_result_names),
    )
    if prepared.active_skill_names:
        guidance = SKILL_COMPACTION_GUIDANCE.format(
            skill_names_json=json.dumps(
                list(prepared.active_skill_names),
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
        projection = _append_compaction_skill_guidance(projection, guidance)
    _validate_projection(projection)
    reclaimed_tokens = _estimate_token_span(prepared.effective_messages) - _estimate_token_span(
        projection
    )
    if minimum_reclaim_tokens > 0 and reclaimed_tokens < minimum_reclaim_tokens:
        raise CompactionInsufficientReclaimError(
            "Compaction projection reclaimed "
            f"{max(0, reclaimed_tokens)} tokens; "
            f"minimum is {minimum_reclaim_tokens}"
        )
    return ChatMessage.compaction_checkpoint(
        summary=summary,
        projection=projection,
        compacted_token_count=plan.compacted_token_count,
        policy=prepared.strategy_id,
        strategy=prepared.strategy_id,
    )


def _append_compaction_skill_guidance(
    projection: list[ChatMessage],
    guidance: str,
) -> list[ChatMessage]:
    guided = list(projection)
    summary_index = next(
        (index for index, message in enumerate(guided) if _is_compaction_summary_note(message)),
        None,
    )
    if summary_index is None:
        return guided
    guided.insert(
        summary_index + 1,
        ChatMessage.note(f"{COMPACTION_SKILL_NOTE_PREFIX}{guidance}"),
    )
    return guided


def _tail_estimator(adapter: Any | None, model_id: str | None) -> RequestTokenEstimator | None:
    if adapter is None or model_id is None:
        return None
    return partial(estimate_wire_request_input_tokens, adapter, model_id=model_id)


def _plan_working_tail(
    messages: list[ChatMessage],
    tail_tokens: int,
    *,
    request_messages: tuple[JsonObject, ...] | None = None,
    estimate_tail_tokens: RequestTokenEstimator | None = None,
) -> _TailPlan:
    """Keep a chronological suffix of whole steps within the request-side budget.

    Only the newest indivisible step may exceed the budget. There are no User
    or older Assistant anchors and no payload edits inside retained steps.
    Live request slices include replayed reasoning and request-only Tool media;
    the selected Adapter counts the representation it will actually serialize.
    """
    if not messages:
        raise CompactionError("Cannot find tail boundary for an empty message list")
    if tail_tokens <= 0:
        raise CompactionError("tail_tokens must be positive")
    safe_boundaries = _safe_tail_boundary_indices(messages)
    if not safe_boundaries:
        raise CompactionError("Cannot find a provider-safe tail boundary")

    request_indices = (
        {message.get("id"): index for index, message in enumerate(request_messages)}
        if request_messages is not None
        else {}
    )
    selected_start = safe_boundaries[-1]
    for boundary_index in reversed(safe_boundaries):
        if request_messages is None:
            candidate = [message.to_dict() for message in messages[boundary_index:]]
        else:
            request_index = request_indices.get(messages[boundary_index].id)
            if request_index is None:
                raise CompactionError("Tail boundary was not found in the active request Context")
            candidate = list(request_messages[request_index:])
        tokens = (
            estimate_tail_tokens(candidate)
            if estimate_tail_tokens is not None
            else estimate_request_input_tokens(candidate)[0]
        )
        if boundary_index != safe_boundaries[-1] and tokens > tail_tokens:
            break
        selected_start = boundary_index
        if tokens >= tail_tokens:
            break

    return _TailPlan(
        boundary_id=messages[selected_start].id,
        boundary_index=selected_start,
        projected_suffix=tuple(
            compaction_projection_without_provider_state(messages[selected_start:])
        ),
    )


def _safe_tail_boundary_indices(messages: list[ChatMessage]) -> list[int]:
    boundaries: list[int] = []
    for index, message in enumerate(messages):
        if not _can_start_tail(message):
            continue
        try:
            _validate_projection(messages[index:])
        except CompactionError:
            continue
        boundaries.append(index)
    return boundaries


def _summary_user_quote(
    head: list[ChatMessage], tail: tuple[ChatMessage, ...]
) -> ChatMessage | None:
    """Carry one exact historical User quote without reopening hidden history."""
    if any(message.role == "user" for message in tail):
        return None
    for message in reversed(head):
        if message.role == "user":
            # JSON preserves the original content, attribution and timestamp.
            # Escape reminder delimiters even inside a malicious quoted string.
            quoted = json.dumps(message.to_dict(), ensure_ascii=False, separators=(",", ":"))
            quoted = quoted.replace("<", "\\u003c").replace(">", "\\u003e")
            return ChatMessage.note(f"{COMPACTION_USER_QUOTE_PREFIX}{quoted}")
        if _is_compaction_user_quote(message):
            return message
    return None


def _is_compaction_user_quote(message: ChatMessage) -> bool:
    return (
        message.role == "note"
        and isinstance(message.content, str)
        and message.content.startswith(COMPACTION_USER_QUOTE_PREFIX)
    )


def _is_compaction_summary_note(message: ChatMessage) -> bool:
    return (
        message.role == "note"
        and isinstance(message.content, str)
        and message.content.startswith(COMPACTION_SUMMARY_NOTE_PREFIX)
    )


def _is_compaction_checkpoint_note(message: ChatMessage) -> bool:
    return (
        _is_compaction_summary_note(message)
        or _is_compaction_user_quote(message)
        or (
            message.role == "note"
            and (
                message.content == _LEGACY_COMPACTION_TAIL_GUIDANCE
                or (
                    isinstance(message.content, str)
                    and message.content.startswith(COMPACTION_SKILL_NOTE_PREFIX)
                )
            )
        )
    )


def _plan_model_target(
    plan: CompactionPlan,
    *,
    summary_adapter: Any,
    summary_model_id: str,
    active_adapter: Any | None,
    active_model_id: str | None,
) -> tuple[Any, str]:
    if plan.model_target == "summary":
        return summary_adapter, summary_model_id
    if active_adapter is None or not active_model_id:
        raise CompactionError("Continuation compaction requires the active Model target")
    return active_adapter, active_model_id


def _validate_plan(plan: CompactionPlan) -> None:
    if (
        plan.model_messages is None
        and not plan.summary_text
        and not (plan.before_summary or plan.after_summary)
    ):
        raise CompactionError("Compaction plan cannot produce an empty Context")
    if plan.model_messages is not None and not plan.model_messages:
        raise CompactionError("Compaction Model request cannot be empty")
    if plan.compacted_token_count < 0:
        raise CompactionError("Compacted token count cannot be negative")


def _validate_projection(messages: list[ChatMessage]) -> None:
    pending: set[str] = set()
    for message in messages:
        if message.role in {"note", "run_summary", "agent_takeover", "error"}:
            continue
        if message.role == "assistant":
            if pending:
                raise CompactionError("Compaction projection splits an unresolved Tool cycle")
            pending = {call.id for call in message.tool_calls or []}
            continue
        if message.role == "tool":
            if message.tool_call_id not in pending:
                raise CompactionError("Compaction projection contains an orphan Tool result")
            pending.remove(cast(str, message.tool_call_id))
            continue
        if pending:
            raise CompactionError("Compaction projection splits an unresolved Tool cycle")
    if pending:
        raise CompactionError("Compaction projection ends inside a Tool cycle")


def _can_start_tail(message: ChatMessage) -> bool:
    """Return whether a canonical message has a stable provider-visible boundary."""

    if message.role == "user":
        return True
    return message.role == "assistant" and (message.content is not None or bool(message.tool_calls))


def _estimate_token_span(messages: list[ChatMessage]) -> int:
    return sum(_estimate_message_tokens(message) for message in messages)


def _estimate_message_tokens(message: ChatMessage) -> int:
    estimated_tokens, _ = estimate_message_tokens(message.to_dict())
    return estimated_tokens


def _request_prefix_before_tail(
    request_messages: tuple[JsonObject, ...],
    tail_boundary_id: str,
) -> tuple[JsonObject, ...]:
    """Slice the already-built provider request immediately before the Tail."""

    if not request_messages:
        raise CompactionError("Summary+Tail compaction requires an active request Context")
    for index, message in enumerate(request_messages):
        if message.get("id") == tail_boundary_id:
            return tuple(dict(item) for item in request_messages[:index])
    raise CompactionError(
        f"Tail boundary was not found in the active request Context: {tail_boundary_id}"
    )


def _system_reminder_request_message(content: str) -> JsonObject:
    """Render through Chat's canonical System Reminder request channel."""

    rendered = _notes_to_request_messages([ChatMessage.note(content)])
    if len(rendered) != 1:
        raise CompactionError("Compaction System Reminder must render as one request message")
    return rendered[0]


def _reference_summary(summary: str) -> str:
    """Frame the cutoff and continuation semantics of one historical summary."""

    body = _strip_outer_system_reminder_tags(summary)
    if body.startswith(COMPACTION_REFERENCE_PREFIX):
        body = body.removeprefix(COMPACTION_REFERENCE_PREFIX).lstrip()
    if body.endswith(COMPACTION_SUMMARY_END_MARKER):
        body = body.removesuffix(COMPACTION_SUMMARY_END_MARKER).rstrip()
    body = _strip_outer_system_reminder_tags(body)
    return f"{COMPACTION_REFERENCE_PREFIX}\n{body}\n{COMPACTION_SUMMARY_END_MARKER}"


def _strip_outer_system_reminder_tags(summary: str) -> str:
    """Discard reminder delimiters copied from the summary request boundary."""

    lines = summary.strip().splitlines()
    while lines and lines[0].strip() == SYSTEM_REMINDER_OPEN_TAG:
        lines.pop(0)
    while lines and lines[-1].strip() == SYSTEM_REMINDER_CLOSE_TAG:
        lines.pop()
    return "\n".join(lines).strip()


def _strip_assistant_reasoning_fields(messages: list[JsonObject]) -> None:
    """Remove Provider-owned reasoning state before a different Model target."""

    for message in messages:
        if message.get("role") != "assistant":
            continue
        message.pop("reasoning", None)
        message.pop("reasoning_meta", None)
        message.pop("reasoning_scope", None)


def is_compacted_tool_result_content(content: Any) -> bool:
    """Recognize a deterministic Tool Result digest in a checkpoint."""

    if not isinstance(content, str):
        return False
    try:
        parsed = json.loads(content)
    except (TypeError, ValueError):
        return False
    return isinstance(parsed, dict) and parsed.get(TOOL_RESULT_COMPACTED_FIELD) is True


def _previous_compacted_token_count(checkpoint: ChatMessage | None) -> int:
    if checkpoint is None or not isinstance(checkpoint.usage, dict):
        return 0
    count = checkpoint.usage.get("compacted_token_count")
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        return 0
    return count


def _build_compaction_instruction(
    prompt_fragment: str,
    instruction: str | None = None,
) -> str:
    sections = [prompt_fragment.strip()]
    if instruction and instruction.strip():
        sections.append(f"<user_instruction>\n{instruction.strip()}\n</user_instruction>")
    return "\n\n".join(sections)


async def _send_streaming_model_request(
    adapter: Any,
    messages: list[dict[str, Any]],
    request_options: dict[str, Any],
) -> dict[str, Any]:
    """Consume one canonical stream, accepting only a completed text response.

    Some providers (observed on OpenRouter's stealth tier) reject large
    non-streaming completions outright while streaming the same payload fine,
    so Compaction always streams. Adapter deltas are already normalized and must
    never be passed back through a raw-wire response parser.
    """
    accumulator = StreamingAccumulator()
    async for delta in adapter.stream(messages, **request_options):
        if delta.get("type") != "heartbeat":
            accumulator.add_delta(delta)
    if accumulator.finish_reason != TERMINAL_OUTCOME_STOP:
        raise CompactionError(
            "Summary stream did not complete successfully "
            f"(outcome={accumulator.finish_reason or 'missing'})"
        )
    if accumulator.has_partial_tool_call:
        raise CompactionError("Summary stream requested Tools instead of completing its summary")
    return accumulator.finalize_assistant_fields().to_response_dict()


def _extract_summary_text(response: dict[str, Any]) -> str:
    content = response.get("content")
    if isinstance(content, str) and content.strip():
        return content.strip()
    if isinstance(content, list):
        chunks = [
            item if isinstance(item, str) else item.get("text", "")
            for item in content
            if isinstance(item, (str, dict))
        ]
        summary = "\n".join(chunk for chunk in chunks if isinstance(chunk, str) and chunk).strip()
        if summary:
            return summary
    raise CompactionError("Summary response did not include text content")
