"""Decision Model execution: typed answers to questions about text, one item at a time."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any, NamedTuple

from core.model_tasks.decision_profile import DecisionProfile
from core.model_tasks.decision_providers import ProviderDecisionClient, decision_payload
from core.model_tasks.decision_types import (
    DecisionError,
    json_copy,
    validate_questions,
)
from core.model_tasks.model_tasks import (
    TaskModelBinding,
    TaskModelService,
    TaskModelTargetRef,
)
from core.model_tasks.task_execution import (
    TaskAttempt,
    TaskBindingResolver,
    TaskCandidateMismatchError,
    TaskUsage,
    TaskUsageContext,
)
from core.providers.errors import NetworkError, ProviderError, ProviderRequestTooLargeError
from core.providers.task_client import TaskClientRuntime
from core.secrets.agent_secrets import EMPTY_MASKER, SecretMasker
from core.usage import UsageRecorder
from core.utils.errors import ConfigError, TaskError, VBotError
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
        self._resolver = TaskBindingResolver(model_tasks, configuration_error=DecisionError)

    def available(self) -> bool:
        try:
            self._target()
        except DecisionError:
            return False
        return True

    def _target(self, binding: TaskModelBinding | None = None) -> _Target:
        try:
            if binding is None:
                binding, _options, ref = self._resolver.resolve("decision")
            else:
                binding, _options, ref = self._resolver.resolve_binding(binding)
            profile = self._model_tasks.decision_profile(ref)
            options = json_copy(self._model_tasks.options_with_defaults(binding))
        except (VBotError, ValueError) as exc:
            raise DecisionError(NOT_CONFIGURED_MESSAGE, code="not_configured") from exc
        if not profile.supported:
            raise DecisionError(NOT_CONFIGURED_MESSAGE, code="not_configured")
        return _Target(ref, profile.max_input_tokens, profile, options)

    def _targets(
        self, questions: list[dict[str, Any]]
    ) -> tuple[tuple[TaskModelBinding, ...], dict[str, _Target]]:
        try:
            primary = self._resolver.binding_for("decision")
        except TaskError as exc:
            raise DecisionError(NOT_CONFIGURED_MESSAGE, code="not_configured") from exc
        bindings = []
        targets = {}
        last: DecisionError | None = None
        for binding in (primary, *getattr(primary, "fallbacks", ())):
            try:
                target = self._target(binding)
                target.profile.validate_questions(questions)
            except DecisionError as exc:
                last = exc
                continue
            bindings.append(binding)
            targets[binding.target] = target
        if not bindings:
            assert last is not None
            raise last
        return tuple(bindings), targets

    async def classify(
        self,
        items: Any,
        questions: Any,
        *,
        context: str | None = None,
        usage_context: TaskUsageContext | None = None,
        secret_masker: Callable[[], SecretMasker] = lambda: EMPTY_MASKER,
        require_exact: bool = False,
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
        bindings, targets = await _DECISION_SETUP_WORKERS.run(self._targets, questions)
        slots = asyncio.Semaphore(_ITEM_CONCURRENCY)

        async def judge(state: Any) -> dict[str, Any] | DecisionError:
            try:

                async def execute(attempt: TaskAttempt) -> dict[str, Any]:
                    target = targets[attempt.binding.target]._replace(ref=attempt.target)
                    try:
                        await _DECISION_SETUP_WORKERS.run(_check_size, state, questions, target)
                    except DecisionError as exc:
                        raise TaskCandidateMismatchError(exc) from exc
                    return await self._evaluate(
                        target,
                        state,
                        questions,
                        usage_context=usage_context,
                        secret_masker=secret_masker,
                        require_exact=require_exact,
                    )

                async with slots:
                    state = await _DECISION_SETUP_WORKERS.run(json_copy, state)
                    return await self._resolver.execute("decision", execute, bindings=bindings)
            except DecisionError as exc:
                return exc

        async with asyncio.TaskGroup() as group:
            tasks = [group.create_task(judge(state)) for state in states]
        return [task.result() for task in tasks]

    async def _evaluate(
        self,
        target: _Target,
        state: Any,
        questions: list[dict[str, Any]],
        *,
        usage_context: TaskUsageContext | None = None,
        secret_masker: Callable[[], SecretMasker],
        require_exact: bool,
    ) -> dict[str, Any]:
        ref = target.ref
        try:
            try:
                client = await ProviderDecisionClient.from_runtime_async(
                    self._runtime,
                    ref,
                    usage_observer=TaskUsage(
                        self._usage_recorder, "decision", ref, context=usage_context
                    ),
                )
            except (ConfigError, KeyError) as exc:
                # A binding snapshot cannot keep its Provider, Connection or
                # credentials alive while other items are being evaluated.
                raise DecisionError(NOT_CONFIGURED_MESSAGE, code="not_configured") from exc
            masker = secret_masker()
            projected = _project_decision(state, questions, masker, require_exact)

            def check_before_send() -> None:
                current = secret_masker()
                if (
                    current is not masker
                    and _project_decision(state, questions, current, require_exact) != projected
                ):
                    raise DecisionError(
                        "Secret access changed before the decision was sent. Repeat the call.",
                        code="secret_scope_changed",
                    )

            return await client.evaluate(
                *projected,
                profile=target.profile,
                options=target.options,
                before_send=check_before_send,
            )
        except ProviderError as exc:
            if isinstance(exc, ProviderRequestTooLargeError) or (
                exc.status_code == 400 and _reports_context_overflow(str(exc))
            ):
                raise _rejected_as_too_large(target.context_window) from exc
            raise DecisionError(str(exc), code="provider_error") from exc
        except NetworkError as exc:
            raise DecisionError(str(exc), code="provider_error") from exc


def _project_decision(
    state: Any,
    questions: list[dict[str, Any]],
    masker: SecretMasker,
    require_exact: bool,
) -> tuple[Any, list[dict[str, Any]]]:
    projected_state = masker.mask_value(state, include_keys=True)
    projected_questions = []
    for question in questions:
        projected = {
            **question,
            "id": masker.mask(question["id"]),
            "instructions": masker.mask(question["instructions"]),
        }
        if "criteria" in question:
            projected["criteria"] = masker.mask_value(
                question["criteria"], include_keys=question["type"] == "choice"
            )
        if question["id"] != projected["id"] or (
            question["type"] == "choice"
            and question["criteria"].keys() != projected["criteria"].keys()
        ):
            raise DecisionError(
                "Use question IDs and choice labels that contain no private Secret data."
            )
        projected_questions.append(projected)
    if require_exact and (state != projected_state or questions != projected_questions):
        raise DecisionError(
            "The exact request contains protected Secret data.", code="incomplete_authority"
        )
    return projected_state, projected_questions


# How a 400 body names a request beyond the context window: TypeSafe's
# documented code (docs.typesafe.ai/api#errors, 2026-10-19), the OpenAI-style
# code and OpenRouter's own message. OpenRouter's wrapping of upstream errors
# is undocumented, so only these exact markers count.
_CONTEXT_OVERFLOW_MARKERS = (
    "max_tokens_exceeded",
    "context_length_exceeded",
    "maximum context length",
    "context window exceeded",
    "exceeds context",
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

    ref: TaskModelTargetRef
    context_window: int | None
    profile: DecisionProfile
    options: dict[str, Any]


def _check_size(state: Any, questions: list[dict[str, Any]], target: _Target) -> None:
    """Check the evidence's native wire footprint against reported limits.

    Scoring Usage can count shared evidence once per question; it is not the
    Context footprint and must never calibrate this estimate.
    """
    body, _ = decision_payload(
        target.ref.model_id, state, questions, target.profile, target.options
    )
    encoded = json.dumps(body, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode(
        "utf-8"
    )
    byte_limit = target.profile.max_request_bytes
    if byte_limit is not None and len(encoded) > byte_limit:
        raise DecisionError(
            f"This item and its questions exceed the Decision request limit of {byte_limit} bytes. "
            "Shorten the item, context or questions.",
            code="too_large",
        )
    text_limit = target.profile.max_text_bytes
    if text_limit is not None and len(encoded) > text_limit:
        raise DecisionError(
            f"This item and its questions exceed the Decision text limit of {text_limit} bytes. "
            "Shorten the item, context or questions.",
            code="too_large",
        )
    estimate, _ = estimate_json_tokens(body)
    tokens = estimate
    limit = target.context_window
    if limit is not None and tokens > limit:
        raise DecisionError(
            f"This item has about {tokens} tokens together with context and all questions; "
            f"the Decision Model reads at most {limit} tokens. Shorten the item, context or "
            "questions.",
            code="too_large",
        )
