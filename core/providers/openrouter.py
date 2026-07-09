"""OpenRouter provider adapter."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace
from typing import Any

from core.models.models import Capabilities, Model, ReasoningCapabilities
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
    model_reasoning_budget_max,
    model_reasoning_control,
    model_reasoning_levels,
    resolve_reasoning_intent,
)
from core.utils.logging import get_logger

OPENROUTER_REASONING_EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh"}
# OpenRouter's documented off-shape for a native thinking toggle (``on_off``
# models). An effort-spelled-off wire (``levels``/unknown control with a ``none``
# rung) keeps the byte-identical ``{"effort": "none"}`` instead — see the render.
OPENROUTER_REASONING_OFF = {"enabled": False}
OPENROUTER_NONE_EFFORT = "none"

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

# Per-model endpoint-detail fetches run concurrently but bounded, so a large
# image catalog does not open dozens of simultaneous connections during an
# explicit refresh.
_IMAGE_DETAIL_CONCURRENCY = 8

_LOGGER = get_logger("providers.openrouter")


class OpenRouterAdapter(OpenAICompatibleAdapter):
    """OpenAI-compatible adapter with OpenRouter-specific behavior."""

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
        """Discover image models and their typed option schemas.

        Fetches the dedicated image API catalog plus the per-model endpoint
        details and projects them into ``capabilities.task_options`` under the
        ``image_generation`` key: the typed ``parameters`` schema from the
        list entry and the per-provider ``passthrough`` key lists from the
        endpoint records. Models already discovered through ``/models`` are
        returned enriched; image-API-only entries become new minimal models
        (no context window, no chat capabilities) so they still appear as
        image-generation targets.
        """

        payload = await fetch_json(IMAGE_MODELS_ENDPOINT)
        entries = payload.get("data") if isinstance(payload, Mapping) else None
        if not isinstance(entries, list):
            raise ValueError("Image models response must contain a data list")

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
        return payload


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
            payload["include_reasoning"] = True
        else:
            payload["reasoning"] = dict(OPENROUTER_REASONING_OFF)


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
