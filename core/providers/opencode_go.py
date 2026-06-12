"""OpenCode Go provider adapter."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from core.debug import ProviderDebugRecorder

from core.providers.adapter import ModelLookup
from core.providers.anthropic import AnthropicAdapter
from core.providers.openai_compatible import OpenAICompatibleAdapter, _to_openai_assistant_message
from core.providers.providers import AuthConfig, ProviderConfig
from core.providers.reasoning import REASONING_REPLAY_FULL_HISTORY, ReasoningReplayPolicy
from core.providers.token_getter import TokenGetter

_ANTHROPIC_MESSAGES_MODELS: frozenset[str] = frozenset(
    {
        "minimax-m2.7",
        "minimax-m2.5",
        "qwen3.5-plus",
    }
)
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
        if _uses_anthropic_messages_path(model_id):
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
        if _uses_anthropic_messages_path(model_id):
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

    def normalize_response(self, response: dict[str, Any]) -> dict[str, Any]:
        if "choices" in response:
            return super().normalize_response(response)
        return self._anthropic.normalize_response(response)

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


def _has_explicit_output_limit(kwargs: dict[str, Any]) -> bool:
    return any(key in kwargs for key in _OUTPUT_LIMIT_KEYS)


def _model_lookup_candidates(model_id: str) -> tuple[str, ...]:
    without_connection_suffix = model_id.split("::", 1)[0]
    candidates = [model_id, without_connection_suffix]
    if "/" in without_connection_suffix:
        candidates.append(without_connection_suffix.rsplit("/", 1)[-1])
    return tuple(dict.fromkeys(candidate for candidate in candidates if candidate))


def _uses_anthropic_messages_path(model_id: str) -> bool:
    return model_id in _ANTHROPIC_MESSAGES_MODELS
