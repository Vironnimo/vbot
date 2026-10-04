"""xAI Responses adapter for API-key and SuperGrok OAuth Connections.

Every xAI request speaks the stateless ``/responses`` protocol. Its request
shape (optional parameters, reasoning, media) comes from the wire profile
(``resources/wire/xai.json``) through the inherited Responses codec.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar, override

from core.providers.openai import OpenAIAdapter
from core.providers.providers import ConnectionConfig
from core.providers.wire_profile import Protocol


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
