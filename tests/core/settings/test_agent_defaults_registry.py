"""Consistency between the agent-defaults registry and every surface that derives from it.

Renaming or adding a ``defaults.agent`` field must touch the registry and nowhere
else. These tests fail if any derived surface (Settings-path catalog, raw-file
validation, ``settings.update`` parse) drifts from the single field set, so a
future edit is caught here instead of by a 65-file fan-out.
"""

from __future__ import annotations

from typing import Any

import pytest

from core.config_validation import JsonDiagnostic
from core.settings import (
    AGENT_DEFAULT_FIELDS,
    MAX_FALLBACK_MODELS,
    AgentDefaults,
    agent_default_catalog,
    bake_agent_defaults,
    diagnose_agent_default_value,
    normalize_agent_default_value,
    parse_agent_default_value,
)
from core.settings.agent_defaults import agent_default_specs
from core.settings.paths import setting_definitions
from core.settings.settings import (
    ALLOWED_THINKING_EFFORTS,
    MAX_TEMPERATURE,
    MIN_TEMPERATURE,
    SettingsValidationError,
)
from core.utils.errors import StorageError


def test_registry_fields_match_canonical_set() -> None:
    assert set(agent_default_specs()) == set(AGENT_DEFAULT_FIELDS)


def test_catalog_fields_match_registry() -> None:
    catalog_fields = [field for field, *_ in agent_default_catalog()]
    assert set(catalog_fields) == set(AGENT_DEFAULT_FIELDS)


def test_settings_catalog_carries_exactly_agent_default_paths() -> None:
    agent_paths = {
        definition.template
        for definition in setting_definitions()
        if definition.template.startswith("defaults.agent.")
    }
    assert agent_paths == {f"defaults.agent.{field}" for field in AGENT_DEFAULT_FIELDS}


@pytest.mark.parametrize("field", sorted(AGENT_DEFAULT_FIELDS))
def test_every_field_dispatches_through_all_three_surfaces(field: str) -> None:
    # parse: a valid non-null value returns unchanged (kind-preserving types).
    if field == "model":
        assert parse_agent_default_value(field, "openai/gpt-4", label="l") == "openai/gpt-4"
    elif field == "fallback_models":
        assert parse_agent_default_value(field, ["openai/gpt-4"], label="l") == ["openai/gpt-4"]
    elif field == "temperature":
        assert parse_agent_default_value(field, 0.5, label="l") == 0.5
    else:
        assert parse_agent_default_value(field, "high", label="l") == "high"

    # normalize: null passes through; a value normalizes.
    assert normalize_agent_default_value(field, None) is None

    # diagnose: a null value appends nothing.
    diagnostics: list[JsonDiagnostic] = []
    diagnose_agent_default_value(diagnostics, f"$.defaults.agent.{field}", None)
    assert diagnostics == []


def test_unknown_field_rejected_by_parse_and_normalize() -> None:
    with pytest.raises(StorageError):
        normalize_agent_default_value("unknown_field", "x")
    with pytest.raises(StorageError):
        parse_agent_default_value("unknown_field", "x", label="l")


def test_agent_defaults_round_trips_non_null_fields() -> None:
    data = {
        "model": "openai/gpt-4",
        "fallback_models": ["openai/gpt-mini"],
        "temperature": 0.7,
        "thinking_effort": "high",
    }
    assert AgentDefaults.from_dict(data).to_dict() == data


def test_agent_defaults_absent_fields_are_none() -> None:
    defaults = AgentDefaults.from_dict({})
    assert defaults.model is None
    assert defaults.fallback_models is None
    assert defaults.temperature is None
    assert defaults.thinking_effort is None
    assert defaults.to_dict() == {}


# --- fallback chain rules -----------------------------------------------------

# One chain-rule breach per case; every surface must reject the same classes.
_INVALID_CHAINS = [
    pytest.param(["   "], id="empty-after-strip"),
    pytest.param(["openai/gpt"] * (MAX_FALLBACK_MODELS + 1), id="over-cap"),
    pytest.param(["openai/gpt", "openai/gpt"], id="duplicates"),
]


def _bake_defaults(agent_defaults: dict[str, Any]) -> dict[str, Any]:
    return bake_agent_defaults(
        model="",
        fallback_models=[],
        temperature=None,
        thinking_effort=None,
        defaults=AgentDefaults.from_dict(agent_defaults),
    )


@pytest.mark.parametrize("chain", _INVALID_CHAINS)
def test_invalid_fallback_chain_rejected_by_parse(chain: list[str]) -> None:
    with pytest.raises(SettingsValidationError):
        parse_agent_default_value(
            "fallback_models", chain, label="params.defaults.agent.fallback_models"
        )


@pytest.mark.parametrize("chain", _INVALID_CHAINS)
def test_invalid_fallback_chain_rejected_by_normalize(chain: list[str]) -> None:
    with pytest.raises(StorageError):
        normalize_agent_default_value("fallback_models", chain)


@pytest.mark.parametrize("chain", _INVALID_CHAINS)
def test_invalid_fallback_chain_rejected_by_bake(chain: list[str]) -> None:
    with pytest.raises(StorageError):
        _bake_defaults({"fallback_models": chain})


def test_valid_fallback_chain_is_stripped_by_normalize_and_bake() -> None:
    assert normalize_agent_default_value("fallback_models", [" openai/gpt "]) == ["openai/gpt"]
    assert _bake_defaults({"fallback_models": [" openai/gpt "]}) == {
        "fallback_models": ["openai/gpt"]
    }


def test_bake_rejects_out_of_range_temperature() -> None:
    with pytest.raises(StorageError):
        _bake_defaults({"temperature": MAX_TEMPERATURE + 1})


def test_bake_rejects_unknown_thinking_effort() -> None:
    with pytest.raises(StorageError):
        _bake_defaults({"thinking_effort": "ultra"})


def test_bake_rejects_non_string_model() -> None:
    with pytest.raises(StorageError):
        _bake_defaults({"model": 42})


# --- catalog bounds derive from kind ------------------------------------------


def test_catalog_carries_bounds_for_bounded_kinds_only() -> None:
    catalog = {entry[0]: entry for entry in agent_default_catalog()}

    temperature = catalog["temperature"]
    assert (
        temperature[3] == ()
        and temperature[4] == MIN_TEMPERATURE
        and temperature[5] == MAX_TEMPERATURE
    )

    effort = catalog["thinking_effort"]
    assert effort[3] == tuple(sorted(ALLOWED_THINKING_EFFORTS))
    assert effort[4] is None and effort[5] is None

    model = catalog["model"]
    assert model[3] == () and model[4] is None and model[5] is None
