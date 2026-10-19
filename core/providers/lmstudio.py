"""LM Studio provider adapter with native discovery and lazy model loading."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Callable, Mapping
from contextlib import aclosing
from typing import TYPE_CHECKING, Any, cast, override

import httpx

from core.models.models import (
    REASONING_CONTROL_ON_OFF,
    Capabilities,
    Model,
    ReasoningCapabilities,
    text_embedding_capabilities,
)
from core.providers.adapter import ModelLookup
from core.providers.errors import CatalogEntrySkipped, ProviderError
from core.providers.http import (
    classify_http_status,
    decode_response_json,
    wrap_network_error,
)
from core.providers.openai_compatible import OpenAICompatibleAdapter
from core.providers.providers import AuthConfig, ProviderConfig
from core.providers.token_getter import TokenGetter
from core.utils.retry import retry_async

if TYPE_CHECKING:
    from core.debug import ProviderDebugRecorder


NATIVE_MODELS_ENDPOINT = "/api/v1/models"
NATIVE_MODEL_LOAD_ENDPOINT = "/api/v1/models/load"
LMSTUDIO_METADATA_KEY = "lmstudio"
# Native ``GET /api/v1/models`` entry types: chat Models and embedding Models.
_LLM_TYPE = "llm"
_EMBEDDING_TYPE = "embedding"


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
        loaded_context_reporter: Callable[[str, int | None], None] | None = None,
    ) -> None:
        """Bind the Connection; ``local_context_resolver`` and ``loaded_context_reporter`` are DI.

        ``local_context_resolver`` maps a Model id to the window vBot loads an
        unloaded Model with (the user's local setting or the local cap).
        ``loaded_context_reporter`` receives the window LM Studio reports for a
        Model's loaded instance, or ``None`` when no loaded instance reports
        one, each time the Adapter reads it before a request.
        """
        resolved_base_url = base_url or config.base_url
        self._native_base_url = _native_base_url(resolved_base_url)
        self._local_context_resolver = local_context_resolver
        self._loaded_context_reporter = loaded_context_reporter
        self._loaded_context_windows: dict[str, int] = {}
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
    @override
    def openai_compatible_base_url(cls, base_url: str) -> str:
        """LM Studio serves its OpenAI-compatible API under ``/v1``."""

        return _chat_base_url(base_url)

    @classmethod
    @override
    def normalize_catalog_entry(
        cls,
        raw: Mapping[str, Any],
        defaults: Mapping[str, Any] | None = None,
    ) -> Model:
        """Normalize one native ``GET /api/v1/models`` LLM or embedding entry."""

        del defaults
        model_type = raw.get("type")
        if model_type not in {_LLM_TYPE, _EMBEDDING_TYPE}:
            raise CatalogEntrySkipped("LM Studio entry is neither a chat nor an embedding model")

        model_id = raw.get("key")
        if not isinstance(model_id, str) or not model_id:
            raise ProviderError("LM Studio model entry has no key", retryable=False)

        display_name = raw.get("display_name")
        name = display_name if isinstance(display_name, str) and display_name else model_id
        architecture = raw.get("architecture")
        family = architecture if isinstance(architecture, str) else ""
        context_window = _positive_int(raw.get("max_context_length"))
        metadata = {LMSTUDIO_METADATA_KEY: {"local": True}}
        if model_type == _EMBEDDING_TYPE:
            return Model(
                model_id=model_id,
                name=name,
                capabilities=text_embedding_capabilities(),
                context_window=context_window,
                max_output_tokens=None,
                family=family,
                metadata=metadata,
            )

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

        return Model(
            model_id=model_id,
            name=name,
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
            context_window=context_window,
            max_output_tokens=None,
            family=family,
            metadata=metadata,
        )

    @override
    async def send(
        self,
        messages: list[dict[str, Any]],
        *,
        model_id: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        await self._ensure_model_loaded(model_id)
        return await super().send(messages, model_id=model_id, **kwargs)

    @override
    async def stream(
        self,
        messages: list[dict[str, Any]],
        *,
        model_id: str,
        **kwargs: Any,
    ) -> AsyncGenerator[dict[str, Any]]:
        await self._ensure_model_loaded(model_id)
        async with aclosing(
            cast(
                AsyncGenerator[dict[str, Any]],
                super().stream(messages, model_id=model_id, **kwargs),
            )
        ) as events:
            async for event in events:
                yield event

    @override
    def _request_context_window(self, model_id: str) -> int:
        """The window of the loaded instance, else the one vBot loads the Model with."""

        bare_model_id = model_id.split("::", 1)[0]
        loaded = self._loaded_context_windows.get(bare_model_id)
        if loaded is not None:
            return loaded
        if self._local_context_resolver is not None:
            configured = self._local_context_resolver(bare_model_id)
            if configured is not None:
                return configured
        return super()._request_context_window(model_id)

    async def _ensure_model_loaded(self, model_id: str) -> None:
        """Reuse a loaded instance, or load one; then record the window it reports.

        A loaded instance is used as it is, whatever the local setting says:
        its reported ``context_length`` is the window LM Studio serves. Only a
        Model that is not loaded is loaded with the resolved local window, and
        the new instance is read back for the window LM Studio applied.
        """
        bare_model_id = model_id.split("::", 1)[0]
        async with self._model_load_lock:
            model = await self._native_model(bare_model_id)
            loaded_instances = model.get("loaded_instances")
            if not (isinstance(loaded_instances, list) and loaded_instances):
                # Forget the window of an instance that is gone before resolving
                # the one to load with, so the local setting decides again.
                self._record_loaded_context_window(bare_model_id, None)
                payload: dict[str, Any] = {"model": bare_model_id}
                if self._local_context_resolver is not None:
                    context_length = self._local_context_resolver(bare_model_id)
                    if context_length is not None:
                        payload["context_length"] = context_length
                await self._load_native_model(payload)
                model = await self._native_model(bare_model_id)
            self._record_loaded_context_window(bare_model_id, _loaded_context_length(model))

    def _record_loaded_context_window(self, model_id: str, window: int | None) -> None:
        if window is None:
            self._loaded_context_windows.pop(model_id, None)
        else:
            self._loaded_context_windows[model_id] = window
        if self._loaded_context_reporter is not None:
            self._loaded_context_reporter(model_id, window)

    async def _native_model(self, model_id: str) -> Mapping[str, Any]:
        async def _request() -> Mapping[str, Any]:
            try:
                response = await self._client.get(
                    f"{self._native_base_url}{NATIVE_MODELS_ENDPOINT}"
                )
            except httpx.TransportError as exc:
                raise wrap_network_error(exc) from exc
            _classify_lmstudio_response(response, idempotent=True)
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
        _classify_lmstudio_response(response, idempotent=False)
        decode_response_json(response, "LM Studio")


def _classify_lmstudio_response(response: httpx.Response, *, idempotent: bool) -> None:
    detail = f"{response.status_code} {response.text}".strip()
    classify_http_status(
        response.status_code,
        idempotent=idempotent,
        detail=detail,
        response_headers=response.headers,
    )


def _positive_int(value: Any) -> int | None:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        return None
    return value


def _loaded_context_length(model: Mapping[str, Any]) -> int | None:
    """The ``context_length`` LM Studio reports for the Model's loaded instances.

    Each entry of ``loaded_instances`` carries the window it was loaded with in
    ``config.context_length``. With several instances any of them may serve a
    request, so the smallest reported window holds; ``None`` when none reports one.
    """
    instances = model.get("loaded_instances")
    if not isinstance(instances, list):
        return None
    windows = [
        window
        for instance in instances
        if isinstance(instance, Mapping)
        and isinstance(config := instance.get("config"), Mapping)
        and (window := _positive_int(config.get("context_length"))) is not None
    ]
    return min(windows) if windows else None


def _chat_base_url(base_url: str) -> str:
    base = base_url.rstrip("/")
    return base if base.endswith("/v1") else f"{base}/v1"


def _native_base_url(base_url: str) -> str:
    base = base_url.rstrip("/")
    return base.removesuffix("/v1")
