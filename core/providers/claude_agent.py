"""Claude Code as a Provider, with vBot owning Agents, Tools and System Prompts."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Mapping, Sequence
from contextlib import aclosing
from typing import TYPE_CHECKING, Any, ClassVar, Literal, override

from core.models.models import Capabilities, Model, ReasoningCapabilities
from core.providers._claude_agent_sdk import SDK_POOL
from core.providers._claude_agent_sessions import ClaudeAgentSessions, Conversation
from core.providers._claude_agent_wire import JsonObject, history, system_prompt
from core.providers.adapter import ModelLookup, ProviderAdapter, normalize_tool_call_candidates
from core.providers.errors import CatalogEntrySkipped, ProviderError
from core.providers.providers import AuthConfig, ProviderConfig
from core.providers.reasoning_dialects import render_reasoning
from core.providers.tool_schema import render_tool_definitions
from core.providers.wire_profile import Protocol

if TYPE_CHECKING:
    from core.debug import ProviderDebugRecorder
    from core.providers.http import ProviderHTTPTransportScope
    from core.providers.token_getter import TokenGetter

__all__ = ["ClaudeAgentAdapter", "ClaudeAgentSessions"]

CONVERSATION_KEY = "_claude_agent_conversation"


class ClaudeAgentAdapter(ProviderAdapter):
    """Translate one canonical Model step through a parked native Claude turn."""

    WIRE_PROTOCOLS: ClassVar[tuple[Protocol, ...]] = ("claude_agent",)

    def __init__(
        self,
        config: ProviderConfig,
        token_getter: TokenGetter | str | None = None,
        base_url: str | None = None,
        auth_config: AuthConfig | None = None,
        model_lookup: ModelLookup | None = None,
        debug_recorder: ProviderDebugRecorder | None = None,
        *,
        connection_mode: str | None = None,
        http_transport: ProviderHTTPTransportScope | None = None,
        sessions: ClaudeAgentSessions | None = None,
        connection_id: str | None = None,
    ) -> None:
        super().__init__(model_lookup, debug_recorder)
        del token_getter, base_url, auth_config, http_transport
        self._config = config
        self._connection_mode = connection_mode
        self._connection_id = connection_id or config.connections[0].id
        self._sessions = sessions or ClaudeAgentSessions()
        self._owns_sessions = sessions is None
        self._used: set[Conversation] = set()

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
    ) -> JsonObject:
        del prompt_cache_affinity_id
        if request_kind != "chat":
            return {}
        return {
            CONVERSATION_KEY: Conversation(
                project_id, agent_id, session_id, session_generation_id, self._connection_id
            )
        }

    @override
    async def aclose(self) -> None:
        if self._owns_sessions:
            await self._sessions.aclose()
        else:
            for conversation in self._used:
                await self._sessions.release(conversation)
        self._used.clear()

    @override
    async def stream(
        self, messages: list[dict], *, model_id: str, **kwargs: Any
    ) -> AsyncGenerator[dict]:
        self._refuse_unadmitted_model(model_id)
        conversation = kwargs.pop(CONVERSATION_KEY, None)
        if conversation is not None and not isinstance(conversation, Conversation):
            raise ProviderError("Invalid Claude Agent conversation context", retryable=False)
        if conversation is not None:
            self._used.add(conversation)
        tools = await SDK_POOL.run(
            render_tool_definitions, kwargs.get("tools") or [], profile="omit_strict"
        )
        if kwargs.get("tool_choice") == "none":
            tools = []
        controls: JsonObject = {}
        limit = kwargs.get("max_tokens")
        if isinstance(limit, int) and not isinstance(limit, bool) and limit > 0:
            controls["max_tokens"] = limit
        reasoning = self.wire_profile(model_id).reasoning
        render_reasoning(
            reasoning,
            reasoning.plan(kwargs.get("thinking_effort"), output_allowance=limit),
            controls,
            output_allowance=limit,
        )
        async with aclosing(
            self._sessions.stream(
                conversation=conversation,
                messages=messages,
                system=system_prompt(messages),
                model=model_id,
                tools=tools,
                controls=controls,
            )
        ) as stream:
            async for delta in stream:
                yield delta

    @override
    async def send(self, messages: list[dict], *, model_id: str, **kwargs: Any) -> dict:
        response: JsonObject = {
            "content": "",
            "reasoning": "",
            "usage": {},
            "terminal_outcome": "unknown",
        }
        calls: dict[str, JsonObject] = {}
        async for delta in self.stream(messages, model_id=model_id, **kwargs):
            delta_type = delta["type"]
            if delta_type == "content_delta":
                response["content"] += delta["text"]
            elif delta_type == "reasoning_delta":
                response["reasoning"] += delta["text"]
            elif delta_type == "tool_call_delta":
                call = calls.setdefault(
                    delta["id"], {"id": delta["id"], "name": "", "arguments": ""}
                )
                call["name"] += delta.get("name_delta", "")
                call["arguments"] += delta.get("arguments_delta", "")
            elif delta_type == "usage":
                response["usage"] = {key: value for key, value in delta.items() if key != "type"}
            elif delta_type == "finish":
                response["terminal_outcome"] = delta["reason"]
        if calls:
            response["tool_calls"] = _normalize_calls(list(calls.values()))
        return response

    @override
    def normalize_response(
        self, response: JsonObject, *, model_id: str | None = None
    ) -> JsonObject:
        del model_id
        result = dict(response)
        if result.get("tool_calls"):
            result["tool_calls"] = _normalize_calls(result["tool_calls"])
        return result

    @override
    def estimate_request_input_tokens(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        model_id: str,
        tools: Sequence[Mapping[str, Any]] | None = None,
    ) -> int:
        # Rebuilds frame previous roles and calls as text, so budget that footprint.
        from core.utils.tokens import estimate_structured_tokens

        payload = [system_prompt(messages), history(messages), tools or []]
        return estimate_structured_tokens(payload, model_id=model_id)[0]

    @classmethod
    def normalize_catalog_entry(
        cls, raw: Mapping[str, Any], defaults: Mapping[str, Any] | None = None
    ) -> Model:
        del defaults
        model_id = raw.get("value")
        if not isinstance(model_id, str) or not model_id or model_id == "default":
            raise CatalogEntrySkipped("Claude Code model has no stable id")
        efforts = tuple(
            level
            for level in raw.get("supportedEffortLevels", ())
            if level in {"low", "medium", "high", "xhigh", "max"}
        )
        context = raw.get("contextWindow")
        context = (
            context
            if isinstance(context, int) and not isinstance(context, bool) and context > 0
            else None
        )
        return Model(
            model_id=model_id,
            name=str(raw.get("displayName") or model_id),
            capabilities=Capabilities(
                vision=True,
                tools=True,
                json_mode=False,
                reasoning=ReasoningCapabilities(
                    supported=True if efforts else None,
                    control="levels" if efforts else None,
                    levels=efforts,
                ),
                input_modalities=("text", "image", "pdf"),
                output_modalities=("text",),
            ),
            context_window=context,
            max_output_tokens=None,
            metadata={"claude_agent": {"resolved_model": raw["resolvedModel"]}}
            if isinstance(raw.get("resolvedModel"), str)
            else {},
        )


def _normalize_calls(calls: list[JsonObject]) -> list[JsonObject]:
    return [
        normalized
        for index, call in enumerate(calls)
        for normalized in normalize_tool_call_candidates(
            tool_call_id=call.get("id"),
            name=call.get("name"),
            arguments=call.get("arguments"),
            fallback_id=f"tool_call_{index}",
        )
    ]
