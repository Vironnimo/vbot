"""Decision Model execution: typed answers to questions about text, one item at a time."""

from __future__ import annotations

import asyncio
import math
from typing import Any, NamedTuple

from core.model_tasks.decision_providers import ProviderDecisionClient
from core.model_tasks.decision_types import (
    DecisionError,
    json_copy,
    validate_questions,
    wire_questions,
)
from core.model_tasks.model_tasks import TaskModelService, parse_task_model_target_id
from core.model_tasks.task_execution import TaskUsage, TaskUsageContext
from core.providers.errors import NetworkError, ProviderError, ProviderRequestTooLargeError
from core.providers.task_client import TaskClientRuntime
from core.usage import UsageRecorder, last_request_usage
from core.utils.errors import VBotError
from core.utils.tokens import estimate_json_tokens
from core.utils.workers import BoundedWorkerPool

# The most items one classify call judges.
ITEM_LIMIT = 100
# Items judged at the same time; each is one Provider request.
_ITEM_CONCURRENCY = 8
_DECISION_SETUP_WORKERS = BoundedWorkerPool(name="decision-setup", max_workers=2)

NOT_CONFIGURED_MESSAGE = (
    "Configure an available Decision model in Settings → Tools → Decision Model."
)


class DecisionService:
    """The one executor of Decision Model requests, for Tools and internal consumers.

    It keeps no state: every call resolves the configured ``decision`` binding,
    sends one request per item and records each attempt's Usage.
    """

    def __init__(
        self,
        model_tasks: TaskModelService,
        runtime: TaskClientRuntime,
        *,
        usage_recorder: UsageRecorder | None = None,
    ) -> None:
        self._model_tasks = model_tasks
        self._runtime = runtime
        self._usage_recorder = usage_recorder

    def available(self) -> bool:
        try:
            self._target()
        except DecisionError:
            return False
        return True

    def _target(self) -> _Target:
        try:
            binding = self._model_tasks.binding_for("decision")
            self._model_tasks.validate_execution_target(binding)
            ref = parse_task_model_target_id(binding.target)
        except (VBotError, ValueError) as exc:
            raise DecisionError(NOT_CONFIGURED_MESSAGE, code="not_configured") from exc
        if ref.provider_id != "openrouter":
            raise DecisionError(NOT_CONFIGURED_MESSAGE, code="not_configured")
        model = self._model_tasks.model_for_target(ref)
        window = model.context_window_for(ref.local_connection_id) if model is not None else None
        return _Target(binding.target, window if isinstance(window, int) and window > 0 else None)

    async def classify(
        self,
        items: Any,
        questions: Any,
        *,
        context: str | None = None,
        usage_context: TaskUsageContext | None = None,
    ) -> list[dict[str, Any] | DecisionError]:
        """Answer every question about each item separately, in item order.

        Each item is one request whose state is the item itself, or
        ``{"context": context, "item": item}`` when ``context`` is given. A
        failed item yields its ``DecisionError`` in its place; the other items
        still complete. Invalid questions, items or a missing binding raise
        before any request is sent.
        """
        questions = validate_questions(questions)
        if not isinstance(items, list) or not items:
            raise DecisionError("items must contain at least one item.")
        if len(items) > ITEM_LIMIT:
            raise DecisionError(
                f"{len(items)} items were given; one call classifies at most {ITEM_LIMIT}. "
                f"Nothing was sent. Split the items into calls of at most {ITEM_LIMIT}."
            )
        if context is not None and (not isinstance(context, str) or not context.strip()):
            raise DecisionError("context must be non-empty text.")
        for item in items:
            if not isinstance(item, str | dict | list):
                raise DecisionError("Each item must be text, a JSON object, or a JSON array.")
        states = [item if context is None else {"context": context, "item": item} for item in items]
        resolved = await _DECISION_SETUP_WORKERS.run(self._target)
        target = resolved.target
        slots = asyncio.Semaphore(_ITEM_CONCURRENCY)

        async def judge(state: Any) -> dict[str, Any] | DecisionError:
            try:
                factor = (
                    self._usage_recorder.input_estimate_factor(target)
                    if self._usage_recorder is not None
                    else 1.0
                )
                # Copying and counting an item of up to 1 MB must not block the Event Loop.
                state, estimate = await _DECISION_SETUP_WORKERS.run(
                    _sized_state, state, questions, factor, resolved.context_window
                )
                async with slots:
                    result = await self._evaluate(
                        resolved,
                        state,
                        questions,
                        usage_context=usage_context,
                    )
                await self._learn_input_estimate(target, result, estimate)
                return result
            except DecisionError as exc:
                return exc

        async with asyncio.TaskGroup() as group:
            tasks = [group.create_task(judge(state)) for state in states]
        return [task.result() for task in tasks]

    async def _learn_input_estimate(
        self, target: str, result: dict[str, Any], estimated: int
    ) -> None:
        """Correct later size checks by the input the Provider reported."""
        recorder = self._usage_recorder
        usage = result.get("usage")
        measured = last_request_usage(usage if isinstance(usage, dict) else None).get(
            "input_tokens"
        )
        if recorder is None or not isinstance(measured, int) or isinstance(measured, bool):
            return
        await _DECISION_SETUP_WORKERS.run(
            recorder.record_input_estimate, target, measured=measured, estimated=estimated
        )

    async def _evaluate(
        self,
        target: _Target,
        state: Any,
        questions: list[dict[str, Any]],
        *,
        usage_context: TaskUsageContext | None = None,
    ) -> dict[str, Any]:
        ref = parse_task_model_target_id(target.target)
        try:
            client = await ProviderDecisionClient.from_runtime_async(
                self._runtime,
                ref,
                usage_observer=TaskUsage(
                    self._usage_recorder, "decision", ref, context=usage_context
                ),
            )
            return await client.evaluate(state, questions)
        except ProviderError as exc:
            if isinstance(exc, ProviderRequestTooLargeError) or (
                exc.status_code == 400 and _reports_context_overflow(str(exc))
            ):
                raise _rejected_as_too_large(target.context_window) from exc
            raise DecisionError(str(exc), code="provider_error") from exc
        except NetworkError as exc:
            raise DecisionError(str(exc), code="provider_error") from exc


# How a 400 body names a request beyond the context window: TypeSafe's
# documented code (docs.typesafe.ai/api#errors, 2026-10-19), the OpenAI-style
# code and OpenRouter's own message. OpenRouter's wrapping of upstream errors
# is undocumented, so only these exact markers count.
_CONTEXT_OVERFLOW_MARKERS = (
    "max_tokens_exceeded",
    "context_length_exceeded",
    "maximum context length",
)


def _reports_context_overflow(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in _CONTEXT_OVERFLOW_MARKERS)


def _rejected_as_too_large(limit: int | None) -> DecisionError:
    reads = (
        f"; it reads at most {limit} tokens together with context and all questions"
        if limit is not None
        else ""
    )
    return DecisionError(
        f"The Decision Model rejected this item as too large{reads}. Shorten the item, "
        "context or questions.",
        code="too_large",
    )


class _Target(NamedTuple):
    """The configured target and the context window its catalog reports, if any."""

    target: str
    context_window: int | None


def _sized_state(
    state: Any, questions: list[dict[str, Any]], factor: float, limit: int | None
) -> tuple[Any, int]:
    """A private copy of ``state`` and the uncorrected estimate of its whole request.

    The estimate times the Model's input estimate *factor* must fit *limit*,
    the reported context window for the state plus all questions. Without a
    reported window nothing is checked here: the Provider rejects what is too
    large.
    """
    state = json_copy(state)
    estimate, _ = estimate_json_tokens({"state": state, "questions": wire_questions(questions)})
    tokens = math.ceil(estimate * factor)
    if limit is not None and tokens > limit:
        raise DecisionError(
            f"This item has about {tokens} tokens together with context and all questions; "
            f"the Decision Model reads at most {limit} tokens. Shorten the item, context or "
            "questions.",
            code="too_large",
        )
    return state, estimate
