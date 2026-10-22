"""xAI Responses adapter for API-key and SuperGrok OAuth Connections.

Every xAI request speaks the stateless ``/responses`` protocol. Its request
shape (optional parameters, reasoning, media) comes from the wire profile
(``resources/wire/xai.json``) through the inherited Responses codec.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Mapping
from contextlib import aclosing
from typing import Any, ClassVar, override

import httpx

from core.models.pricing import nonnegative_amount
from core.providers._responses_wire import ResponsesStreamState
from core.providers.openai import OpenAIAdapter
from core.providers.providers import ConnectionConfig
from core.providers.wire_profile import Protocol

# xAI reports billed cost in ticks: https://docs.x.ai/developers/cost-tracking
XAI_TICKS_PER_USD = 10_000_000_000


def xai_usage_extras(usage: Any) -> dict[str, Any]:
    """Map xAI's billed ``usage.cost_in_usd_ticks`` to canonical ``reported_cost_usd``.

    The amount is what xAI bills for the request: after cache discounts and
    including server-side Tool fees. An absent or invalid value adds nothing.
    """

    if not isinstance(usage, Mapping):
        return {}
    ticks = nonnegative_amount(usage.get("cost_in_usd_ticks"))
    if ticks is None:
        return {}
    return {"reported_cost_usd": ticks / XAI_TICKS_PER_USD}


class XAIAdapter(OpenAIAdapter):
    """Translate vBot requests to xAI's stateless ``/responses`` protocol."""

    WIRE_PROTOCOLS: ClassVar[tuple[Protocol, ...]] = ("responses",)

    @classmethod
    @override
    def accepts_discovered_model(
        cls,
        raw: Mapping[str, Any],
        connection: ConnectionConfig | None,
    ) -> bool:
        """Keep every entry of xAI's Model listing."""

        del cls, raw, connection
        return True

    @override
    def request_context_kwargs(
        self,
        *,
        agent_id: str,
        session_id: str,
        project_id: str | None = None,
        prompt_cache_affinity_id: str | None = None,
    ) -> dict[str, Any]:
        """Route cache-compatible Session prefixes to the same xAI cache shard."""

        del project_id
        conversation_id = f"{agent_id}:{session_id}"
        return {"prompt_cache_key": prompt_cache_affinity_id or conversation_id}

    @override
    def normalize_response(
        self, response: dict[str, Any], *, model_id: str | None = None
    ) -> dict[str, Any]:
        """Normalize a completed Responses result, keeping xAI's billed cost."""

        normalized = super().normalize_response(response, model_id=model_id)
        usage = normalized.get("usage")
        if isinstance(usage, dict):
            usage.update(xai_usage_extras(response.get("usage")))
        return normalized

    @override
    async def _responses_sse_deltas(
        self,
        response: httpx.Response,
        state: ResponsesStreamState,
    ) -> AsyncGenerator[dict[str, Any]]:
        async with aclosing(super()._responses_sse_deltas(response, state)) as deltas:
            async for delta in deltas:
                if delta.get("type") == "usage":
                    delta.update(xai_usage_extras((state.completed_response or {}).get("usage")))
                yield delta
