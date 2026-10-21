"""Consistency between the agent-defaults registry and every surface that derives from it.

Renaming or adding a ``defaults.agent`` field must touch the registry and nowhere
else. These tests fail if any derived surface (Settings-path catalog, raw-file
validation, ``settings.update`` parse) drifts from the single field set, so a
future edit is caught here instead of by a 65-file fan-out.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from core.config_validation import JsonDiagnostic
from core.permissions import normalize_permission_rules
from core.settings import (
    AGENT_DEFAULT_FIELDS,
    MAX_FALLBACK_MODELS,
    AgentDefaults,
    agent_default_catalog,
    bake_agent_defaults,
    diagnose_agent_default_value,
    load_runtime_settings_json,
    normalize_agent_default_value,
    parse_agent_default_value,
)
from core.settings.agent_defaults import (
    INVALID_PERMISSION_RULES,
    InvalidPermissionRulesDefaultError,
    agent_default_specs,
)
from core.settings.paths import setting_definitions
from core.settings.settings import (
    ALLOWED_THINKING_EFFORTS,
    MAX_TEMPERATURE,
    MIN_TEMPERATURE,
    SettingsValidationError,
)
from core.utils.errors import StorageError


def test_every_derived_surface_carries_exactly_the_registry_fields() -> None:
    fields = set(AGENT_DEFAULT_FIELDS)
    settings_paths = {
        definition.template
        for definition in setting_definitions()
        if definition.template.startswith("defaults.agent.")
    }

    assert set(agent_default_specs()) == fields
    assert {field for field, *_ in agent_default_catalog()} == fields
    assert settings_paths == {f"defaults.agent.{field}" for field in fields}


@pytest.mark.parametrize("field", sorted(AGENT_DEFAULT_FIELDS))
def test_every_field_dispatches_through_all_three_surfaces(field: str) -> None:
    # parse: a valid non-null value returns unchanged (kind-preserving types).
    if field == "model":
        assert parse_agent_default_value(field, "openai/gpt-4", label="l") == "openai/gpt-4"
    elif field == "fallback_models":
        assert parse_agent_default_value(field, ["openai/gpt-4"], label="l") == ["openai/gpt-4"]
    elif field in {"temperature", "top_p"}:
        assert parse_agent_default_value(field, 0.5, label="l") == 0.5
    elif field == "tool_loading":
        value = {"on_demand": True, "always_loaded": ["read"]}
        assert parse_agent_default_value(field, value, label="l") == value
    elif field == "permission_rules":
        value = {"read": "work_area", "deny": ["Read({workdir}/x)"]}
        assert parse_agent_default_value(field, value, label="l") == value
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
        "top_p": 0.9,
        "thinking_effort": "high",
        "tool_loading": {"on_demand": True},
        "permission_rules": {"write": "scratch"},
    }
    assert AgentDefaults.from_dict(data).to_dict() == data


def test_agent_defaults_absent_fields_are_none() -> None:
    defaults = AgentDefaults.from_dict({})
    assert defaults.model is None
    assert defaults.fallback_models is None
    assert defaults.temperature is None
    assert defaults.top_p is None
    assert defaults.thinking_effort is None
    assert defaults.tool_loading is None
    assert defaults.permission_rules is None
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
        top_p=None,
        thinking_effort=None,
        tool_loading=None,
        defaults=AgentDefaults.from_dict(agent_defaults),
    )


@pytest.mark.parametrize("chain", _INVALID_CHAINS)
def test_invalid_fallback_chain_is_rejected_by_every_surface(
    tmp_path: Path, chain: list[str]
) -> None:
    with pytest.raises(SettingsValidationError):
        parse_agent_default_value(
            "fallback_models", chain, label="params.defaults.agent.fallback_models"
        )
    with pytest.raises(StorageError):
        normalize_agent_default_value("fallback_models", chain)
    with pytest.raises(StorageError):
        _bake_defaults({"fallback_models": chain})
    diagnostics: list[JsonDiagnostic] = []
    diagnose_agent_default_value(diagnostics, "$.defaults.agent.fallback_models", chain)
    assert [(item.severity, item.path) for item in diagnostics] == [
        ("error", "$.defaults.agent.fallback_models")
    ]

    path = tmp_path / "settings.json"
    content = json.dumps(
        {"format_version": 1, "defaults": {"agent": {"fallback_models": chain}}, "port": 23456}
    )
    path.write_text(content, encoding="utf-8")
    usable, ignored = load_runtime_settings_json(path)
    assert usable == {"port": 23456}
    assert ignored == tuple(diagnostics)
    assert path.read_text(encoding="utf-8") == content


@pytest.mark.parametrize(
    ("rules", "loaded"),
    [
        pytest.param({"write": "nowhere"}, INVALID_PERMISSION_RULES, id="invalid"),
        pytest.param({"write": "scratch"}, {"write": "scratch"}, id="valid-beside-invalid"),
    ],
)
def test_runtime_settings_never_drop_default_permission_rules(
    tmp_path: Path, rules: dict[str, Any], loaded: object
) -> None:
    # An invalid ``defaults`` section is left out, but losing the rules would give
    # every inheriting Agent full access: they stay, or refuse when unreadable.
    path = tmp_path / "settings.json"
    agent = {"permission_rules": rules, "temperature": 9}
    path.write_text(json.dumps({"format_version": 1, "defaults": {"agent": agent}}))

    usable, ignored = load_runtime_settings_json(path)

    assert usable == {"defaults": {"agent": {"permission_rules": loaded}}}
    assert ignored
    defaults = AgentDefaults.from_dict(usable["defaults"]["agent"])
    if loaded == INVALID_PERMISSION_RULES:
        with pytest.raises(InvalidPermissionRulesDefaultError, match="vbot doctor config"):
            defaults.resolved_permission_rules()
        # Baking skips it, so the Agent still lists; resolution refuses it.
        assert "permission_rules" not in _bake_defaults(usable["defaults"]["agent"])
    else:
        assert _bake_defaults(usable["defaults"]["agent"]) == {
            "permission_rules": normalize_permission_rules(rules)
        }


def test_valid_fallback_chain_is_stripped_by_normalize_and_bake() -> None:
    assert normalize_agent_default_value("fallback_models", [" openai/gpt "]) == ["openai/gpt"]
    assert _bake_defaults({"fallback_models": [" openai/gpt "]}) == {
        "fallback_models": ["openai/gpt"]
    }


@pytest.mark.parametrize(
    "agent_defaults",
    [
        pytest.param({"temperature": MAX_TEMPERATURE + 1}, id="out-of-range-temperature"),
        pytest.param({"thinking_effort": "ultra"}, id="unknown-thinking-effort"),
        pytest.param({"model": 42}, id="non-string-model"),
        pytest.param({"tool_loading": {"on_demand": "yes"}}, id="non-boolean-tool-loading"),
    ],
)
def test_bake_rejects_unusable_defaults(agent_defaults: dict[str, Any]) -> None:
    with pytest.raises(StorageError):
        _bake_defaults(agent_defaults)


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
