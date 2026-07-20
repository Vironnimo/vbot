"""Sub-Agent prompt-target catalog tests."""

from __future__ import annotations

from types import SimpleNamespace

from core.subagents.catalog import build_subagent_prompt_targets


class _Resolver:
    def __init__(self, teams: dict[str, list[SimpleNamespace]]) -> None:
        self._teams = teams

    def team_for_project(self, project_id: str) -> list[SimpleNamespace]:
        return list(self._teams[project_id])


def _member(agent_id: str, name: str, description: str = "") -> SimpleNamespace:
    return SimpleNamespace(
        agent_id=agent_id,
        display_name=name,
        description=description,
    )


def _runtime(
    *,
    identities: list[SimpleNamespace] | None = None,
    teams: dict[str, list[SimpleNamespace]] | None = None,
) -> SimpleNamespace:
    active_teams = teams or {}
    return SimpleNamespace(
        agents=SimpleNamespace(list=lambda: list(identities or [])),
        projects=SimpleNamespace(
            list=lambda: [SimpleNamespace(project_id=project_id) for project_id in active_teams]
        ),
        agent_resolver=_Resolver(active_teams),
    )


def test_project_catalog_lists_only_additional_allowed_team_members() -> None:
    runtime = _runtime(
        teams={
            "vbot": [
                _member("orchestrator", "Orchestrator"),
                _member("builder", "Builder", "Implements focused changes."),
                _member("reviewer", "Reviewer", "Reviews completed work."),
            ]
        }
    )
    agent = SimpleNamespace(
        id="orchestrator",
        tools={"subagent": {"allowed_agents": ["reviewer"]}},
    )

    targets = build_subagent_prompt_targets(runtime, agent, "vbot")

    assert [(target.agent_id, target.name, target.description) for target in targets] == [
        ("reviewer", "Reviewer", "Reviews completed work.")
    ]


def test_identity_wildcard_catalog_qualifies_project_agents_and_excludes_self() -> None:
    runtime = _runtime(
        identities=[
            SimpleNamespace(id="main", name="Main"),
            SimpleNamespace(id="researcher", name="Researcher"),
        ],
        teams={"vbot": [_member("builder", "Builder", "Builds features.")]},
    )
    agent = SimpleNamespace(id="main", tools={})

    targets = build_subagent_prompt_targets(runtime, agent, None)

    assert [(target.agent_id, target.name) for target in targets] == [
        ("builder@vbot", "Builder"),
        ("researcher", "Researcher"),
    ]


def test_identity_explicit_catalog_omits_unknown_and_calling_agent_entries() -> None:
    runtime = _runtime(
        identities=[
            SimpleNamespace(id="main", name="Main"),
            SimpleNamespace(id="researcher", name="Researcher"),
        ],
        teams={"vbot": [_member("builder", "Builder")]},
    )
    agent = SimpleNamespace(
        id="main",
        tools={"subagent": {"allowed_agents": ["main", "missing", "researcher", "builder@vbot"]}},
    )

    targets = build_subagent_prompt_targets(runtime, agent, None)

    assert [target.agent_id for target in targets] == ["builder@vbot", "researcher"]
