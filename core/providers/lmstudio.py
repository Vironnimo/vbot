"""LM Studio provider adapter with native discovery and lazy model loading."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Mapping
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from core.debug import ProviderDebugRecorder

from core.models.models import REASONING_CONTROL_ON_OFF, Capabilities, Model, ReasoningCapabilities
from core.providers._http_shared import (
    classify_http_status,
    decode_response_json,
    wrap_network_error,
)
from core.providers.adapter import ModelLookup
from core.providers.errors import CatalogEntrySkipped, ProviderError
from core.providers.openai_compatible import OpenAICompatibleAdapter
from core.providers.providers import AuthConfig, ProviderConfig
from core.providers.token_getter import TokenGetter
from core.utils.retry import retry_async

NATIVE_MODELS_ENDPOINT = "/api/v1/models"
NATIVE_MODEL_LOAD_ENDPOINT = "/api/v1/models/load"
LMSTUDIO_METADATA_KEY = "lmstudio"


class LMStudioAdapter(OpenAICompatibleAdapter):
    """Use LM Studio's OpenAI-compatible Chat wire and native model lifecycle API."""

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
        local_context_resolver: Callable[[str], int | None] | None = None,
    ) -> None:
        resolved_base_url = base_url or config.base_url
        self._native_base_url = _native_base_url(resolved_base_url)
        self._local_context_resolver = local_context_resolver
        self._model_load_lock = asyncio.Lock()
        super().__init__(
            config,
            token_getter,
            _chat_base_url(resolved_base_url),
            auth_config,
            model_lookup,
            debug_recorder,
            connection_mode=connection_mode,
        )

    @classmethod
    def normalize_catalog_entry(
        cls,
        raw: Mapping[str, Any],
        defaults: Mapping[str, Any] | None = None,
    ) -> Model:
        """Normalize one native ``GET /api/v1/models`` LLM entry."""

        del defaults
        if raw.get("type") != "llm":
            raise CatalogEntrySkipped("LM Studio entry is not a chat model")

        model_id = raw.get("key")
        if not isinstance(model_id, str) or not model_id:
            raise ProviderError("LM Studio model entry has no key", retryable=False)

        raw_capabilities = raw.get("capabilities")
        capabilities = raw_capabilities if isinstance(raw_capabilities, Mapping) else {}
        raw_reasoning = capabilities.get("reasoning")
        reasoning = raw_reasoning if isinstance(raw_reasoning, Mapping) else {}
        allowed_options = reasoning.get("allowed_options")
        reasoning_options = (
            {option for option in allowed_options if isinstance(option, str)}
            if isinstance(allowed_options, list)
            else set()
        )
        supports_reasoning = "on" in reasoning_options
        supports_vision = capabilities.get("vision") is True

        display_name = raw.get("display_name")
        architecture = raw.get("architecture")
        return Model(
            model_id=model_id,
            name=(display_name if isinstance(display_name, str) and display_name else model_id),
            capabilities=Capabilities(
                vision=supports_vision,
                tools=capabilities.get("trained_for_tool_use") is True,
                json_mode=False,
                reasoning=ReasoningCapabilities(
                    supported=supports_reasoning,
                    control=REASONING_CONTROL_ON_OFF if supports_reasoning else None,
                ),
                input_modalities=("text", "image") if supports_vision else ("text",),
                output_modalities=("text",),
            ),
            context_window=_positive_int(raw.get("max_context_length")),
            max_output_tokens=None,
            family=architecture if isinstance(architecture, str) else "",
            metadata={LMSTUDIO_METADATA_KEY: {"local": True}},
        )

    async def send(
        self,
        messages: list[dict[str, Any]],
        *,
        model_id: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        await self._ensure_model_loaded(model_id)
        return await super().send(messages, model_id=model_id, **kwargs)

    async def stream(
        self,
        messages: list[dict[str, Any]],
        *,
        model_id: str,
        **kwargs: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        await self._ensure_model_loaded(model_id)
        async for event in super().stream(messages, model_id=model_id, **kwargs):
            yield event

    async def _ensure_model_loaded(self, model_id: str) -> None:
        bare_model_id = model_id.split("::", 1)[0]
        async with self._model_load_lock:
            model = await self._native_model(bare_model_id)
            loaded_instances = model.get("loaded_instances")
            if isinstance(loaded_instances, list) and loaded_instances:
                return

            payload: dict[str, Any] = {"model": bare_model_id}
            if self._local_context_resolver is not None:
                context_length = self._local_context_resolver(bare_model_id)
                if context_length is not None:
                    payload["context_length"] = context_length
            await self._load_native_model(payload)

    async def _native_model(self, model_id: str) -> Mapping[str, Any]:
        async def _request() -> Mapping[str, Any]:
            try:
                response = await self._client.get(
                    f"{self._native_base_url}{NATIVE_MODELS_ENDPOINT}"
                )
            except httpx.TransportError as exc:
                raise wrap_network_error(exc) from exc
            _classify_lmstudio_response(response)
            payload = decode_response_json(response, "LM Studio")
            models = payload.get("models")
            if not isinstance(models, list):
                raise ProviderError("LM Studio models response has no models list", retryable=False)
            for model in models:
                if isinstance(model, Mapping) and model.get("key") == model_id:
                    return model
            raise ProviderError(
                f"LM Studio model '{model_id}' is not installed",
                retryable=False,
            )

        return await retry_async(_request)

    async def _load_native_model(self, payload: Mapping[str, Any]) -> None:
        try:
            response = await self._client.post(
                f"{self._native_base_url}{NATIVE_MODEL_LOAD_ENDPOINT}",
                json=dict(payload),
            )
        except httpx.TransportError as exc:
            raise wrap_network_error(exc) from exc
        _classify_lmstudio_response(response)
        decode_response_json(response, "LM Studio")


def _classify_lmstudio_response(response: httpx.Response) -> None:
    detail = f"{response.status_code} {response.text}".strip()
    classify_http_status(
        response.status_code,
        detail=detail,
        response_headers=response.headers,
    )


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _chat_base_url(base_url: str) -> str:
    base = base_url.rstrip("/")
    return base if base.endswith("/v1") else f"{base}/v1"


def _native_base_url(base_url: str) -> str:
    base = base_url.rstrip("/")
    return base.removesuffix("/v1")
