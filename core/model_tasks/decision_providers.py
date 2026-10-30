"""Native Decision wires: typed judgments, never generated Chat completions."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any, override
from urllib.parse import urlsplit, urlunsplit

import httpx

from core.model_tasks.decision_pricing import price_decision_usage
from core.model_tasks.decision_profile import DecisionProfile
from core.model_tasks.decision_types import DecisionError, validate_answers, wire_questions
from core.providers.task_client import (
    DEFAULT_TASK_REQUEST_RETRY_POLICY,
    ProviderTaskClient,
    is_omittable_option,
)
from core.providers.wire_profiles import standalone_wire_binding


def decision_payload(
    model_id: str,
    state: Any,
    questions: list[dict[str, Any]],
    profile: DecisionProfile,
    options: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, str]]:
    """Build the exact body used for both preflight and transport.

    Caller ids are labels, not instructions. Remap ids that a native wire
    cannot represent, and restore them on receipt without altering evidence.
    """
    wire = profile.wire
    if not profile.supported:
        raise DecisionError("This target has no native Decision route.", code="not_configured")
    ids = {q["id"]: q["id"] for q in questions}
    if wire.protocol == "system_one" and any(
        re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", q["id"]) is None for q in questions
    ):
        ids = {q["id"]: f"q{index}" for index, q in enumerate(questions, 1)}
    if wire.protocol == "openai_decisions":
        # State is evidence. JSON keys never become instructions or image fetches.
        evidence: Any = state if isinstance(state, str) else _json_text(state)
        body: dict[str, Any] = {
            "model": model_id,
            "input": evidence,
            "questions": [_openai_question(q) for q in questions],
        }
    else:
        # This executor judges text/JSON, not media. Some native gateways
        # interpret image-shaped objects even deep inside state; JSON text
        # keeps all supplied fields evidence, with no hidden media operation.
        evidence = state if isinstance(state, str) else _json_text(state)
        if wire.state_field:
            evidence = {wire.state_field: evidence}
        mapped = [{**q, "id": ids[q["id"]]} for q in questions]
        body = {"model": model_id, "state": evidence, "questions": wire_questions(mapped)}
    for name in wire.allowed_options:
        value = options.get(name)
        if is_omittable_option(value):
            continue
        if name == "provider_options":
            if not isinstance(value, Mapping):
                raise DecisionError("Decision provider_options must be an object.")
            body["provider"] = dict(value)
        elif name in {"keep_alive", "prompt_cache_key"}:
            if not isinstance(value, str):
                raise DecisionError(f"Decision {name} must be text.")
            if name == "prompt_cache_key" and len(value) > 64:
                raise DecisionError("Decision prompt_cache_key accepts at most 64 characters.")
            body[name] = value
    return body, {sent: original for original, sent in ids.items()}


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _openai_question(question: dict[str, Any]) -> dict[str, Any]:
    kind = question["type"]
    instructions = question["instructions"]
    criteria: Any = question.get("criteria")
    result = {
        "name": question["id"],
        "type": "predicate" if kind == "noul" else kind,
        "instructions": instructions,
    }
    if kind == "noul" and criteria:
        result["instructions"] = (
            f"{instructions}\nYes means: {criteria['true']}\nNo means: {criteria['false']}"
        )
    elif kind == "choice":
        result["choices"] = [
            {"value": label, "description": text} for label, text in criteria.items()
        ]
    elif kind == "score":
        result["levels"] = [
            {"label": str(index), "description": text} for index, text in enumerate(criteria)
        ]
    return result


class ProviderDecisionClient(ProviderTaskClient):
    # Explicit edge/overload failures documented for Decisions.
    EXTRA_RETRYABLE_STATUS_CODES = frozenset({524, 529})
    _decision_profile: DecisionProfile | None = None

    async def evaluate(
        self,
        state: Any,
        questions: list[dict[str, Any]],
        *,
        profile: DecisionProfile | None = None,
        options: Mapping[str, Any] | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> dict[str, Any]:
        if profile is None:
            binding = standalone_wire_binding(
                provider_id=self._provider.id,
                connection_id=self._connection.id,
                protocols=("chat_completions", "responses", "ollama_chat"),
                model_lookup=None,
            )
            profile = DecisionProfile(binding.profile(self._model_id).decision)
        profile.validate_questions(questions)
        self._decision_profile = profile
        body, ids = decision_payload(self._model_id, state, questions, profile, options or {})
        base = urlsplit(self._base_url)
        endpoint = urlunsplit((base.scheme, base.netloc, profile.wire.endpoint, "", ""))
        return await self.post_and_parse(
            endpoint,
            timeout=60,
            http_client=http_client,
            retry_policy=DEFAULT_TASK_REQUEST_RETRY_POLICY,
            json=body,
            parse=lambda response: _parse(response, questions, profile, ids),
        )

    @override
    async def _observe_response_usage(self, response: httpx.Response) -> Mapping[str, Any] | None:
        raw = await super()._observe_response_usage(response)
        profile = self._decision_profile
        return price_decision_usage(raw or {}, profile.pricing if profile is not None else None)


def _parse(
    response: httpx.Response,
    questions: list[dict[str, Any]],
    profile: DecisionProfile,
    ids: dict[str, str],
) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError, UnicodeError:
        payload = None
    if isinstance(payload, dict):
        payload = dict(payload)
        if profile.wire.protocol == "openai_decisions":
            payload["answers"] = _openai_answers(payload.get("answers"))
        elif isinstance(payload.get("answers"), dict):
            payload["answers"] = {
                ids.get(key, key): value for key, value in payload["answers"].items()
            }
    result = validate_answers(payload, questions)
    result["usage"] = price_decision_usage(result["usage"], profile.pricing)
    return result


def _openai_answers(answers: Any) -> Any:
    if not isinstance(answers, list):
        return None
    result: dict[str, Any] = {}
    for answer in answers:
        if not isinstance(answer, dict) or not isinstance(answer.get("name"), str):
            return None
        name = answer["name"]
        if name in result:
            return None
        normalized = {key: value for key, value in answer.items() if key != "name"}
        if answer.get("type") == "predicate":
            normalized["type"] = "noul"
            normalized["noul"] = normalized.pop("probability", None)
        probabilities = answer.get("probabilities")
        if "probabilities" in answer and not isinstance(probabilities, list):
            return None
        if isinstance(probabilities, list):
            values: dict[str, Any] = {}
            for entry in probabilities:
                if not isinstance(entry, dict):
                    return None
                value = entry.get("value")
                if isinstance(value, bool) or not isinstance(value, str | int):
                    return None
                label = str(value)
                if label in values:
                    return None
                values[label] = entry.get("probability")
            normalized["probabilities"] = values
        result[name] = normalized
    return result
