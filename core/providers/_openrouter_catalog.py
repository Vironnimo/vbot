"""Openrouter catalog."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from core.models.models import (
    MODEL_TASK_ORDER,
    Capabilities,
    Model,
    ReasoningCapabilities,
    derive_model_task_types,
)


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
