"""Image options."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from core.model_tasks._option_types import (
    DALL_E_STYLE_CHOICES,
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
    PROVIDER_DEFAULT_CHOICE_LABEL,
    TaskModelOptionChoice,
    TaskModelOptionField,
)
from core.model_tasks.image_profile import ImageProfile
from core.models import Model


def _image_generation_fields(
    profile: ImageProfile, model: Model | None
) -> tuple[TaskModelOptionField, ...]:
    """Image option fields, driven by the target's image profile.

    The profile holds the Model's typed parameter specs after the wire's rules
    (``core/model_tasks/image_profile.py``). An OpenAI-wire Model without
    published facts falls back to a conservative gpt-image-shaped union; any
    other Model without facts gets no generated fields, because the UI must
    not invent values the Model may reject.
    """

    fields: list[TaskModelOptionField] = []
    if profile.parameters:
        fields.extend(_fields_from_image_parameters(profile.parameters))
    elif profile.wire in {"openai_images", "openai_subscription"}:
        fields.extend(_openai_image_fallback_fields(profile, model))
    if profile.passthrough:
        fields.append(_provider_options_field(profile.passthrough))
    return tuple(fields)


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
    if spec_type == "number":
        return TaskModelOptionField(
            name=name,
            type="number",
            label=_image_parameter_label(name),
            default=None,
            step=1,
            description=_image_parameter_description(name, spec),
        )
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
    example: dict[str, dict[str, str]] = {}
    for slug in sorted(str(key) for key in passthrough):
        keys = passthrough.get(slug)
        if isinstance(keys, list | tuple) and keys:
            allowed_parts.append(f"{slug}: {', '.join(str(key) for key in keys)}")
            if not example:
                example[slug] = {str(keys[0]): "..."}
    description = (
        "Options only one upstream provider understands, as a JSON object keyed by that "
        "provider. vBot sends them unchanged; the provider's documentation lists their values."
    )
    if allowed_parts:
        description = f"{description} Supported keys: {'; '.join(allowed_parts)}."
    return TaskModelOptionField(
        name="provider_options",
        type="json",
        label="Provider-specific options",
        default={},
        description=description,
        placeholder=json.dumps(example) if example else "",
    )


def _openai_image_fallback_fields(
    profile: ImageProfile, model: Model | None
) -> list[TaskModelOptionField]:
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
        if field_name == "n" and profile.wire == "openai_subscription":
            return False
        return supported is None or field_name in supported

    union: tuple[tuple[str, dict[str, Any]], ...] = (
        ("size", {"type": "enum", "values": list(GPT_IMAGE_SIZE_CHOICES)}),
        ("quality", {"type": "enum", "values": list(GPT_IMAGE_QUALITY_CHOICES)}),
        ("background", {"type": "enum", "values": list(GPT_IMAGE_BACKGROUND_CHOICES)}),
        ("n", {"type": "range", "min": 1, "max": 10}),
        ("output_format", {"type": "enum", "values": list(GPT_IMAGE_OUTPUT_FORMAT_CHOICES)}),
        ("style", {"type": "enum", "values": list(DALL_E_STYLE_CHOICES)}),
    )
    fields: list[TaskModelOptionField] = []
    for name, spec in union:
        if not has(name):
            continue
        field = _field_from_image_parameter(name, spec)
        if field is not None:
            fields.append(field)
    return fields
