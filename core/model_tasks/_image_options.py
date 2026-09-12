"""Image options."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from core.model_tasks._option_types import (
    DALL_E_STYLE_CHOICES,
    FALLBACK_ASPECT_RATIOS,
    FALLBACK_RESOLUTIONS,
    GPT_IMAGE_BACKGROUND_CHOICES,
    GPT_IMAGE_OUTPUT_FORMAT_CHOICES,
    GPT_IMAGE_QUALITY_CHOICES,
    GPT_IMAGE_SIZE_CHOICES,
    IMAGE_CHOICE_LABELS,
    IMAGE_ENUM_FORCED_DEFAULTS,
    IMAGE_NUMBER_VALUED_PARAMETERS,
    IMAGE_PARAMETER_DESCRIPTIONS,
    IMAGE_PARAMETER_LABELS,
    IMAGE_PARAMETER_ORDER,
    IMAGE_PARAMETER_SKIP,
    IMAGE_SIZE_SHORTHAND_CONFLICTS,
    OPENAI_IMAGE_RESPONSE_FORMAT_CHOICES,
    PROVIDER_DEFAULT_CHOICE_LABEL,
    TaskModelOptionChoice,
    TaskModelOptionField,
)
from core.model_tasks.constants import (
    TASK_IMAGE_GENERATION,
)
from core.models import Model


def _image_generation_fields(
    provider_id: str,
    model: Model | None,
) -> tuple[TaskModelOptionField, ...]:
    """Image option fields, driven by the model's typed parameter schema.

    When the model carries ``capabilities.task_options.image_generation``
    (projected at refresh from the OpenRouter image API, or hand-authored in
    the override layer), fields render generically from that data. Without
    it, a conservative provider-level fallback applies. Providers with no
    image execution path get no fields — the UI must not invent inputs.
    """

    image_options = _image_task_options(model)
    parameters = image_options.get("parameters")
    fields: list[TaskModelOptionField] = []
    if isinstance(parameters, Mapping) and parameters:
        fields.extend(_fields_from_image_parameters(parameters))
    elif provider_id == "openrouter":
        fields.extend(_openrouter_image_fallback_fields(model))
    elif provider_id == "openai":
        fields.extend(_openai_image_fallback_fields(model))

    passthrough = image_options.get("passthrough")
    if isinstance(passthrough, Mapping) and passthrough:
        fields.append(_provider_options_field(passthrough))
    return tuple(fields)


def _image_task_options(model: Model | None) -> Mapping[str, Any]:
    if model is None:
        return {}
    image_options = model.capabilities.task_options.get(TASK_IMAGE_GENERATION)
    return image_options if isinstance(image_options, Mapping) else {}


def _fields_from_image_parameters(
    parameters: Mapping[str, Any],
) -> list[TaskModelOptionField]:
    """Render typed parameter specs into option fields.

    ``enum`` → select (with a "Provider default" empty choice unless the wire
    layer forces a value), ``range`` → bounded number (skipped when the range
    collapses to a single value — nothing to configure), ``boolean`` → number
    for free-value names like ``seed``, toggle otherwise, ``string`` → free
    text (hand-authored specs for parameters with open value spaces, e.g.
    gpt-image-2 arbitrary sizes). Unknown spec types are skipped fail-soft;
    the raw catalog still carries them.
    """

    known_order = [name for name in IMAGE_PARAMETER_ORDER if name in parameters]
    remaining = sorted(name for name in parameters if name not in IMAGE_PARAMETER_ORDER)
    fields: list[TaskModelOptionField] = []
    for name in (*known_order, *remaining):
        if name in IMAGE_PARAMETER_SKIP:
            continue
        if name == "size" and any(
            conflict in parameters for conflict in IMAGE_SIZE_SHORTHAND_CONFLICTS
        ):
            continue
        spec = parameters.get(name)
        if not isinstance(spec, Mapping):
            continue
        field = _field_from_image_parameter(name, spec)
        if field is not None:
            fields.append(field)
    return fields


def _field_from_image_parameter(
    name: str,
    spec: Mapping[str, Any],
) -> TaskModelOptionField | None:
    spec_type = spec.get("type")
    if spec_type == "enum":
        return _enum_image_field(name, spec)
    if spec_type == "range":
        return _range_image_field(name, spec)
    if spec_type == "string":
        return TaskModelOptionField(
            name=name,
            type="text",
            label=_image_parameter_label(name),
            default="",
            description=_image_parameter_description(name, spec),
        )
    if spec_type == "boolean":
        if name in IMAGE_NUMBER_VALUED_PARAMETERS:
            return TaskModelOptionField(
                name=name,
                type="number",
                label=_image_parameter_label(name),
                default=None,
                step=1,
                description=_image_parameter_description(name, spec),
            )
        return TaskModelOptionField(
            name=name,
            type="boolean",
            label=_image_parameter_label(name),
            default=None,
            description=_image_parameter_description(name, spec),
        )
    return None


def _enum_image_field(name: str, spec: Mapping[str, Any]) -> TaskModelOptionField | None:
    # Loaded ``task_options`` are frozen (lists become tuples), while
    # fallback specs are built inline as lists — accept both sequence forms.
    raw_values = spec.get("values")
    if not isinstance(raw_values, list | tuple):
        return None
    values = [value for value in raw_values if isinstance(value, str) and value]
    forced_default = IMAGE_ENUM_FORCED_DEFAULTS.get(name)
    if forced_default is not None and forced_default in values:
        choices = tuple(_image_choice(value) for value in values)
        default: str = forced_default
    else:
        if len(values) < 2:
            # A single published value offers no choice; leaving the
            # parameter unsent keeps the provider's fixed value
            # authoritative (mirrors the collapsed-range rule).
            return None
        choices = (
            TaskModelOptionChoice(value="", label=PROVIDER_DEFAULT_CHOICE_LABEL),
            *(_image_choice(value) for value in values),
        )
        default = ""
    return TaskModelOptionField(
        name=name,
        type="select",
        label=_image_parameter_label(name),
        default=default,
        options=choices,
        description=_image_parameter_description(name, spec),
    )


def _range_image_field(name: str, spec: Mapping[str, Any]) -> TaskModelOptionField | None:
    minimum = spec.get("min")
    maximum = spec.get("max")
    if not isinstance(minimum, int | float) or not isinstance(maximum, int | float):
        return None
    if minimum == maximum:
        # A collapsed range offers no choice; leaving the parameter unsent
        # keeps the provider's fixed value authoritative.
        return None
    return TaskModelOptionField(
        name=name,
        type="number",
        label=_image_parameter_label(name),
        default=None,
        min_value=float(minimum),
        max_value=float(maximum),
        step=1,
        description=_image_parameter_description(name, spec),
    )


def _image_parameter_label(name: str) -> str:
    label = IMAGE_PARAMETER_LABELS.get(name)
    if label is not None:
        return label
    return name.replace("_", " ").capitalize()


def _image_parameter_description(name: str, spec: Mapping[str, Any]) -> str:
    """Per-spec description wins over the code hint.

    Hand-authored override specs may carry a model-specific ``description``
    (e.g. gpt-image-2's arbitrary-size constraints) that a generic per-name
    hint cannot express.
    """

    description = spec.get("description")
    if isinstance(description, str) and description:
        return description
    return IMAGE_PARAMETER_DESCRIPTIONS.get(name, "")


def _image_choice(value: str) -> TaskModelOptionChoice:
    return TaskModelOptionChoice(value=value, label=IMAGE_CHOICE_LABELS.get(value, value))


def _provider_options_field(passthrough: Mapping[str, Any]) -> TaskModelOptionField:
    allowed_parts: list[str] = []
    for slug in sorted(str(key) for key in passthrough):
        keys = passthrough.get(slug)
        if isinstance(keys, list | tuple) and keys:
            allowed_parts.append(f"{slug}: {', '.join(str(key) for key in keys)}")
    allowed = "; ".join(allowed_parts)
    description = (
        "Provider-specific options (JSON object) sent as provider.options, keyed by provider slug."
    )
    if allowed:
        description = f"{description} Allowed keys — {allowed}."
    return TaskModelOptionField(
        name="provider_options",
        type="json",
        label="Provider options",
        default={},
        description=description,
    )


def _openrouter_image_fallback_fields(model: Model | None) -> list[TaskModelOptionField]:
    fields = [
        _enum_image_field("aspect_ratio", {"type": "enum", "values": list(FALLBACK_ASPECT_RATIOS)}),
        _enum_image_field("resolution", {"type": "enum", "values": list(FALLBACK_RESOLUTIONS)}),
    ]
    if model is not None and "seed" in model.capabilities.supported_parameters:
        fields.append(_field_from_image_parameter("seed", {"type": "boolean"}))
    return [field for field in fields if field is not None]


def _openai_image_fallback_fields(model: Model | None) -> list[TaskModelOptionField]:
    """OpenAI native image fallback (gpt-image-shaped union).

    Applies only when the model carries no ``task_options`` data — e.g. an
    unrefreshed catalog. Each field is gated by ``supported_parameters`` when
    the model is known; with no model at all the whole union renders so the
    target stays configurable.
    """

    supported: frozenset[str] | None = (
        frozenset(model.capabilities.supported_parameters) if model is not None else None
    )

    def has(field_name: str) -> bool:
        return supported is None or field_name in supported

    union: tuple[tuple[str, dict[str, Any]], ...] = (
        ("size", {"type": "enum", "values": list(GPT_IMAGE_SIZE_CHOICES)}),
        ("quality", {"type": "enum", "values": list(GPT_IMAGE_QUALITY_CHOICES)}),
        ("background", {"type": "enum", "values": list(GPT_IMAGE_BACKGROUND_CHOICES)}),
        ("n", {"type": "range", "min": 1, "max": 10}),
        ("output_format", {"type": "enum", "values": list(GPT_IMAGE_OUTPUT_FORMAT_CHOICES)}),
        ("style", {"type": "enum", "values": list(DALL_E_STYLE_CHOICES)}),
        (
            "response_format",
            {"type": "enum", "values": list(OPENAI_IMAGE_RESPONSE_FORMAT_CHOICES)},
        ),
    )
    fields: list[TaskModelOptionField] = []
    for name, spec in union:
        if not has(name):
            continue
        field = _field_from_image_parameter(name, spec)
        if field is not None:
            fields.append(field)
    return fields
