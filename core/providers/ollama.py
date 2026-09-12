"""Ollama provider adapters.

Local ``ollama`` speaks the native ``/api/chat`` wire. Direct ``ollama-cloud``
chat uses Ollama's OpenAI-compatible ``/v1/chat/completions`` wire because that
route reliably returns model reasoning and accepts the documented
``reasoning_effort`` control. Both Providers still share Ollama's native
``/api/tags`` and ``/api/show`` discovery contract, and account usage remains a
separate native ``/api/usage`` concern.

Key wire facts (verified live against Ollama 0.24.0 on 2026-07-07):

- Non-streaming: ``POST /api/chat`` with ``"stream": false`` returns one JSON
  object with ``message`` (``content``, optional ``thinking``, optional
  ``tool_calls``), ``done_reason``, and usage counters
  ``prompt_eval_count``/``eval_count``.
- Streaming: ``"stream": true`` returns **NDJSON lines** (one JSON object per
  line, not SSE); the final line has ``"done": true`` plus usage counters.
- Tool-call ``function.arguments`` is a JSON **object**, not a string (unlike
  OpenAI). vBot's canonical arguments are also a dict, so the mapping is direct.
- Sampling/runtime parameters ride under ``options`` (``temperature``,
  ``num_predict``, ``num_ctx``, …).
- Reasoning uses Ollama's ``think`` Boolean/level control; ``capabilities``
  containing ``"thinking"`` (from ``POST /api/show``) marks support and the
  Model DB supplies any Provider-specific effort ladder.
- Catalog discovery: ``GET /api/tags`` lists available Models; a local proxy's
  Cloud Models are recognized by ``remote_host`` (the ``:cloud`` suffix is
  convention, ``remote_host`` is the fact), while direct Cloud scope is known
  from its Connection. Capabilities and theoretical context come from
  ``POST /api/show`` per Model (the discovery enrichment hook)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import httpx

from core.models.models import (
    REASONING_CONTROL_LEVELS,
    Capabilities,
    Model,
)
from core.providers._http_shared import (
    build_async_client,
    classify_http_status,
    connect_streaming_with_retry,
    decode_response_json,
    parse_sse_json_data,
    wrap_network_error,
)
from core.providers._ollama_catalog import (
    _enrich_from_show,
    _is_gpt_oss_model,
    _ollama_reasoning_capabilities,
    _positive_int,
)
from core.providers._ollama_cloud import (
    OllamaCloudAdapter,
)
from core.providers._ollama_constants import (
    _CAPABILITY_THINKING,
    _CAPABILITY_TOOLS,
    _CAPABILITY_VISION,
    _LOGGER,
    _OPTION_KWARG_MAP,
    _SHOW_DETAIL_CONCURRENCY,
    CHAT_ENDPOINT,
    LOCAL_METADATA_FIELD,
    OLLAMA_CLOUD_MODE,
    OLLAMA_CLOUD_REASONING_EFFORTS,
    OLLAMA_EFFORT_FLOOR,
    OLLAMA_GPT_OSS_EFFORTS,
    OLLAMA_LOCAL_MODE,
    OLLAMA_METADATA_KEY,
    REMOTE_METADATA_FIELD,
    SHOW_ENDPOINT,
)
from core.providers._ollama_wire import (
    _build_error_detail,
    _extract_ollama_tool_calls,
    _extract_ollama_usage,
    _normalize_ollama_done_reason,
    _ollama_stream_tool_calls,
    _to_ollama_messages,
)
from core.providers.adapter import (
    IMAGE_WIRE_MEDIA_TYPES,
    ModelLookup,
    ProviderAdapter,
)
from core.providers.errors import NetworkError, ProviderError
from core.providers.providers import AuthConfig, ConnectionConfig, ProviderConfig
from core.providers.reasoning import (
    REASONING_INTENT_DEFAULT,
    REASONING_INTENT_EFFORT,
    REASONING_INTENT_OFF,
    model_reasoning_budget_max,
    model_reasoning_control,
    model_reasoning_levels,
    model_reasoning_supported,
    resolve_reasoning_intent,
)
from core.providers.token_getter import StaticTokenGetter, TokenGetter
from core.providers.tool_schema import render_tool_definitions
from core.utils.retry import retry_async

if TYPE_CHECKING:
    from core.debug import ProviderDebugRecorder

__all__ = [
    "CHAT_ENDPOINT",
    "LOCAL_METADATA_FIELD",
    "OLLAMA_CLOUD_MODE",
    "OLLAMA_CLOUD_REASONING_EFFORTS",
    "OLLAMA_EFFORT_FLOOR",
    "OLLAMA_GPT_OSS_EFFORTS",
    "OLLAMA_LOCAL_MODE",
    "OLLAMA_METADATA_KEY",
    "OllamaAdapter",
    "OllamaCloudAdapter",
    "REMOTE_METADATA_FIELD",
    "SHOW_ENDPOINT",
]


class OllamaAdapter(ProviderAdapter):
    """Native ``/api/chat`` adapter for local and locally proxied Cloud Models.

    Args:
        config: Immutable provider configuration.
        token_getter: Async callable returning the current auth token. Empty
            for the keyless local connection; the auth header is skipped when
            either the header name or the token is empty.
        base_url: Per-connection base URL override.
        auth_config: Per-connection auth configuration.
        local_context_resolver: Optional callable mapping a model id to the
            enforced effective context window for flagged-local models
            (``None`` for everything else). When it returns a window, the
            request carries ``options.num_ctx`` so Ollama loads the model
            with exactly that window instead of silently truncating.
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
        local_context_resolver: Callable[[str], int | None] | None = None,
    ) -> None:
        self._config = config
        self._token_getter = (
            StaticTokenGetter(token_getter) if isinstance(token_getter, str) else token_getter
        )
        self._auth_config = auth_config or config.connections[0].auth
        self._local_context_resolver = local_context_resolver
        self._connection_mode = connection_mode or OLLAMA_LOCAL_MODE
        super().__init__(
            model_lookup=model_lookup,
            debug_recorder=debug_recorder,
            reasoning_replay_default=config.reasoning_replay,
        )
        self._base_url = base_url or config.base_url
        self._client = build_async_client(
            base_url=self._base_url,
            debug_recorder=debug_recorder,
        )

    def _wrap_transport_error(self, exc: httpx.TransportError) -> Exception:
        """Classify a transport failure, naming the likely cause for connect errors.

        A refused/failed connection to an Ollama endpoint almost always means
        the service is simply not running — say so instead of surfacing a bare
        socket error. Stays a retryable ``NetworkError`` so the shared retry
        and chat-loop error handling are unchanged.
        """

        if isinstance(exc, httpx.ConnectError):
            if self._connection_mode == OLLAMA_CLOUD_MODE:
                return NetworkError(f"Ollama Cloud is not reachable at {self._base_url} ({exc})")
            return NetworkError(
                f"Ollama is not reachable at {self._base_url} — "
                f"is the Ollama service running? ({exc})"
            )
        return wrap_network_error(exc)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def aclose(self) -> None:
        """Close the HTTP client and release resources."""
        await self._client.aclose()

    async def __aenter__(self) -> OllamaAdapter:
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.aclose()

    # ------------------------------------------------------------------
    # Wire media capability
    # ------------------------------------------------------------------

    def wire_media_support(self, model_id: str) -> frozenset[str]:
        """The Ollama chat wire carries base64 images (per-message ``images`` list)."""
        del model_id
        return IMAGE_WIRE_MEDIA_TYPES

    # ------------------------------------------------------------------
    # Catalog discovery
    # ------------------------------------------------------------------

    @classmethod
    def normalize_catalog_entry(
        cls,
        raw: Mapping[str, Any],
        defaults: Mapping[str, Any] | None = None,
    ) -> Model:
        """Normalize one ``/api/tags`` entry into a vBot :class:`Model`.

        Current Ollama versions expose capabilities and context length here;
        older versions omit them. Missing facts stay conservative until the
        ``/api/show`` enrichment hook fills them in. Locality is stamped here:
        an entry with ``remote_host`` is proxied, while one without runs on the
        local Ollama host.
        """

        del defaults
        model_id = raw.get("model") or raw.get("name")
        if not isinstance(model_id, str) or not model_id:
            raise ProviderError("Ollama /api/tags entry has no model id", retryable=False)

        details = raw.get("details")
        family = ""
        if isinstance(details, Mapping):
            family_value = details.get("family")
            if isinstance(family_value, str):
                family = family_value

        raw_capabilities = raw.get("capabilities")
        capability_names = (
            {name for name in raw_capabilities if isinstance(name, str)}
            if isinstance(raw_capabilities, list)
            else set()
        )
        tools = _CAPABILITY_TOOLS in capability_names
        vision = _CAPABILITY_VISION in capability_names
        thinking = _CAPABILITY_THINKING in capability_names

        is_remote = bool(raw.get("remote_host"))
        locality_field = REMOTE_METADATA_FIELD if is_remote else LOCAL_METADATA_FIELD

        return Model(
            model_id=model_id,
            name=model_id,
            capabilities=Capabilities(
                vision=vision,
                tools=tools,
                json_mode=False,
                reasoning=_ollama_reasoning_capabilities(model_id, thinking),
                input_modalities=("text", "image") if vision else ("text",),
                output_modalities=("text",),
            ),
            context_window=(
                _positive_int(details.get("context_length"))
                if isinstance(details, Mapping)
                else None
            ),
            max_output_tokens=None,
            family=family,
            metadata={OLLAMA_METADATA_KEY: {locality_field: True}},
        )

    @classmethod
    def finalize_discovered_model(
        cls,
        model: Model,
        connection: ConnectionConfig | None,
    ) -> Model:
        """Stamp direct Ollama Cloud catalog entries as remote.

        A local Ollama proxy identifies offloaded Models with ``remote_host``.
        The direct ``ollama.com/api/tags`` response has no such field because
        every entry is already remote, so Connection context supplies the
        missing fact.
        """

        if connection is None or connection.mode != OLLAMA_CLOUD_MODE:
            return model
        metadata = {
            key: dict(value) if isinstance(value, Mapping) else value
            for key, value in model.metadata.items()
        }
        ollama_metadata = metadata.get(OLLAMA_METADATA_KEY)
        provider_metadata = dict(ollama_metadata) if isinstance(ollama_metadata, Mapping) else {}
        provider_metadata.pop(LOCAL_METADATA_FIELD, None)
        provider_metadata[REMOTE_METADATA_FIELD] = True
        metadata[OLLAMA_METADATA_KEY] = provider_metadata
        return replace(model, metadata=metadata)

    @classmethod
    async def enrich_discovered_models(
        cls,
        normalized_models: Mapping[str, Model],
        post_json: Callable[[str, dict[str, Any]], Awaitable[Any]],
    ) -> dict[str, Model]:
        """Enrich each discovered model from ``POST /api/show``.

        Fills typed capabilities (tools / vision / thinking) from the
        ``capabilities`` list and the model's theoretical context window from
        ``model_info["<architecture>.context_length"]``. A failed or malformed
        per-model response leaves that model at its conservative baseline —
        enrichment is fail-soft per model, never a failed refresh.
        """

        semaphore = asyncio.Semaphore(_SHOW_DETAIL_CONCURRENCY)
        model_ids = list(normalized_models.keys())

        async def _fetch_show(model_id: str) -> Any:
            async with semaphore:
                return await post_json(SHOW_ENDPOINT, {"model": model_id})

        details = await asyncio.gather(
            *(_fetch_show(model_id) for model_id in model_ids),
            return_exceptions=True,
        )

        enriched: dict[str, Model] = {}
        for model_id, detail in zip(model_ids, details, strict=True):
            if isinstance(detail, BaseException) and not isinstance(detail, Exception):
                raise detail
            if isinstance(detail, Exception):
                _LOGGER.warning("Ollama /api/show failed for '%s': %s", model_id, detail)
                continue
            if not isinstance(detail, Mapping):
                _LOGGER.warning("Ollama /api/show returned a non-object for '%s'", model_id)
                continue
            enriched[model_id] = _enrich_from_show(normalized_models[model_id], detail)
        return enriched

    # ------------------------------------------------------------------
    # Header / payload helpers
    # ------------------------------------------------------------------

    async def _build_headers(self) -> dict[str, str]:
        """Build request headers; the auth header is skipped when keyless."""
        headers: dict[str, str] = {}
        token = await self._token_getter()
        if self._auth_config.header and token:
            headers[self._auth_config.header] = f"{self._auth_config.prefix}{token}"
        if self._config.extra_headers:
            headers.update(self._config.extra_headers)
        return headers

    def _build_payload(
        self,
        messages: list[dict[str, Any]],
        model_id: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Build the native ``/api/chat`` request payload."""

        request_kwargs = {key: value for key, value in kwargs.items() if value is not None}
        payload: dict[str, Any] = {
            "model": model_id,
            "messages": _to_ollama_messages(messages),
        }

        tools = request_kwargs.pop("tools", None)
        if tools:
            rendered_tools = render_tool_definitions(tools, profile="omit_strict")
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool["name"],
                        "description": tool["description"],
                        "parameters": tool["parameters"],
                    },
                }
                for tool in rendered_tools
            ]

        self._apply_reasoning(payload, request_kwargs, model_id)

        options: dict[str, Any] = {}
        merged_defaults = dict(self._config.defaults or {})
        merged_defaults.update(request_kwargs)
        for kwarg_name, option_name in _OPTION_KWARG_MAP.items():
            value = merged_defaults.get(kwarg_name)
            if value is not None:
                options[option_name] = value

        enforced_context = self._resolve_enforced_context(model_id)
        if enforced_context is not None:
            options["num_ctx"] = enforced_context

        if options:
            payload["options"] = options
        return payload

    def _resolve_enforced_context(self, model_id: str) -> int | None:
        """Return the enforced ``num_ctx`` for flagged-local models, else ``None``."""

        if self._local_context_resolver is None:
            return None
        return self._local_context_resolver(model_id)

    def _apply_reasoning(
        self,
        payload: dict[str, Any],
        request_kwargs: dict[str, Any],
        model_id: str,
    ) -> None:
        """Render the shared reasoning intent onto the Model's Ollama ``think`` control.

        The toggle is only sent when the catalog positively marks the model as
        thinking-capable — Ollama rejects ``think`` on models that cannot
        reason, so unknown support means the field stays absent. Model metadata
        chooses Boolean or level control; GPT-OSS is the level-only special case
        that cannot accept Boolean off.
        """

        thinking_effort = request_kwargs.pop("thinking_effort", "")
        supported = model_reasoning_supported(self._model_lookup, model_id)
        if supported is not True:
            return
        intent = resolve_reasoning_intent(
            supported=supported,
            control=model_reasoning_control(self._model_lookup, model_id),
            levels=model_reasoning_levels(self._model_lookup, model_id) or OLLAMA_EFFORT_FLOOR,
            effort=thinking_effort,
            budget_max=model_reasoning_budget_max(self._model_lookup, model_id),
            max_tokens=None,
        )
        if intent.kind == REASONING_INTENT_DEFAULT:
            return
        if model_reasoning_control(self._model_lookup, model_id) == REASONING_CONTROL_LEVELS:
            if intent.kind == REASONING_INTENT_EFFORT and intent.effort_level is not None:
                payload["think"] = intent.effort_level
            elif intent.kind == REASONING_INTENT_OFF and not _is_gpt_oss_model(model_id):
                # Direct Cloud Models may expose an effort ladder through
                # models.dev while still accepting Ollama's documented Boolean
                # off switch. GPT-OSS is the exception: it ignores Booleans and
                # cannot disable its trace.
                payload["think"] = False
            return
        payload["think"] = intent.kind != REASONING_INTENT_OFF

    # ------------------------------------------------------------------
    # Response normalization
    # ------------------------------------------------------------------

    def normalize_response(
        self, response: dict[str, Any], *, model_id: str | None = None
    ) -> dict[str, Any]:
        """Normalize an Ollama ``/api/chat`` response to canonical assistant fields."""

        del model_id
        message = response.get("message")
        if not isinstance(message, dict):
            message = {}
        content = message.get("content")
        thinking = message.get("thinking")
        normalized: dict[str, Any] = {
            "role": "assistant",
            "content": content if isinstance(content, str) and content else None,
            "reasoning": thinking if isinstance(thinking, str) and thinking else None,
            "reasoning_meta": None,
            "tool_calls": _extract_ollama_tool_calls(message.get("tool_calls")),
        }
        usage = _extract_ollama_usage(response)
        if usage is not None:
            normalized["usage"] = usage
        return normalized

    # ------------------------------------------------------------------
    # send() — non-streaming
    # ------------------------------------------------------------------

    async def send(
        self,
        messages: list[dict[str, Any]],
        *,
        model_id: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Send a non-streaming ``/api/chat`` request.

        Retries on retryable errors (transport failures, 429/502/503/504) via
        ``retry_async``; fails immediately on fatal statuses.
        """

        payload = self._build_payload(messages, model_id, **kwargs)
        payload["stream"] = False

        async def _do_request() -> dict[str, Any]:
            headers = await self._build_headers()
            try:
                response = await self._client.post(CHAT_ENDPOINT, json=payload, headers=headers)
            except httpx.TransportError as exc:
                raise self._wrap_transport_error(exc) from exc
            if response.status_code >= 400:
                detail = _build_error_detail(response.status_code, response.text)
                classify_http_status(
                    response.status_code,
                    idempotent=False,
                    detail=detail,
                    response_headers=response.headers,
                )
            return dict(decode_response_json(response, "Ollama provider"))

        return await retry_async(_do_request)

    # ------------------------------------------------------------------
    # stream() — NDJSON streaming
    # ------------------------------------------------------------------

    async def stream(
        self,
        messages: list[dict[str, Any]],
        *,
        model_id: str,
        **kwargs: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        """Send a streaming ``/api/chat`` request and yield normalized deltas.

        Ollama streams NDJSON — one complete JSON object per line, no SSE
        framing. The final line carries ``"done": true`` plus the usage
        counters; the stream ending without it is a mid-stream failure.
        """

        payload = self._build_payload(messages, model_id, **kwargs)
        payload["stream"] = True

        def _handle_error_status(
            status_code: int,
            error_body: str,
            response_headers: httpx.Headers,
        ) -> None:
            classify_http_status(
                status_code,
                idempotent=False,
                detail=_build_error_detail(status_code, error_body),
                response_headers=response_headers,
            )

        response = await connect_streaming_with_retry(
            self._client,
            CHAT_ENDPOINT,
            payload,
            build_headers=self._build_headers,
            handle_error_status=_handle_error_status,
            wrap_transport_error=self._wrap_transport_error,
        )

        has_tool_calls = False
        tool_call_count = 0
        seen_done = False
        try:
            async for line in response.aiter_lines():
                if not line.strip():
                    continue
                parsed = parse_sse_json_data(line, context="Ollama provider")
                if not isinstance(parsed, dict):
                    continue
                error_message = parsed.get("error")
                if error_message:
                    raise ProviderError(f"Provider stream error: {error_message}", retryable=False)

                message = parsed.get("message")
                if isinstance(message, dict):
                    thinking = message.get("thinking")
                    if isinstance(thinking, str) and thinking:
                        yield {"type": "reasoning_delta", "text": thinking}
                    content = message.get("content")
                    if isinstance(content, str) and content:
                        yield {"type": "content_delta", "text": content}
                    for tool_call in _ollama_stream_tool_calls(
                        message.get("tool_calls"), start_index=tool_call_count
                    ):
                        tool_call_count += 1
                        has_tool_calls = True
                        yield {
                            "type": "tool_call_delta",
                            "id": tool_call["id"],
                            "name_delta": tool_call["name"],
                            "arguments_delta": json.dumps(
                                tool_call["arguments"], separators=(",", ":")
                            ),
                        }

                if parsed.get("done") is True:
                    seen_done = True
                    usage = _extract_ollama_usage(parsed)
                    if usage is not None:
                        yield {"type": "usage", **usage}
                    yield {
                        "type": "finish",
                        "reason": _normalize_ollama_done_reason(
                            parsed.get("done_reason"), has_tool_calls=has_tool_calls
                        ),
                    }
                    break
            if not seen_done:
                raise NetworkError("Stream ended without a done chunk")
        except httpx.TimeoutException as exc:
            raise wrap_network_error(exc) from exc
        except httpx.TransportError as exc:
            raise NetworkError(f"Stream read failed: {exc}") from exc
        finally:
            await response.aclose()
