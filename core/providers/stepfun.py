"""StepFun direct API and Step Plan policy on the shared Chat Completions wire.

Accepted request parameters and ranges, reasoning ladders, output limits, media,
the admitted Models and the Step Plan-only routing Model live in
``resources/wire/stepfun.json`` and the per-Model catalog facts in
``resources/models/stepfun.overrides.json``; this Adapter owns streaming options
and StepFun status errors."""

from __future__ import annotations

from typing import Any, override

import httpx

from core.providers.errors import ProviderError
from core.providers.openai_compatible import OpenAICompatibleAdapter


class StepFunAdapter(OpenAICompatibleAdapter):
    """StepFun's Chat Completions streaming options and status errors."""

    @override
    def _prepare_stream_payload(self, payload: dict[str, Any]) -> None:
        """Enable SSE without the undocumented OpenAI ``stream_options`` extension."""

        payload["stream"] = True

    @override
    def _classify_http_status(
        self,
        status_code: int,
        *,
        detail: str,
        response_headers: httpx.Headers,
    ) -> None:
        if status_code == 402:
            raise ProviderError(
                f"StepFun balance or Step Plan entitlement is insufficient: {detail}",
                retryable=False,
            )
        if status_code == 451:
            raise ProviderError(
                f"StepFun content safety review rejected the request or response: {detail}",
                retryable=False,
            )
        super()._classify_http_status(
            status_code,
            detail=detail,
            response_headers=response_headers,
        )
