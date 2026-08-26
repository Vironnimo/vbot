"""Single source of truth for the ``defaults.agent`` field surface.

Every consumer of ``defaults.agent`` — the public ``settings.update`` parser
(``settings.py``), raw-file validation (``validation.py``), storage normalization
(``normalizers.py`` + ``storage.py``), the Settings-path catalog (``paths.py``),
and the Agent store's default baking (``agents.py``) / resolver provenance
(``resolver.py``) — derives its field knowledge from this module. No other module
hardcodes the field list or per-field rules; the literal field set below is the
only one.

The module is a leaf: it imports only ``core.utils.errors`` at top level, and
reaches into ``core.settings.settings`` / ``core.settings.validation`` lazily
inside the behavior callables so the package has no import cycle. That keeps the
canonical validators in those modules the one implementation of each value rule.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from core.config_validation import JsonDiagnostic
from core.utils.errors import StorageError

# Only literal enumeration of the defaults.agent field names. Every consumer
# imports this; none may repeat the set.
AGENT_DEFAULT_FIELDS = frozenset({"model", "fallback_models", "temperature", "thinking_effort"})

# Stable catalog order (matches the historical Settings-path listing).
AGENT_DEFAULT_FIELD_ORDER = ("model", "fallback_models", "temperature", "thinking_effort")

# Value kinds: the shape each field carries. Consumers branch on shape through
# these names rather than re-listing field names.
KIND_MODEL_BINDING = "model_binding"
KIND_MODEL_BINDING_LIST = "model_binding_list"
KIND_NULLABLE_NUMBER = "nullable_number"
KIND_NULLABLE_ENUM = "nullable_enum"


@dataclass(frozen=True)
class AgentDefaultSpec:
    """One ``defaults.agent`` field's shared rules, keyed by id."""

    name: str
    kind: str
    value_type: str
    description: str
    parse: Callable[[Any, str], Any]
    diagnose: Callable[[list[JsonDiagnostic], str, Any], None]
    normalize: Callable[[Any], str | list[str] | float | None]
    allowed_values: tuple[str, ...] = ()
    minimum: float | None = None
    maximum: float | None = None


def _parse_model_binding(value: Any, label: str) -> str:
    from core.settings.settings import SettingsValidationError

    if not isinstance(value, str):
        raise SettingsValidationError(f"{label} must be a string or null")
    return value


def _parse_fallback_models(value: Any, label: str) -> list[str]:
    from core.settings.settings import SettingsValidationError

    del label  # message is fixed for this field
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise SettingsValidationError(
            "params.defaults.agent.fallback_models must be a string list or null"
        )
    return value


def _parse_temperature(value: Any, label: str) -> float | None:
    from core.settings.settings import validate_temperature

    return validate_temperature(value, label=label, allow_none=True)


def _parse_thinking_effort(value: Any, label: str) -> str | None:
    from core.settings.settings import validate_thinking_effort

    return validate_thinking_effort(value, label=label, allow_none=True)


def _diagnose_model_binding(diagnostics: list[JsonDiagnostic], path: str, value: Any) -> None:
    from core.config_validation import add_error

    if value is not None and not isinstance(value, str):
        add_error(diagnostics, path, "must be a string or null")


def _diagnose_fallback_models(diagnostics: list[JsonDiagnostic], path: str, value: Any) -> None:
    from core.config_validation import add_error

    if value is not None and (
        not isinstance(value, list) or any(not isinstance(item, str) for item in value)
    ):
        add_error(diagnostics, path, "must be a string list or null")


def _diagnose_temperature(diagnostics: list[JsonDiagnostic], path: str, value: Any) -> None:
    from core.settings.validation import validate_temperature_diagnostic

    validate_temperature_diagnostic(diagnostics, path, value, allow_none=True)


def _diagnose_thinking_effort(diagnostics: list[JsonDiagnostic], path: str, value: Any) -> None:
    from core.settings.validation import validate_thinking_effort_diagnostic

    validate_thinking_effort_diagnostic(diagnostics, path, value, allow_none=True)


def _normalize_model_binding(value: Any) -> str:
    if not isinstance(value, str):
        raise StorageError("Agent default model must be a string")
    return value


def _normalize_fallback_models(value: Any) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise StorageError("Agent default fallback_models must be a string list")
    return list(value)


def _normalize_temperature(value: Any) -> float:
    from core.settings.settings import MAX_TEMPERATURE, MIN_TEMPERATURE

    if isinstance(value, bool) or not isinstance(value, int | float):
        raise StorageError("Agent default temperature must be a number or null")
    temperature = float(value)
    if not math.isfinite(temperature):
        raise StorageError("Agent default temperature must be finite")
    if temperature < MIN_TEMPERATURE or temperature > MAX_TEMPERATURE:
        raise StorageError(
            f"Agent default temperature must be between {MIN_TEMPERATURE:g} and {MAX_TEMPERATURE:g}"
        )
    return temperature


def _normalize_thinking_effort(value: Any) -> str:
    from core.settings.settings import ALLOWED_THINKING_EFFORTS

    if not isinstance(value, str):
        raise StorageError("Agent default thinking_effort must be a string or null")
    if value not in ALLOWED_THINKING_EFFORTS:
        allowed = ", ".join(repr(item) for item in sorted(ALLOWED_THINKING_EFFORTS))
        raise StorageError(f"Agent default thinking_effort must be one of: {allowed}")
    return value


_AGENT_DEFAULT_SPECS: dict[str, AgentDefaultSpec] = {
    "model": AgentDefaultSpec(
        name="model",
        kind=KIND_MODEL_BINDING,
        value_type="string",
        description="Default Chat Model binding.",
        parse=_parse_model_binding,
        diagnose=_diagnose_model_binding,
        normalize=_normalize_model_binding,
    ),
    "fallback_models": AgentDefaultSpec(
        name="fallback_models",
        kind=KIND_MODEL_BINDING_LIST,
        value_type="array",
        description="Ordered fallback Chat Model bindings tried when the primary fails.",
        parse=_parse_fallback_models,
        diagnose=_diagnose_fallback_models,
        normalize=_normalize_fallback_models,
    ),
    "temperature": AgentDefaultSpec(
        name="temperature",
        kind=KIND_NULLABLE_NUMBER,
        value_type="number",
        description="Default sampling temperature.",
        parse=_parse_temperature,
        diagnose=_diagnose_temperature,
        normalize=_normalize_temperature,
        minimum=None,
        maximum=None,
    ),
    "thinking_effort": AgentDefaultSpec(
        name="thinking_effort",
        kind=KIND_NULLABLE_ENUM,
        value_type="string",
        description="Default Reasoning effort.",
        parse=_parse_thinking_effort,
        diagnose=_diagnose_thinking_effort,
        normalize=_normalize_thinking_effort,
    ),
}


def agent_default_specs() -> Mapping[str, AgentDefaultSpec]:
    """Return the field specs keyed by field name (read-only snapshot)."""
    return _AGENT_DEFAULT_SPECS


def parse_agent_default_value(field: str, value: Any, label: str) -> Any:
    """Validate and normalize one ``defaults.agent`` field for ``settings.update``.

    Raises ``SettingsValidationError`` on a bad value. Mirrors the public update
    parser's messages exactly. Callers must skip ``None`` (the parser handles the
    null-passthrough itself).
    """
    spec = _AGENT_DEFAULT_SPECS.get(field)
    if spec is None:
        raise StorageError(f"Unsupported defaults.agent setting: {field}")
    return spec.parse(value, label)


def diagnose_agent_default_value(diagnostics: list[JsonDiagnostic], path: str, value: Any) -> None:
    """Append a diagnostic for one ``defaults.agent`` field when invalid.

    No-op for ``None`` (the caller skips absent values). Mirrors the raw-file
    validator's messages exactly.
    """
    spec = _AGENT_DEFAULT_SPECS.get(path.rsplit(".", 1)[-1])
    if spec is None:
        return
    spec.diagnose(diagnostics, path, value)


def normalize_agent_default_value(field: str, value: Any) -> str | list[str] | float | None:
    """Validate and normalize a single ``defaults.agent`` field value for storage.

    Raises ``StorageError`` on a bad value; ``None`` passes through unchanged.
    Centralizes the storage-layer messages so no caller re-implements them.
    """
    if value is None:
        return None
    spec = _AGENT_DEFAULT_SPECS.get(field)
    if spec is None:
        raise StorageError(f"Unsupported defaults.agent setting: {field}")
    return spec.normalize(value)


def agent_default_catalog() -> list[
    tuple[str, str, str, tuple[str, ...], float | None, float | None]
]:
    """Return ``(field, value_type, description, allowed_values, minimum, maximum)``.

    Ordered to match ``AGENT_DEFAULT_FIELDS``; consumed by the Settings-path catalog
    so the public path entries never drift from the registry.
    """
    from core.settings.settings import ALLOWED_THINKING_EFFORTS, MAX_TEMPERATURE, MIN_TEMPERATURE

    entries: list[tuple[str, str, str, tuple[str, ...], float | None, float | None]] = []
    for field in AGENT_DEFAULT_FIELD_ORDER:
        spec = _AGENT_DEFAULT_SPECS[field]
        allowed_values = spec.allowed_values
        minimum = spec.minimum
        maximum = spec.maximum
        if field == "thinking_effort":
            allowed_values = tuple(sorted(ALLOWED_THINKING_EFFORTS))
        if field == "temperature":
            minimum = MIN_TEMPERATURE
            maximum = MAX_TEMPERATURE
        entries.append((field, spec.value_type, spec.description, allowed_values, minimum, maximum))
    return entries


@dataclass(frozen=True)
class AgentDefaults:
    """Typed ``defaults.agent`` map consumed internally by baking and resolution.

    ``None`` on a field means "no global default for that field" (absent key). A
    present-but-empty value (``""`` model, ``[]`` fallback chain, ``""`` thinking
    effort) is a real default and is preserved through ``to_dict`` so the persisted
    shape round-trips unchanged.
    """

    model: str | None = None
    fallback_models: list[str] | None = None
    temperature: float | None = None
    thinking_effort: str | None = None

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> AgentDefaults:
        """Build from a raw ``defaults.agent`` mapping; missing keys are ``None``."""
        if not data:
            return cls()
        fallback_models = data.get("fallback_models")
        return cls(
            model=data.get("model"),
            fallback_models=list(fallback_models) if fallback_models is not None else None,
            temperature=data.get("temperature"),
            thinking_effort=data.get("thinking_effort"),
        )

    def to_dict(self) -> dict[str, Any]:
        """Return the non-``None`` fields as a plain ``defaults.agent`` mapping."""
        result: dict[str, Any] = {}
        if self.model is not None:
            result["model"] = self.model
        if self.fallback_models is not None:
            result["fallback_models"] = list(self.fallback_models)
        if self.temperature is not None:
            result["temperature"] = self.temperature
        if self.thinking_effort is not None:
            result["thinking_effort"] = self.thinking_effort
        return result

    def get(self, field: str) -> Any:
        """Return one field's value (``None`` when absent)."""
        if field == "model":
            return self.model
        if field == "fallback_models":
            return self.fallback_models
        if field == "temperature":
            return self.temperature
        if field == "thinking_effort":
            return self.thinking_effort
        raise KeyError(field)


def bake_agent_defaults(
    *,
    model: str,
    fallback_models: list[str],
    temperature: float | None,
    thinking_effort: str | None,
    defaults: AgentDefaults,
) -> dict[str, Any]:
    """Compute the fields changed by baking global defaults into one agent.

    Mirrors the resolver's identity-branch provenance exactly: a default applies
    only when the agent's own value is empty/absent. Returns the changed fields; an
    empty dict means nothing changed. Shared by ``AgentStore._apply_defaults`` and
    the server test double so the baking rule lives in one place.
    """
    changes: dict[str, Any] = {}
    if model == "" and defaults.model is not None:
        changes["model"] = defaults.model
    if fallback_models == [] and defaults.fallback_models is not None:
        changes["fallback_models"] = list(defaults.fallback_models)
    if temperature is None and defaults.temperature is not None:
        changes["temperature"] = defaults.temperature
    if thinking_effort is None and defaults.thinking_effort is not None:
        changes["thinking_effort"] = defaults.thinking_effort
    return changes
