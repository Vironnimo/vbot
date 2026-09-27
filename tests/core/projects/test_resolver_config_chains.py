"""Model, temperature, and thinking resolution chains and their effective provenance."""

from typing import Any

from .resolver_test_support import (
    AgentResolutionError,
    AgentStore,
    Path,
    ProjectStore,
    ResolutionAgentNotFoundError,
    ResolutionProjectNotFoundError,
    _openai_configured,
    _project,
    _resolver,
    _write_agent,
    pytest,
)
from .resolver_test_support import agents as agents
from .resolver_test_support import data_dir as data_dir
from .resolver_test_support import projects as projects
from .resolver_test_support import repo as repo
from .resolver_test_support import template_dir as template_dir

_GPT = "openai/gpt-5.2"
_MINI = "openai/gpt-mini"
_GHOST = "openai/ghost-model"


# Each row: field, the Agent file, Project defaults, vBot overrides, global defaults,
# and the expected value with its provenance.
@pytest.mark.parametrize(
    ("field", "agent", "project", "overrides", "global_defaults", "value", "source"),
    [
        pytest.param(
            "model",
            {"model": _GPT},
            {},
            {"model": _MINI},
            {},
            _MINI,
            "override",
            id="model-override",
        ),
        pytest.param("model", {"model": _GPT}, {}, {}, {}, _GPT, "agent", id="model-agent"),
        pytest.param(
            "model",
            {"model": ""},
            {"default_model": _MINI},
            {},
            {},
            _MINI,
            "project_default",
            id="model-project-default",
        ),
        pytest.param(
            "model",
            {"model": ""},
            {},
            {},
            {"global_default": _GPT},
            _GPT,
            "global_default",
            id="model-global-default",
        ),
        # An unconfigured Model is skipped by the usable-Model gate at every tier.
        pytest.param(
            "model",
            {"model": _GHOST},
            {"default_model": _GPT},
            {},
            {},
            _GPT,
            "project_default",
            id="unconfigured-agent-model",
        ),
        pytest.param(
            "model",
            {"model": _GPT},
            {},
            {"model": _GHOST},
            {},
            _GPT,
            "agent",
            id="unconfigured-override",
        ),
        # 0.0 is the sampling floor, a real value at every tier.
        pytest.param(
            "temperature",
            {"model": _GPT, "temperature": 0.7},
            {"default_temperature": 0.2},
            {"temperature": 0.0},
            {"global_temperature": 0.9},
            0.0,
            "override",
            id="temperature-override-zero",
        ),
        pytest.param(
            "temperature",
            {"model": _GPT, "temperature": 0.7},
            {"default_temperature": 0.2},
            {},
            {"global_temperature": 0.9},
            0.7,
            "agent",
            id="temperature-agent",
        ),
        pytest.param(
            "temperature",
            {"model": _GPT, "temperature": None},
            {"default_temperature": 0.2},
            {},
            {"global_temperature": 0.9},
            0.2,
            "project_default",
            id="temperature-project-default",
        ),
        pytest.param(
            "temperature",
            {"model": _GPT, "temperature": None},
            {"default_temperature": 0.0},
            {},
            {"global_temperature": 0.9},
            0.0,
            "project_default",
            id="temperature-project-zero-stops-chain",
        ),
        pytest.param(
            "temperature",
            {"model": _GPT, "temperature": None},
            {},
            {},
            {"global_temperature": 0.9},
            0.9,
            "global_default",
            id="temperature-global-default",
        ),
        pytest.param(
            "temperature",
            {"model": _GPT, "temperature": None},
            {},
            {},
            {},
            None,
            None,
            id="temperature-unset",
        ),
        # "" means Provider default, a real value that stops the chain.
        pytest.param(
            "thinking_effort",
            {"model": _GPT, "reasoning_effort": "high"},
            {"default_thinking_effort": "low"},
            {"thinking_effort": ""},
            {"global_thinking_effort": "medium"},
            "",
            "override",
            id="thinking-override-empty",
        ),
        pytest.param(
            "thinking_effort",
            {"model": _GPT, "reasoning_effort": "high"},
            {"default_thinking_effort": "low"},
            {},
            {"global_thinking_effort": "medium"},
            "high",
            "agent",
            id="thinking-agent",
        ),
        pytest.param(
            "thinking_effort",
            {"model": _GPT},
            {"default_thinking_effort": "low"},
            {},
            {"global_thinking_effort": "medium"},
            "low",
            "project_default",
            id="thinking-project-default",
        ),
        pytest.param(
            "thinking_effort",
            {"model": _GPT},
            {"default_thinking_effort": ""},
            {},
            {"global_thinking_effort": "medium"},
            "",
            "project_default",
            id="thinking-project-empty-stops-chain",
        ),
        pytest.param(
            "thinking_effort",
            {"model": _GPT},
            {},
            {},
            {"global_thinking_effort": "medium"},
            "medium",
            "global_default",
            id="thinking-global-default",
        ),
        pytest.param(
            "thinking_effort", {"model": _GPT}, {}, {}, {}, None, None, id="thinking-unset"
        ),
    ],
)
def test_config_chain_resolves_the_first_usable_tier(
    agents: AgentStore,
    projects: ProjectStore,
    repo: Path,
    field: str,
    agent: dict[str, Any],
    project: dict[str, Any],
    overrides: dict[str, Any],
    global_defaults: dict[str, Any],
    value: object,
    source: str | None,
) -> None:
    _write_agent(repo, "builder.md", **agent)
    _project(projects, repo, **project)
    for override_field, override in overrides.items():
        projects.set_override("vbot", "builder", override_field, override)
    resolver = _resolver(agents, projects, _openai_configured(), **global_defaults)

    # The runtime view and the reported provenance use the same chain.
    assert getattr(resolver.resolve_agent("vbot", "builder"), field) == value
    assert resolver.effective_config("vbot", "builder")[field] == {
        "value": value,
        "source": source,
    }


def test_model_chain_without_a_usable_model_reports_none_but_cannot_run(
    agents: AgentStore, projects: ProjectStore, repo: Path
) -> None:
    _write_agent(repo, "writer.md", model="")
    _project(projects, repo)
    resolver = _resolver(agents, projects, _openai_configured())

    assert resolver.effective_config("vbot", "writer")["model"] == {"value": None, "source": None}
    # An existing Agent without a usable Model is not a missing resource.
    with pytest.raises(AgentResolutionError) as error:
        resolver.resolve_agent("vbot", "writer")
    assert not isinstance(
        error.value, (ResolutionAgentNotFoundError, ResolutionProjectNotFoundError)
    )


def test_model_override_applies_only_to_its_agent(
    agents: AgentStore, projects: ProjectStore, repo: Path
) -> None:
    _write_agent(repo, "builder.md", model=_GPT)
    _write_agent(repo, "planner.md", model=_GPT)
    _project(projects, repo)
    projects.set_override("vbot", "builder", "model", _MINI)
    resolver = _resolver(agents, projects, _openai_configured())

    assert resolver.resolve_agent("vbot", "builder").model == _MINI
    assert resolver.resolve_agent("vbot", "planner").model == _GPT


def test_effective_config_for_member_matches_effective_config(
    agents: AgentStore, projects: ProjectStore, repo: Path
) -> None:
    # The scanned-member seam runs the same per-tier chain as effective_config,
    # so a team listing never re-scans yet reports the identical result.
    _write_agent(repo, "builder.md", model=_GPT, temperature=0.7)
    project = _project(projects, repo)
    projects.set_override("vbot", "builder", "model", _MINI)
    resolver = _resolver(agents, projects, _openai_configured())
    result = resolver.scan_project_report(project)
    member = next(m for m in result.team if m.agent_id == "builder")

    from_member = resolver.effective_config_for_member(projects.get("vbot"), member)

    assert from_member == resolver.effective_config("vbot", "builder")
    assert from_member["model"] == {"value": _MINI, "source": "override"}


def _provenance(value: object, source: str | None) -> dict[str, object]:
    return {"value": value, "source": source}


@pytest.mark.parametrize(
    ("own", "global_defaults", "expected"),
    [
        pytest.param(
            {
                "model": _GPT,
                "fallback_models": [_MINI],
                "temperature": 0.3,
                "thinking_effort": "high",
            },
            {"global_default": "openai/ghost"},
            {
                "model": _provenance(_GPT, "agent"),
                "fallback_models": _provenance([_MINI], "agent"),
                "temperature": _provenance(0.3, "agent"),
                "thinking_effort": _provenance("high", "agent"),
            },
            id="own-values",
        ),
        # 0.0 and "" are present own values that stop the chain.
        pytest.param(
            {"temperature": 0.0, "thinking_effort": ""},
            {"global_temperature": 0.9, "global_thinking_effort": "medium"},
            {
                "model": _provenance(None, None),
                "fallback_models": _provenance(None, None),
                "temperature": _provenance(0.0, "agent"),
                "thinking_effort": _provenance("", "agent"),
            },
            id="own-zero-and-empty",
        ),
        pytest.param(
            {},
            {
                "global_default": _GPT,
                "global_temperature": 0.9,
                "global_thinking_effort": "medium",
            },
            {
                "model": _provenance(_GPT, "global_default"),
                "fallback_models": _provenance(None, None),
                "temperature": _provenance(0.9, "global_default"),
                "thinking_effort": _provenance("medium", "global_default"),
            },
            id="global-defaults",
        ),
        pytest.param(
            {},
            {},
            {
                "model": _provenance(None, None),
                "fallback_models": _provenance(None, None),
                "temperature": _provenance(None, None),
                "thinking_effort": _provenance(None, None),
            },
            id="unset",
        ),
    ],
)
def test_identity_effective_config_reports_own_values_before_global_defaults(
    agents: AgentStore,
    projects: ProjectStore,
    own: dict[str, Any],
    global_defaults: dict[str, Any],
    expected: dict[str, object],
) -> None:
    agents.create("orchestrator", "Orchestrator", **own)
    resolver = _resolver(agents, projects, _openai_configured(), **global_defaults)

    result = resolver.effective_config(None, "orchestrator")

    assert {field: result[field] for field in expected} == expected


@pytest.mark.parametrize(
    ("project_id", "agent_id", "error"),
    [
        pytest.param("missing", "builder", ResolutionProjectNotFoundError, id="unknown-project"),
        pytest.param("vbot", "ghost", ResolutionAgentNotFoundError, id="unknown-project-agent"),
        pytest.param(None, "missing-agent", ResolutionAgentNotFoundError, id="unknown-identity"),
    ],
)
def test_unknown_agents_are_not_found_by_every_resolution_seam(
    agents: AgentStore,
    projects: ProjectStore,
    repo: Path,
    project_id: str | None,
    agent_id: str,
    error: type[Exception],
) -> None:
    _write_agent(repo, "builder.md", model=_GPT)
    _project(projects, repo)
    resolver = _resolver(agents, projects, _openai_configured())

    with pytest.raises(error):
        resolver.resolve_agent(project_id, agent_id)
    with pytest.raises(error):
        resolver.effective_config(project_id, agent_id)
