"""Live voice options: the spoken voice and who answers the voice model's requests.

The fields render from the ``live_voice`` facts in
``capabilities.task_options``. An enum ``voice`` parameter offers the Provider's
voices. An enum ``backend`` parameter says who answers what the voice model
hands on: ``vbot`` (the built-in Live backend Agent), ``openai`` (OpenAI's
hosted backend model operates vBot with the voice Agent's Tools), or ``none``
(the voice model uses its own Tools only). An enum ``openai_backend_model``
parameter picks OpenAI's backend model; it shows only with ``backend`` ``openai``.
An enum ``thinking_level`` parameter sets how deeply the voice model reasons
(Gemini Live). A ``tool_calls: "non_blocking"`` fact says the voice model runs
function calls only asynchronously.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from core.model_tasks._option_types import (
    TaskModelOptionChoice,
    TaskModelOptionField,
    TaskModelOptionsBy,
    _string_values,
    _task_options,
)
from core.model_tasks.constants import TASK_LIVE_VOICE
from core.models import Model

LIVE_BACKEND_VBOT = "vbot"
LIVE_BACKEND_OPENAI = "openai"
LIVE_BACKEND_NONE = "none"
LIVE_BACKENDS = (LIVE_BACKEND_VBOT, LIVE_BACKEND_OPENAI, LIVE_BACKEND_NONE)
_BACKEND_LABELS = {
    LIVE_BACKEND_VBOT: "vBot (the Live backend Agent)",
    LIVE_BACKEND_OPENAI: "OpenAI (its backend model uses the voice Agent's Tools)",
    LIVE_BACKEND_NONE: "None (the voice model uses only its own Tools)",
}


def live_backend_choices(model: Model | None) -> tuple[str, ...]:
    """The ``backend`` values *model* offers, its default first; empty without the option."""
    parameters = _task_options(model, TASK_LIVE_VOICE).get("parameters")
    spec = parameters.get("backend") if isinstance(parameters, Mapping) else None
    values = tuple(value for value in _string_values(spec) if value in LIVE_BACKENDS)
    if not values or not isinstance(spec, Mapping):
        return ()
    default = _declared_default(spec, values)
    return (default, *(value for value in values if value != default)) if default else values


def live_thinking_level(model: Model | None, options: Mapping[str, Any]) -> str | None:
    """The thinking level to request: the option, else the declared default.

    ``None`` when *model* declares no ``thinking_level``; a Model that
    requires a level declares a default.
    """
    spec = _parameter(model, "thinking_level")
    values = _string_values(spec) if isinstance(spec, Mapping) else ()
    if not values or not isinstance(spec, Mapping):
        return None
    chosen = options.get("thinking_level")
    if isinstance(chosen, str) and chosen in values:
        return chosen
    return _declared_default(spec, values)


def live_tools_non_blocking(model: Model | None) -> bool:
    """Whether *model* runs function calls only asynchronously."""
    return _task_options(model, TASK_LIVE_VOICE).get("tool_calls") == "non_blocking"


def _parameter(model: Model | None, name: str) -> Any:
    parameters = _task_options(model, TASK_LIVE_VOICE).get("parameters")
    return parameters.get(name) if isinstance(parameters, Mapping) else None


def _live_voice_fields(model: Model | None) -> tuple[TaskModelOptionField, ...]:
    parameters = _task_options(model, TASK_LIVE_VOICE).get("parameters")
    if not isinstance(parameters, Mapping):
        return ()

    fields: list[TaskModelOptionField] = []
    voice_field = _voice_field(parameters.get("voice"))
    if voice_field is not None:
        fields.append(voice_field)
    thinking_field = _thinking_level_field(parameters.get("thinking_level"))
    if thinking_field is not None:
        fields.append(thinking_field)
    backends = live_backend_choices(model)
    if backends:
        fields.append(_backend_field(backends))
        backend_model = _openai_backend_model_field(
            parameters.get("openai_backend_model"), backends
        )
        if backend_model is not None:
            fields.append(backend_model)
    return tuple(fields)


def _voice_field(spec: Any) -> TaskModelOptionField | None:
    if not isinstance(spec, Mapping) or spec.get("type") != "enum":
        return None
    values = tuple(dict.fromkeys(_string_values(spec)))
    if not values:
        return None
    return TaskModelOptionField(
        name="voice",
        type="select",
        label="Voice",
        default=_declared_default(spec, values),
        required=True,
        description="Voice the live Model speaks with.",
        options=tuple(
            TaskModelOptionChoice(value=value, label=value.capitalize()) for value in values
        ),
    )


def _thinking_level_field(spec: Any) -> TaskModelOptionField | None:
    if not isinstance(spec, Mapping) or spec.get("type") != "enum":
        return None
    values = tuple(dict.fromkeys(_string_values(spec)))
    if not values:
        return None
    return TaskModelOptionField(
        name="thinking_level",
        type="select",
        label="Thinking level",
        default=_declared_default(spec, values),
        required=True,
        description="How deeply the voice model reasons before it answers; deeper is slower.",
        options=tuple(
            TaskModelOptionChoice(value=value, label=value.capitalize()) for value in values
        ),
    )


def _backend_field(backends: tuple[str, ...]) -> TaskModelOptionField:
    return TaskModelOptionField(
        name="backend",
        type="select",
        label="Backend",
        default=backends[0],
        required=True,
        description=(
            "Who answers the requests the voice model hands on and operates vBot during a call."
        ),
        options=tuple(
            TaskModelOptionChoice(value=value, label=_BACKEND_LABELS[value])
            for value in sorted(backends, key=LIVE_BACKENDS.index)
        ),
    )


def _openai_backend_model_field(
    spec: Any, backends: tuple[str, ...]
) -> TaskModelOptionField | None:
    if LIVE_BACKEND_OPENAI not in backends or not isinstance(spec, Mapping):
        return None
    values = tuple(dict.fromkeys(_string_values(spec)))
    if not values:
        return None
    hidden: dict[str, tuple[str, ...]] = {
        backend: () for backend in backends if backend != LIVE_BACKEND_OPENAI
    }
    return TaskModelOptionField(
        name="openai_backend_model",
        type="select",
        label="OpenAI backend model",
        default=_declared_default(spec, values),
        description="OpenAI's Model that answers the voice model's requests.",
        options=tuple(TaskModelOptionChoice(value=value, label=value) for value in values),
        options_by=TaskModelOptionsBy(field="backend", values=hidden),
    )


def _declared_default(spec: Mapping[str, Any], values: tuple[str, ...]) -> str | None:
    declared = spec.get("default")
    if isinstance(declared, str) and declared in values:
        return declared
    return values[0] if values else None
