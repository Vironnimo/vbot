"""Reusable, pure query over the model registry.

This module centralizes capability / task / modality / context-window
filtering for :class:`core.models.models.Model` data. The same matcher is
used by both the chat-model RPC (``model.list``) and the specialized
target discovery (``task_model.list_targets``) so the two share a single
vocabulary and a single source of truth for matching rules.

The query is intentionally **pure**: it operates on ``Model`` data and
takes no runtime, credentials, or transport awareness. Credential gating
and response shaping remain responsibilities of the callers.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from core.models.models import Capabilities, Model

# Boolean capabilities that map to a top-level ``Capabilities`` field (or to
# ``capabilities.reasoning.supported``) rather than to a ``task_types`` entry.
_BOOLEAN_MODEL_CAPABILITIES = frozenset(("vision", "tools", "json_mode", "reasoning"))


@dataclass(frozen=True)
class ModelQuery:
    """A reusable, pure query over the model registry.

    Each field is an independent filter. ``None`` / empty values mean
    "no filter" for that dimension.

    * ``provider_id`` — exact (case-insensitive) provider match. ``None``
      or empty means any provider.
    * ``tasks`` — every listed task must be in ``model.capabilities.task_types``.
    * ``capabilities`` — mixed filter: entries in
      ``{"vision", "tools", "json_mode", "reasoning"}`` check the
      corresponding boolean on ``Capabilities``; any other entry is
      treated as a required ``task_type``.
    * ``input_modalities`` / ``output_modalities`` — every listed modality
      must be in the corresponding ``Capabilities`` modality tuple.
    * ``min_context_window`` — ``model.context_window`` must be at least
      this large. ``None`` means no minimum.

    The match is a logical AND across all non-empty fields.
    """

    provider_id: str | None = None
    tasks: tuple[str, ...] = ()
    capabilities: tuple[str, ...] = ()
    input_modalities: tuple[str, ...] = ()
    output_modalities: tuple[str, ...] = ()
    min_context_window: int | None = None

    @classmethod
    def from_filters(cls, params: Mapping[str, Any]) -> ModelQuery:
        """Build a :class:`ModelQuery` from raw filter values.

        ``params`` mirrors the shape accepted by the ``model.list`` RPC:
        keys may use the alias names listed below, values may be a single
        string or a list of strings, casing is mixed, and duplicates are
        permitted. All values are normalized (trimmed, lowercased,
        deduplicated) before being stored on the resulting query.

        Aliases (multiple keys contribute to the same filter):

        * ``"capability"`` / ``"capabilities"``
        * ``"task"`` / ``"tasks"`` / ``"task_type"`` / ``"task_types"``
        * ``"input_modality"`` / ``"input_modalities"``
        * ``"output_modality"`` / ``"output_modalities"``

        Raises:
            ValueError: When a value is of an unexpected type for its
                field (for example a list of non-strings, a non-string
                ``provider_id``, or a negative / non-integer
                ``min_context_window``).
        """

        provider_id = _extract_optional_string(params, "provider_id")
        capabilities = _extract_string_values(params, ("capability", "capabilities"))
        tasks = _extract_string_values(params, ("task", "tasks", "task_type", "task_types"))
        input_modalities = _extract_string_values(params, ("input_modality", "input_modalities"))
        output_modalities = _extract_string_values(params, ("output_modality", "output_modalities"))
        min_context_window = _extract_optional_non_negative_int(params, "min_context_window")
        return cls(
            provider_id=provider_id,
            capabilities=capabilities,
            tasks=tasks,
            input_modalities=input_modalities,
            output_modalities=output_modalities,
            min_context_window=min_context_window,
        )

    def matches(self, model: Model) -> bool:
        """Return ``True`` if ``model`` satisfies every filter on this query."""

        capabilities = model.capabilities
        task_types = set(capabilities.task_types)
        input_modalities = set(capabilities.input_modalities)
        output_modalities = set(capabilities.output_modalities)

        for capability in self.capabilities:
            if capability in _BOOLEAN_MODEL_CAPABILITIES:
                if not _boolean_model_capability(capabilities, capability):
                    return False
            elif capability not in task_types:
                return False

        if any(task not in task_types for task in self.tasks):
            return False

        if any(modality not in input_modalities for modality in self.input_modalities):
            return False

        if any(modality not in output_modalities for modality in self.output_modalities):
            return False

        return not (
            self.min_context_window is not None and model.context_window < self.min_context_window
        )


def _extract_optional_string(params: Mapping[str, Any], field_name: str) -> str | None:
    value = params.get(field_name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    normalized = value.strip().lower()
    return normalized or None


def _extract_string_values(
    params: Mapping[str, Any], field_names: Iterable[str]
) -> tuple[str, ...]:
    raw_values: list[str] = []
    for field_name in field_names:
        if field_name not in params:
            continue
        value = params[field_name]
        if isinstance(value, str):
            raw_values.append(value)
        elif isinstance(value, list) and all(isinstance(item, str) for item in value):
            raw_values.extend(value)
        else:
            raise ValueError(f"{field_name} must be a string or list of strings")
    return _normalize_string_values(raw_values)


def _extract_optional_non_negative_int(params: Mapping[str, Any], field_name: str) -> int | None:
    value = params.get(field_name)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_name} must be a non-negative integer")
    if value == 0:
        return None
    return value


def _normalize_string_values(values: Iterable[str]) -> tuple[str, ...]:
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized_value = value.strip().lower()
        if not normalized_value or normalized_value in seen:
            continue
        seen.add(normalized_value)
        normalized.append(normalized_value)
    return tuple(normalized)


def _boolean_model_capability(capabilities: Capabilities, capability: str) -> bool:
    if capability == "reasoning":
        return bool(capabilities.reasoning.supported)
    return bool(getattr(capabilities, capability))
