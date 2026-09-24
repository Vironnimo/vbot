"""Live voice options: spoken voice, the delegating backend Model, and its reasoning.

The fields render from the ``live_voice`` facts in
``capabilities.task_options``. A ``model``-typed ``backend_model`` parameter
offers the tool-capable chat Models of the same Provider that the target's
Connection allows; the Live runtime re-checks the configured backend with
:func:`live_backend_candidates` when a call starts. A spec with
``"allow_none": true`` also offers ``""`` (no backend model: the voice Model
calls the Live app Tools itself) and makes the field optional. The backend's
reasoning effort accompanies that field, narrows its visible choices to each
candidate's published reasoning ladder, and is hidden without a backend.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Protocol

from core.model_tasks._option_types import (
    TaskModelOptionChoice,
    TaskModelOptionField,
    TaskModelOptionsBy,
    _string_values,
    _task_options,
)
from core.model_tasks.constants import TASK_LIVE_VOICE
from core.models import Model, ModelQuery

_BACKEND_MODEL_QUERY_TASKS = ("chat",)
_BACKEND_MODEL_QUERY_CAPABILITIES = ("tools",)

BACKEND_THINKING_EFFORT_DEFAULT = "low"
"""Default backend reasoning effort: spoken answers need low latency."""

# "" requests no explicit effort, so the Provider default of the Model applies.
_MODEL_DEFAULT_EFFORT = ""
_MODEL_DEFAULT_EFFORT_LABEL = "Model default"
# Efforts that stay available for every published ladder: the Model default
# and reasoning off. The Agent editor offers the same set.
_ALWAYS_ALLOWED_EFFORTS = (_MODEL_DEFAULT_EFFORT, "none")

# ``backend_model`` value for "no backend model" on targets that allow it.
NO_BACKEND_MODEL = ""
_NO_BACKEND_MODEL_LABEL = "None (the voice model uses vBot directly)"


class ModelCatalog(Protocol):
    """The Model registry read surface option building needs."""

    def query(self, model_query: ModelQuery) -> Iterable[tuple[str, Model]]: ...


def live_backend_candidates(
    models: ModelCatalog,
    provider_id: str,
    connection_id: str,
) -> tuple[Model, ...]:
    """Return the Models a live voice target may delegate to, sorted by name.

    Candidates are tool-capable chat Models of *provider_id* whose Connection
    allowlist permits the local *connection_id* (for example
    ``"subscription"``), so a delegation runs on the Connection the live call
    already uses.
    """

    model_query = ModelQuery(
        provider_id=provider_id,
        tasks=_BACKEND_MODEL_QUERY_TASKS,
        capabilities=_BACKEND_MODEL_QUERY_CAPABILITIES,
    )
    candidates = [
        model
        for matched_provider_id, model in models.query(model_query)
        if matched_provider_id == provider_id and model.allows_connection(connection_id)
    ]
    return tuple(sorted(candidates, key=lambda model: (model.name.casefold(), model.model_id)))


def _live_voice_fields(
    provider_id: str,
    model: Model | None,
    *,
    models: ModelCatalog | None,
    connection_id: str,
) -> tuple[TaskModelOptionField, ...]:
    parameters = _task_options(model, TASK_LIVE_VOICE).get("parameters")
    if not isinstance(parameters, Mapping):
        return ()

    fields: list[TaskModelOptionField] = []
    voice_field = _voice_field(parameters.get("voice"))
    if voice_field is not None:
        fields.append(voice_field)
    backend_spec = parameters.get("backend_model")
    if isinstance(backend_spec, Mapping) and backend_spec.get("type") == "model":
        candidates = (
            live_backend_candidates(models, provider_id, connection_id)
            if models is not None
            else ()
        )
        allow_none = backend_spec.get("allow_none") is True
        fields.append(_backend_model_field(backend_spec, candidates, allow_none=allow_none))
        fields.append(_backend_thinking_effort_field(candidates, allow_none=allow_none))
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


def _backend_model_field(
    spec: Mapping[str, Any],
    candidates: tuple[Model, ...],
    *,
    allow_none: bool,
) -> TaskModelOptionField:
    choices = tuple(
        TaskModelOptionChoice(value=candidate.model_id, label=candidate.name)
        for candidate in candidates
    )
    if allow_none:
        choices = (
            TaskModelOptionChoice(value=NO_BACKEND_MODEL, label=_NO_BACKEND_MODEL_LABEL),
            *choices,
        )
    return TaskModelOptionField(
        name="backend_model",
        type="select",
        label="Backend model",
        default=_declared_default(spec, tuple(choice.value for choice in choices)),
        required=not allow_none,
        description=(
            "Model that answers requests and operates vBot during a live call. "
            "Only tool-capable Models of the same Provider on this Connection are offered."
        ),
        options=choices,
    )


def backend_thinking_efforts() -> tuple[str, ...]:
    """Return every backend reasoning effort in canonical order, ``""`` first."""

    # Deferred: ``core.settings.settings`` imports this module (through
    # ``core.model_tasks.options``) while it loads. A module-level import of
    # ``core.providers`` would load every Provider Adapter at that point, and
    # the OpenRouter Adapter imports the half-initialized settings module.
    from core.providers.reasoning import THINKING_EFFORT_ORDER

    return (_MODEL_DEFAULT_EFFORT, *THINKING_EFFORT_ORDER)


def _backend_thinking_effort_field(
    candidates: tuple[Model, ...], *, allow_none: bool
) -> TaskModelOptionField:
    efforts = backend_thinking_efforts()
    allowed_by_backend: dict[str, tuple[str, ...]] = {}
    if allow_none:
        # Without a backend model there is nothing to reason; hide the field.
        allowed_by_backend[NO_BACKEND_MODEL] = ()
    for candidate in candidates:
        # Without a published ladder the Adapter applies a Provider-specific
        # floor that the UI cannot see, so every effort stays visible.
        levels = candidate.capabilities.reasoning.levels
        if levels:
            allowed = {*_ALWAYS_ALLOWED_EFFORTS, *levels}
            allowed_by_backend[candidate.model_id] = tuple(
                effort for effort in efforts if effort in allowed
            )
    return TaskModelOptionField(
        name="backend_thinking_effort",
        type="select",
        label="Backend reasoning",
        default=BACKEND_THINKING_EFFORT_DEFAULT,
        description=(
            "Reasoning effort of the backend model for each request. "
            "Higher effort can make spoken answers slower."
        ),
        options=tuple(
            TaskModelOptionChoice(
                value=effort,
                label=_MODEL_DEFAULT_EFFORT_LABEL if effort == _MODEL_DEFAULT_EFFORT else effort,
            )
            for effort in efforts
        ),
        options_by=(
            TaskModelOptionsBy(field="backend_model", values=allowed_by_backend)
            if allowed_by_backend
            else None
        ),
    )


def _declared_default(spec: Mapping[str, Any], values: tuple[str, ...]) -> str | None:
    declared = spec.get("default")
    if isinstance(declared, str) and declared in values:
        return declared
    return values[0] if values else None
