"""Wire profile files, layered resolution and the reasoning decision."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from core.models.models import Capabilities, Model, ReasoningCapabilities
from core.providers._wire_profile_files import (
    load_wire_profile_files,
    parse_wire_profile_file,
)
from core.providers.reasoning import ReasoningIntent
from core.providers.wire_observations import ObservedFacts, WireObservations
from core.providers.wire_profile import (
    BudgetRule,
    ParameterRule,
    ReasoningWire,
    RequestRules,
    WireProfile,
)
from core.providers.wire_profiles import WireProfiles
from core.utils.errors import ProviderError

RESOURCES = Path(__file__).resolve().parents[3] / "resources"


def _model(
    model_id: str,
    *,
    supported: bool = True,
    control: str | None = None,
    levels: tuple[str, ...] = (),
    budget_max: int | None = None,
    family: str = "",
    metadata: Mapping[str, Any] | None = None,
) -> Model:
    return Model(
        model_id=model_id,
        name=model_id,
        capabilities=Capabilities(
            vision=False,
            tools=True,
            json_mode=False,
            reasoning=ReasoningCapabilities(
                supported=supported, control=control, levels=levels, budget_max=budget_max
            ),
        ),
        context_window=100_000,
        max_output_tokens=8_000,
        family=family,
        metadata=metadata or {},
    )


def _profiles(
    document: Mapping[str, Any] | None,
    models: Mapping[str, Model] = {},
    *,
    protocols: tuple[str, ...] = ("chat_completions", "messages", "responses"),
    observed: ObservedFacts | None = None,
) -> tuple[WireProfiles, list[str]]:
    issues: list[str] = []
    files = {}
    if document is not None:
        parsed = parse_wire_profile_file("acme", document, source="acme.json", report=issues.append)
        assert parsed is not None
        files["acme"] = parsed

    store = WireObservations(None)
    if observed is not None:
        for target in ("m",):
            if observed.reasoning_field:
                store.record_reasoning_field("acme", "api-key", target, observed.reasoning_field)
            for parameter in observed.rejected_parameters:
                store.record_rejected_parameter("acme", "api-key", target, parameter)

    profiles = WireProfiles(
        files=files,
        protocol_support=lambda provider_id: protocols,  # type: ignore[arg-type,return-value]
        model_resolver=lambda provider_id, model_id: models.get(model_id),
        report=issues.append,
        observations=store,
    )
    return profiles, issues


def _resolve(profiles: WireProfiles, model_id: str, connection: str = "api-key") -> WireProfile:
    return profiles.resolve("acme", connection, model_id)


def test_invalid_values_are_reported_and_omitted_individually() -> None:
    issues: list[str] = []
    parsed = parse_wire_profile_file(
        "acme",
        {
            "format_version": 1,
            "defaults": {
                "request": {"output_limit_field": "max_completion_tokens", "tool_schema": "loose"},
                "reasoning": {"levels": ["low", "ultra"], "dialect": "reasoning_effort"},
                "bogus": True,
            },
            "rules": [
                {"when": {"prefix": "x-"}, "set": {"reasoning": {"dialect": "none"}}},
                {"when": {"colour": "red"}, "set": {}},
            ],
            "models": {"m": {"set": {"replay": {"scope": "forever"}}, "verified": {"date": "x"}}},
        },
        source="acme.json",
        report=issues.append,
    )

    assert parsed is not None
    assert dict(parsed.defaults["request"]) == {"output_limit_field": "max_completion_tokens"}
    assert dict(parsed.defaults["reasoning"]) == {"dialect": "reasoning_effort"}
    assert [rule.index for rule in parsed.rules] == [0]
    assert dict(parsed.models["m"].values) == {"replay": {}}
    assert parsed.models["m"].verification is None
    assert len(issues) == 6
    assert any("tool_schema" in issue for issue in issues)
    assert any("colour" in issue for issue in issues)


@pytest.mark.parametrize(
    "document",
    [[], {"format_version": 2}, {"defaults": {}}],
    ids=["not-an-object", "future-format", "missing-format"],
)
def test_a_document_without_the_supported_format_is_rejected(document: Any) -> None:
    issues: list[str] = []
    assert (
        parse_wire_profile_file("acme", document, source="acme.json", report=issues.append) is None
    )
    assert issues


def test_layers_apply_in_order_and_record_their_provenance() -> None:
    model = _model("m", control="levels", levels=("low", "high"), metadata={"acme": {}})
    profiles, issues = _profiles(
        {
            "format_version": 1,
            "defaults": {
                "request": {"output_limit_cap": 1000, "body_defaults": {"a": 1, "b": 1}},
                "reasoning": {"control": "budget"},
                "replay": {"echo_response_field": True},
            },
            "protocols": {"chat_completions": {"request": {"output_limit_cap": 2000}}},
            "connections": {"api-key": {"request": {"body_defaults": {"b": 2}}}},
            "rules": [
                {"when": {"prefix": "m"}, "set": {"replay": {"scope": "current_run"}}},
                {"when": {"ids": ["other"]}, "set": {"replay": {"scope": "none"}}},
            ],
            "models": {
                "m": {
                    "set": {"response": {"reasoning_fields": ["thinking"]}},
                    "connections": {"api-key": {"request": {"output_limit_cap": 3000}}},
                }
            },
        },
        {"m": model},
        observed=ObservedFacts(reasoning_field="reasoning", rejected_parameters=("top_k",)),
    )

    profile = _resolve(profiles, "m::pinned")

    assert issues == []
    assert profile.model_id == "m"
    assert profile.request.output_limit_field == "max_tokens"
    assert profile.source_of("request.output_limit_field") == "protocol"
    assert profile.request.output_limit_cap == 3000
    assert profile.source_of("request.output_limit_cap") == "model_connection"
    assert dict(profile.request.body_defaults) == {"a": 1, "b": 2}
    assert profile.source_of("request.body_defaults.b") == "connections"
    # The catalog control beats file defaults; rules beat the catalog.
    assert profile.reasoning.control == "levels"
    assert profile.source_of("reasoning.control") == "catalog"
    assert profile.reasoning.ladder == ("low", "high")
    assert profile.replay.scope == "current_run"
    assert profile.source_of("replay.scope") == "rule[0]"
    # Observations beat rules and catalog, never Model entries.
    assert profile.replay.history_field == "reasoning"
    assert profile.source_of("replay.history_field") == "observed"
    assert profile.request.parameters["top_k"].mode == "drop"
    assert profile.response.reasoning_fields == ("thinking",)
    assert profile.source_of("response.reasoning_fields") == "model"
    assert profile.status == "configured"


def test_catalog_hints_prefer_the_reported_reasoning_carrier() -> None:
    model = _model("m", metadata={"acme": {"interleaved_field": "reasoning_content"}})
    profiles, _ = _profiles(None, {"m": model})

    profile = _resolve(profiles, "m")

    assert profile.response.reasoning_fields == (
        "reasoning_content",
        "reasoning",
        "reasoning_text",
        "thinking",
    )
    assert profile.replay.history_field == "reasoning_content"
    assert profile.source_of("replay.history_field") == "catalog"


def test_protocol_follows_precedence_and_skips_protocols_the_adapter_cannot_speak() -> None:
    document = {
        "format_version": 1,
        "defaults": {"protocol": "responses"},
        "connections": {"subscription": {"protocol": "messages"}},
        "rules": [
            {"when": {"prefix": "claude-"}, "set": {"protocol": "messages"}},
            {"when": {"ids": ["claude-x"]}, "set": {"protocol": "gemini"}},
        ],
        "models": {"gpt-y": {"connections": {"subscription": {"protocol": "chat_completions"}}}},
    }
    profiles, issues = _profiles(document)

    assert _resolve(profiles, "unknown").protocol == "responses"
    assert _resolve(profiles, "unknown", "subscription").protocol == "messages"
    assert _resolve(profiles, "gpt-y", "subscription").protocol == "chat_completions"
    claude = _resolve(profiles, "claude-x")
    assert (claude.protocol, claude.source_of("protocol")) == ("messages", "rule[0]")
    assert any("'gemini'" in issue for issue in issues)

    bare, _ = _profiles(None, protocols=("ollama_chat",))
    assert _resolve(bare, "anything").protocol == "ollama_chat"


@pytest.mark.parametrize(
    ("when", "model_id", "connection", "matches"),
    [
        ({"ids": ["a", "b"]}, "b", "api-key", True),
        ({"prefix": ["x-", "y-"]}, "y-1", "api-key", True),
        ({"suffix": ":free"}, "q:free", "api-key", True),
        ({"family": "fam"}, "known", "api-key", True),
        ({"npm": "@ai-sdk/anthropic"}, "known", "api-key", True),
        ({"metadata": {"endpoints": {"contains": "/messages"}}}, "known", "api-key", True),
        ({"metadata": {"vendor": "Acme"}}, "known", "api-key", True),
        ({"metadata": {"vendor": "Other"}}, "known", "api-key", False),
        ({"control": "none"}, "known", "api-key", True),
        ({"unknown": True}, "missing", "api-key", True),
        ({"unknown": True}, "known", "api-key", False),
        ({"connections": "subscription"}, "known", "api-key", False),
        ({"prefix": "kn", "connections": "subscription"}, "known", "subscription", True),
    ],
)
def test_rule_conditions_must_all_hold(
    when: dict[str, Any], model_id: str, connection: str, matches: bool
) -> None:
    known = _model(
        "known",
        family="fam",
        metadata={
            "acme": {"npm": "@ai-sdk/anthropic", "endpoints": ["/messages"], "vendor": "Acme"}
        },
    )
    profiles, issues = _profiles(
        {
            "format_version": 1,
            "rules": [{"when": when, "set": {"admission": {"state": "restricted"}}}],
        },
        {"known": known},
    )

    profile = _resolve(profiles, model_id, connection)

    assert issues == []
    assert (profile.admission.state == "restricted") is matches


def test_status_reflects_verification_entries_and_explicit_rules() -> None:
    document = {
        "format_version": 1,
        "rules": [
            {"when": {"prefix": "p-"}, "set": {}},
            {"when": {"ids": ["listed"]}, "set": {}},
        ],
        "models": {
            "checked": {"verified": {"date": "2026-10-02", "connections": ["api-key"]}},
            "entry": {"set": {}},
        },
    }
    profiles, _ = _profiles(document)

    assert _resolve(profiles, "checked").status == "verified"
    assert _resolve(profiles, "checked").verification is not None
    assert _resolve(profiles, "checked", "subscription").status == "configured"
    assert _resolve(profiles, "entry").status == "configured"
    assert _resolve(profiles, "listed").status == "configured"
    assert _resolve(profiles, "p-1").status == "inferred"
    assert _resolve(profiles, "p-1").known_model is False
    # The listing accessor answers the same without resolving the profile.
    for model_id, connection in (
        ("checked", "api-key"),
        ("checked", "subscription"),
        ("entry", "api-key"),
        ("listed", "api-key"),
        ("p-1", "api-key"),
    ):
        profile = _resolve(profiles, model_id, connection)
        assert profiles.status("acme", connection, model_id) == (
            profile.status,
            profile.verification,
        )


def test_profiles_are_cached_until_the_model_or_the_files_change() -> None:
    models = {"m": _model("m", supported=True)}
    profiles, _ = _profiles(None, models)

    first = _resolve(profiles, "m")
    assert _resolve(profiles, "m") is first

    models["m"] = _model("m", supported=False)
    second = _resolve(profiles, "m")
    assert second.reasoning.supported is False

    parsed = parse_wire_profile_file(
        "acme",
        {"format_version": 1, "defaults": {"request": {"output_limit_cap": 5}}},
        source="acme.json",
        report=lambda message: None,
    )
    assert parsed is not None
    profiles.replace_files({"acme": parsed})
    assert _resolve(profiles, "m").request.output_limit_cap == 5


def test_bundled_wire_profile_files_are_valid(tmp_path: Path) -> None:
    issues: list[str] = []
    files = load_wire_profile_files(RESOURCES, report=issues.append)
    assert issues == []

    (tmp_path / "wire").mkdir()
    (tmp_path / "wire" / "acme.json").write_text(
        json.dumps({"format_version": 1, "defaults": {"protocol": "messages"}}), encoding="utf-8"
    )
    (tmp_path / "wire" / "broken.json").write_text("{", encoding="utf-8")
    loaded = load_wire_profile_files(tmp_path, report=issues.append)
    assert set(loaded) == {"acme"}
    assert len(issues) == 1
    assert all(name == files[name].provider_id for name in files)


_LADDER = ("low", "medium", "high")


@pytest.mark.parametrize(
    ("wire", "effort", "expected"),
    [
        (ReasoningWire(dialect="reasoning_effort", supported=False), "high", ("default", None)),
        (ReasoningWire(dialect="none"), "high", ("default", None)),
        (ReasoningWire(dialect="reasoning_effort", floor=_LADDER), None, ("default", None)),
        (
            ReasoningWire(dialect="reasoning_effort", floor=_LADDER, unset="enabled"),
            None,
            ("on", None),
        ),
        (
            ReasoningWire(dialect="reasoning_effort", floor=_LADDER, unset="medium"),
            "",
            ("effort", "medium"),
        ),
        (ReasoningWire(dialect="reasoning_effort", floor=_LADDER), "minimal", ("effort", "low")),
        (ReasoningWire(dialect="reasoning_effort", floor=_LADDER), "max", ("effort", "high")),
        (
            ReasoningWire(dialect="reasoning_effort", catalog_levels=("low", "high", "max")),
            "xhigh",
            ("effort", "high"),
        ),
        (
            ReasoningWire(
                dialect="reasoning_effort", catalog_levels=("low", "high", "max"), snap="up"
            ),
            "xhigh",
            ("effort", "max"),
        ),
        (
            ReasoningWire(
                dialect="reasoning_effort", catalog_levels=("low", "high"), levels=("medium",)
            ),
            "high",
            ("effort", "medium"),
        ),
        (
            ReasoningWire(dialect="reasoning_effort", floor=_LADDER, effort_map={"max": "max"}),
            "max",
            ("effort", "max"),
        ),
        (
            ReasoningWire(
                dialect="reasoning_effort",
                catalog_levels=("low", "medium", "high"),
                effort_map={"xhigh": "max"},
            ),
            "xhigh",
            ("effort", "high"),
        ),
        (ReasoningWire(dialect="reasoning_effort", floor=_LADDER), "none", ("off", None)),
        (
            ReasoningWire(dialect="reasoning_effort", supported=True, floor=("none", *_LADDER)),
            "none",
            ("off", "none"),
        ),
        (
            ReasoningWire(dialect="reasoning_effort", floor=("none", *_LADDER)),
            "none",
            ("off", None),
        ),
        (
            ReasoningWire(dialect="thinking_toggle", control="on_off", floor=("none", *_LADDER)),
            "none",
            ("off", None),
        ),
        (
            ReasoningWire(dialect="reasoning_effort", floor=_LADDER, mandatory=True),
            "none",
            ("effort", "low"),
        ),
        (
            ReasoningWire(dialect="reasoning_effort", control="on_off", off="none"),
            "none",
            ("off", "none"),
        ),
        (
            ReasoningWire(dialect="reasoning_effort", floor=_LADDER, off="omit"),
            "none",
            ("default", None),
        ),
        (
            ReasoningWire(dialect="reasoning_effort", floor=_LADDER, off="enabled"),
            "none",
            ("on", None),
        ),
        (
            ReasoningWire(dialect="reasoning_effort", floor=_LADDER, off="medium"),
            "none",
            ("effort", "medium"),
        ),
        (
            ReasoningWire(dialect="thinking_toggle", control="on_off", floor=_LADDER),
            "max",
            ("on", "high"),
        ),
    ],
)
def test_reasoning_plan_turns_an_effort_into_one_wire_decision(
    wire: ReasoningWire, effort: str | None, expected: tuple[str, str | None]
) -> None:
    plan = wire.plan(effort)
    assert (plan.kind, plan.effort_level) == expected


@pytest.mark.parametrize(
    ("wire", "allowance", "expected"),
    [
        (ReasoningWire(budget_max=32_000), None, ReasoningIntent("budget", "high", 24_000)),
        (ReasoningWire(), None, ReasoningIntent("budget", "high", 16_384)),
        (ReasoningWire(budget_max=32_000), 10_000, ReasoningIntent("budget", "high", 9_999)),
        (ReasoningWire(budget_max=32_000), 1_024, ReasoningIntent("on", "high")),
        (
            ReasoningWire(budget_max=32_000, budget=BudgetRule(strategy="absolute")),
            None,
            ReasoningIntent("budget", "high", 16_384),
        ),
        (
            ReasoningWire(budget=BudgetRule(minimum=2_048, maximum=8_000)),
            None,
            ReasoningIntent("budget", "high", 8_000),
        ),
    ],
)
def test_budget_controls_plan_a_token_budget_within_the_output_allowance(
    wire: ReasoningWire, allowance: int | None, expected: ReasoningIntent
) -> None:
    budget_wire = ReasoningWire(
        dialect="anthropic_thinking",
        control="budget",
        floor=("low", "medium", "high"),
        budget_max=wire.budget_max,
        budget=wire.budget,
    )
    assert budget_wire.plan("high", output_allowance=allowance) == expected


def test_learned_facts_shape_unconfigured_profiles_and_survive_a_restart(tmp_path: Path) -> None:
    path = tmp_path / "artifacts" / "wire-observations.json"
    store = WireObservations.load(path, save_delay=None)
    models = {"m": _model("m", control="levels", levels=("none", "low", "high", "xhigh"))}
    profiles, _ = _profiles(None, models)
    profiles.set_observations(store)
    before = _resolve(profiles, "m")
    assert _resolve(profiles, "unknown").reasoning.supported is None

    store.record_reasoning_returned("acme", "api-key", "unknown")
    store.record_reasoning_field("acme", "api-key", "m", "reasoning_content")
    store.record_rejected_effort("acme", "api-key", "m::pinned", "xhigh")
    store.record_rejected_parameter("acme", "api-key", "m", "temperature")
    store.record_rejected_parameter("acme", "other", "m", "top_p")
    store.flush()
    after = _resolve(profiles, "m")

    assert before.response.reasoning_fields[0] == "reasoning"
    assert after.response.reasoning_fields == (
        "reasoning_content",
        "reasoning",
        "reasoning_text",
        "thinking",
    )
    assert after.replay.history_field == "reasoning_content"
    assert after.reasoning.ladder == ("none", "low", "high")
    assert after.reasoning.plan("max").effort_level == "high"
    assert set(after.request.parameters) == {"temperature"}
    assert _resolve(profiles, "unknown").reasoning.supported is True

    reloaded = WireObservations.load(path, save_delay=None)
    assert reloaded.facts_for("acme", "api-key", "m") == store.facts_for("acme", "api-key", "m")
    reloaded.forget("acme", "api-key")
    assert reloaded.facts_for("acme", "api-key", "m").is_empty()
    assert reloaded.facts_for("acme", "other", "m").rejected_parameters == ("top_p",)


@pytest.mark.parametrize(
    ("document", "metadata"),
    [
        (
            {
                "format_version": 1,
                "models": {"m": {"set": {"replay": {"history_field": "reasoning_content"}}}},
            },
            None,
        ),
        (None, {"acme": {"interleaved_field": "reasoning_content"}}),
    ],
    ids=["model-entry", "catalog-hint"],
)
def test_learned_facts_never_override_a_known_history_carrier(
    document: Mapping[str, Any] | None, metadata: Mapping[str, Any] | None
) -> None:
    profiles, _ = _profiles(
        document,
        {"m": _model("m", metadata=metadata)},
        observed=ObservedFacts(reasoning_field="reasoning"),
    )

    profile = _resolve(profiles, "m")

    assert profile.replay.history_field == "reasoning_content"
    assert profile.response.reasoning_fields[0] == "reasoning"


@pytest.mark.parametrize("content", ["{", '{"format_version": 99, "targets": {}}', "[]"])
def test_an_unreadable_observation_cache_starts_empty(tmp_path: Path, content: str) -> None:
    path = tmp_path / "wire-observations.json"
    path.write_text(content, encoding="utf-8")

    store = WireObservations.load(path, save_delay=None)

    assert store.snapshot() == {}


REFUSED = object()


@pytest.mark.parametrize(
    ("rule", "reasoning_active", "value", "expected"),
    [
        (ParameterRule(mode="drop"), False, 0.5, None),
        (ParameterRule(mode="drop_while_thinking"), True, 0.5, None),
        (ParameterRule(mode="drop_while_thinking"), False, 0.5, 0.5),
        (ParameterRule(minimum=0.0, maximum=1.0), False, 1.5, 1.0),
        (ParameterRule(minimum=0.0, maximum=1.0, out_of_range="drop"), False, 1.5, None),
        (ParameterRule(minimum=0.0, exclusive_minimum=True), False, 0.0, None),
        (ParameterRule(minimum=0.0, exclusive_minimum=True), False, 0.1, 0.1),
        (ParameterRule(mode="reject"), False, 0.5, REFUSED),
        (ParameterRule(minimum=0.0, maximum=1.0, out_of_range="reject"), False, 1.5, REFUSED),
        (ParameterRule(values=(0.5,), out_of_range="reject"), False, 0.5, 0.5),
        (ParameterRule(values=(0.5,), out_of_range="reject"), False, 0.7, REFUSED),
    ],
    ids=[
        "drop",
        "drop-while-thinking",
        "kept-without-thinking",
        "clamped",
        "out-of-range-dropped",
        "exclusive-bound-dropped",
        "inside-exclusive-bound",
        "rejected-parameter-refused",
        "out-of-range-refused",
        "listed-value-kept",
        "unlisted-value-refused",
    ],
)
def test_parameter_rules_shape_the_request_fields(
    rule: ParameterRule, reasoning_active: bool, value: float, expected: object
) -> None:
    payload: dict[str, Any] = {"temperature": value, "model": "m"}
    rules = RequestRules(parameters={"temperature": rule})

    def shape() -> None:
        rules.shape_parameters(payload, reasoning_active=reasoning_active, provider_label="Acme")

    if expected is REFUSED:
        with pytest.raises(ProviderError, match="Acme .*temperature") as refused:
            shape()
        assert refused.value.retryable is False
        return
    shape()

    assert payload.get("temperature") == expected
    assert payload["model"] == "m"
