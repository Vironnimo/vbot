"""OpenCode Go provider adapter."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from core.debug import ProviderDebugRecorder

from core.providers.adapter import ModelLookup
from core.providers.anthropic import AnthropicAdapter
from core.providers.openai_compatible import OpenAICompatibleAdapter, _to_openai_assistant_message
from core.providers.providers import AuthConfig, ProviderConfig
from core.providers.reasoning import REASONING_REPLAY_FULL_HISTORY, ReasoningReplayPolicy
from core.providers.token_getter import TokenGetter
from core.utils.logging import get_logger

_LOGGER = get_logger("providers.opencode_go")

# Provider-scoped metadata blob + field carrying the per-model wire protocol
# (Phase 5). The published opencode-go protocol table is a per-model FACT, so it
# lives in data (the opencode-go override's ``metadata.opencode_go.protocol``),
# not in a hardcoded adapter set. The adapter only owns the MECHANICS — how to
# build an Anthropic ``/messages`` vs an OpenAI ``/chat/completions`` request.
OPENCODE_GO_METADATA_KEY = "opencode_go"
PROTOCOL_METADATA_KEY = "protocol"
PROTOCOL_ANTHROPIC = "anthropic"
PROTOCOL_OPENAI = "openai"
# The endpoint returns bare ids with no protocol, so a model the override does
# not mark is unknown: route it the SAFE default (OpenAI chat/completions) and
# warn, so a newly added model is never silently misrouted onto the wrong wire.
_DEFAULT_PROTOCOL = PROTOCOL_OPENAI

_OUTPUT_LIMIT_KEYS = frozenset({"max_tokens", "max_completion_tokens", "max_output_tokens"})


class OpenCodeGoAdapter(OpenAICompatibleAdapter):
    """OpenAI-compatible adapter for the OpenCode Go gateway.

    Models with reasoning capability (DeepSeek, Kimi, GLM, ...) return
    ``reasoning_content`` in assistant messages.

    Both routes replay assistant reasoning for the full same-model history
    (``full_history`` policy): the OpenAI ``/chat/completions`` route expects
    ``reasoning_content`` round-tripping for every historical assistant
    message, and the Anthropic ``/messages`` route accepts replayed signed
    thinking blocks across run boundaries (both verified against the real
    gateway, 2026-06-13). History shaping is owned by the chat layer; this
    adapter only translates whatever reasoning survives shaping onto the wire.
    """

    def __init__(
        self,
        config: ProviderConfig,
        token_getter: TokenGetter | str,
        base_url: str | None = None,
        auth_config: AuthConfig | None = None,
        model_lookup: ModelLookup | None = None,
        debug_recorder: ProviderDebugRecorder | None = None,
        *,
        connection_mode: str | None = None,
    ) -> None:
        # ``connection_mode`` is accepted for parity with the unified
        # ``get_adapter`` call site but is not used by the OpenCode Go
        # adapter; the inner OpenAI-compatible and Anthropic adapters
        # inherit it through the same parameter.
        del connection_mode
        super().__init__(
            config,
            token_getter,
            base_url,
            auth_config,
            model_lookup=model_lookup,
            debug_recorder=debug_recorder,
        )
        selected_auth_config = auth_config or config.connections[0].auth
        # The inner adapter shares the same recorder, so the single context
        # set via set_debug_context() is seen by whichever client handles the
        # request (OpenAI chat/completions or the Anthropic messages path).
        self._anthropic = AnthropicAdapter(
            config,
            token_getter,
            base_url=base_url,
            auth_config=AuthConfig(
                header="x-api-key",
                prefix="",
                credential_key=selected_auth_config.credential_key,
            ),
            model_lookup=model_lookup,
            debug_recorder=debug_recorder,
        )

    async def aclose(self) -> None:
        await super().aclose()
        await self._anthropic.aclose()

    def reasoning_replay_policy(self, model_id: str) -> ReasoningReplayPolicy:
        """Replay persisted reasoning across runs on both gateway routes.

        Verified against the real gateway (2026-06-13): the OpenAI route
        accepts ``reasoning_content`` on completed historical assistant
        messages, the Anthropic route accepts replayed signed thinking blocks
        across run boundaries.
        """
        del model_id
        return REASONING_REPLAY_FULL_HISTORY

    async def send(
        self,
        messages: list[dict[str, Any]],
        *,
        model_id: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        request_kwargs = self._kwargs_with_model_output_limit(model_id, kwargs)
        if self._uses_anthropic_messages_path(model_id):
            return await self._anthropic.send(
                messages,
                model_id=model_id,
                **request_kwargs,
            )
        return await super().send(messages, model_id=model_id, **request_kwargs)

    def stream(
        self,
        messages: list[dict[str, Any]],
        *,
        model_id: str,
        **kwargs: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        request_kwargs = self._kwargs_with_model_output_limit(model_id, kwargs)
        if self._uses_anthropic_messages_path(model_id):
            return self._anthropic.stream(
                messages,
                model_id=model_id,
                **request_kwargs,
            )
        return super().stream(messages, model_id=model_id, **request_kwargs)

    def _build_payload(
        self,
        messages: list[dict[str, Any]],
        model_id: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        request_kwargs = self._kwargs_with_model_output_limit(model_id, kwargs)
        return super()._build_payload(messages, model_id, **request_kwargs)

    def normalize_response(
        self, response: dict[str, Any], *, model_id: str | None = None
    ) -> dict[str, Any]:
        if "choices" in response:
            return super().normalize_response(response, model_id=model_id)
        return self._anthropic.normalize_response(response, model_id=model_id)

    def _format_assistant_message(self, message: dict[str, Any]) -> dict[str, Any]:
        wire = _to_openai_assistant_message(message)
        reasoning = message.get("reasoning")
        if isinstance(reasoning, str) and reasoning:
            wire["reasoning_content"] = reasoning
        return wire

    def _kwargs_with_model_output_limit(
        self,
        model_id: str,
        kwargs: dict[str, Any],
    ) -> dict[str, Any]:
        request_kwargs = dict(kwargs)
        if _has_explicit_output_limit(request_kwargs):
            return request_kwargs

        max_output_tokens = self._model_max_output_tokens(model_id)
        if max_output_tokens is not None:
            request_kwargs["max_tokens"] = max_output_tokens
        return request_kwargs

    def _model_max_output_tokens(self, model_id: str) -> int | None:
        if self._model_lookup is None:
            return None

        for candidate in _model_lookup_candidates(model_id):
            model = self._model_lookup(candidate)
            if (
                model is not None
                and model.max_output_tokens is not None
                and model.max_output_tokens > 0
            ):
                return model.max_output_tokens
        return None

    def _uses_anthropic_messages_path(self, model_id: str) -> bool:
        """Route by the per-model ``metadata.opencode_go.protocol`` wire fact.

        ``"anthropic"`` → the internal Messages adapter; anything else →
        the OpenAI ``/chat/completions`` default. A model the override does not
        mark (no metadata, or no ``protocol`` key) is unknown: it takes the
        safe OpenAI default AND logs a ``warn``, so a newly added model is never
        silently misrouted onto the wrong wire.
        """

        return self._model_protocol(model_id) == PROTOCOL_ANTHROPIC

    def _model_protocol(self, model_id: str) -> str:
        protocol = self._lookup_protocol(model_id)
        if protocol in (PROTOCOL_ANTHROPIC, PROTOCOL_OPENAI):
            return protocol
        # Unknown model (or a malformed/absent protocol fact): default safe and
        # warn so a misroute surfaces in logs instead of silently picking a wire.
        _LOGGER.warning(
            "OpenCode Go model '%s' has no metadata protocol; defaulting to '%s' "
            "(chat/completions). Add metadata.opencode_go.protocol to its override "
            "entry to route it explicitly.",
            model_id,
            _DEFAULT_PROTOCOL,
        )
        return _DEFAULT_PROTOCOL

    def _lookup_protocol(self, model_id: str) -> str | None:
        if self._model_lookup is None:
            return None
        for candidate in _model_lookup_candidates(model_id):
            model = self._model_lookup(candidate)
            if model is None:
                continue
            opencode_go = model.metadata.get(OPENCODE_GO_METADATA_KEY)
            if isinstance(opencode_go, Mapping):
                protocol = opencode_go.get(PROTOCOL_METADATA_KEY)
                if isinstance(protocol, str):
                    return protocol
            # The model exists but carries no protocol fact — stop here so the
            # caller warns and defaults rather than scanning weaker candidates.
            return None
        return None


def _has_explicit_output_limit(kwargs: dict[str, Any]) -> bool:
    return any(key in kwargs for key in _OUTPUT_LIMIT_KEYS)


def _model_lookup_candidates(model_id: str) -> tuple[str, ...]:
    without_connection_suffix = model_id.split("::", 1)[0]
    candidates = [model_id, without_connection_suffix]
    if "/" in without_connection_suffix:
        candidates.append(without_connection_suffix.rsplit("/", 1)[-1])
    return tuple(dict.fromkeys(candidate for candidate in candidates if candidate))
