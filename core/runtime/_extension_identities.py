"""Registration-bound management of Extension-created Identity Agents."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from typing import Any

from core.agents import Agent, AgentError, AgentStore
from core.utils.workers import BoundedWorkerPool

_WORKERS = BoundedWorkerPool(name="extension-identities", max_workers=2)
_FIELDS = frozenset(
    {
        "name",
        "model",
        "fallback_models",
        "temperature",
        "top_p",
        "thinking_effort",
        "memory_prompt_mode",
        "project_memory_enabled",
        "tool_access",
        "allowed_skills",
        "excluded_skills",
        "tools",
        "compaction_policy",
        "tool_loading",
        "permission_rules",
        "description",
    }
)


class ExtensionIdentityAgents:
    """Trusted owner integration; domain/remit authorization remains with the Extension."""

    def __init__(
        self,
        agents: AgentStore,
        identity: Any,
        require_current: Callable[[Any], None],
        *,
        require_chat_model: Callable[[str], None] | None = None,
    ):
        self._agents = agents
        self._identity = identity
        self._require_current = require_current
        self._require_chat_model = require_chat_model

    def _record(self, agent: Agent) -> dict[str, Any]:
        return {
            "agent_id": agent.id,
            "incarnation": agent.created_at,
            "name": agent.name,
            "model": agent.model,
            "created_by_owner": agent.extension_owner == self._identity.name,
        }

    def _get(self, agent_id: str) -> Agent:
        self._require_current(self._identity)
        # Explicitly establish older identities' missing creation proof using the
        # existing incarnation owner, under its mutation/lifecycle guard.
        with self._agents.lifecycle_guard():
            self._require_current(self._identity)
            agent = self._agents.identify(agent_id)
            if agent.builtin is not None:
                raise AgentError("Built-in Agents cannot join an Extension team")
            return agent

    async def get(self, agent_id: str) -> dict[str, Any]:
        return self._record(await _WORKERS.run(self._get, agent_id))

    async def create(
        self, agent_id: str, *, request_id: str | None = None, **config: Any
    ) -> dict[str, Any]:
        self._check_fields(config)
        if request_id is not None and (
            not isinstance(request_id, str) or not request_id or len(request_id) > 128
        ):
            raise AgentError("Invalid identity creation request id")
        request = (
            None
            if request_id is None
            else {
                "id": request_id,
                "hash": hashlib.sha256(
                    json.dumps(
                        config,
                        sort_keys=True,
                        separators=(",", ":"),
                        default=lambda value: value.to_dict(),
                    ).encode()
                ).hexdigest(),
            }
        )

        def create() -> Agent:
            with self._agents.lifecycle_guard():
                self._require_current(self._identity)
                if self._agents.exists(agent_id):
                    existing = self._agents.get(agent_id)
                    if (
                        request is not None
                        and existing.extension_owner == self._identity.name
                        and existing.extension_creation_request == request
                    ):
                        return existing
                    raise AgentError("Identity creation request conflicts with an existing Agent")
                return self._agents.create(
                    agent_id,
                    extension_owner=self._identity.name,
                    extension_creation_request=request,
                    **config,
                )

        return self._record(await _WORKERS.run(create))

    async def update(
        self, agent_id: str, *, expected_incarnation: str, **changes: Any
    ) -> dict[str, Any]:
        self._check_fields(changes)

        def update() -> Agent:
            with self._agents.lifecycle_guard():
                agent = self._get(agent_id)
                if (
                    agent.created_at != expected_incarnation
                    or agent.extension_owner != self._identity.name
                ):
                    raise AgentError(
                        "Identity configuration requires this Extension's original incarnation"
                    )
                return self._agents.update(agent_id, **changes)

        return self._record(await _WORKERS.run(update))

    def _check_fields(self, config: dict[str, Any]) -> None:
        if self._require_chat_model is not None:
            fallbacks = config.get("fallback_models")
            models = [config.get("model"), *(fallbacks if isinstance(fallbacks, list) else [])]
            for model in models:
                if isinstance(model, str) and model:
                    self._require_chat_model(model)
        if unknown := config.keys() - _FIELDS:
            raise AgentError("Unsupported identity settings: " + ", ".join(sorted(unknown)))
