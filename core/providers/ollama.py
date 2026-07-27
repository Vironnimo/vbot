"""Ollama provider adapter.

Speaks Ollama's native ``/api/chat`` wire protocol — not the OpenAI-compatible
shim — so it subclasses :class:`ProviderAdapter` directly (like the Anthropic
adapter). One adapter serves both connections: the keyless ``local`` connection
(``http://localhost:11434``) and the API-key ``cloud`` connection
(``https://ollama.com``, same native API with Bearer auth).

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
- Reasoning is a binary ``think`` toggle; ``capabilities`` containing
  ``"thinking"`` (from ``POST /api/show``) marks support.
- Catalog discovery: ``GET /api/tags`` lists installed models; proxied cloud
  models are recognized by the presence of ``remote_host`` (the ``:cloud``
  name suffix is convention, ``remote_host`` is the fact). Capabilities and
  the model's theoretical context window come from ``POST /api/show`` per
  model (the discovery enrichment hook).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from core.debug import ProviderDebugRecorder

from core.models.models import (
    REASONING_CONTROL_LEVELS,
    REASONING_CONTROL_ON_OFF,
    Capabilities,
    Model,
    ReasoningCapabilities,
)
from core.providers._http_shared import (
    build_async_client,
    classify_http_status,
    decode_response_json,
    parse_sse_json_data,
    wrap_network_error,
)
from core.providers.adapter import IMAGE_WIRE_MEDIA_TYPES, ModelLookup, ProviderAdapter
from core.providers.errors import NetworkError, ProviderError
from core.providers.providers import AuthConfig, ProviderConfig
from core.providers.reasoning import (
    REASONING_INTENT_DEFAULT,
    REASONING_INTENT_EFFORT,
    REASONING_INTENT_OFF,
    REASONING_REPLAY_CURRENT_RUN,
    REASONING_REPLAY_FULL_HISTORY,
    ReasoningReplayPolicy,
    model_reasoning_budget_max,
    model_reasoning_control,
    model_reasoning_levels,
    model_reasoning_supported,
    resolve_reasoning_intent,
)
from core.providers.token_getter import StaticTokenGetter, TokenGetter
from core.providers.tool_schema import render_tool_definitions
from core.utils.logging import get_logger
from core.utils.retry import retry_async

_LOGGER = get_logger("providers.ollama")

CHAT_ENDPOINT = "/api/chat"
SHOW_ENDPOINT = "/api/show"

# Provider-scoped metadata key: ``metadata.ollama.local`` / ``metadata.ollama.remote``
# mark where a discovered model actually runs (see ``normalize_catalog_entry``).
OLLAMA_METADATA_KEY = "ollama"
LOCAL_METADATA_FIELD = "local"
REMOTE_METADATA_FIELD = "remote"
REASONING_REPLAY_METADATA_FIELD = "reasoning_replay"

# ``/api/show`` capability strings.
_CAPABILITY_TOOLS = "tools"
_CAPABILITY_VISION = "vision"
_CAPABILITY_THINKING = "thinking"
_CAPABILITY_COMPLETION = "completion"
_CAPABILITY_EMBEDDING = "embedding"

# Effort ladder used only for snapping when a model has no feed ladder; the
# ``on_off`` render is binary, so the snapped level never reaches the wire.
OLLAMA_EFFORT_FLOOR = ("low", "medium", "high")
OLLAMA_GPT_OSS_EFFORTS = ("low", "medium", "high")
OLLAMA_FULL_HISTORY_MODEL_PREFIXES = ("glm-4.7",)

# Per-model ``/api/show`` enrichment calls run concurrently but bounded, so a
# host with many installed models is not hit with dozens of simultaneous
# requests during a refresh.
_SHOW_DETAIL_CONCURRENCY = 8

# Caller kwargs that translate onto Ollama's ``options`` object.
_OPTION_KWARG_MAP = {
    "temperature": "temperature",
    "max_tokens": "num_predict",
    "top_p": "top_p",
}

_OLLAMA_TOOL_DONE_REASONS = frozenset({"tool_calls"})


class OllamaAdapter(ProviderAdapter):
    """Adapter for Ollama's native ``/api/chat`` API (local and cloud).

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
        # ``connection_mode`` is accepted for parity with the unified
        # ``get_adapter`` call site; both Ollama connections speak one wire.
        del connection_mode
        super().__init__(model_lookup=model_lookup, debug_recorder=debug_recorder)
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
        """Normalize one ``/api/tags`` entry into a conservative vBot :class:`Model`.

        ``/api/tags`` carries no capability or window facts, so the entry is
        conservative (text-only, no tools) until the ``/api/show`` enrichment
        hook fills in capabilities and the context window. Locality is stamped
        here: an entry with ``remote_host`` runs on the remote host (a proxied
        cloud model); one without runs on this Ollama host.
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

        is_remote = bool(raw.get("remote_host"))
        locality_field = REMOTE_METADATA_FIELD if is_remote else LOCAL_METADATA_FIELD

        return Model(
            model_id=model_id,
            name=model_id,
            capabilities=Capabilities(
                vision=False,
                tools=False,
                json_mode=False,
                reasoning=ReasoningCapabilities(supported=False),
                input_modalities=("text",),
                output_modalities=("text",),
            ),
            context_window=None,
            max_output_tokens=None,
            family=family,
            metadata={OLLAMA_METADATA_KEY: {locality_field: True}},
        )

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
            rendered_tools = render_tool_definitions(tools, profile="best_effort")
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

    def reasoning_replay_policy(self, model_id: str) -> ReasoningReplayPolicy:
        """Resolve native Thinking replay from the discovered Model profile."""

        if self._model_lookup is not None:
            model = self._model_lookup(model_id.split("::", 1)[0])
            if model is not None:
                metadata = model.metadata.get(OLLAMA_METADATA_KEY)
                if (
                    isinstance(metadata, Mapping)
                    and metadata.get(REASONING_REPLAY_METADATA_FIELD)
                    == REASONING_REPLAY_FULL_HISTORY
                ):
                    return REASONING_REPLAY_FULL_HISTORY
        return REASONING_REPLAY_CURRENT_RUN

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
        reason, so unknown support means the field stays absent. Most Models use
        a boolean; GPT-OSS is profiled with its documented level-only ladder and
        receives ``low``/``medium``/``high`` strings instead.
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
            # GPT-OSS cannot disable thinking and ignores booleans. Omitting the
            # field is the only truthful representation of an unsupported off
            # request; the provider keeps its default.
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

        async def _connect_stream() -> httpx.Response:
            # Rebuild headers per attempt (parity with the other adapters).
            headers = await self._build_headers()
            request = self._client.build_request(
                "POST",
                CHAT_ENDPOINT,
                json=payload,
                headers=headers,
            )
            try:
                response = await self._client.send(request, stream=True)
            except httpx.TransportError as exc:
                raise self._wrap_transport_error(exc) from exc
            if response.status_code >= 400:
                error_body = (await response.aread()).decode("utf-8", errors="replace")
                await response.aclose()
                detail = _build_error_detail(response.status_code, error_body)
                classify_http_status(
                    response.status_code,
                    detail=detail,
                    response_headers=response.headers,
                )
                raise ProviderError(f"Provider error: {response.status_code}", retryable=False)
            return response

        response = await retry_async(_connect_stream)

        has_tool_calls = False
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
                    for tool_call in _extract_ollama_tool_calls(message.get("tool_calls")) or []:
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


# ---------------------------------------------------------------------------
# Message translation: canonical → Ollama wire
# ---------------------------------------------------------------------------


def _to_ollama_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [_to_ollama_message(message) for message in messages]


def _to_ollama_message(message: dict[str, Any]) -> dict[str, Any]:
    role = message.get("role")
    if role == "tool":
        return {
            "role": "tool",
            "content": _flatten_text_content(message.get("content", "")),
            "tool_call_id": message.get("tool_call_id", ""),
        }
    if role == "assistant":
        return _to_ollama_assistant_message(message)

    content, images = _split_content_blocks(message.get("content", ""))
    wire_message: dict[str, Any] = {"role": role, "content": content}
    if images:
        wire_message["images"] = images
    return wire_message


def _to_ollama_assistant_message(message: dict[str, Any]) -> dict[str, Any]:
    wire_message: dict[str, Any] = {
        "role": "assistant",
        "content": _flatten_text_content(message.get("content") or ""),
    }
    reasoning = message.get("reasoning")
    if isinstance(reasoning, str) and reasoning:
        # Ollama round-trips visible thinking text via the ``thinking`` field.
        wire_message["thinking"] = reasoning
    tool_calls = message.get("tool_calls")
    if tool_calls:
        wire_message["tool_calls"] = [
            {
                "id": tool_call["id"],
                "function": {
                    "name": tool_call["name"],
                    # Ollama's wire takes arguments as a JSON object, matching
                    # the canonical dict — no string encoding (unlike OpenAI).
                    "arguments": tool_call.get("arguments", {}),
                },
            }
            for tool_call in tool_calls
        ]
    return wire_message


def _split_content_blocks(content: Any) -> tuple[str, list[str]]:
    """Split canonical content into flat text plus a base64 image list.

    Ollama carries images as a per-message ``images`` array of bare base64
    strings, separate from the text content.
    """

    if not isinstance(content, list):
        return ("" if content is None else str(content), [])

    text_parts: list[str] = []
    images: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            text_parts.append(str(block))
            continue
        block_type = block.get("type")
        if block_type == "text":
            text = block.get("text")
            if text:
                text_parts.append(str(text))
        elif block_type == "media":
            base64_data = block.get("base64")
            media_type = block.get("media_type")
            if not isinstance(base64_data, str) or not isinstance(media_type, str):
                raise ProviderError(
                    "media content block requires string base64 and media_type fields",
                    retryable=False,
                )
            if not media_type.startswith("image/"):
                raise ProviderError(
                    f"Ollama adapter supports only image media blocks; received {media_type}",
                    retryable=False,
                )
            images.append(base64_data)
        else:
            raise ProviderError(
                f"Ollama adapter does not support '{block_type}' content blocks",
                retryable=False,
            )
    return ("\n\n".join(text_parts), images)


def _flatten_text_content(content: Any) -> str:
    text, _images = _split_content_blocks(content)
    return text


# ---------------------------------------------------------------------------
# Response extraction: Ollama wire → canonical
# ---------------------------------------------------------------------------


def _extract_ollama_tool_calls(raw_tool_calls: Any) -> list[dict[str, Any]] | None:
    if not isinstance(raw_tool_calls, list):
        return None
    tool_calls: list[dict[str, Any]] = []
    for position, raw_call in enumerate(raw_tool_calls):
        if not isinstance(raw_call, dict):
            continue
        function = raw_call.get("function")
        if not isinstance(function, dict):
            continue
        name = function.get("name")
        if not isinstance(name, str) or not name:
            continue
        arguments = function.get("arguments")
        if not isinstance(arguments, dict):
            arguments = {}
        tool_call_id = raw_call.get("id")
        if not isinstance(tool_call_id, str) or not tool_call_id:
            tool_call_id = f"tool_call_{position}"
        tool_calls.append({"id": tool_call_id, "name": name, "arguments": dict(arguments)})
    return tool_calls or None


def _extract_ollama_usage(response: Mapping[str, Any]) -> dict[str, Any] | None:
    input_tokens = response.get("prompt_eval_count")
    output_tokens = response.get("eval_count")
    if not isinstance(input_tokens, int):
        return None
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens if isinstance(output_tokens, int) else 0,
    }


def _normalize_ollama_done_reason(done_reason: Any, *, has_tool_calls: bool) -> str:
    if done_reason in _OLLAMA_TOOL_DONE_REASONS or has_tool_calls:
        return "tool_calls"
    return "stop"


def _build_error_detail(status_code: int, response_body: str = "") -> str:
    """Build an error detail from Ollama's ``{"error": "..."}`` response shape."""

    detail = str(status_code)
    try:
        error_data = json.loads(response_body) if response_body else {}
        error_message = error_data.get("error", "") if isinstance(error_data, dict) else ""
        if error_message:
            detail = f"{status_code}: {error_message}"
    except json.JSONDecodeError:
        if response_body:
            detail = f"{status_code}: {response_body}"
    return detail


# ---------------------------------------------------------------------------
# Discovery enrichment helpers
# ---------------------------------------------------------------------------


def _enrich_from_show(model: Model, show_response: Mapping[str, Any]) -> Model:
    """Return *model* enriched with capabilities and window from ``/api/show``."""

    capabilities_list = show_response.get("capabilities")
    capability_names = (
        {name for name in capabilities_list if isinstance(name, str)}
        if isinstance(capabilities_list, list)
        else set()
    )

    tools = _CAPABILITY_TOOLS in capability_names
    vision = _CAPABILITY_VISION in capability_names
    thinking = _CAPABILITY_THINKING in capability_names

    reasoning = _ollama_reasoning_capabilities(model.model_id, thinking)
    input_modalities = ("text", "image") if vision else ("text",)

    return Model(
        model_id=model.model_id,
        name=model.name,
        capabilities=Capabilities(
            vision=vision,
            tools=tools,
            json_mode=False,
            reasoning=reasoning,
            input_modalities=input_modalities,
            output_modalities=("text",),
        ),
        context_window=_context_window_from_show(show_response),
        max_output_tokens=model.max_output_tokens,
        family=model.family,
        metadata=_ollama_enriched_metadata(model),
        connections=model.connections,
    )


def _ollama_reasoning_capabilities(
    model_id: str,
    thinking: bool,
) -> ReasoningCapabilities:
    if not thinking:
        return ReasoningCapabilities(supported=False)
    bare_id = model_id.split(":", 1)[0].lower()
    if bare_id == "gpt-oss":
        return ReasoningCapabilities(
            supported=True,
            control=REASONING_CONTROL_LEVELS,
            levels=OLLAMA_GPT_OSS_EFFORTS,
        )
    return ReasoningCapabilities(supported=True, control=REASONING_CONTROL_ON_OFF)


def _ollama_enriched_metadata(model: Model) -> dict[str, Any]:
    metadata = {
        key: dict(value) if isinstance(value, Mapping) else value
        for key, value in model.metadata.items()
    }
    ollama_metadata = metadata.get(OLLAMA_METADATA_KEY)
    provider_metadata = dict(ollama_metadata) if isinstance(ollama_metadata, Mapping) else {}
    bare_id = model.model_id.split(":", 1)[0].lower()
    if bare_id.startswith(OLLAMA_FULL_HISTORY_MODEL_PREFIXES):
        provider_metadata[REASONING_REPLAY_METADATA_FIELD] = REASONING_REPLAY_FULL_HISTORY
    metadata[OLLAMA_METADATA_KEY] = provider_metadata
    return metadata


def _context_window_from_show(show_response: Mapping[str, Any]) -> int | None:
    """Read the model's theoretical max context from ``model_info``.

    The window lives under the key ``"<architecture>.context_length"`` where
    ``<architecture>`` is ``model_info["general.architecture"]`` (e.g.
    ``mistral3.context_length``). Only that exact key is read — a suffix scan
    would wrongly match ``*.rope.scaling.original_context_length``.
    """

    model_info = show_response.get("model_info")
    if not isinstance(model_info, Mapping):
        return None
    architecture = model_info.get("general.architecture")
    if not isinstance(architecture, str) or not architecture:
        return None
    value = model_info.get(f"{architecture}.context_length")
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value
