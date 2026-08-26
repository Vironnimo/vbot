"""Effective configuration value and provenance tests."""

from .resolver_test_support import (
    AgentResolutionError,
    AgentStore,
    Path,
    ProjectStore,
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


def test_effective_config_model_override_wins(
    agents: AgentStore, projects: ProjectStore, repo: Path
) -> None:
    _write_agent(repo, "builder.md", model="openai/gpt-5.2")
    _project(projects, repo)
    projects.set_override("vbot", "builder", "model", "openai/gpt-mini")
    resolver = _resolver(agents, projects, _openai_configured())

    result = resolver.effective_config("vbot", "builder")

    assert result["model"] == {"value": "openai/gpt-mini", "source": "override"}


def test_effective_config_model_agent_wins(
    agents: AgentStore, projects: ProjectStore, repo: Path
) -> None:
    _write_agent(repo, "builder.md", model="openai/gpt-5.2")
    _project(projects, repo)
    resolver = _resolver(agents, projects, _openai_configured())

    result = resolver.effective_config("vbot", "builder")

    assert result["model"] == {"value": "openai/gpt-5.2", "source": "agent"}


def test_effective_config_model_project_default_wins(
    agents: AgentStore, projects: ProjectStore, repo: Path
) -> None:
    _write_agent(repo, "writer.md", model="")
    _project(projects, repo, default_model="openai/gpt-mini")
    resolver = _resolver(agents, projects, _openai_configured())

    result = resolver.effective_config("vbot", "writer")

    assert result["model"] == {"value": "openai/gpt-mini", "source": "project_default"}


def test_effective_config_model_global_default_wins(
    agents: AgentStore, projects: ProjectStore, repo: Path
) -> None:
    _write_agent(repo, "writer.md", model="")
    _project(projects, repo, default_model="")
    resolver = _resolver(agents, projects, _openai_configured(), global_default="openai/gpt-5.2")

    result = resolver.effective_config("vbot", "writer")

    assert result["model"] == {"value": "openai/gpt-5.2", "source": "global_default"}


def test_effective_config_model_fell_through_returns_none_without_raising(
    agents: AgentStore, projects: ProjectStore, repo: Path
) -> None:
    # A fully-fallen-through model chain does NOT raise here (unlike resolve_agent);
    # it reports {"value": None, "source": None}.
    _write_agent(repo, "writer.md", model="")
    _project(projects, repo, default_model="")
    resolver = _resolver(agents, projects, _openai_configured(), global_default="")

    result = resolver.effective_config("vbot", "writer")

    assert result["model"] == {"value": None, "source": None}


def test_effective_config_unconfigured_model_override_skipped_falls_to_agent(
    agents: AgentStore, projects: ProjectStore, repo: Path
) -> None:
    # An unconfigured overridden model is skipped by the is_configured gate for BOTH
    # resolve_agent and effective_config — the chain falls to the repo-declared model.
    _write_agent(repo, "builder.md", model="openai/gpt-5.2")
    _project(projects, repo)
    projects.set_override("vbot", "builder", "model", "openai/ghost-model")
    resolver = _resolver(agents, projects, _openai_configured())

    assert resolver.resolve_agent("vbot", "builder").model == "openai/gpt-5.2"
    assert resolver.effective_config("vbot", "builder")["model"] == {
        "value": "openai/gpt-5.2",
        "source": "agent",
    }


def test_effective_config_temperature_override_zero_wins(
    agents: AgentStore, projects: ProjectStore, repo: Path
) -> None:
    # A temperature override of 0.0 (a real value) is the top tier and wins.
    _write_agent(repo, "builder.md", model="openai/gpt-5.2", temperature=0.7)
    _project(projects, repo, default_temperature=0.2)
    projects.set_override("vbot", "builder", "temperature", 0.0)
    resolver = _resolver(agents, projects, _openai_configured(), global_temperature=0.9)

    result = resolver.effective_config("vbot", "builder")

    assert result["temperature"] == {"value": 0.0, "source": "override"}


def test_effective_config_temperature_agent_wins(
    agents: AgentStore, projects: ProjectStore, repo: Path
) -> None:
    _write_agent(repo, "builder.md", model="openai/gpt-5.2", temperature=0.7)
    _project(projects, repo, default_temperature=0.2)
    resolver = _resolver(agents, projects, _openai_configured(), global_temperature=0.9)

    result = resolver.effective_config("vbot", "builder")

    assert result["temperature"] == {"value": 0.7, "source": "agent"}


def test_effective_config_temperature_project_default_wins(
    agents: AgentStore, projects: ProjectStore, repo: Path
) -> None:
    _write_agent(repo, "builder.md", model="openai/gpt-5.2", temperature=None)
    _project(projects, repo, default_temperature=0.2)
    resolver = _resolver(agents, projects, _openai_configured(), global_temperature=0.9)

    result = resolver.effective_config("vbot", "builder")

    assert result["temperature"] == {"value": 0.2, "source": "project_default"}


def test_effective_config_temperature_global_default_wins(
    agents: AgentStore, projects: ProjectStore, repo: Path
) -> None:
    _write_agent(repo, "builder.md", model="openai/gpt-5.2", temperature=None)
    _project(projects, repo)
    resolver = _resolver(agents, projects, _openai_configured(), global_temperature=0.9)

    result = resolver.effective_config("vbot", "builder")

    assert result["temperature"] == {"value": 0.9, "source": "global_default"}


def test_effective_config_thinking_override_empty_string_wins(
    agents: AgentStore, projects: ProjectStore, repo: Path
) -> None:
    # A thinking_effort override of "" (force provider default, a real value) wins.
    _write_agent(repo, "builder.md", model="openai/gpt-5.2", reasoning_effort="high")
    _project(projects, repo, default_thinking_effort="low")
    projects.set_override("vbot", "builder", "thinking_effort", "")
    resolver = _resolver(agents, projects, _openai_configured(), global_thinking_effort="medium")

    result = resolver.effective_config("vbot", "builder")

    assert result["thinking_effort"] == {"value": "", "source": "override"}


def test_effective_config_thinking_agent_wins(
    agents: AgentStore, projects: ProjectStore, repo: Path
) -> None:
    _write_agent(repo, "builder.md", model="openai/gpt-5.2", reasoning_effort="high")
    _project(projects, repo, default_thinking_effort="low")
    resolver = _resolver(agents, projects, _openai_configured(), global_thinking_effort="medium")

    result = resolver.effective_config("vbot", "builder")

    assert result["thinking_effort"] == {"value": "high", "source": "agent"}


def test_effective_config_thinking_project_default_wins(
    agents: AgentStore, projects: ProjectStore, repo: Path
) -> None:
    _write_agent(repo, "builder.md", model="openai/gpt-5.2")
    _project(projects, repo, default_thinking_effort="low")
    resolver = _resolver(agents, projects, _openai_configured(), global_thinking_effort="medium")

    result = resolver.effective_config("vbot", "builder")

    assert result["thinking_effort"] == {"value": "low", "source": "project_default"}


def test_effective_config_thinking_global_default_wins(
    agents: AgentStore, projects: ProjectStore, repo: Path
) -> None:
    _write_agent(repo, "builder.md", model="openai/gpt-5.2")
    _project(projects, repo)
    resolver = _resolver(agents, projects, _openai_configured(), global_thinking_effort="medium")

    result = resolver.effective_config("vbot", "builder")

    assert result["thinking_effort"] == {"value": "medium", "source": "global_default"}


def test_effective_config_unknown_project_raises(
    agents: AgentStore, projects: ProjectStore, repo: Path
) -> None:
    resolver = _resolver(agents, projects, _openai_configured())

    with pytest.raises(AgentResolutionError):
        resolver.effective_config("missing", "builder")


def test_effective_config_unknown_agent_raises(
    agents: AgentStore, projects: ProjectStore, repo: Path
) -> None:
    _write_agent(repo, "builder.md", model="openai/gpt-5.2")
    _project(projects, repo)
    resolver = _resolver(agents, projects, _openai_configured())

    with pytest.raises(AgentResolutionError):
        resolver.effective_config("vbot", "ghost")


def test_effective_config_for_member_matches_effective_config(
    agents: AgentStore, projects: ProjectStore, repo: Path
) -> None:
    # The scanned-member seam runs the same per-tier chain as effective_config,
    # so a team listing never re-scans yet reports the identical result.
    _write_agent(repo, "builder.md", model="openai/gpt-5.2", temperature=0.7)
    project = _project(projects, repo)
    projects.set_override("vbot", "builder", "model", "openai/gpt-mini")
    resolver = _resolver(agents, projects, _openai_configured())
    result = resolver.scan_project_report(project)
    member = next(m for m in result.team if m.agent_id == "builder")

    from_member = resolver.effective_config_for_member(projects.get("vbot"), member)

    assert from_member == resolver.effective_config("vbot", "builder")
    assert from_member["model"] == {"value": "openai/gpt-mini", "source": "override"}


def test_identity_effective_config_reports_own_value_as_agent(
    agents: AgentStore, projects: ProjectStore, repo: Path
) -> None:
    agents.create(
        "orchestrator",
        "Orchestrator",
        model="openai/gpt-5.2",
        fallback_models=["openai/gpt-mini"],
        temperature=0.3,
        thinking_effort="high",
    )
    resolver = _resolver(agents, projects, _openai_configured(), global_default="openai/ghost")

    result = resolver.effective_config(None, "orchestrator")

    assert result["model"] == {"value": "openai/gpt-5.2", "source": "agent"}
    assert result["fallback_models"] == {"value": ["openai/gpt-mini"], "source": "agent"}
    assert result["temperature"] == {"value": 0.3, "source": "agent"}
    assert result["thinking_effort"] == {"value": "high", "source": "agent"}


def test_identity_effective_config_reports_global_default_when_own_empty(
    agents: AgentStore, projects: ProjectStore, repo: Path
) -> None:
    # No own model/temperature/thinking → the global default is the source, exactly
    # as AgentStore.get would bake it. get_raw is what lets the resolver see the "".
    agents.create("orchestrator", "Orchestrator")
    resolver = _resolver(
        agents,
        projects,
        _openai_configured(),
        global_default="openai/gpt-5.2",
        global_temperature=0.9,
        global_thinking_effort="medium",
    )

    result = resolver.effective_config(None, "orchestrator")

    assert result["model"] == {"value": "openai/gpt-5.2", "source": "global_default"}
    assert result["temperature"] == {"value": 0.9, "source": "global_default"}
    assert result["thinking_effort"] == {"value": "medium", "source": "global_default"}


def test_identity_effective_config_reports_none_when_neither(
    agents: AgentStore, projects: ProjectStore, repo: Path
) -> None:
    agents.create("orchestrator", "Orchestrator")
    resolver = _resolver(agents, projects, _openai_configured())

    result = resolver.effective_config(None, "orchestrator")

    assert result["model"] == {"value": None, "source": None}
    assert result["fallback_models"] == {"value": None, "source": None}
    assert result["temperature"] == {"value": None, "source": None}
    assert result["thinking_effort"] == {"value": None, "source": None}


def test_identity_effective_config_own_zero_temperature_is_agent(
    agents: AgentStore, projects: ProjectStore, repo: Path
) -> None:
    # A present own value including 0.0 temperature and "" thinking effort stops the
    # chain at "agent", never falling to the global default.
    agents.create("orchestrator", "Orchestrator", temperature=0.0, thinking_effort="")
    resolver = _resolver(
        agents,
        projects,
        _openai_configured(),
        global_temperature=0.9,
        global_thinking_effort="medium",
    )

    result = resolver.effective_config(None, "orchestrator")

    assert result["temperature"] == {"value": 0.0, "source": "agent"}
    assert result["thinking_effort"] == {"value": "", "source": "agent"}


def test_identity_effective_config_unknown_agent_raises(
    agents: AgentStore, projects: ProjectStore, repo: Path
) -> None:
    resolver = _resolver(agents, projects, _openai_configured())

    with pytest.raises(AgentResolutionError):
        resolver.effective_config(None, "missing-agent")


def test_get_bakes_global_default_but_get_raw_does_not(
    agents: AgentStore, projects: ProjectStore, repo: Path, data_dir: Path, template_dir: Path
) -> None:
    # The get/get_raw seam the identity effective_config relies on: with a global
    # default set, get() bakes it in while get_raw() returns the unbaked "" / None.
    baking_store = AgentStore(
        data_dir,
        template_dir=template_dir,
        defaults_provider=lambda: {"model": "openai/gpt-5.2", "temperature": 0.9},
    )
    baking_store.create("orchestrator", "Orchestrator")

    baked = baking_store.get("orchestrator")
    raw = baking_store.get_raw("orchestrator")

    assert baked.model == "openai/gpt-5.2"
    assert baked.temperature == 0.9
    assert raw.model == ""
    assert raw.temperature is None
