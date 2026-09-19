"""OpenRouter Decisions wire; reuses the existing Connection and HTTP owner."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from core.model_tasks.decision_types import DecisionError, validate_answers
from core.providers.task_client import NON_IDEMPOTENT_TASK_REQUEST_RETRY_POLICY, ProviderTaskClient


class ProviderDecisionClient(ProviderTaskClient):
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
            retry_policy=NON_IDEMPOTENT_TASK_REQUEST_RETRY_POLICY,
            json={
                "model": self._model_id,
                "state": state,
                "questions": {
                    q["id"]: {k: v for k, v in q.items() if k != "id"} for q in questions
                },
            },
            parse=lambda response: validate_answers(response.json(), questions),
        )
