"""OpenCode gateway session affinity shared by its Go and Zen chat codecs."""

from __future__ import annotations

import hashlib
import json
from functools import cached_property
from typing import Any, Literal, override
from uuid import uuid4

from core.providers.adapter import ProviderAdapter

OPENCODE_SESSION_HEADER = "x-opencode-session"
OPENCODE_SESSION_ID_KWARG = "_opencode_session_id"


class OpenCodeSessionRouting(ProviderAdapter):
    """Route Chat forks by their cache lineage and isolated calls by an opaque ID."""

    @override
    def request_context_kwargs(
        self,
        *,
        agent_id: str,
        session_id: str,
        project_id: str | None = None,
        prompt_cache_affinity_id: str | None = None,
        session_generation_id: str | None = None,
        request_kind: Literal["chat", "compaction", "utility"] = "chat",
    ) -> dict[str, Any]:
        del session_generation_id, request_kind
        if prompt_cache_affinity_id is not None:
            routing_id = prompt_cache_affinity_id
        else:
            address = json.dumps(
                [project_id, agent_id, session_id], ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
            routing_id = hashlib.sha256(address).hexdigest()[:32]
        return {OPENCODE_SESSION_ID_KWARG: f"vbot-{routing_id}"}

    @cached_property
    def _isolated_session_id(self) -> str:
        # Standalone model jobs acquire their own adapter and reuse it for retries.
        return f"vbot-{uuid4().hex}"

    @override
    def _request_headers_from_kwargs(self, request_kwargs: dict[str, Any]) -> dict[str, str]:
        session_id = request_kwargs.pop(OPENCODE_SESSION_ID_KWARG, None)
        if not isinstance(session_id, str) or not session_id:
            session_id = self._isolated_session_id
        return {OPENCODE_SESSION_HEADER: session_id}
