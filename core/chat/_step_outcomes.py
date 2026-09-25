"""Completed and interrupted Model and Tool outcome handling."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from core.chat.messages import (
    ChatMessage,
    JsonObject,
    ToolCall,
)
from core.chat.output_files import resolve_assistant_file_references
from core.chat.wire_shaping import _complete_usage_with_estimates
from core.models.pricing import TokenPricing, price_usage
from core.providers.adapter import (
    TERMINAL_OUTCOME_OUTPUT_TRUNCATED,
    TERMINAL_OUTCOME_STOP,
    TERMINAL_OUTCOME_TOOL_CALLS,
    TERMINAL_OUTCOME_UNKNOWN,
    TerminalOutcome,
)
from core.sessions import ToolResultFacts
from core.tools import ToolNotFoundError, called_tool_name
from core.utils.errors import ProviderError

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

STREAM_RECOVERY_NOTE = (
    "The previous Model response stream ended unexpectedly after producing visible "
    "answer text. The partial Assistant response is already part of this conversation. "
    "Continue the same task from exactly where it stopped without repeating that visible "
    "text. No Tool Call from the interrupted Model step was executed; if tools are still "
    "needed, emit every intended Tool Call again as a complete call."
)

OUTPUT_INTEGRITY_RECOVERY_NOTE = (
    "The previous response ended before completing the task. Continue from the work and "
    "partial answer already present without repeating visible text. Use Tools if further "
    "work is needed, then provide the missing answer. Do not emit raw Reasoning delimiter tags."
)


def _prepare_completed_assistant(
    assistant_message: ChatMessage,
    request_messages: list[JsonObject],
    output_cwd: Path | None,
    estimated_input_tokens: int,
    pricing: TokenPricing | None = None,
) -> ChatMessage:
    """Fill estimated Usage and resolve output-file references off the Event Loop."""
    completed = _complete_usage_with_estimates(
        assistant_message,
        request_messages,
        estimated_input_tokens=estimated_input_tokens,
    )
    completed = replace(
        completed, usage={**(completed.usage or {}), "cost": price_usage(completed.usage, pricing)}
    )
    return _with_assistant_output_files(completed, cwd=output_cwd)


def _with_offered_tool_names(
    assistant_message: ChatMessage, offered: Sequence[str], registry: Any
) -> ChatMessage:
    """Store each Tool Call under the offered Tool its called name clearly means.

    Models call Tools by names from other harnesses or with a namespace prefix
    (``Read``, ``functions.bash``, ``Grep``). Resolving before persistence makes
    history, dispatch and Run events agree, and the next request replays the
    call under the name the Model should use.
    """
    tool_calls = assistant_message.tool_calls
    if not tool_calls or not offered:
        return assistant_message
    registered = _RegisteredNames(registry)
    resolved = [
        replace(call, name=called_tool_name(call.name, offered, registered=registered))
        for call in tool_calls
    ]
    if all(new.name == old.name for new, old in zip(resolved, tool_calls, strict=True)):
        return assistant_message
    return replace(assistant_message, tool_calls=resolved)


@dataclass(frozen=True)
class _RegisteredNames:
    """Membership view of the Tool registry for name resolution."""

    registry: Any

    def __contains__(self, name: object) -> bool:
        try:
            self.registry.get(name)
        except (KeyError, ToolNotFoundError):
            return False
        return True


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


@dataclass
class _ToolProgress:
    """Run-owned Tool budgets, preserved when the Model route changes."""

    iteration_count: int = 0
    failed_calls: _FailedToolCallCircuitBreaker = field(
        default_factory=_FailedToolCallCircuitBreaker
    )
    finalization_reason: str | None = None
    finalization_violations: int = 0


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


# Failure codes that mean the Tool call was cancelled rather than failed.
_CANCELLED_TOOL_CODES = frozenset({"cancelled", "tool_cancelled", "user_cancelled"})


def tool_result_facts(tool_messages: Sequence[ChatMessage]) -> dict[str, ToolResultFacts]:
    """Report how each Tool call ended, read from its Tool Result envelope.

    Sessions stores these facts with the Tool Result, so history readers never
    parse Tool output. A result that is not a failure envelope completed.
    """
    facts: dict[str, ToolResultFacts] = {}
    for message in tool_messages:
        if message.role != "tool" or message.tool_call_id is None:
            continue
        result: Any = None
        if isinstance(message.content, str):
            try:
                result = json.loads(message.content)
            except (TypeError, ValueError):
                result = None
        if not isinstance(result, dict) or not isinstance(result.get("ok"), bool):
            facts[message.tool_call_id] = ToolResultFacts(status="completed")
            continue
        if result["ok"]:
            facts[message.tool_call_id] = ToolResultFacts(status="completed", ok=True)
            continue
        error = result.get("error")
        error = error if isinstance(error, dict) else {}
        code = error.get("code")
        code = code if isinstance(code, str) and code else None
        retryable = error.get("retryable")
        attempts = error.get("attempts_made")
        facts[message.tool_call_id] = ToolResultFacts(
            status="cancelled" if code in _CANCELLED_TOOL_CODES else "failed",
            ok=False,
            error_code=code,
            error_retryable=retryable if isinstance(retryable, bool) else None,
            error_attempts=(
                attempts
                if isinstance(attempts, int) and not isinstance(attempts, bool) and attempts >= 0
                else None
            ),
        )
    return facts


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


def _usage_token_count(usage: Any, key: str) -> int:
    """Return one non-negative token count from a usage payload, else 0."""
    if not isinstance(usage, dict):
        return 0
    value = usage.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value
