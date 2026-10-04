"""What one image generation target can do, and how a request reaches its wire.

The Model DB publishes each image Model's raw parameter facts
(``capabilities.task_options.image_generation``). The same Model behaves
differently per wire: the ChatGPT subscription renders through a carrier Model
that rejects ``n``, and OpenAI's native sizes are pixel strings rather than the
aspect ratio and resolution tiers OpenRouter accepts. :class:`ImageProfile`
combines the facts with the wire once, so Settings fields, the Agent Tool's
per-call choices and the request translation all read the same answer.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal

from core.model_tasks.constants import TASK_IMAGE_GENERATION

JsonObject = dict[str, Any]
ImageWire = Literal["openrouter", "openai_images", "openai_subscription", "unsupported"]

# Per-call intent the image Tool can carry; every other image option is Settings-only.
CALL_OPTION_NAMES: tuple[str, ...] = ("aspect_ratio", "resolution", "background")

# gpt-image-2 accepts any WIDTHxHEIGHT with both edges divisible by 16, at most
# 3840 px per edge, an aspect ratio of at most 3:1 and between 655,360 and
# 8,294,400 pixels (OpenAI image generation guide, read 2026-10-04).
_FLEXIBLE_SIZE_STEP = 16
_FLEXIBLE_SIZE_MAX_EDGE = 3840
_FLEXIBLE_SIZE_MIN_PIXELS = 655_360
_FLEXIBLE_SIZE_MAX_PIXELS = 8_294_400
_FLEXIBLE_ASPECT_RATIOS: tuple[str, ...] = (
    "1:1",
    "3:2",
    "2:3",
    "4:3",
    "3:4",
    "16:9",
    "9:16",
    "21:9",
)
# A resolution tier is a pixel budget, so every aspect ratio of one tier has
# about the same number of pixels.
_FLEXIBLE_RESOLUTION_PIXELS: Mapping[str, int] = MappingProxyType(
    {"1K": 1024 * 1024, "2K": 2048 * 2048, "4K": _FLEXIBLE_SIZE_MAX_PIXELS}
)
_FLEXIBLE_SIZE_PRESET_ASPECTS: tuple[str, ...] = ("1:1", "3:2", "2:3", "16:9", "9:16")

# Parameters a wire never takes from Settings: the subscription's image tool
# rejects ``n`` and fixes the backend Model; vBot always asks DALL-E for Base64.
_WIRE_HIDDEN_PARAMETERS: Mapping[ImageWire, frozenset[str]] = MappingProxyType(
    {
        "openrouter": frozenset({"input_references", "stream"}),
        "openai_images": frozenset({"response_format", "stream"}),
        "openai_subscription": frozenset({"n", "response_format", "stream", "model"}),
        "unsupported": frozenset(),
    }
)
# OpenRouter publishes ``{"type": "boolean"}`` for a parameter it supports
# without listing values. Only these names are real numbers; any other such
# parameter takes free text.
_FREE_NUMBER_PARAMETERS: frozenset[str] = frozenset({"seed"})
# The edit endpoint of OpenAI's native image API takes up to 16 images
# (OpenAI images API reference, read 2026-10-04).
_OPENAI_MAX_SOURCE_IMAGES = 16


def image_wire(provider_id: str, adapter: str, connection_mode: str | None) -> ImageWire:
    """Return which image wire serves a Provider Connection."""

    # Imported here: the Provider package imports Settings, which import this module.
    from core.providers._openai_constants import CODEX_RESPONSES_MODE

    if provider_id == "openrouter":
        return "openrouter"
    if adapter in {"openai", "openai_compatible"}:
        if connection_mode == CODEX_RESPONSES_MODE:
            return "openai_subscription"
        return "openai_images"
    return "unsupported"


class ImageCallOptionError(ValueError):
    """Raised when a per-call image option is not one the configured Model offers."""


@dataclass(frozen=True)
class ImageProfile:
    """One image target's settable parameters and per-call choices.

    ``parameters`` are the Settings-facing parameter specs after wire rules:
    hidden wire parameters are gone, open ``boolean`` specs are typed, and a
    flexible gpt-image size offers presets. ``call_choices`` maps each per-call
    option the Agent can pass to its allowed values; an option without at
    least two values is absent. ``max_source_images`` is ``0`` for text-only
    targets and ``None`` when the target accepts images without a known limit.
    """

    wire: ImageWire
    parameters: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    passthrough: Mapping[str, Any] = field(default_factory=dict)
    call_choices: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    max_source_images: int | None = 0
    size_translation: Literal["none", "fixed", "flexible"] = "none"
    fixed_sizes: Mapping[str, str] = field(default_factory=dict)
    # Wire options vBot always sends, whatever the Settings hold.
    fixed_options: Mapping[str, Any] = field(default_factory=dict)

    @property
    def accepts_source_images(self) -> bool:
        return self.max_source_images is None or self.max_source_images > 0

    def wire_options(
        self, call_options: Mapping[str, Any], binding_options: Mapping[str, Any]
    ) -> JsonObject:
        """Translate per-call intent into the wire options that override Settings.

        Raises :class:`ImageCallOptionError` for an option or value the profile
        does not offer, so an unsupported request is never sent.
        """

        requested: dict[str, str] = {}
        for name, raw_value in call_options.items():
            value = raw_value.strip() if isinstance(raw_value, str) else raw_value
            if value is None or value == "":
                continue
            choices = self.call_choices.get(name)
            if choices is None:
                raise ImageCallOptionError(missing_choice_message("image", name))
            matched = match_choice(str(value), choices)
            if matched is None:
                raise ImageCallOptionError(unoffered_choice_message("image", name, value, choices))
            requested[name] = matched
        if self.size_translation == "none":
            return dict(requested)
        translated: JsonObject = {}
        if "background" in requested:
            translated["background"] = requested["background"]
        if "aspect_ratio" in requested or "resolution" in requested:
            translated["size"] = self._size_for(
                requested.get("aspect_ratio"), requested.get("resolution"), binding_options
            )
        return translated

    def _size_for(
        self,
        aspect_ratio: str | None,
        resolution: str | None,
        binding_options: Mapping[str, Any],
    ) -> str:
        if self.size_translation == "fixed":
            # Fixed-size Models offer aspect ratios only.
            return self.fixed_sizes[aspect_ratio or "1:1"]
        configured = _parse_size(binding_options.get("size"))
        if aspect_ratio is None:
            aspect_ratio = _nearest_aspect(*configured) if configured else "1:1"
        if resolution is None:
            resolution = _resolution_tier(configured[0] * configured[1]) if configured else "1K"
        return flexible_size(aspect_ratio, resolution)


def build_image_profile(model: Any | None, wire: ImageWire) -> ImageProfile:
    """Combine one Model's published image facts with its wire."""

    if wire == "unsupported":
        return ImageProfile(wire=wire)
    facts = _image_facts(model)
    raw_parameters = facts.get("parameters")
    raw_parameters = raw_parameters if isinstance(raw_parameters, Mapping) else {}
    hidden = _WIRE_HIDDEN_PARAMETERS[wire]
    parameters: dict[str, Mapping[str, Any]] = {}
    for name, spec in raw_parameters.items():
        if name in hidden or not isinstance(spec, Mapping):
            continue
        parameters[str(name)] = _typed_spec(str(name), spec)
    passthrough = facts.get("passthrough")
    passthrough = passthrough if isinstance(passthrough, Mapping) else {}

    size_translation: Literal["none", "fixed", "flexible"] = "none"
    fixed_sizes: dict[str, str] = {}
    call_choices: dict[str, tuple[str, ...]] = {}
    size_spec = parameters.get("size")
    if wire != "openrouter" and size_spec is not None:
        if size_spec.get("type") == "string":
            size_translation = "flexible"
            parameters["size"] = _flexible_size_spec(size_spec)
            call_choices["aspect_ratio"] = _FLEXIBLE_ASPECT_RATIOS
            call_choices["resolution"] = tuple(_FLEXIBLE_RESOLUTION_PIXELS)
        else:
            fixed_sizes = _fixed_sizes_by_aspect(_spec_values(size_spec))
            if len(fixed_sizes) > 1:
                size_translation = "fixed"
                call_choices["aspect_ratio"] = tuple(fixed_sizes)
    else:
        for name in ("aspect_ratio", "resolution"):
            values = tuple(value for value in _spec_values(parameters.get(name)) if value != "auto")
            if len(values) > 1:
                call_choices[name] = values
    backgrounds = _spec_values(parameters.get("background"))
    if "transparent" in backgrounds:
        call_choices["background"] = tuple(
            value for value in ("transparent", "opaque") if value in backgrounds
        )

    fixed_options: JsonObject = {}
    if wire == "openai_images" and "response_format" in raw_parameters:
        # DALL-E answers with a URL unless asked for Base64, which vBot decodes.
        fixed_options["response_format"] = "b64_json"

    return ImageProfile(
        wire=wire,
        parameters=parameters,
        passthrough=passthrough,
        call_choices=call_choices,
        max_source_images=_max_source_images(model, raw_parameters, wire),
        size_translation=size_translation,
        fixed_sizes=fixed_sizes,
        fixed_options=fixed_options,
    )


def missing_choice_message(medium: str, name: str) -> str:
    """Refuse a per-call option the configured *medium* Model does not offer."""

    return (
        f"Nothing was generated. The configured {medium} model has no {name} choice. "
        f"Repeat the call without {name}."
    )


def unoffered_choice_message(medium: str, name: str, value: Any, choices: tuple[str, ...]) -> str:
    """Refuse a per-call value the configured *medium* Model does not offer."""

    return (
        f"Nothing was generated. The configured {medium} model does not offer {name} "
        f"{value!r}. Pass one of {', '.join(choices)}, or omit {name} to use the default."
    )


def match_choice(value: str, choices: tuple[str, ...]) -> str | None:
    """Return the choice *value* names, tolerating case, spaces and separators."""

    if value in choices:
        return value
    key = _choice_key(value)
    for choice in choices:
        if _choice_key(choice) == key:
            return choice
    return None


def flexible_size(aspect_ratio: str, resolution: str) -> str:
    """Return the gpt-image WIDTHxHEIGHT for an aspect ratio and resolution tier.

    Both edges are whole multiples of the reduced ratio times 16, so the size
    keeps the exact ratio and both edges stay divisible by 16.
    """

    width_part, height_part = aspect_ratio.split(":")
    divisor = math.gcd(int(width_part), int(height_part))
    ratio_width, ratio_height = int(width_part) // divisor, int(height_part) // divisor
    unit_width = ratio_width * _FLEXIBLE_SIZE_STEP
    unit_height = ratio_height * _FLEXIBLE_SIZE_STEP
    pixels = _FLEXIBLE_RESOLUTION_PIXELS[resolution]
    units = max(1, round(math.sqrt(pixels / (unit_width * unit_height))))
    units = min(units, _FLEXIBLE_SIZE_MAX_EDGE // max(unit_width, unit_height))
    while units > 1 and units * units * unit_width * unit_height > _FLEXIBLE_SIZE_MAX_PIXELS:
        units -= 1
    while units * units * unit_width * unit_height < _FLEXIBLE_SIZE_MIN_PIXELS:
        units += 1
    return f"{units * unit_width}x{units * unit_height}"


def _image_facts(model: Any | None) -> Mapping[str, Any]:
    task_options = getattr(getattr(model, "capabilities", None), "task_options", None)
    if not isinstance(task_options, Mapping):
        return {}
    facts = task_options.get(TASK_IMAGE_GENERATION)
    return facts if isinstance(facts, Mapping) else {}


def _typed_spec(name: str, spec: Mapping[str, Any]) -> Mapping[str, Any]:
    if spec.get("type") != "boolean":
        return spec
    # OpenRouter marks a supported parameter without published values as
    # ``boolean``; only a few such names are numbers, the rest take text.
    typed = dict(spec)
    typed["type"] = "number" if name in _FREE_NUMBER_PARAMETERS else "string"
    return typed


def _flexible_size_spec(spec: Mapping[str, Any]) -> Mapping[str, Any]:
    presets = ["auto", "1024x1024", "1536x1024", "1024x1536"]
    for resolution in ("2K", "4K"):
        for aspect_ratio in _FLEXIBLE_SIZE_PRESET_ASPECTS:
            size = flexible_size(aspect_ratio, resolution)
            if size not in presets:
                presets.append(size)
    typed = dict(spec)
    typed["type"] = "enum"
    typed["values"] = tuple(presets)
    typed.pop("description", None)
    return typed


def _fixed_sizes_by_aspect(sizes: tuple[str, ...]) -> dict[str, str]:
    by_aspect: dict[str, str] = {}
    for size in sizes:
        parsed = _parse_size(size)
        if parsed is None:
            continue
        aspect = _aspect_label(*parsed)
        current = _parse_size(by_aspect.get(aspect))
        # One size per aspect ratio: the largest one the Model offers.
        if current is None or parsed[0] * parsed[1] > current[0] * current[1]:
            by_aspect[aspect] = size
    return by_aspect


def _max_source_images(
    model: Any | None, raw_parameters: Mapping[str, Any], wire: ImageWire
) -> int | None:
    input_modalities = getattr(getattr(model, "capabilities", None), "input_modalities", ())
    if model is not None and "image" not in input_modalities:
        return 0
    references = raw_parameters.get("input_references")
    if isinstance(references, Mapping):
        maximum = references.get("max")
        if isinstance(maximum, int) and not isinstance(maximum, bool) and maximum >= 0:
            return maximum
    if model is None:
        return 0
    if wire == "openai_images":
        return _OPENAI_MAX_SOURCE_IMAGES
    return None


def _spec_values(spec: Any) -> tuple[str, ...]:
    if not isinstance(spec, Mapping):
        return ()
    values = spec.get("values")
    if not isinstance(values, list | tuple):
        return ()
    return tuple(str(value) for value in values if isinstance(value, str) and value)


def _parse_size(value: Any) -> tuple[int, int] | None:
    if not isinstance(value, str):
        return None
    width, separator, height = value.lower().partition("x")
    if not separator or not width.isdecimal() or not height.isdecimal():
        return None
    parsed = (int(width), int(height))
    return parsed if parsed[0] > 0 and parsed[1] > 0 else None


def _aspect_label(width: int, height: int) -> str:
    """Name a fixed size by the common ratio within 5% of it, else by its exact ratio."""

    nearest = _nearest_aspect(width, height)
    nearest_width, nearest_height = (int(part) for part in nearest.split(":"))
    if abs(math.log((width / height) / (nearest_width / nearest_height))) < math.log(1.05):
        return nearest
    divisor = math.gcd(width, height)
    return f"{width // divisor}:{height // divisor}"


def _nearest_aspect(width: int, height: int) -> str:
    target = math.log(width / height)

    def distance(aspect_ratio: str) -> float:
        width_part, height_part = aspect_ratio.split(":")
        return abs(math.log(int(width_part) / int(height_part)) - target)

    return min(_FLEXIBLE_ASPECT_RATIOS, key=distance)


def _resolution_tier(pixels: int) -> str:
    return min(
        _FLEXIBLE_RESOLUTION_PIXELS,
        key=lambda tier: abs(_FLEXIBLE_RESOLUTION_PIXELS[tier] - pixels),
    )


def _choice_key(value: str) -> str:
    return "".join(value.split()).casefold().replace("x", ":").replace("/", ":")
