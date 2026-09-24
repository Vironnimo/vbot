"""Live voice options: spoken voice and the delegating backend Model.

Both fields render from the ``live_voice`` facts in
``capabilities.task_options``. A ``model``-typed ``backend_model`` parameter
offers the tool-capable chat Models of the same Provider that the target's
Connection allows; the Live runtime re-checks the configured backend with
:func:`live_backend_candidates` when a call starts.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Protocol

from core.model_tasks._option_types import (
    TaskModelOptionChoice,
    TaskModelOptionField,
    _string_values,
    _task_options,
)
from core.model_tasks.constants import TASK_LIVE_VOICE
from core.models import Model, ModelQuery

_BACKEND_MODEL_QUERY_TASKS = ("chat",)
_BACKEND_MODEL_QUERY_CAPABILITIES = ("tools",)


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
        fields.append(_backend_model_field(backend_spec, candidates))
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
) -> TaskModelOptionField:
    values = tuple(candidate.model_id for candidate in candidates)
    return TaskModelOptionField(
        name="backend_model",
        type="select",
        label="Backend model",
        default=_declared_default(spec, values),
        required=True,
        description=(
            "Model that answers requests and operates vBot during a live call. "
            "Only tool-capable Models of the same Provider on this Connection are offered."
        ),
        options=tuple(
            TaskModelOptionChoice(value=candidate.model_id, label=candidate.name)
            for candidate in candidates
        ),
    )


def _declared_default(spec: Mapping[str, Any], values: tuple[str, ...]) -> str | None:
    declared = spec.get("default")
    if isinstance(declared, str) and declared in values:
        return declared
    return values[0] if values else None
