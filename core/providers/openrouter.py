"""OpenRouter provider adapter."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from typing import Any
from urllib.parse import quote

import httpx

from core.models.models import (
    MODEL_TASK_ORDER,
    Capabilities,
    Model,
    ReasoningCapabilities,
    derive_model_task_types,
)
from core.providers._http_shared import (
    build_streaming_request,
    classify_http_status,
    decode_response_json,
    wrap_network_error,
)
from core.providers.errors import (
    NetworkError,
    ProviderError,
    classify_in_band_provider_error,
)
from core.providers.github_copilot_responses import (
    ResponsesStreamState,
    build_responses_payload,
    iter_responses_sse_deltas_with_state,
    normalize_responses_response,
)
from core.providers.openai_compatible import (
    OpenAICompatibleAdapter,
    _parse_optional_int,
    _read_mapping,
    _read_string,
    _read_string_list,
)
from core.providers.reasoning import (
    REASONING_INTENT_BUDGET,
    REASONING_INTENT_EFFORT,
    REASONING_INTENT_OFF,
    REASONING_INTENT_ON,
    ReasoningIntent,
    closest_supported_effort,
    model_reasoning_budget_max,
    model_reasoning_control,
    model_reasoning_levels,
    normalize_thinking_effort,
    resolve_reasoning_intent,
)
from core.settings.settings import parse_openrouter_routing
from core.utils.logging import get_logger
from core.utils.retry import retry_async

OPENROUTER_REASONING_EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh"}
# OpenRouter's documented off-shape for a native thinking toggle (``on_off``
# models). An effort-spelled-off wire (``levels``/unknown control with a ``none``
# rung) keeps the byte-identical ``{"effort": "none"}`` instead — see the render.
OPENROUTER_REASONING_OFF = {"enabled": False}
OPENROUTER_NONE_EFFORT = "none"
OPENROUTER_RESPONSES_ENDPOINT = "/responses"
OPENROUTER_ALL_TURNS_RESPONSES_MODELS = frozenset(
    {
        "openai/gpt-5.6-luna",
        "openai/gpt-5.6-luna-pro",
        "openai/gpt-5.6-sol",
        "openai/gpt-5.6-sol-pro",
        "openai/gpt-5.6-terra",
        "openai/gpt-5.6-terra-pro",
    }
)
OPENROUTER_RESPONSES_REQUEST_PARAMETERS = frozenset({"max_tokens", "max_output_tokens", "top_p"})

# Statuses whose handling the shared HTTP policy already gets right (auth,
# rate limit with Retry-After, transient 5xx). Only other error statuses go
# through OpenRouter's structured-body refinement in _classify_http_status.
_OPENROUTER_SHARED_POLICY_STATUSES = frozenset({401, 403, 429, 502, 503, 504})

# Some OpenRouter upstreams serialize visible reasoning summaries with runs of
# bare newlines between word chunks (observed 2026-08-24 on stealth/ox-alpha:
# nine "\n" separators per word gap). The visible reasoning text is display
# material — replay round-trips the opaque ``reasoning_details`` meta untouched,
# never this text — so vBot collapses any run longer than a paragraph break at
# accumulation time. Runs spanning delta boundaries are handled statefully via
# _REASONING_TRAILING_NEWLINES_STATE_KEY.
MAX_REASONING_PARAGRAPH_NEWLINES = 2
REASONING_NEWLINE_RUN_PATTERN = re.compile(r"\n{3,}")
_REASONING_TRAILING_NEWLINES_STATE_KEY = "openrouter_reasoning_trailing_newlines"

# Prompt caching for Claude-family models routed through OpenRouter. Anthropic
# caches nothing unless a content block carries ``cache_control`` (verified live:
# a stable 13k-token prefix returned 0 cache reads across 6 turns without it).
# OpenRouter forwards Anthropic's own semantics but on the OpenAI ``/chat/
# completions`` wire, so the marker rides **inside a content part** ("envelope
# layout"), not as a native top-level ``system`` block the way the Anthropic
# adapter places it. Same strategy as the native path otherwise: one marker on
# the system message (caches tools + system, which Anthropic renders first) and
# up to three rolling markers on the most recent non-system messages, never more
# than Anthropic's four-breakpoint limit. Non-Claude models are left untouched —
# OpenAI/Gemini cache implicitly and a stray ``cache_control`` key risks tripping
# a strict upstream. The ``{"type": "ephemeral"}`` marker is the 5-minute TTL.
OPENROUTER_CACHE_CONTROL_EPHEMERAL: dict[str, str] = {"type": "ephemeral"}
OPENROUTER_CACHE_BREAKPOINT_LIMIT = 4
OPENROUTER_MAX_HISTORY_CACHE_BREAKPOINTS = 3

# OpenRouter uses the ``output_modalities`` query parameter to filter models
# by their output capability.  The default ``/models`` call returns only
# text-output models, so every non-text-output catalog family needs its own
# supplementary fetch: ``transcription`` (STT), ``speech`` (TTS), ``image``
# (image generation), ``audio`` (generic audio generation), ``video`` (video
# generation), and ``embeddings`` (text embedding).  Without these filters
# the corresponding task types (``video_generation``, ``text_embedding``,
# etc.) stay empty even though OpenRouter publishes those models.
SUPPLEMENTARY_OUTPUT_MODALITIES = (
    "transcription",
    "speech",
    "image",
    "audio",
    "video",
    "embeddings",
)

# OpenRouter's dedicated image API publishes a typed parameter schema per
# model (enum values, numeric ranges, boolean support flags) plus per-endpoint
# provider passthrough keys — facts the ``/models`` catalog omits entirely.
# New image models are added exclusively to this API.
IMAGE_MODELS_ENDPOINT = "/images/models"
VIDEO_MODELS_ENDPOINT = "/videos/models"

# Per-model endpoint-detail fetches run concurrently but bounded, so a large
# image catalog does not open dozens of simultaneous connections during an
# explicit refresh.
_IMAGE_DETAIL_CONCURRENCY = 8

_LOGGER = get_logger("providers.openrouter")


class OpenRouterAdapter(OpenAICompatibleAdapter):
    """OpenAI-compatible adapter with OpenRouter-specific behavior."""

    def __init__(
        self,
        *args: Any,
        routing: Mapping[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._routing = parse_openrouter_routing(routing or {})

    async def send(
        self,
        messages: list[dict[str, Any]],
        *,
        model_id: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Route GPT-5.6 Models through OpenRouter's stateless Responses wire."""

        if not self._uses_all_turns_responses(model_id):
            return await super().send(messages, model_id=model_id, **kwargs)
        payload = self._build_openrouter_responses_payload(
            messages,
            model_id=model_id,
            **self._request_kwargs_with_defaults(kwargs),
        )
        return await self._post_responses_json(payload)

    async def stream(
        self,
        messages: list[dict[str, Any]],
        *,
        model_id: str,
        **kwargs: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        """Stream GPT-5.6 via Responses; retain Chat Completions for other Models."""

        if not self._uses_all_turns_responses(model_id):
            async for delta in super().stream(messages, model_id=model_id, **kwargs):
                yield delta
            return
        payload = self._build_openrouter_responses_payload(
            messages,
            model_id=model_id,
            stream=True,
            **self._request_kwargs_with_defaults(kwargs),
        )
        async for delta in self._stream_responses(payload):
            yield delta

    def normalize_response(
        self,
        response: dict[str, Any],
        *,
        model_id: str | None = None,
    ) -> dict[str, Any]:
        """Normalize either OpenRouter endpoint family."""

        if isinstance(response.get("output"), list):
            normalized = normalize_responses_response(response)
        else:
            normalized = super().normalize_response(response, model_id=model_id)
        reasoning = normalized.get("reasoning")
        if isinstance(reasoning, str) and reasoning:
            # Non-streaming counterpart of the streamed newline-run collapse:
            # a complete response has no delta boundaries, so one pass suffices.
            normalized["reasoning"] = _collapse_reasoning_newline_runs(reasoning, None)
        return normalized

    def _uses_all_turns_responses(self, model_id: str) -> bool:
        return model_id.split("::", 1)[0] in OPENROUTER_ALL_TURNS_RESPONSES_MODELS

    def _normalize_stream_chunk(
        self,
        raw_chunk: dict[str, Any],
        tool_call_slots: set[int],
        normalization_state: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        error = raw_chunk.get("error")
        if isinstance(error, Mapping):
            # OpenRouter fronts heterogeneous upstreams behind automatic fallback
            # routing: the identical request can legitimately succeed through a
            # different endpoint. Unclassified in-band failures therefore become
            # retryable so Chat's bounded stream restart can re-route, while the
            # known-fatal classes (context/token limits, auth, billing, content
            # policy) stay fatal — see classify_in_band_provider_error.
            raise classify_in_band_provider_error(error, lenient_unknown=True)
        normalized = super()._normalize_stream_chunk(
            raw_chunk,
            tool_call_slots,
            normalization_state=normalization_state,
        )
        return _collapse_reasoning_delta_texts(normalized, normalization_state)

    def _classify_http_status(
        self,
        status_code: int,
        *,
        detail: str,
        response_headers: httpx.Headers,
    ) -> None:
        if status_code < 400 or status_code in _OPENROUTER_SHARED_POLICY_STATUSES:
            super()._classify_http_status(
                status_code,
                detail=detail,
                response_headers=response_headers,
            )
            return
        error = _openrouter_status_error_payload(detail)
        if error is None:
            super()._classify_http_status(
                status_code,
                detail=detail,
                response_headers=response_headers,
            )
            return
        # Same router leniency as the streaming path: a structured OpenRouter
        # error body on an otherwise fatal status may still name a transient or
        # endpoint-specific condition, so classify from its structured fields.
        raise classify_in_band_provider_error(error, lenient_unknown=True)

    def _request_kwargs_with_defaults(self, kwargs: Mapping[str, Any]) -> dict[str, Any]:
        request_kwargs: dict[str, Any] = {}
        if self._config.defaults:
            request_kwargs.update(self._config.defaults)
        request_kwargs.update(kwargs)
        return request_kwargs

    def _build_openrouter_responses_payload(
        self,
        messages: list[dict[str, Any]],
        *,
        model_id: str,
        stream: bool = False,
        **kwargs: Any,
    ) -> dict[str, Any]:
        session_id = kwargs.pop("session_id", None)
        payload = build_responses_payload(
            messages,
            model_id=model_id,
            policy=self._responses_policy_for_model(model_id),
            stream=stream,
            **kwargs,
        )
        payload["store"] = False
        if isinstance(session_id, str) and session_id:
            payload["session_id"] = session_id
        provider_preferences = _openrouter_provider_preferences(self._routing, model_id)
        if provider_preferences:
            payload["provider"] = provider_preferences
        return payload

    def _responses_policy_for_model(self, model_id: str) -> OpenRouterResponsesPolicy:
        model = (
            self._model_lookup(model_id.split("::", 1)[0])
            if self._model_lookup is not None
            else None
        )
        capabilities = model.capabilities if model is not None else None
        reasoning_supported = capabilities.reasoning.supported if capabilities is not None else True
        supports_tools = capabilities.tools if capabilities is not None else True
        supported_parameters = set(capabilities.supported_parameters) if capabilities else set()
        return OpenRouterResponsesPolicy(
            allowed_reasoning_efforts=(
                frozenset(model_reasoning_levels(self._model_lookup, model_id) or ())
                if reasoning_supported
                else frozenset()
            )
            or (frozenset(OPENROUTER_REASONING_EFFORTS) if reasoning_supported else frozenset()),
            supports_tools=supports_tools,
            supports_parallel_tool_calls=(
                supports_tools
                and (
                    not supported_parameters
                    or "parallel_tool_calls" in supported_parameters
                    or "tools" in supported_parameters
                )
            ),
            supports_structured_outputs=(
                capabilities.json_mode if capabilities is not None else True
            ),
        )

    async def _post_responses_json(self, payload: dict[str, Any]) -> dict[str, Any]:
        async def _do_request() -> dict[str, Any]:
            headers = await self._build_headers()
            try:
                response = await self._client.post(
                    OPENROUTER_RESPONSES_ENDPOINT,
                    json=payload,
                    headers=headers,
                )
            except httpx.TransportError as exc:
                raise wrap_network_error(exc) from exc
            classify_http_status(
                response.status_code,
                idempotent=False,
                detail=_openrouter_http_error_detail(response),
                response_headers=response.headers,
            )
            return dict(decode_response_json(response, "OpenRouter Responses"))

        return await retry_async(_do_request)

    async def _stream_responses(
        self,
        payload: dict[str, Any],
    ) -> AsyncIterator[dict[str, Any]]:
        response = await self._connect_responses_stream(payload)
        state = ResponsesStreamState()
        newline_state: dict[str, Any] = {}
        event_lines: list[str] = []
        seen_finish_delta = False
        try:
            async for line in response.aiter_lines():
                if line:
                    event_lines.append(line)
                    continue
                for delta in _collapse_reasoning_delta_texts(
                    iter_responses_sse_deltas_with_state(event_lines, state),
                    newline_state,
                ):
                    if delta.get("type") == "finish":
                        seen_finish_delta = True
                    yield delta
                event_lines = []
            if event_lines:
                for delta in _collapse_reasoning_delta_texts(
                    iter_responses_sse_deltas_with_state(event_lines, state),
                    newline_state,
                ):
                    if delta.get("type") == "finish":
                        seen_finish_delta = True
                    yield delta
            if not seen_finish_delta:
                raise NetworkError("Stream ended without response completion event")
        except httpx.TimeoutException as exc:
            raise wrap_network_error(exc) from exc
        except httpx.TransportError as exc:
            raise NetworkError(f"Stream read failed: {exc}") from exc
        finally:
            await response.aclose()

    async def _connect_responses_stream(
        self,
        payload: dict[str, Any],
    ) -> httpx.Response:
        async def _connect() -> httpx.Response:
            headers = await self._build_headers()
            request = build_streaming_request(
                self._client,
                "POST",
                OPENROUTER_RESPONSES_ENDPOINT,
                json=payload,
                headers=headers,
            )
            try:
                response = await self._client.send(request, stream=True)
            except httpx.TransportError as exc:
                raise wrap_network_error(exc) from exc
            if response.status_code >= 400:
                body = (await response.aread()).decode("utf-8", errors="replace")
                await response.aclose()
                classify_http_status(
                    response.status_code,
                    idempotent=False,
                    detail=_openrouter_http_error_detail(response, body),
                    response_headers=response.headers,
                )
                raise ProviderError(
                    f"Provider error: {response.status_code}",
                    retryable=False,
                )
            return response

        return await retry_async(_connect)

    def request_context_kwargs(
        self,
        *,
        agent_id: str,
        session_id: str,
        project_id: str | None = None,
        prompt_cache_affinity_id: str | None = None,
    ) -> dict[str, Any]:
        """Pin OpenRouter routing to one cache lineage without leaking its address."""

        scope = (
            ["prompt-cache-affinity", prompt_cache_affinity_id]
            if prompt_cache_affinity_id is not None
            else ["session", project_id, agent_id, session_id]
        )
        address = json.dumps(scope, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        digest = hashlib.sha256(address).hexdigest()
        return {"session_id": f"vbot-{digest}"}

    async def routing_provider_options(self, model_id: str | None = None) -> list[dict[str, str]]:
        """Return selectable OpenRouter base providers or one Model's endpoints."""

        endpoint = f"/models/{quote(model_id, safe='/')}/endpoints" if model_id else "/providers"

        async def _fetch() -> dict[str, Any]:
            try:
                response = await self._client.get(endpoint, headers=await self._build_headers())
            except httpx.TransportError as exc:
                raise wrap_network_error(exc) from exc
            detail = f"{response.status_code} {response.text}".strip()
            classify_http_status(
                response.status_code,
                idempotent=True,
                detail=detail,
                response_headers=response.headers,
            )
            return decode_response_json(response, "OpenRouter routing catalog")

        payload = await retry_async(_fetch)
        return _openrouter_routing_options(payload, model_specific=bool(model_id))

    @classmethod
    def supplementary_discovery_params(cls) -> list[dict[str, str]]:
        """Return query-parameter dicts for supplementary model fetches.

        The OpenRouter ``/models`` endpoint defaults to returning only
        text-output models.  Dedicated STT, TTS, image-, audio-, video-,
        and text-embedding-generation models are excluded unless the
        ``output_modalities`` query parameter is set to ``transcription``,
        ``speech``, ``image``, ``audio``, ``video``, or ``embeddings``
        respectively.

        Each dict returned here is appended as query parameters to the
        models endpoint URL during discovery, and the resulting models
        are merged into the main catalog (deduplicated by ``model_id``).
        """
        return [{"output_modalities": m} for m in SUPPLEMENTARY_OUTPUT_MODALITIES]

    @classmethod
    async def discover_task_models(
        cls,
        normalized_models: Mapping[str, Model],
        fetch_json: Callable[[str], Awaitable[Any]],
    ) -> dict[str, Model]:
        """Discover image/video models and their typed option schemas.

        Fetches the dedicated image API catalog plus the per-model endpoint
        details and projects them into ``capabilities.task_options`` under the
        ``image_generation`` key: the typed ``parameters`` schema from the
        list entry and the per-provider ``passthrough`` key lists from the
        endpoint records. Models already discovered through ``/models`` are
        returned enriched; image-API-only entries become new minimal models
        (no context window, no chat capabilities) so they still appear as
        image-generation targets.
        """

        task_payloads = await asyncio.gather(
            fetch_json(IMAGE_MODELS_ENDPOINT),
            fetch_json(VIDEO_MODELS_ENDPOINT),
            return_exceptions=True,
        )
        image_payload_result: Any = task_payloads[0]
        video_payload_result: Any = task_payloads[1]
        if isinstance(image_payload_result, BaseException):
            raise image_payload_result
        if isinstance(video_payload_result, BaseException) and not isinstance(
            video_payload_result, Exception
        ):
            raise video_payload_result
        if isinstance(video_payload_result, Exception):
            _LOGGER.warning("Video model catalog fetch failed: %s", video_payload_result)
            video_payload_result = {"data": []}
        payload: Any = image_payload_result
        video_payload: Any = video_payload_result
        entries = payload.get("data") if isinstance(payload, Mapping) else None
        if not isinstance(entries, list):
            raise ValueError("Image models response must contain a data list")
        video_entries = video_payload.get("data") if isinstance(video_payload, Mapping) else None
        if not isinstance(video_entries, list):
            raise ValueError("Video models response must contain a data list")

        valid_entries = [
            entry
            for entry in entries
            if isinstance(entry, Mapping) and isinstance(entry.get("id"), str) and entry.get("id")
        ]

        semaphore = asyncio.Semaphore(_IMAGE_DETAIL_CONCURRENCY)

        async def _fetch_detail(model_id: str) -> Any:
            async with semaphore:
                return await fetch_json(f"{IMAGE_MODELS_ENDPOINT}/{model_id}/endpoints")

        details = await asyncio.gather(
            *(_fetch_detail(entry["id"]) for entry in valid_entries),
            return_exceptions=True,
        )

        discovered: dict[str, Model] = {}
        for entry, detail in zip(valid_entries, details, strict=True):
            model_id = entry["id"]
            if isinstance(detail, BaseException) and not isinstance(detail, Exception):
                raise detail
            if isinstance(detail, Exception):
                _LOGGER.warning("Image endpoint detail fetch failed for '%s': %s", model_id, detail)
                detail = None

            image_options: dict[str, Any] = {}
            parameters = _normalize_image_parameters(entry.get("supported_parameters"))
            if parameters:
                image_options["parameters"] = parameters
            passthrough = _passthrough_from_detail(detail)
            if passthrough:
                image_options["passthrough"] = passthrough

            existing = normalized_models.get(model_id)
            if existing is not None:
                if not image_options:
                    continue
                merged_task_options = dict(existing.capabilities.task_options)
                merged_task_options["image_generation"] = image_options
                discovered[model_id] = replace(
                    existing,
                    capabilities=replace(
                        existing.capabilities,
                        task_options=merged_task_options,
                    ),
                )
                continue
            discovered[model_id] = _image_catalog_model(entry, image_options)

        for entry in video_entries:
            if (
                not isinstance(entry, Mapping)
                or not isinstance(entry.get("id"), str)
                or not entry.get("id")
            ):
                continue
            model_id = entry["id"]
            video_options = _normalize_video_options(entry)
            existing = discovered.get(model_id) or normalized_models.get(model_id)
            if existing is not None:
                merged_task_options = dict(existing.capabilities.task_options)
                if video_options:
                    merged_task_options["video_generation"] = video_options
                discovered[model_id] = replace(
                    existing,
                    capabilities=replace(
                        existing.capabilities,
                        task_options=merged_task_options,
                    ),
                )
                continue
            discovered[model_id] = _video_catalog_model(entry, video_options)
        return discovered

    @classmethod
    def normalize_catalog_entry(
        cls,
        raw: Mapping[str, Any],
        defaults: Mapping[str, Any] | None = None,
    ) -> Model:
        """Normalize one OpenRouter ``/models`` entry into a vBot ``Model``."""

        architecture = _read_mapping(raw, "architecture")
        top_provider = _read_mapping(raw, "top_provider")
        supported_parameters = _read_string_list(raw, "supported_parameters")
        input_modalities = _read_string_list(architecture, "input_modalities")
        output_modalities = (
            _read_string_list(architecture, "output_modalities")
            if "output_modalities" in architecture
            else ["text"]
        )
        # OpenRouter publishes ``supported_voices`` as a top-level array of
        # plain voice-id strings on TTS-capable models (and empty/present on
        # other models). Defensive default keeps the field safe to read for
        # every model entry — providers omit it, not raise, when irrelevant.
        supported_voices = _read_optional_string_list(raw, "supported_voices")
        task_types = _openrouter_task_types(raw, input_modalities, output_modalities)

        return Model(
            model_id=_read_string(raw, "id"),
            name=_read_string(raw, "name"),
            capabilities=Capabilities(
                vision="image" in input_modalities,
                tools="tools" in supported_parameters,
                json_mode=(
                    "response_format" in supported_parameters
                    or "structured_outputs" in supported_parameters
                ),
                reasoning=ReasoningCapabilities(
                    supported=(
                        "reasoning" in supported_parameters
                        or "include_reasoning" in supported_parameters
                    ),
                ),
                input_modalities=tuple(input_modalities),
                output_modalities=tuple(output_modalities),
                supported_parameters=tuple(supported_parameters),
                supported_voices=tuple(supported_voices),
                task_types=task_types,
            ),
            # OpenRouter reports ``context_length: 0`` for non-chat models
            # (transcription, image/video generation). A ``0`` is no usable
            # window, so it normalizes to ``None`` (honest "unknown") rather than
            # a fake fact; the read-side default chain fills it at use time.
            context_window=_parse_optional_int(raw.get("context_length")) or None,
            max_output_tokens=_parse_optional_int(top_provider.get("max_completion_tokens")),
            metadata=_openrouter_runtime_metadata(architecture),
        )

    def _build_payload(
        self,
        messages: list[dict[str, Any]],
        model_id: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Build an OpenRouter payload with OpenRouter reasoning parameters."""

        thinking_effort = kwargs.pop("thinking_effort", "")
        reasoning_effort = kwargs.pop("reasoning_effort", "")
        payload = super()._build_payload(messages, model_id, **kwargs)
        reasoning_supported = self._model_reasoning_supported(model_id)
        if reasoning_supported is False:
            payload.pop("reasoning", None)
            payload.pop("include_reasoning", None)
        else:
            # Snap against the effective per-model ladder when the DB carries one;
            # the provider-global constant is only the floor for a model without a
            # feed ladder.
            intent = resolve_reasoning_intent(
                supported=reasoning_supported,
                control=model_reasoning_control(self._model_lookup, model_id),
                levels=(
                    model_reasoning_levels(self._model_lookup, model_id)
                    or tuple(OPENROUTER_REASONING_EFFORTS)
                ),
                effort=thinking_effort or reasoning_effort,
                budget_max=model_reasoning_budget_max(self._model_lookup, model_id),
                # OpenRouter resolves a budget from the effort internally, so vBot
                # deliberately never sends a token budget here (no ``max_tokens``).
                max_tokens=None,
            )
            _render_openrouter_reasoning(payload, intent)

        # Cache stable prefixes last, after every other payload mutation, so the
        # markers land on the final messages that go on the wire. Claude-only:
        # non-Claude models cache implicitly and reject a stray marker.
        if _is_claude_family(model_id):
            _apply_openrouter_prompt_caching(payload)
        provider_preferences = _openrouter_provider_preferences(self._routing, model_id)
        if provider_preferences:
            payload["provider"] = provider_preferences
        return payload


@dataclass(frozen=True)
class OpenRouterResponsesPolicy:
    """Request-shaping facts for an exact OpenRouter Responses-routed Model."""

    allowed_reasoning_efforts: frozenset[str]
    supports_tools: bool
    supports_parallel_tool_calls: bool
    supports_structured_outputs: bool

    @property
    def allows_any_reasoning_controls(self) -> bool:
        return bool(self.allowed_reasoning_efforts)

    def filter_request_kwargs(self, kwargs: Mapping[str, Any]) -> dict[str, Any]:
        filtered = {key: value for key, value in kwargs.items() if value is not None}
        if not self.supports_tools:
            for name in ("tools", "tool_choice", "parallel_tool_calls"):
                filtered.pop(name, None)
        elif not self.supports_parallel_tool_calls:
            filtered.pop("parallel_tool_calls", None)

        if not self.supports_structured_outputs:
            for name in ("response_format", "structured_outputs", "json_mode", "text"):
                filtered.pop(name, None)

        if not self.allows_any_reasoning_controls:
            for name in ("thinking_effort", "reasoning_effort", "reasoning", "include_reasoning"):
                filtered.pop(name, None)
        else:
            self._normalize_effort(filtered, "thinking_effort")
            self._normalize_effort(filtered, "reasoning_effort")

        for name in ("max_tokens", "max_output_tokens", "temperature", "top_p", "top_k"):
            if name in filtered and name not in OPENROUTER_RESPONSES_REQUEST_PARAMETERS:
                filtered.pop(name, None)
        return filtered

    def closest_reasoning_effort(self, effort: Any) -> str | None:
        normalized = normalize_thinking_effort(effort)
        if not normalized:
            return None
        if normalized == OPENROUTER_NONE_EFFORT:
            return (
                OPENROUTER_NONE_EFFORT
                if OPENROUTER_NONE_EFFORT in self.allowed_reasoning_efforts
                else None
            )
        return closest_supported_effort(normalized, self.allowed_reasoning_efforts)

    def supports_request_parameter(self, parameter_name: str) -> bool:
        return parameter_name in OPENROUTER_RESPONSES_REQUEST_PARAMETERS

    def _normalize_effort(
        self,
        filtered: dict[str, Any],
        parameter_name: str,
    ) -> None:
        if parameter_name not in filtered:
            return
        safe_effort = self.closest_reasoning_effort(filtered[parameter_name])
        if safe_effort is None:
            filtered.pop(parameter_name, None)
        else:
            filtered[parameter_name] = safe_effort


def _openrouter_provider_preferences(
    routing: Mapping[str, Any],
    model_id: str,
) -> dict[str, Any]:
    """Render vBot's routing policy to OpenRouter's request ``provider`` object."""

    default_policy = routing["default"]
    model_policy = routing["models"].get(model_id)
    policy = model_policy or default_policy

    blocked: list[str] = list(default_policy["blocked"])
    if model_policy is not None:
        blocked.extend(slug for slug in model_policy["blocked"] if slug not in blocked)

    preferences: dict[str, Any] = {}
    mode = policy["mode"]
    if mode == "allowed":
        preferences["only"] = list(policy["providers"])
    elif mode == "ordered":
        preferences["order"] = list(policy["providers"])
    if blocked:
        preferences["ignore"] = blocked
    if policy["allow_fallbacks"] is False:
        preferences["allow_fallbacks"] = False
    return preferences


def _openrouter_http_error_detail(
    response: httpx.Response,
    body: str | None = None,
) -> str:
    reason = response.text if body is None else body
    return f"{response.status_code} {reason}".strip() if reason else str(response.status_code)


def _openrouter_status_error_payload(detail: str) -> dict[str, Any] | None:
    """Extract the structured ``error`` object from an HTTP error detail.

    The compatible base renders establishment failures as ``"<status> <body>"``
    and OpenRouter bodies are JSON with a documented ``error`` object. Returns
    ``None`` when the detail carries no parseable OpenRouter-shaped error, so
    the shared status policy applies unchanged.
    """

    start = detail.find("{")
    if start < 0:
        return None
    try:
        parsed = json.loads(detail[start:])
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    error = parsed.get("error")
    return error if isinstance(error, dict) else None


def _openrouter_routing_options(
    payload: Mapping[str, Any],
    *,
    model_specific: bool,
) -> list[dict[str, str]]:
    """Normalize OpenRouter provider and endpoint catalogs for the Settings UI."""

    raw_data = payload.get("data")
    if model_specific:
        entries = raw_data.get("endpoints") if isinstance(raw_data, Mapping) else None
    else:
        entries = raw_data
    if not isinstance(entries, list):
        return []

    options: dict[str, str] = {}
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        raw_slug = entry.get("tag") if model_specific else entry.get("slug")
        raw_name = entry.get("provider_name") if model_specific else entry.get("name")
        if not isinstance(raw_slug, str) or not raw_slug.strip():
            continue
        slug = raw_slug.strip().lower()
        name = raw_name.strip() if isinstance(raw_name, str) and raw_name.strip() else slug
        options.setdefault(slug, name)
    return [
        {"slug": slug, "name": name}
        for slug, name in sorted(
            options.items(),
            key=lambda item: (item[1].casefold(), item[0]),
        )
    ]


def _collapse_reasoning_newline_runs(text: str, state: dict[str, Any] | None) -> str:
    """Collapse newline-run noise in reasoning text, across delta boundaries.

    Interior runs of three or more newlines collapse to one paragraph break.
    A run split across consecutive deltas is bounded through the per-stream
    ``state`` mapping: it tracks how many newlines the emitted stream ends
    with so the next fragment's leading run cannot push the joined total past
    a paragraph break. Without ``state`` each fragment collapses in isolation.
    An empty result means this fragment was pure separator noise; the caller
    drops it and the trailing-run tracking keeps its previous value.
    """

    trailing = 0
    if state is not None:
        stored = state.get(_REASONING_TRAILING_NEWLINES_STATE_KEY, 0)
        trailing = stored if isinstance(stored, int) else 0
    leading = len(text) - len(text.lstrip("\n"))
    if trailing + leading > MAX_REASONING_PARAGRAPH_NEWLINES:
        excess = trailing + leading - MAX_REASONING_PARAGRAPH_NEWLINES
        text = text[excess:]
    collapsed = REASONING_NEWLINE_RUN_PATTERN.sub(
        "\n" * MAX_REASONING_PARAGRAPH_NEWLINES,
        text,
    )
    if not collapsed:
        return ""
    if state is not None:
        state[_REASONING_TRAILING_NEWLINES_STATE_KEY] = min(
            MAX_REASONING_PARAGRAPH_NEWLINES,
            len(collapsed) - len(collapsed.rstrip("\n")),
        )
    return collapsed


def _collapse_reasoning_delta_texts(
    deltas: Iterable[dict[str, Any]],
    state: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Rewrite ``reasoning_delta`` texts with newline runs collapsed.

    Fragments that collapse to nothing are dropped entirely — Chat ignores
    empty reasoning text anyway, and dropping keeps the visible delta stream
    free of no-op events. Non-reasoning deltas pass through untouched.
    """

    result: list[dict[str, Any]] = []
    for delta in deltas:
        if delta.get("type") != "reasoning_delta":
            result.append(delta)
            continue
        collapsed = _collapse_reasoning_newline_runs(str(delta.get("text", "")), state)
        if collapsed:
            result.append({"type": "reasoning_delta", "text": collapsed})
    return result


def _render_openrouter_reasoning(payload: dict[str, Any], intent: ReasoningIntent) -> None:
    """Render a reasoning intent onto an OpenRouter payload.

    OpenRouter speaks ``reasoning: {effort}`` / ``{enabled}``. An ``effort``
    intent maps straight through; ``budget`` also renders as an effort (OpenRouter
    maps effort→budget internally, so no token budget is sent); ``on`` toggles
    ``enabled: true``. ``off`` keeps the byte-identical ``{"effort": "none"}`` for
    an effort-spelled-off wire (``effort_level == "none"``) and falls back to the
    documented ``{"enabled": false}`` toggle otherwise; ``default`` omits the
    field entirely.
    """

    if intent.kind == REASONING_INTENT_ON:
        payload["reasoning"] = {"enabled": True}
        payload["include_reasoning"] = True
    elif intent.kind in (REASONING_INTENT_EFFORT, REASONING_INTENT_BUDGET):
        if intent.effort_level is not None:
            payload["reasoning"] = {"effort": intent.effort_level}
            payload["include_reasoning"] = True
    elif intent.kind == REASONING_INTENT_OFF:
        if intent.effort_level == OPENROUTER_NONE_EFFORT:
            payload["reasoning"] = {"effort": OPENROUTER_NONE_EFFORT}
        else:
            payload["reasoning"] = dict(OPENROUTER_REASONING_OFF)
        # Some upstreams honor the output toggle even when they ignore the
        # requested effort. Never ask one to return reasoning for an off intent.
        payload.pop("include_reasoning", None)


def _is_claude_family(model_id: str) -> bool:
    """True for Anthropic Claude models on OpenRouter (``anthropic/claude-*``).

    Matching on the ``claude`` substring covers the vendor-prefixed slug, the
    tilde auto-router form (``~anthropic/claude-haiku-latest``), and any dated
    variant, while never matching a non-Claude model. Only these need explicit
    ``cache_control`` — every other family caches implicitly upstream.
    """

    return "claude" in model_id.lower()


def _apply_openrouter_prompt_caching(payload: dict[str, Any]) -> None:
    """Place ``cache_control`` breakpoints on the OpenAI-wire message array.

    Envelope layout (marker inside a content part): one marker on the last
    system message (caches tools + system) and up to
    :data:`OPENROUTER_MAX_HISTORY_CACHE_BREAKPOINTS` rolling markers on the most
    recent non-system messages, capped at :data:`OPENROUTER_CACHE_BREAKPOINT_LIMIT`.
    A message whose content cannot carry a marker (empty string, or a pure
    tool-call assistant turn with ``None`` content) is skipped so a breakpoint is
    never wasted on a part OpenRouter would ignore.
    """

    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        return

    remaining = OPENROUTER_CACHE_BREAKPOINT_LIMIT
    last_system = _last_index(messages, role="system")
    if last_system is not None and _mark_openrouter_message(messages[last_system]):
        remaining -= 1

    history_budget = min(remaining, OPENROUTER_MAX_HISTORY_CACHE_BREAKPOINTS)
    marked = 0
    for index in range(len(messages) - 1, -1, -1):
        if marked >= history_budget:
            break
        message = messages[index]
        if not isinstance(message, dict) or message.get("role") == "system":
            continue
        if _mark_openrouter_message(message):
            marked += 1


def _last_index(messages: list[Any], *, role: str) -> int | None:
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if isinstance(message, dict) and message.get("role") == role:
            return index
    return None


def _mark_openrouter_message(message: dict[str, Any]) -> bool:
    """Add ``cache_control`` to a message's last content part; return whether it did.

    A string content is wrapped into a single ``text`` part to carry the marker;
    a list content takes the marker on its last dict part. Empty/`None` content
    carries nothing (``False``), so the caller moves the breakpoint to an older
    message.
    """

    content = message.get("content")
    if isinstance(content, str):
        if not content.strip():
            return False
        message["content"] = [
            {
                "type": "text",
                "text": content,
                "cache_control": dict(OPENROUTER_CACHE_CONTROL_EPHEMERAL),
            }
        ]
        return True
    if isinstance(content, list):
        for index in range(len(content) - 1, -1, -1):
            part = content[index]
            if isinstance(part, dict):
                part["cache_control"] = dict(OPENROUTER_CACHE_CONTROL_EPHEMERAL)
                return True
    return False


def _normalize_image_parameters(raw_parameters: Any) -> dict[str, Any]:
    """Project the image API's typed parameter schema to plain JSON data.

    Known spec shapes are validated strictly (an ``enum`` needs a string list,
    a ``range`` numeric bounds); an unknown spec ``type`` is kept verbatim so
    a feed extension survives the projection and only the render layer needs
    to learn it. Shapeless entries are dropped.
    """

    if not isinstance(raw_parameters, Mapping):
        return {}
    parameters: dict[str, Any] = {}
    for name, spec in raw_parameters.items():
        if not isinstance(name, str) or not name or not isinstance(spec, Mapping):
            continue
        spec_type = spec.get("type")
        if not isinstance(spec_type, str) or not spec_type:
            continue
        if spec_type == "enum":
            values = spec.get("values")
            if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
                continue
            parameters[name] = {"type": "enum", "values": list(values)}
        elif spec_type == "range":
            minimum = spec.get("min")
            maximum = spec.get("max")
            if not isinstance(minimum, int | float) or not isinstance(maximum, int | float):
                continue
            parameters[name] = {"type": "range", "min": minimum, "max": maximum}
        elif spec_type == "boolean":
            parameters[name] = {"type": "boolean"}
        else:
            parameters[name] = {str(key): value for key, value in spec.items()}
    return parameters


def _passthrough_from_detail(detail: Any) -> dict[str, list[str]]:
    """Collect per-provider passthrough keys from an endpoint-detail response.

    Multiple endpoints of the same upstream provider merge their key lists
    (union, sorted) so the projection is deterministic regardless of endpoint
    ordering.
    """

    if not isinstance(detail, Mapping):
        return {}
    endpoints = detail.get("endpoints")
    if not isinstance(endpoints, list):
        return {}
    passthrough: dict[str, set[str]] = {}
    for endpoint in endpoints:
        if not isinstance(endpoint, Mapping):
            continue
        slug = endpoint.get("provider_slug")
        keys = endpoint.get("allowed_passthrough_parameters")
        if not isinstance(slug, str) or not slug or not isinstance(keys, list):
            continue
        valid_keys = {key for key in keys if isinstance(key, str) and key}
        if valid_keys:
            passthrough.setdefault(slug, set()).update(valid_keys)
    return {slug: sorted(keys) for slug, keys in sorted(passthrough.items())}


def _openrouter_task_types(
    raw: Mapping[str, Any],
    input_modalities: list[str],
    output_modalities: list[str],
) -> tuple[str, ...]:
    """Derive OpenRouter tasks, conservatively separating music from audio.

    OpenRouter currently exposes Music and conversational Audio models through
    the same ``output_modalities=audio`` filter and publishes no explicit Music
    task tag. Its Music models have a distinct capability signature: text plus
    optional image input, audio output, and no audio input. Keeping this rule in
    the provider normalizer avoids misclassifying GPT Audio as Music while the
    provider feed lacks a first-class semantic tag.
    """

    tasks = set(derive_model_task_types(input_modalities, output_modalities))
    inputs = set(input_modalities)
    outputs = set(output_modalities)
    architecture = raw.get("architecture")
    modality = architecture.get("modality") if isinstance(architecture, Mapping) else None
    if (
        modality == "text+image->text+audio"
        and inputs == {"text", "image"}
        and {"text", "audio"}.issubset(outputs)
    ):
        tasks.add("music_generation")
    return tuple(task for task in MODEL_TASK_ORDER if task in tasks)


def _normalize_video_options(entry: Mapping[str, Any]) -> dict[str, Any]:
    """Project OpenRouter's dedicated video catalog to typed task options."""

    parameters: dict[str, Any] = {}
    enum_fields = (
        ("resolution", "supported_resolutions"),
        ("aspect_ratio", "supported_aspect_ratios"),
        ("size", "supported_sizes"),
    )
    for name, source_name in enum_fields:
        values = _read_optional_string_list(entry, source_name)
        if values:
            parameters[name] = {"type": "enum", "values": values}

    durations = entry.get("supported_durations")
    if isinstance(durations, list):
        values = [str(value) for value in durations if isinstance(value, int) and value > 0]
        if values:
            parameters["duration"] = {"type": "enum", "values": values}
    if entry.get("generate_audio") is True:
        parameters["generate_audio"] = {"type": "boolean"}
    if entry.get("seed") is True:
        parameters["seed"] = {"type": "boolean"}

    options: dict[str, Any] = {}
    if parameters:
        options["parameters"] = parameters
    frame_images = _read_optional_string_list(entry, "supported_frame_images")
    supported_frames = [
        frame_type for frame_type in frame_images if frame_type in {"first_frame", "last_frame"}
    ]
    if supported_frames:
        options["frame_images"] = supported_frames
    passthrough = _read_optional_string_list(entry, "allowed_passthrough_parameters")
    if passthrough:
        options["passthrough_parameters"] = passthrough
    return options


def _image_catalog_model(entry: Mapping[str, Any], image_options: dict[str, Any]) -> Model:
    """Build a minimal ``Model`` for an image-API-only catalog entry.

    The image API publishes no context window, no chat parameters, and no
    reasoning facts — the entry exists so the model appears as an
    image-generation target with its typed option schema; chat-facing
    capabilities honestly stay off/unknown.
    """

    raw_architecture = entry.get("architecture")
    architecture = raw_architecture if isinstance(raw_architecture, Mapping) else {}
    input_modalities = _read_optional_string_list(architecture, "input_modalities") or ["text"]
    output_modalities = _read_optional_string_list(architecture, "output_modalities") or ["image"]
    name = entry.get("name")
    task_options = {"image_generation": image_options} if image_options else {}
    return Model(
        model_id=str(entry["id"]),
        name=name if isinstance(name, str) and name else str(entry["id"]),
        capabilities=Capabilities(
            vision="image" in input_modalities,
            tools=False,
            json_mode=False,
            reasoning=ReasoningCapabilities(supported=False),
            input_modalities=tuple(input_modalities),
            output_modalities=tuple(output_modalities),
            task_options=task_options,
        ),
        context_window=None,
        max_output_tokens=None,
    )


def _video_catalog_model(entry: Mapping[str, Any], video_options: dict[str, Any]) -> Model:
    """Build a minimal ``Model`` for a video-API-only catalog entry."""

    name = entry.get("name")
    task_options = {"video_generation": video_options} if video_options else {}
    return Model(
        model_id=str(entry["id"]),
        name=name if isinstance(name, str) and name else str(entry["id"]),
        capabilities=Capabilities(
            vision=False,
            tools=False,
            json_mode=False,
            reasoning=ReasoningCapabilities(supported=False),
            input_modalities=("text",),
            output_modalities=("video",),
            task_options=task_options,
        ),
        context_window=None,
        max_output_tokens=None,
    )


def _openrouter_runtime_metadata(architecture: Mapping[str, Any]) -> Mapping[str, Any]:
    modality = architecture.get("modality")
    if isinstance(modality, str) and modality:
        return {"openrouter": {"modality": modality}}
    return {}


def _read_optional_string_list(data: Mapping[str, Any], key: str) -> list[str]:
    """Read an optional list-of-strings field, returning ``[]`` when absent or malformed.

    Used for OpenRouter fields that are present-but-empty on most models (such as
    ``supported_voices`` on non-TTS models) where a missing or wrong-shaped value
    is a normal "not applicable" signal rather than a hard schema error.
    """

    value = data.get(key)
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return []
    return value
