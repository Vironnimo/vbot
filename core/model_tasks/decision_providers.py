"""OpenRouter Decisions wire; reuses the existing Connection and HTTP owner."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from core.model_tasks.decision_types import DecisionError, validate_answers, wire_questions
from core.providers.task_client import DEFAULT_TASK_REQUEST_RETRY_POLICY, ProviderTaskClient


class ProviderDecisionClient(ProviderTaskClient):
    # OpenRouter documents 524 (edge timeout) and 529 (Provider overloaded) for
    # this endpoint; TypeSafe asks to retry 529 with backoff (2026-10-19).
    EXTRA_RETRYABLE_STATUS_CODES = frozenset({524, 529})

    async def evaluate(
        self,
        state: Any,
        questions: list[dict[str, Any]],
        *,
        http_client: httpx.AsyncClient | None = None,
    ) -> dict[str, Any]:
        if self._provider.id != "openrouter":
            raise DecisionError(
                "Decision execution currently requires an OpenRouter target.",
                code="unsupported_target",
            )
        # Alpha lives at the origin, outside the normal /api/v1 prefix.
        base = urlsplit(self._base_url)
        endpoint = urlunsplit((base.scheme, base.netloc, "/api/alpha/decisions", "", ""))
        return await self.post_and_parse(
            endpoint,
            timeout=60,
            http_client=http_client,
            # A judgment changes nothing beyond its small charge, and Usage
            # records every attempt, so an unanswered request is safe to repeat.
            retry_policy=DEFAULT_TASK_REQUEST_RETRY_POLICY,
            json={
                "model": self._model_id,
                "state": state,
                "questions": wire_questions(questions),
            },
            parse=lambda response: validate_answers(_json_or_none(response), questions),
        )


def _json_or_none(response: httpx.Response) -> Any:
    """The response JSON; ``None`` (an invalid result) when the body is not JSON."""
    try:
        return response.json()
    except ValueError, UnicodeError:
        return None
