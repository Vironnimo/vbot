"""Policy-driven compaction engine for provider-neutral chat Context."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import Any, Literal, Protocol, cast

from core.chat.content_blocks import content_block_to_dict
from core.chat.messages import (
    COMPACTION_SUMMARY_NOTE_PREFIX,
    ChatMessage,
    JsonObject,
    ToolCall,
    _effective_compaction_messages,
    _latest_compaction_checkpoint,
)
from core.debug.redaction import redact_json_body
from core.sessions import skill_context_note_name
from core.utils.errors import VBotError
from core.utils.tokens import estimate_message_tokens, estimate_request_input_tokens

TOOL_RESULT_CONTENT_PLACEHOLDER = "[tool result content omitted during compaction]"
TOOL_RESULT_COMPACTED_FIELD = "_vbot_compacted_tool_result"
SKILL_ACTIVATION_CONTENT_PLACEHOLDER = "[skill '{name}' activated - content omitted]"

TRIGGER_CONTEXT_RATIO = "context_ratio"
TRIGGER_INPUT_TOKENS = "input_tokens"
STRATEGY_SUMMARY_TAIL = "summary_tail"
STRATEGY_CONTINUATION = "continuation"
COMPACTION_POLICY_META_KEY = "compaction_policy"

MIN_AUTO_COMPACTION_RECLAIM_TOKENS = 4_096
MAX_TOOL_RESULT_DIGEST_CHARS = 1_600
MAX_TOOL_RESULT_VALUE_CHARS = 320
MAX_TOOL_ARGUMENTS_CHARS = 2_000
MAX_TOOL_ARGUMENT_VALUE_CHARS = 512
MAX_PROJECTED_COLLECTION_ITEMS = 12
PROTECTED_RECENT_TOOL_BATCHES = 1
TOOL_AGING_ONLY_SUMMARY = (
    "The current Run remains active. Older completed Tool payloads were compacted "
    "deterministically; the User request and newest Tool batch remain intact."
)

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
    previous_summary: str | None
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
    """Return the user-message id where the preserved tail should start."""
    if not messages:
        raise CompactionError("Cannot find tail boundary for an empty message list")
    if tail_tokens <= 0:
        raise CompactionError("tail_tokens must be positive")
    turn_ranges = _turn_ranges(messages)
    if not turn_ranges:
        raise CompactionError("Cannot compact history without at least one user message")
    boundary_index = turn_ranges[0][0]
    accumulated_tokens = 0
    for start_index, end_index in reversed(turn_ranges):
        accumulated_tokens += _estimate_token_span(messages[start_index:end_index])
        boundary_index = start_index
        if accumulated_tokens >= tail_tokens:
            break
    return messages[boundary_index].id


class SummarizationStrategy:
    """Summarize old Context and age older Tool payloads inside the retained tail."""

    id = STRATEGY_SUMMARY_TAIL

    def plan(self, context: CompactionContext, settings: CompactionSettings) -> CompactionPlan:
        messages = list(context.messages)
        if not messages:
            raise CompactionError("Cannot compact an empty Context")
        boundary_id = find_tail_boundary(messages, settings.tail_tokens)
        boundary_index = _find_boundary_index(messages, boundary_id)
        pre_tail = messages[:boundary_index]
        tail = messages[boundary_index:]
        projected_tail, tool_reclaim = _age_tool_payloads(tail)
        history = [message for message in pre_tail if not _is_compaction_summary_note(message)]
        previous_summary = (context.previous_summary or "").strip() or None
        if tool_reclaim > 0 and not history and not (context.instruction or "").strip():
            return CompactionPlan(
                model_messages=None,
                model_target="summary",
                after_summary=projected_tail,
                summary_text=previous_summary or TOOL_AGING_ONLY_SUMMARY,
                compacted_token_count=(context.previous_compacted_token_count + tool_reclaim),
            )
        prompt = _build_compaction_prompt(
            context.storage.read_prompt_fragment("compaction.md"),
            _render_history_for_prompt(history),
            context.instruction,
            previous_summary=previous_summary,
        )
        return CompactionPlan(
            model_messages=({"role": "user", "content": prompt},),
            model_target="summary",
            after_summary=projected_tail,
            compacted_token_count=(
                context.previous_compacted_token_count
                + _estimate_token_span(history)
                + tool_reclaim
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
        """Return whether automatic Compaction can release additional Context.

        Summary+Tail retains complete user turns. Once a checkpoint has been
        written inside a still-running turn, later Assistant/Tool steps remain
        in that same retained tail. Re-summarizing only the checkpoint's own
        leading summary cannot reduce the growing turn, so automatic retries
        must wait until a later turn boundary makes retained Context eligible.
        Manual Compaction deliberately bypasses this preflight.
        """

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
        if any(not _is_compaction_summary_note(message) for message in compactable_prefix):
            return True
        _, tool_reclaim = _age_tool_payloads(
            effective[boundary_index:],
            minimum_reclaim_tokens=MIN_AUTO_COMPACTION_RECLAIM_TOKENS,
        )
        return tool_reclaim >= MIN_AUTO_COMPACTION_RECLAIM_TOKENS

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
    ) -> ChatMessage:
        """Execute at most one Model request and persist its assembled projection."""
        del agent
        if minimum_reclaim_tokens < 0:
            raise CompactionError("minimum_reclaim_tokens cannot be negative")
        strategy = self._strategies.get(settings.strategy)
        if strategy is None:
            raise CompactionError(f"Unknown compaction strategy: {settings.strategy}")
        effective = _effective_compaction_messages(messages)
        checkpoint = _latest_compaction_checkpoint(messages)
        previous_count = _previous_compacted_token_count(checkpoint)
        context = CompactionContext(
            messages=tuple(effective),
            request_messages=tuple(dict(message) for message in request_messages or []),
            previous_summary=(
                checkpoint.content
                if checkpoint is not None and isinstance(checkpoint.content, str)
                else None
            ),
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
                if plan.model_target == "active":
                    request_options["tools"] = list(active_tools or [])
                response = await adapter.send(
                    [dict(message) for message in plan.model_messages], **request_options
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


def _turn_ranges(messages: list[ChatMessage]) -> list[tuple[int, int]]:
    user_indices = [index for index, message in enumerate(messages) if message.role == "user"]
    return [
        (start, user_indices[index + 1] if index + 1 < len(user_indices) else len(messages))
        for index, start in enumerate(user_indices)
    ]


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


@dataclass(frozen=True)
class _ToolBatch:
    assistant_index: int
    result_indices: tuple[int, ...]


def _complete_tool_batches(messages: list[ChatMessage]) -> list[_ToolBatch]:
    """Return completed Assistant/Tool batches without crossing message boundaries."""

    batches: list[_ToolBatch] = []
    pending_assistant_index: int | None = None
    pending_call_ids: set[str] = set()
    result_indices: list[int] = []
    for index, message in enumerate(messages):
        if message.role in {"note", "run_summary", "agent_takeover", "error"}:
            continue
        if message.role == "assistant":
            pending_assistant_index = index if message.tool_calls else None
            pending_call_ids = {call.id for call in message.tool_calls or []}
            result_indices = []
            continue
        if message.role == "tool":
            if pending_assistant_index is not None and message.tool_call_id in pending_call_ids:
                pending_call_ids.remove(cast(str, message.tool_call_id))
                result_indices.append(index)
                if not pending_call_ids:
                    batches.append(
                        _ToolBatch(
                            assistant_index=pending_assistant_index,
                            result_indices=tuple(result_indices),
                        )
                    )
                    pending_assistant_index = None
                    result_indices = []
            continue
        pending_assistant_index = None
        pending_call_ids = set()
        result_indices = []
    return batches


def _age_tool_payloads(
    messages: list[ChatMessage],
    *,
    minimum_reclaim_tokens: int = 0,
) -> tuple[tuple[ChatMessage, ...], int]:
    """Age older completed Tool batches while preserving the newest batch verbatim."""

    batches = _complete_tool_batches(messages)
    if len(batches) <= PROTECTED_RECENT_TOOL_BATCHES:
        return tuple(messages), 0

    projected = list(messages)
    for batch in batches[:-PROTECTED_RECENT_TOOL_BATCHES]:
        assistant_message = projected[batch.assistant_index]
        if assistant_message.tool_calls:
            compacted_calls = [
                _compact_tool_call(tool_call) for tool_call in assistant_message.tool_calls
            ]
            original_calls = json.dumps(
                [call.to_dict() for call in assistant_message.tool_calls],
                ensure_ascii=False,
                separators=(",", ":"),
            )
            projected_calls = json.dumps(
                [call.to_dict() for call in compacted_calls],
                ensure_ascii=False,
                separators=(",", ":"),
            )
            if len(projected_calls) < len(original_calls):
                projected[batch.assistant_index] = replace(
                    assistant_message,
                    tool_calls=compacted_calls,
                )

        for result_index in batch.result_indices:
            result_message = projected[result_index]
            if not isinstance(result_message.content, str) or is_compacted_tool_result_content(
                result_message.content
            ):
                continue
            digest = _tool_result_digest(result_message)
            if len(digest) < len(result_message.content):
                projected[result_index] = replace(result_message, content=digest)

    original_tokens = _estimate_token_span(messages)
    projected_tokens = _estimate_token_span(projected)
    reclaimed_tokens = max(0, original_tokens - projected_tokens)
    if reclaimed_tokens <= 0 or reclaimed_tokens < minimum_reclaim_tokens:
        return tuple(messages), 0
    return tuple(projected), reclaimed_tokens


def _compact_tool_call(tool_call: ToolCall) -> ToolCall:
    original = cast(JsonObject, redact_json_body(tool_call.arguments))
    compacted = _compact_json_value(
        original,
        string_limit=MAX_TOOL_ARGUMENT_VALUE_CHARS,
        include_string_preview=True,
    )
    if not isinstance(compacted, dict):
        return tool_call
    serialized = _json_dumps(compacted)
    if len(serialized) > MAX_TOOL_ARGUMENTS_CHARS:
        compacted = _compact_oversized_arguments(
            original,
            original_chars=len(_json_dumps(original)),
        )
    if len(_json_dumps(compacted)) > MAX_TOOL_ARGUMENTS_CHARS:
        compacted = {
            "_vbot_compacted_arguments": {
                "original_chars": len(_json_dumps(original)),
                "key_count": len(original),
                "keys": [
                    _shorten_text(str(key), max_chars=64)
                    for key in list(original)[:MAX_PROJECTED_COLLECTION_ITEMS]
                ],
            }
        }
    if compacted == tool_call.arguments:
        return tool_call
    return replace(tool_call, arguments=cast(JsonObject, compacted))


def _compact_oversized_arguments(arguments: JsonObject, *, original_chars: int) -> JsonObject:
    projected: JsonObject = {}
    for key, value in list(arguments.items())[:MAX_PROJECTED_COLLECTION_ITEMS]:
        projected[key] = _compact_json_value(
            value,
            string_limit=96,
            include_string_preview=True,
            max_depth=2,
        )
    projected["_vbot_compacted_arguments"] = {
        "original_chars": original_chars,
        "omitted_keys": max(0, len(arguments) - len(projected)),
    }
    return projected


def _tool_result_digest(message: ChatMessage) -> str:
    content = cast(str, message.content)
    try:
        parsed = json.loads(content)
    except (TypeError, ValueError):
        outcome: Any = {"type": "text", "chars": len(content)}
    else:
        outcome = _compact_json_value(
            redact_json_body(parsed),
            string_limit=MAX_TOOL_RESULT_VALUE_CHARS,
            include_string_preview=True,
        )
    payload: JsonObject = {
        TOOL_RESULT_COMPACTED_FIELD: True,
        "tool": message.name,
        "original_chars": len(content),
        "outcome": outcome,
    }
    serialized = _json_dumps(payload)
    if len(serialized) <= MAX_TOOL_RESULT_DIGEST_CHARS:
        return serialized
    payload["outcome"] = {
        "type": type(outcome).__name__,
        "preview": _shorten_text(
            _json_dumps(outcome),
            max_chars=MAX_TOOL_RESULT_DIGEST_CHARS // 2,
        ),
    }
    serialized = _json_dumps(payload)
    if len(serialized) <= MAX_TOOL_RESULT_DIGEST_CHARS:
        return serialized
    payload["outcome"] = {
        "type": type(outcome).__name__,
        "compacted": True,
    }
    return _json_dumps(payload)


def is_compacted_tool_result_content(content: Any) -> bool:
    """Return whether a Tool Result is an already-aged valid JSON digest."""

    if not isinstance(content, str):
        return False
    try:
        parsed = json.loads(content)
    except (TypeError, ValueError):
        return False
    return isinstance(parsed, dict) and parsed.get(TOOL_RESULT_COMPACTED_FIELD) is True


def _compact_json_value(
    value: Any,
    *,
    string_limit: int,
    include_string_preview: bool,
    max_depth: int = 4,
    _depth: int = 0,
) -> Any:
    if _depth >= max_depth:
        return _value_shape(value)
    if isinstance(value, dict):
        projected: JsonObject = {}
        items = list(value.items())
        for key, item in items[:MAX_PROJECTED_COLLECTION_ITEMS]:
            projected[str(key)] = _compact_json_value(
                item,
                string_limit=string_limit,
                include_string_preview=include_string_preview,
                max_depth=max_depth,
                _depth=_depth + 1,
            )
        if len(items) > MAX_PROJECTED_COLLECTION_ITEMS:
            projected["_vbot_omitted_keys"] = len(items) - MAX_PROJECTED_COLLECTION_ITEMS
        return projected
    if isinstance(value, list):
        projected_items = [
            _compact_json_value(
                item,
                string_limit=string_limit,
                include_string_preview=include_string_preview,
                max_depth=max_depth,
                _depth=_depth + 1,
            )
            for item in value[:MAX_PROJECTED_COLLECTION_ITEMS]
        ]
        if len(value) > MAX_PROJECTED_COLLECTION_ITEMS:
            projected_items.append(
                {"_vbot_omitted_items": len(value) - MAX_PROJECTED_COLLECTION_ITEMS}
            )
        return projected_items
    if isinstance(value, str) and len(value) > string_limit:
        if not include_string_preview:
            return {"_vbot_omitted_chars": len(value)}
        return _shorten_text(value, max_chars=string_limit)
    return value


def _value_shape(value: Any) -> JsonObject:
    if isinstance(value, dict):
        return {"_vbot_compacted_object_keys": len(value)}
    if isinstance(value, list):
        return {"_vbot_compacted_list_items": len(value)}
    if isinstance(value, str):
        return {"_vbot_compacted_string_chars": len(value)}
    return {"_vbot_compacted_type": type(value).__name__}


def _shorten_text(value: str, *, max_chars: int) -> str:
    if len(value) <= max_chars:
        return value
    marker = f"\n...[{len(value)} chars compacted]...\n"
    remaining = max(0, max_chars - len(marker))
    head_chars = (remaining * 2) // 3
    tail_chars = remaining - head_chars
    return f"{value[:head_chars]}{marker}{value[-tail_chars:] if tail_chars else ''}"


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _previous_compacted_token_count(checkpoint: ChatMessage | None) -> int:
    if checkpoint is None or not isinstance(checkpoint.usage, dict):
        return 0
    count = checkpoint.usage.get("compacted_token_count")
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        return 0
    return count


def _build_compaction_prompt(
    prompt_fragment: str,
    history_text: str,
    instruction: str | None = None,
    *,
    previous_summary: str | None = None,
) -> str:
    sections = [prompt_fragment.strip()]
    if previous_summary:
        sections.append(f"<previous_summary>\n{previous_summary.strip()}\n</previous_summary>")
    if instruction and instruction.strip():
        sections.append(f"<user_instruction>\n{instruction.strip()}\n</user_instruction>")
    sections.append(f"<history>\n{history_text}\n</history>")
    return "\n\n".join(sections)


def _render_history_for_prompt(messages: list[ChatMessage]) -> str:
    if not messages:
        return "(no history selected for summarization)"
    return "\n\n".join(_render_message_entry(message) for message in messages)


def _render_message_entry(message: ChatMessage) -> str:
    lines = [f"role={message.role} id={message.id}"]
    content = _render_message_content(message)
    if content is not None:
        lines.append(f"content={content}")
    if message.tool_calls is not None:
        serialized_calls = json.dumps(
            [_compact_tool_call(call).to_dict() for call in message.tool_calls],
            ensure_ascii=False,
        )
        lines.append(f"tool_calls={serialized_calls}")
    if message.tool_call_id is not None:
        lines.append(f"tool_call_id={message.tool_call_id}")
    if message.name is not None:
        lines.append(f"name={message.name}")
    if message.error_kind is not None:
        lines.append(f"error_kind={message.error_kind}")
    return "\n".join(lines)


def _render_message_content(message: ChatMessage) -> str | None:
    if message.role == "tool":
        if is_compacted_tool_result_content(message.content):
            return cast(str, message.content)
        return f"{TOOL_RESULT_CONTENT_PLACEHOLDER} {_tool_result_digest(message)}"
    skill_name = skill_context_note_name(message)
    if skill_name is not None:
        return SKILL_ACTIVATION_CONTENT_PLACEHOLDER.format(name=skill_name)
    content = message.content
    if content is None:
        return None
    if isinstance(content, str):
        return content.removeprefix(COMPACTION_SUMMARY_NOTE_PREFIX)
    return json.dumps([content_block_to_dict(block) for block in content], ensure_ascii=False)


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
