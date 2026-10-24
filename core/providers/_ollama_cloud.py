"""Ollama cloud."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, override

import httpx

from core.providers._ollama_wire import _ollama_openai_base_url
from core.providers.adapter import ModelLookup
from core.providers.errors import NetworkError, ProviderError
from core.providers.openai_compatible import (
    OpenAICompatibleAdapter,
)
from core.providers.providers import AuthConfig, ProviderConfig
from core.providers.token_getter import TokenGetter

if TYPE_CHECKING:
    from core.debug import ProviderDebugRecorder
    from core.providers.http import ProviderHTTPTransportScope


class OllamaCloudAdapter(OpenAICompatibleAdapter):
    """OpenAI-compatible chat transport for direct Ollama Cloud connections.

    Discovery deliberately remains mapped to :class:`OllamaAdapter`; this class
    owns the Cloud chat transport, usage quirk and error classification. The wire
    itself (effort vocabulary, explicit off, reasoning carriers, media, body
    limit) is described by ``resources/wire/ollama-cloud.json``.
    """

    @classmethod
    @override
    def openai_compatible_base_url(cls, base_url: str) -> str:
        """The configured Cloud base is native; its OpenAI-compatible API is ``/v1``."""

        return _ollama_openai_base_url(base_url)

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
        http_transport: ProviderHTTPTransportScope | None = None,
    ) -> None:
        native_base_url = base_url or config.base_url
        self._cloud_base_url = _ollama_openai_base_url(native_base_url)
        super().__init__(
            config,
            token_getter,
            self._cloud_base_url,
            auth_config,
            model_lookup=model_lookup,
            debug_recorder=debug_recorder,
            connection_mode=connection_mode,
            http_transport=http_transport,
        )

    @override
    def _wrap_transport_error(self, exc: httpx.TransportError) -> Exception:
        """Preserve the Provider-specific direct Cloud connection diagnostic."""

        if isinstance(exc, httpx.ConnectError):
            return NetworkError(f"Ollama Cloud is not reachable at {self._cloud_base_url} ({exc})")
        return super()._wrap_transport_error(exc)

    @override
    def _classify_http_status(
        self,
        status_code: int,
        *,
        detail: str,
        response_headers: httpx.Headers,
    ) -> None:
        if status_code == 429 and _is_pro_allowance_exhausted(detail):
            error = ProviderError(f"Rate limited: {detail}", retryable=False)
            error.status_code = status_code
            raise error
        super()._classify_http_status(status_code, detail=detail, response_headers=response_headers)

    @override
    def normalize_response(
        self, response: dict[str, Any], *, model_id: str | None = None
    ) -> dict[str, Any]:
        """Normalize a Cloud response without trusting impossible zero input usage."""

        normalized = super().normalize_response(response, model_id=model_id)
        _drop_ollama_cloud_zero_prompt_tokens(normalized.get("usage"), response.get("usage"))
        return normalized

    @override
    def _normalize_stream_chunk(
        self,
        raw_chunk: dict[str, Any],
        tool_call_slots: set[int],
        normalization_state: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        deltas = super()._normalize_stream_chunk(
            raw_chunk,
            tool_call_slots,
            normalization_state,
        )
        raw_usage = raw_chunk.get("usage")
        for delta in deltas:
            if delta.get("type") == "usage":
                _drop_ollama_cloud_zero_prompt_tokens(delta, raw_usage)
        # A usage chunk whose only counter was the dropped zero input carries
        # nothing; a usage delta must hold a counter.
        return [
            delta
            for delta in deltas
            if delta.get("type") != "usage" or "input_tokens" in delta or "output_tokens" in delta
        ]


def _is_pro_allowance_exhausted(detail: str) -> bool:
    """Recognize the Cloud allowance refusal observed on 2026-10-13.

    Keep other 429 bodies retryable: Cloud also uses that status for transient
    throttling. The optional billing advice and request reference vary.
    """

    try:
        body = json.loads(detail.removeprefix("429 "))
    except json.JSONDecodeError:
        return False
    error = body.get("error") if isinstance(body, dict) else None
    if not isinstance(error, dict) or error.get("type") != "api_error":
        return False
    message = error.get("message")
    return (
        error.get("code") is None
        and isinstance(message, str)
        and message.startswith("You reached your Pro 5-hour limit.")
    )


def _drop_ollama_cloud_zero_prompt_tokens(normalized_usage: Any, raw_usage: Any) -> None:
    """Treat Cloud ``prompt_tokens: 0`` as absent for non-empty chat requests.

    MiniMax M3 returns zero for short and Tool requests while returning positive
    counts for longer prompts. Every vBot chat request has at least one message,
    so zero cannot be a truthful input-token measurement. Removing only that
    field lets the chat layer retain measured output while estimating input.
    """

    if not isinstance(normalized_usage, dict) or not isinstance(raw_usage, Mapping):
        return
    prompt_tokens = raw_usage.get("prompt_tokens")
    if (
        isinstance(prompt_tokens, int)
        and not isinstance(prompt_tokens, bool)
        and prompt_tokens == 0
    ):
        normalized_usage.pop("input_tokens", None)
