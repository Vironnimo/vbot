"""Policy-driven compaction engine for provider-neutral chat Context."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal, Protocol, cast

from core.chat.messages import (
    COMPACTION_SUMMARY_NOTE_PREFIX,
    ChatMessage,
    JsonObject,
    _effective_compaction_messages,
    _latest_compaction_checkpoint,
)
from core.utils.errors import VBotError
from core.utils.tokens import estimate_message_tokens, estimate_request_input_tokens

# Reader compatibility for checkpoints written by the removed Tool-aging projection.
TOOL_RESULT_COMPACTED_FIELD = "_vbot_compacted_tool_result"

TRIGGER_CONTEXT_RATIO = "context_ratio"
TRIGGER_INPUT_TOKENS = "input_tokens"
STRATEGY_SUMMARY_TAIL = "summary_tail"
STRATEGY_CONTINUATION = "continuation"
COMPACTION_POLICY_META_KEY = "compaction_policy"

MIN_AUTO_COMPACTION_RECLAIM_TOKENS = 4_096

ModelTarget = Literal["active", "summary"]


@dataclass(frozen=True)
class CompactionSettings:
    """Resolved policy settings used by Chat until persisted policy resolution lands."""

    auto: bool = True
    threshold: float = 0.8
    tail_tokens: int = 15_000
    summary_model: str | None = None
    trigger: str = TRIGGER_CONTEXT_RATIO
    trigger_tokens: int = 100_000
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
    """Trigger at a configured fraction of the Model Context window."""

    def should_compact(
        self, context: CompactionTriggerContext, settings: CompactionSettings
    ) -> bool:
        if context.context_window <= 0:
            return False
        return (context.input_tokens / context.context_window) >= settings.threshold


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
    compacted_token_count: int = 0


@dataclass(frozen=True)
class CompactionContext:
    """Current effective Context supplied to a Strategy."""

    messages: tuple[ChatMessage, ...]
    request_messages: tuple[JsonObject, ...]
    previous_compacted_token_count: int
    instruction: str | None
    storage: Any


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
    """Return the provider-visible message id where the safe retained tail starts."""
    if not messages:
        raise CompactionError("Cannot find tail boundary for an empty message list")
    if tail_tokens <= 0:
        raise CompactionError("tail_tokens must be positive")

    fallback_id: str | None = None
    accumulated_tokens = 0
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        accumulated_tokens += _estimate_message_tokens(message)
        if not _can_start_tail(message):
            continue
        try:
            _validate_projection(messages[index:])
        except CompactionError:
            continue
        fallback_id = message.id
        if accumulated_tokens >= tail_tokens:
            return message.id
    if fallback_id is not None:
        return fallback_id
    raise CompactionError("Cannot find a provider-safe tail boundary")


class SummarizationStrategy:
    """Summarize an exact provider-request prefix and retain one safe canonical tail."""

    id = STRATEGY_SUMMARY_TAIL

    def plan(self, context: CompactionContext, settings: CompactionSettings) -> CompactionPlan:
        messages = list(context.messages)
        if not messages:
            raise CompactionError("Cannot compact an empty Context")
        boundary_id = find_tail_boundary(messages, settings.tail_tokens)
        boundary_index = _find_boundary_index(messages, boundary_id)
        head = messages[:boundary_index]
        tail = messages[boundary_index:]
        request_prefix = _request_prefix_before_tail(context.request_messages, boundary_id)
        prompt = _build_compaction_instruction(
            context.storage.read_prompt_fragment("compaction.md"),
            context.instruction,
        )
        return CompactionPlan(
            model_messages=(
                *request_prefix,
                {"role": "user", "content": prompt},
            ),
            model_target="summary",
            after_summary=tuple(tail),
            compacted_token_count=(
                context.previous_compacted_token_count
                + _estimate_token_span(
                    [message for message in head if not _is_compaction_summary_note(message)]
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
        base_instruction = context.storage.read_prompt_fragment("compaction.md").strip()
        instruction = (context.instruction or "").strip()
        suffix = base_instruction
        if instruction:
            suffix = f"{suffix}\n\nAdditional instruction: {instruction}"
        model_messages = (
            *context.request_messages,
            {
                "role": "user",
                "content": (
                    "<system-reminder>\n"
                    "Create the next compaction checkpoint now. Return only the compacted "
                    "context text that the agent should retain for continuing this Session.\n\n"
                    f"{suffix}\n"
                    "</system-reminder>"
                ),
            },
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
    ) -> bool:
        """Return whether automatic Compaction has a non-summary Head to replace."""

        strategy = self._strategies.get(settings.strategy)
        if strategy is None:
            raise CompactionError(f"Unknown compaction strategy: {settings.strategy}")
        if strategy.id != STRATEGY_SUMMARY_TAIL:
            return True

        effective = _effective_compaction_messages(messages)
        if not effective:
            return False
        try:
            boundary_id = find_tail_boundary(effective, settings.tail_tokens)
        except CompactionError:
            return False
        boundary_index = _find_boundary_index(effective, boundary_id)
        compactable_prefix = effective[:boundary_index]
        return any(not _is_compaction_summary_note(message) for message in compactable_prefix)

    async def compact(
        self,
        messages: list[ChatMessage],
        *,
        agent: Any,
        summary_adapter: Any,
        summary_model_id: str,
        storage: Any,
        settings: CompactionSettings,
        instruction: str | None = None,
        request_messages: list[JsonObject] | None = None,
        active_adapter: Any | None = None,
        active_model_id: str | None = None,
        active_tools: list[JsonObject] | None = None,
        minimum_reclaim_tokens: int = 0,
        context_tokens_before: int | None = None,
    ) -> ChatMessage:
        """Execute at most one Model request and persist its assembled projection."""
        del agent
        if minimum_reclaim_tokens < 0:
            raise CompactionError("minimum_reclaim_tokens cannot be negative")
        if context_tokens_before is not None and (
            isinstance(context_tokens_before, bool)
            or not isinstance(context_tokens_before, int)
            or context_tokens_before < 0
        ):
            raise CompactionError("context_tokens_before must be a non-negative integer")
        strategy = self._strategies.get(settings.strategy)
        if strategy is None:
            raise CompactionError(f"Unknown compaction strategy: {settings.strategy}")
        effective = _effective_compaction_messages(messages)
        checkpoint = _latest_compaction_checkpoint(messages)
        previous_count = _previous_compacted_token_count(checkpoint)
        context = CompactionContext(
            messages=tuple(effective),
            request_messages=tuple(dict(message) for message in request_messages or []),
            previous_compacted_token_count=previous_count,
            instruction=instruction,
            storage=storage,
        )
        try:
            plan = strategy.plan(context, settings)
            _validate_plan(plan)
            summary = plan.summary_text
            if plan.model_messages is not None:
                adapter, model_id = _plan_model_target(
                    plan,
                    summary_adapter=summary_adapter,
                    summary_model_id=summary_model_id,
                    active_adapter=active_adapter,
                    active_model_id=active_model_id,
                )
                request_options: dict[str, Any] = {
                    "model_id": model_id,
                    "temperature": 0.0,
                    "thinking_effort": "",
                }
                if active_tools is not None:
                    request_options["tools"] = list(active_tools)
                model_messages = [dict(message) for message in plan.model_messages]
                if adapter is not active_adapter or model_id != active_model_id:
                    _strip_assistant_reasoning_fields(model_messages)
                response = await adapter.send(
                    model_messages,
                    **request_options,
                )
                summary = _extract_summary_text(_normalize_response(adapter, response))
            projection = [*plan.before_summary]
            if summary:
                projection.append(ChatMessage.note(f"{COMPACTION_SUMMARY_NOTE_PREFIX}{summary}"))
            projection.extend(plan.after_summary)
            _validate_projection(projection)
            reclaimed_tokens = _estimate_token_span(effective) - _estimate_token_span(projection)
            if minimum_reclaim_tokens > 0 and reclaimed_tokens < minimum_reclaim_tokens:
                raise CompactionInsufficientReclaimError(
                    "Compaction projection reclaimed "
                    f"{max(0, reclaimed_tokens)} tokens; "
                    f"minimum is {minimum_reclaim_tokens}"
                )
        except CompactionError:
            raise
        except Exception as exc:
            raise CompactionError(f"Compaction failed: {exc}") from exc
        return ChatMessage.compaction_checkpoint(
            summary=summary,
            projection=projection,
            compacted_token_count=plan.compacted_token_count,
            context_tokens_before=context_tokens_before,
            context_tokens_after=(
                max(0, context_tokens_before - reclaimed_tokens)
                if context_tokens_before is not None
                else None
            ),
            policy=settings.strategy,
            strategy=strategy.id,
        )

    def estimate_messages_tokens(self, messages: list[dict]) -> int:
        estimated_tokens, _ = estimate_request_input_tokens(messages)
        return estimated_tokens


def _is_compaction_summary_note(message: ChatMessage) -> bool:
    return (
        message.role == "note"
        and isinstance(message.content, str)
        and message.content.startswith(COMPACTION_SUMMARY_NOTE_PREFIX)
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


def _find_boundary_index(messages: list[ChatMessage], boundary_id: str) -> int:
    for index, message in enumerate(messages):
        if message.id == boundary_id:
            return index
    raise CompactionError(f"Tail boundary id was not found in messages: {boundary_id}")


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


def _strip_assistant_reasoning_fields(messages: list[JsonObject]) -> None:
    """Remove Provider-owned reasoning state before a different Model target."""

    for message in messages:
        if message.get("role") != "assistant":
            continue
        message.pop("reasoning", None)
        message.pop("reasoning_meta", None)
        message.pop("reasoning_scope", None)


def is_compacted_tool_result_content(content: Any) -> bool:
    """Recognize a legacy Tool-aging digest from an existing checkpoint."""

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


def _normalize_response(summary_adapter: Any, response: Any) -> dict[str, Any]:
    if not isinstance(response, dict):
        raise CompactionError("Summary adapter returned a non-object response")
    normalize_response = getattr(summary_adapter, "normalize_response", None)
    if callable(normalize_response):
        normalized = normalize_response(response)
        if not isinstance(normalized, dict):
            raise CompactionError("Summary adapter normalize_response() must return an object")
        return cast("dict[str, Any]", normalized)
    return cast("dict[str, Any]", response)


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
