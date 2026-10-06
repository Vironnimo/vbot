"""Decision Model execution: typed answers to questions about text, one item at a time."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from core.model_tasks.decision_providers import ProviderDecisionClient
from core.model_tasks.decision_types import (
    DecisionError,
    json_copy,
    validate_questions,
)
from core.model_tasks.model_tasks import TaskModelService, parse_task_model_target_id
from core.model_tasks.task_execution import TaskUsage, TaskUsageContext
from core.providers.errors import NetworkError, ProviderError, ProviderOutcomeUnknownError
from core.providers.task_client import TaskClientRuntime
from core.usage import UsageRecorder
from core.utils.tls import shared_ssl_context
from core.utils.tokens import estimate_json_tokens

# The most items one classify call judges.
ITEM_LIMIT = 100
# The most tokens the Decision Model reads per request: state plus questions.
INPUT_TOKEN_LIMIT = 32_000
# Items judged at the same time; each is one Provider request.
_ITEM_CONCURRENCY = 8

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
        target = self._target()
        slots = asyncio.Semaphore(_ITEM_CONCURRENCY)

        async def judge(
            state: Any, http_client: httpx.AsyncClient
        ) -> dict[str, Any] | DecisionError:
            try:
                state = json_copy(state)
                tokens, _ = estimate_json_tokens({"state": state, "questions": questions})
                if tokens > INPUT_TOKEN_LIMIT:
                    raise DecisionError(
                        f"This item has about {tokens} tokens together with context and "
                        f"questions; the Decision Model reads at most {INPUT_TOKEN_LIMIT}.",
                        code="too_large",
                    )
                async with slots:
                    return await self._evaluate(
                        target,
                        state,
                        questions,
                        http_client=http_client,
                        usage_context=usage_context,
                    )
            except DecisionError as exc:
                return exc

        async with (
            httpx.AsyncClient(verify=shared_ssl_context()) as http_client,
            asyncio.TaskGroup() as group,
        ):
            tasks = [group.create_task(judge(state, http_client)) for state in states]
        return [task.result() for task in tasks]

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
            client = ProviderDecisionClient.from_runtime(
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
