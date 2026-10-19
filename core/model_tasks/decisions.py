"""Decision Model execution: typed answers to questions about text, one item at a time."""

from __future__ import annotations

import asyncio
import math
from typing import Any, NamedTuple

import httpx

from core.model_tasks.decision_providers import ProviderDecisionClient
from core.model_tasks.decision_types import (
    DecisionError,
    json_copy,
    validate_questions,
    wire_questions,
)
from core.model_tasks.model_tasks import TaskModelService, parse_task_model_target_id
from core.model_tasks.task_execution import TaskUsage, TaskUsageContext
from core.providers.errors import NetworkError, ProviderError, ProviderOutcomeUnknownError
from core.providers.task_client import TaskClientRuntime
from core.usage import UsageRecorder
from core.utils.tls import shared_ssl_context
from core.utils.tokens import estimate_json_tokens
from core.utils.workers import BoundedWorkerPool

# The most items one classify call judges.
ITEM_LIMIT = 100
# TypeSafe's context limits (docs.typesafe.ai/models, without the opt-in long
# context): the state plus all questions, and the state plus any one question.
REQUEST_TOKEN_LIMIT = 64_000
QUESTION_TOKEN_LIMIT = 32_000
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
        if not self._model_tasks.binding_is_usable("decision"):
            return False
        ref = parse_task_model_target_id(self._model_tasks.binding_for("decision").target)
        return ref.provider_id == "openrouter"

    def _target(self) -> str:
        if not self.available():
            raise DecisionError(NOT_CONFIGURED_MESSAGE, code="not_configured")
        return self._model_tasks.binding_for("decision").target

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
        states = [item if context is None else {"context": context, "item": item} for item in items]
        for state in states:
            if not isinstance(state, str | dict | list):
                raise DecisionError("Each item must be text, a JSON object, or a JSON array.")
        target = await _DECISION_SETUP_WORKERS.run(self._target)
        slots = asyncio.Semaphore(_ITEM_CONCURRENCY)

        async def judge(
            state: Any, http_client: httpx.AsyncClient
        ) -> dict[str, Any] | DecisionError:
            try:
                state = json_copy(state)
                estimate = _input_estimate(state, questions)
                factor = (
                    self._usage_recorder.input_estimate_factor(target)
                    if self._usage_recorder is not None
                    else 1.0
                )
                _check_input_size(estimate, factor)
                async with slots:
                    result = await self._evaluate(
                        target,
                        state,
                        questions,
                        http_client=http_client,
                        usage_context=usage_context,
                    )
                await self._learn_input_estimate(target, result, estimate.request)
                return result
            except DecisionError as exc:
                return exc

        async with (
            httpx.AsyncClient(verify=shared_ssl_context()) as http_client,
            asyncio.TaskGroup() as group,
        ):
            tasks = [group.create_task(judge(state, http_client)) for state in states]
        return [task.result() for task in tasks]

    async def _learn_input_estimate(
        self, target: str, result: dict[str, Any], estimated: int
    ) -> None:
        """Correct later size checks by the input the Provider reported."""
        recorder = self._usage_recorder
        usage = result.get("usage")
        measured = usage.get("input_tokens") if isinstance(usage, dict) else None
        if recorder is None or not isinstance(measured, int) or isinstance(measured, bool):
            return
        await _DECISION_SETUP_WORKERS.run(
            recorder.record_input_estimate, target, measured=measured, estimated=estimated
        )

    async def _evaluate(
        self,
        target: str,
        state: Any,
        questions: list[dict[str, Any]],
        *,
        http_client: httpx.AsyncClient | None = None,
        usage_context: TaskUsageContext | None = None,
    ) -> dict[str, Any]:
        ref = parse_task_model_target_id(target)
        try:
            client = await ProviderDecisionClient.from_runtime_async(
                self._runtime,
                ref,
                usage_observer=TaskUsage(
                    self._usage_recorder, "decision", ref, context=usage_context
                ),
            )
            return await client.evaluate(state, questions, http_client=http_client)
        except ProviderOutcomeUnknownError as exc:
            raise DecisionError(
                (
                    "The Provider may have processed this request, but no usable result "
                    "arrived. Asking again may incur another charge."
                ),
                code="outcome_unknown",
            ) from exc
        except (ProviderError, NetworkError) as exc:
            raise DecisionError(str(exc), code="provider_error") from exc


class _InputEstimate(NamedTuple):
    """Uncorrected estimates of one request: the whole of it and its largest question."""

    request: int
    question: int


def _input_estimate(state: Any, questions: list[dict[str, Any]]) -> _InputEstimate:
    request, _ = estimate_json_tokens({"state": state, "questions": wire_questions(questions)})
    state_tokens, _ = estimate_json_tokens({"state": state})
    largest = max(estimate_json_tokens(wire_questions([q]))[0] for q in questions)
    return _InputEstimate(request, state_tokens + largest)


def _check_input_size(estimate: _InputEstimate, factor: float) -> None:
    request = math.ceil(estimate.request * factor)
    question = math.ceil(estimate.question * factor)
    if request > REQUEST_TOKEN_LIMIT:
        raise DecisionError(
            f"This item has about {request} tokens together with context and all "
            f"questions; the Decision Model reads at most {REQUEST_TOKEN_LIMIT}.",
            code="too_large",
        )
    if question > QUESTION_TOKEN_LIMIT:
        raise DecisionError(
            f"This item has about {question} tokens together with context and its largest "
            f"question; the Decision Model reads at most {QUESTION_TOKEN_LIMIT} per question.",
            code="too_large",
        )
