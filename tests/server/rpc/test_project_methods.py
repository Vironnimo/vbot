"""Tests for the ``project.*`` RPC handlers and the remove lock.

Coverage (AAA):
- ``add`` creates a project and returns the scan preview (Team + report),
- ``add`` rejects a non-existent cwd and a cwd already claimed by a project,
- ``add`` rejects a display name that cannot become a project id,
- ``show`` returns config + Team + report (live re-scan),
- ``set`` mutates fields; a cwd change re-scans and the Team changes,
- ``list`` returns the persisted projects,
- ``rm`` archives the anchor (repo untouched),
- ``rm`` is blocked by an active/queued run of a session-owning Project agent,
- ``rm`` is blocked by a cron job pointing at a Project agent,
- ``show`` of an unknown project surfaces a clear error,
- the handlers are registered in the method table.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from core.agents.agents import AgentStore
from core.projects.projects import PROJECT_DEFAULT_ALLOWED_TOOLS
from core.projects.resolver import AgentResolver, ModelConfigurationChecker
from core.projects.scanners.opencode import OPENCODE_AGENTS_SUBPATH
from core.projects.store import ProjectStore
from core.runs import ChatRunManager, Run
from server.rpc.errors import RpcError
from server.rpc.methods import build_method_handlers
from server.rpc.project_methods import (
    _add_project,
    _list_projects,
    _remove_project,
    _set_project,
    _show_project,
)

# ---------------------------------------------------------------------------
# Fakes for the model/provider/credential surface the resolver's checker probes.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _FakeConnection:
    id: str


@dataclass(frozen=True)
class _FakeProviderConfig:
    connections: list[_FakeConnection]


class _FakeModels:
    def __init__(self, known: set[tuple[str, str]]) -> None:
        self._known = known

    def get(self, provider_id: str, model_id: str) -> object:
        if (provider_id, model_id) not in self._known:
            raise KeyError(f"{provider_id}/{model_id}")
        return object()


class _FakeProviders:
    def __init__(self, providers: dict[str, _FakeProviderConfig]) -> None:
        self._providers = providers

    def get(self, provider_id: str) -> _FakeProviderConfig:
        if provider_id not in self._providers:
            raise KeyError(provider_id)
        return self._providers[provider_id]


class _FakeCredentials:
    def __init__(self, usable: set[str]) -> None:
        self._usable = usable

    def has_credentials(self, provider_id: str, connection_id: str | None = None) -> bool:
        target = connection_id if connection_id is not None else provider_id
        return target in self._usable


def _openai_configured() -> ModelConfigurationChecker:
    return ModelConfigurationChecker(
        _FakeModels({("openai", "gpt-5.2"), ("openai", "gpt-mini")}),
        _FakeProviders({"openai": _FakeProviderConfig([_FakeConnection("api-key")])}),
        _FakeCredentials({"openai:api-key"}),
    )


# ---------------------------------------------------------------------------
# Repo + state scaffolding.
# ---------------------------------------------------------------------------


def _write_agent(
    repo: Path,
    filename: str,
    *,
    model: str = "openai/gpt-5.2",
    reasoning_effort: str | None = None,
    permission: dict[str, str] | None = None,
) -> None:
    agents_dir = repo.joinpath(*OPENCODE_AGENTS_SUBPATH)
    agents_dir.mkdir(parents=True, exist_ok=True)
    lines = ["description: An agent."]
    if model:
        lines.append(f"model: {model}")
    if reasoning_effort is not None:
        lines.append(f"reasoningEffort: {reasoning_effort}")
    if permission:
        lines.append("permission:")
        lines.extend(f"  {key}: {value}" for key, value in permission.items())
    front = "\n".join(lines) + "\n"
    (agents_dir / filename).write_text(f"---\n{front}---\nBody.\n", encoding="utf-8")


def _make_repo(tmp_path: Path, name: str, *agents: str) -> Path:
    repo = tmp_path / "repos" / name
    repo.mkdir(parents=True)
    for agent in agents:
        _write_agent(repo, agent)
    return repo


def _make_state(tmp_path: Path, *, cron_jobs: list | None = None) -> SimpleNamespace:
    data_dir = tmp_path / "data"
    projects = ProjectStore(data_dir)
    resolver = AgentResolver(
        # The identity path is unused in these project-scoped tests.
        agents=cast(AgentStore, SimpleNamespace()),
        projects=projects,
        model_checker=_openai_configured(),
        global_agent_defaults=lambda: {},
    )
    chat_runs = ChatRunManager()
    cron_service = SimpleNamespace(list_jobs=lambda: list(cron_jobs or []))
    runtime = SimpleNamespace(
        projects=projects,
        agent_resolver=resolver,
        cron_service=cron_service,
    )
    return SimpleNamespace(runtime=runtime, chat_runs=chat_runs)


# ---------------------------------------------------------------------------
# add: create + scan preview.
# ---------------------------------------------------------------------------


def test_add_creates_project_and_returns_scan_preview(tmp_path: Path) -> None:
    state = _make_state(tmp_path)
    repo = _make_repo(tmp_path, "vbot", "builder.md")

    result = _add_project(state, {"cwd": str(repo), "display_name": "vBot"})

    assert result["project"]["project_id"] == "vbot"
    assert result["project"]["cwd_exists"] is True
    assert [member["agent_id"] for member in result["scan"]["team"]] == ["builder"]
    assert result["scan"]["report"]["clean"] is True
    assert state.runtime.projects.exists("vbot")


def test_add_seeds_agents_file_into_auto_load(tmp_path: Path) -> None:
    # project.add seeds AGENTS.md as the first auto-load entry, so a freshly added
    # project loads the convention file with no extra configuration.
    state = _make_state(tmp_path)
    repo = _make_repo(tmp_path, "vbot", "builder.md")

    result = _add_project(state, {"cwd": str(repo), "display_name": "vBot"})

    assert result["project"]["auto_load"] == ["AGENTS.md"]


def test_add_derives_project_id_from_cwd_when_no_display_name(tmp_path: Path) -> None:
    state = _make_state(tmp_path)
    repo = _make_repo(tmp_path, "my-repo")

    result = _add_project(state, {"cwd": str(repo)})

    assert result["project"]["project_id"] == "my-repo"
    assert result["scan"]["team"] == []
    assert result["scan"]["report"]["clean"] is True


def test_add_report_flags_unconfigured_model(tmp_path: Path) -> None:
    state = _make_state(tmp_path)
    repo = _make_repo(tmp_path, "vbot")
    _write_agent(repo, "weird.md", model="ghost/model-x")

    result = _add_project(state, {"cwd": str(repo), "display_name": "vBot"})

    findings = result["scan"]["report"]["findings"]
    assert result["scan"]["report"]["clean"] is False
    assert any(finding["type"] == "bad_model" for finding in findings)


def test_add_rejects_missing_cwd(tmp_path: Path) -> None:
    state = _make_state(tmp_path)
    missing = tmp_path / "nope"

    with pytest.raises(RpcError) as exc_info:
        _add_project(state, {"cwd": str(missing), "display_name": "vBot"})

    assert exc_info.value.code == "invalid_request"
    assert "not an existing directory" in exc_info.value.message


def test_add_rejects_duplicate_cwd(tmp_path: Path) -> None:
    state = _make_state(tmp_path)
    repo = _make_repo(tmp_path, "vbot")
    _add_project(state, {"cwd": str(repo), "display_name": "vBot"})

    with pytest.raises(RpcError) as exc_info:
        _add_project(state, {"cwd": str(repo), "display_name": "vBot Two"})

    assert exc_info.value.code == "project_already_exists"


def test_add_rejects_unslugifiable_display_name(tmp_path: Path) -> None:
    state = _make_state(tmp_path)
    repo = _make_repo(tmp_path, "vbot")

    with pytest.raises(RpcError) as exc_info:
        _add_project(state, {"cwd": str(repo), "display_name": "!!!"})

    assert exc_info.value.code == "invalid_request"


def test_add_rejects_unknown_field(tmp_path: Path) -> None:
    state = _make_state(tmp_path)
    repo = _make_repo(tmp_path, "vbot")

    with pytest.raises(RpcError, match="unsupported project.add fields: bogus"):
        _add_project(state, {"cwd": str(repo), "bogus": 1})


# ---------------------------------------------------------------------------
# show / list.
# ---------------------------------------------------------------------------


def test_show_returns_config_team_and_report(tmp_path: Path) -> None:
    state = _make_state(tmp_path)
    repo = _make_repo(tmp_path, "vbot", "builder.md", "tester.md")
    _add_project(state, {"cwd": str(repo), "display_name": "vBot"})

    result = _show_project(state, {"project_id": "vbot"})

    assert result["project"]["project_id"] == "vbot"
    assert [member["agent_id"] for member in result["scan"]["team"]] == ["builder", "tester"]


def test_show_rescans_repo_changes(tmp_path: Path) -> None:
    state = _make_state(tmp_path)
    repo = _make_repo(tmp_path, "vbot", "builder.md")
    _add_project(state, {"cwd": str(repo), "display_name": "vBot"})
    _write_agent(repo, "tester.md")

    result = _show_project(state, {"project_id": "vbot"})

    assert [member["agent_id"] for member in result["scan"]["team"]] == ["builder", "tester"]


def test_show_unknown_project_errors(tmp_path: Path) -> None:
    state = _make_state(tmp_path)

    with pytest.raises(RpcError) as exc_info:
        _show_project(state, {"project_id": "ghost"})

    assert exc_info.value.code == "project_not_found"


def test_list_returns_projects(tmp_path: Path) -> None:
    state = _make_state(tmp_path)
    _add_project(state, {"cwd": str(_make_repo(tmp_path, "alpha")), "display_name": "Alpha"})
    _add_project(state, {"cwd": str(_make_repo(tmp_path, "beta")), "display_name": "Beta"})

    result = _list_projects(state, {})

    assert [project["project_id"] for project in result["projects"]] == ["alpha", "beta"]


# ---------------------------------------------------------------------------
# set: mutate + re-scan on cwd change.
# ---------------------------------------------------------------------------


def test_set_changes_default_model(tmp_path: Path) -> None:
    state = _make_state(tmp_path)
    repo = _make_repo(tmp_path, "vbot", "builder.md")
    _add_project(state, {"cwd": str(repo), "display_name": "vBot"})

    result = _set_project(state, {"project_id": "vbot", "default_model": "openai/gpt-mini"})

    assert result["project"]["default_model"] == "openai/gpt-mini"


def test_set_cwd_rescans_team(tmp_path: Path) -> None:
    state = _make_state(tmp_path)
    old_repo = _make_repo(tmp_path, "vbot", "builder.md")
    new_repo = _make_repo(tmp_path, "vbot-moved", "builder.md", "tester.md")
    _add_project(state, {"cwd": str(old_repo), "display_name": "vBot"})

    result = _set_project(state, {"project_id": "vbot", "cwd": str(new_repo)})

    assert [member["agent_id"] for member in result["scan"]["team"]] == ["builder", "tester"]


def test_set_rejects_missing_cwd(tmp_path: Path) -> None:
    state = _make_state(tmp_path)
    _add_project(state, {"cwd": str(_make_repo(tmp_path, "vbot")), "display_name": "vBot"})

    with pytest.raises(RpcError) as exc_info:
        _set_project(state, {"project_id": "vbot", "cwd": str(tmp_path / "nope")})

    assert exc_info.value.code == "invalid_request"


def test_set_requires_a_change(tmp_path: Path) -> None:
    state = _make_state(tmp_path)
    _add_project(state, {"cwd": str(_make_repo(tmp_path, "vbot")), "display_name": "vBot"})

    with pytest.raises(RpcError, match="at least one field"):
        _set_project(state, {"project_id": "vbot"})


# ---------------------------------------------------------------------------
# Tool / Skill Whitelist fields: add defaults, set, validation, team denials.
# ---------------------------------------------------------------------------


def test_add_returns_default_whitelist_fields(tmp_path: Path) -> None:
    state = _make_state(tmp_path)
    repo = _make_repo(tmp_path, "vbot", "builder.md")

    result = _add_project(state, {"cwd": str(repo), "display_name": "vBot"})

    assert result["project"]["allowed_tools"] == list(PROJECT_DEFAULT_ALLOWED_TOOLS)
    assert result["project"]["skills_bundled_enabled"] == []
    assert result["project"]["skills_project_disabled"] == []


def test_set_changes_whitelist_fields(tmp_path: Path) -> None:
    state = _make_state(tmp_path)
    _add_project(state, {"cwd": str(_make_repo(tmp_path, "vbot")), "display_name": "vBot"})

    result = _set_project(
        state,
        {
            "project_id": "vbot",
            "allowed_tools": ["read", "grep"],
            "skills_bundled_enabled": ["frontend-design"],
            "skills_project_disabled": ["debugging"],
        },
    )

    assert result["project"]["allowed_tools"] == ["read", "grep"]
    assert result["project"]["skills_bundled_enabled"] == ["frontend-design"]
    assert result["project"]["skills_project_disabled"] == ["debugging"]


def test_set_allows_empty_allowed_tools(tmp_path: Path) -> None:
    state = _make_state(tmp_path)
    _add_project(state, {"cwd": str(_make_repo(tmp_path, "vbot")), "display_name": "vBot"})

    result = _set_project(state, {"project_id": "vbot", "allowed_tools": []})

    assert result["project"]["allowed_tools"] == []


def test_set_rejects_non_string_tool_entry(tmp_path: Path) -> None:
    state = _make_state(tmp_path)
    _add_project(state, {"cwd": str(_make_repo(tmp_path, "vbot")), "display_name": "vBot"})

    with pytest.raises(RpcError) as exc_info:
        _set_project(state, {"project_id": "vbot", "allowed_tools": ["read", 7]})

    assert exc_info.value.code == "invalid_request"


def test_scan_preview_includes_project_skill_pool(tmp_path: Path) -> None:
    # The scan response carries the editor's skill pool: the project's own skills
    # plus the bundled pool with name collisions removed (project wins).
    state = _make_state(tmp_path)
    repo = _make_repo(tmp_path, "vbot", "builder.md")
    _add_project(state, {"cwd": str(repo), "display_name": "vBot"})
    state.runtime.project_skill_names = lambda _project_id: frozenset({"refactoring", "glossary"})
    state.runtime.skills = SimpleNamespace(
        list_all=lambda: [
            SimpleNamespace(name="glossary"),
            SimpleNamespace(name="pdf"),
        ]
    )

    result = _show_project(state, {"project_id": "vbot"})

    assert result["scan"]["skills"] == {
        "project": ["glossary", "refactoring"],
        # "glossary" is shadowed by the project skill of the same name, so it is not
        # offered again as a bundled opt-in.
        "bundled": ["pdf"],
    }


def test_team_member_reports_denied_tools(tmp_path: Path) -> None:
    # An OpenCode agent denying task → the team response surfaces the mapped vBot
    # tool it turns off, so the editor can show it uses less than the ceiling.
    state = _make_state(tmp_path)
    repo = tmp_path / "repos" / "vbot"
    repo.mkdir(parents=True)
    _write_agent(repo, "explorer.md", permission={"task": "deny", "edit": "deny"})
    _add_project(state, {"cwd": str(repo), "display_name": "vBot"})

    result = _show_project(state, {"project_id": "vbot"})

    member = next(m for m in result["scan"]["team"] if m["agent_id"] == "explorer")
    assert member["denied_tools"] == ["edit", "subagent", "write"]


# ---------------------------------------------------------------------------
# Default temperature / thinking effort: add, set, show, validation.
# ---------------------------------------------------------------------------


def test_add_persists_default_temperature_and_thinking(tmp_path: Path) -> None:
    state = _make_state(tmp_path)
    repo = _make_repo(tmp_path, "vbot", "builder.md")

    result = _add_project(
        state,
        {
            "cwd": str(repo),
            "display_name": "vBot",
            "default_temperature": 0.4,
            "default_thinking_effort": "high",
        },
    )

    assert result["project"]["default_temperature"] == 0.4
    assert result["project"]["default_thinking_effort"] == "high"


def test_add_rejects_temperature_out_of_range(tmp_path: Path) -> None:
    state = _make_state(tmp_path)
    repo = _make_repo(tmp_path, "vbot")

    with pytest.raises(RpcError) as exc_info:
        _add_project(state, {"cwd": str(repo), "display_name": "vBot", "default_temperature": 3.0})

    assert exc_info.value.code == "invalid_request"


def test_set_changes_default_temperature_and_thinking(tmp_path: Path) -> None:
    state = _make_state(tmp_path)
    _add_project(state, {"cwd": str(_make_repo(tmp_path, "vbot")), "display_name": "vBot"})

    result = _set_project(
        state,
        {"project_id": "vbot", "default_temperature": 0.2, "default_thinking_effort": "low"},
    )

    assert result["project"]["default_temperature"] == 0.2
    assert result["project"]["default_thinking_effort"] == "low"


def test_set_accepts_empty_thinking_effort_as_provider_default(tmp_path: Path) -> None:
    # "" is a real value (provider default), distinct from null — and _optional_string
    # would reject it, so this also guards against using the wrong helper (D5).
    state = _make_state(tmp_path)
    _add_project(state, {"cwd": str(_make_repo(tmp_path, "vbot")), "display_name": "vBot"})

    result = _set_project(state, {"project_id": "vbot", "default_thinking_effort": ""})

    assert result["project"]["default_thinking_effort"] == ""


def test_set_null_clears_default_thinking_effort(tmp_path: Path) -> None:
    state = _make_state(tmp_path)
    _add_project(
        state,
        {
            "cwd": str(_make_repo(tmp_path, "vbot")),
            "display_name": "vBot",
            "default_thinking_effort": "high",
        },
    )

    result = _set_project(state, {"project_id": "vbot", "default_thinking_effort": None})

    assert result["project"]["default_thinking_effort"] is None


def test_set_rejects_unknown_thinking_effort(tmp_path: Path) -> None:
    state = _make_state(tmp_path)
    _add_project(state, {"cwd": str(_make_repo(tmp_path, "vbot")), "display_name": "vBot"})

    with pytest.raises(RpcError) as exc_info:
        _set_project(state, {"project_id": "vbot", "default_thinking_effort": "ultra"})

    assert exc_info.value.code == "invalid_request"


def test_set_rejects_temperature_out_of_range(tmp_path: Path) -> None:
    state = _make_state(tmp_path)
    _add_project(state, {"cwd": str(_make_repo(tmp_path, "vbot")), "display_name": "vBot"})

    with pytest.raises(RpcError) as exc_info:
        _set_project(state, {"project_id": "vbot", "default_temperature": 3.0})

    assert exc_info.value.code == "invalid_request"


def test_show_includes_default_temperature_and_thinking(tmp_path: Path) -> None:
    state = _make_state(tmp_path)
    _add_project(
        state,
        {
            "cwd": str(_make_repo(tmp_path, "vbot")),
            "display_name": "vBot",
            "default_temperature": 0.7,
            "default_thinking_effort": "medium",
        },
    )

    result = _show_project(state, {"project_id": "vbot"})

    assert result["project"]["default_temperature"] == 0.7
    assert result["project"]["default_thinking_effort"] == "medium"


def test_team_member_response_includes_thinking_effort(tmp_path: Path) -> None:
    state = _make_state(tmp_path)
    repo = _make_repo(tmp_path, "vbot")
    _write_agent(repo, "thinker.md", reasoning_effort="high")

    result = _add_project(state, {"cwd": str(repo), "display_name": "vBot"})

    member = result["scan"]["team"][0]
    assert member["agent_id"] == "thinker"
    assert member["thinking_effort"] == "high"


# ---------------------------------------------------------------------------
# rm: archive + remove lock.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rm_archives_project(tmp_path: Path) -> None:
    state = _make_state(tmp_path)
    repo = _make_repo(tmp_path, "vbot", "builder.md")
    _add_project(state, {"cwd": str(repo), "display_name": "vBot"})

    result = await _remove_project(state, {"project_id": "vbot"})

    assert result["archived"] is True
    assert not state.runtime.projects.exists("vbot")
    # The repo (cwd) is never touched by removal.
    assert repo.joinpath(*OPENCODE_AGENTS_SUBPATH, "builder.md").exists()


@pytest.mark.asyncio
async def test_rm_blocked_by_active_run_of_project_agent(tmp_path: Path) -> None:
    state = _make_state(tmp_path)
    repo = _make_repo(tmp_path, "vbot", "builder.md")
    _add_project(state, {"cwd": str(repo), "display_name": "vBot"})
    # A session-owning agent is one with a session file under the anchor; create
    # one so the busy check has an owner to match.
    session_dir = state.runtime.projects.sessions_dir("vbot", "builder")
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / "s1.jsonl").write_text("", encoding="utf-8")

    release = asyncio.Event()

    async def hold_run(_run: Run) -> str:
        await release.wait()
        return "done"

    run = await state.chat_runs.start(
        agent_id="builder",
        session_id="s1",
        executor=hold_run,
        project_id="vbot",
    )

    with pytest.raises(RpcError) as exc_info:
        await _remove_project(state, {"project_id": "vbot"})

    assert exc_info.value.code == "project_busy"
    assert state.runtime.projects.exists("vbot")

    release.set()
    assert await run.wait() == "done"


@pytest.mark.asyncio
async def test_rm_blocked_by_cron_pointing_at_project_agent(tmp_path: Path) -> None:
    # A cron job qualified with this project's id blocks removal.
    cron_jobs = [SimpleNamespace(id="job-1", agent_id="builder", project_id="vbot")]
    state = _make_state(tmp_path, cron_jobs=cron_jobs)
    repo = _make_repo(tmp_path, "vbot", "builder.md")
    _add_project(state, {"cwd": str(repo), "display_name": "vBot"})

    with pytest.raises(RpcError) as exc_info:
        await _remove_project(state, {"project_id": "vbot"})

    assert exc_info.value.code == "project_in_use"
    assert "cron:job-1" in exc_info.value.message
    assert state.runtime.projects.exists("vbot")


@pytest.mark.asyncio
async def test_rm_ignores_bare_cron_with_same_named_identity_agent(tmp_path: Path) -> None:
    # A bare job (project_id=None) targets the identity agent, not this project's
    # Team agent — even when the ids collide by name — so it must not block.
    cron_jobs = [SimpleNamespace(id="job-1", agent_id="builder", project_id=None)]
    state = _make_state(tmp_path, cron_jobs=cron_jobs)
    repo = _make_repo(tmp_path, "vbot", "builder.md")
    _add_project(state, {"cwd": str(repo), "display_name": "vBot"})

    result = await _remove_project(state, {"project_id": "vbot"})

    assert result["archived"] is True


@pytest.mark.asyncio
async def test_rm_ignores_cron_pointing_at_other_project_agent(tmp_path: Path) -> None:
    # A cron job qualified with a different project's id does not block.
    cron_jobs = [SimpleNamespace(id="job-1", agent_id="builder", project_id="other")]
    state = _make_state(tmp_path, cron_jobs=cron_jobs)
    repo = _make_repo(tmp_path, "vbot", "builder.md")
    _add_project(state, {"cwd": str(repo), "display_name": "vBot"})

    result = await _remove_project(state, {"project_id": "vbot"})

    assert result["archived"] is True


@pytest.mark.asyncio
async def test_rm_unknown_project_errors(tmp_path: Path) -> None:
    state = _make_state(tmp_path)

    with pytest.raises(RpcError) as exc_info:
        await _remove_project(state, {"project_id": "ghost"})

    assert exc_info.value.code == "project_not_found"


# ---------------------------------------------------------------------------
# Registration.
# ---------------------------------------------------------------------------


def test_project_methods_are_registered() -> None:
    handlers = build_method_handlers()

    for method in ("project.add", "project.list", "project.show", "project.set", "project.rm"):
        assert method in handlers
