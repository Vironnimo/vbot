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
import logging
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.agents.agents import AgentStore
from core.projects.projects import PROJECT_DEFAULT_ALLOWED_TOOLS
from core.projects.resolver import (
    AgentResolutionError,
    AgentResolver,
    ModelConfigurationChecker,
)
from core.projects.scanners.opencode import OPENCODE_AGENTS_SUBPATH
from core.projects.store import ProjectStore
from core.runs import ChatRunManager, Run
from core.runtime.runtime import Runtime
from core.skills import SKILL_ORIGIN_BUNDLED, SKILL_ORIGIN_GLOBAL
from core.utils.config import Config
from server.rpc.errors import RPC_ERROR_PROJECT_BUSY, RpcError
from server.rpc.methods import build_method_handlers
from server.rpc.project_methods import (
    _add_project,
    _clear_override,
    _detect_project,
    _list_projects,
    _remove_project,
    _set_override,
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


class _FakeCatalogModel:
    """Catalog-model stub with no connection allowlist (every connection allowed)."""

    connections: tuple[str, ...] = ()

    def allows_connection(self, connection_id: str) -> bool:
        return True


class _FakeModels:
    def __init__(self, known: set[tuple[str, str]]) -> None:
        self._known = known

    def get(self, provider_id: str, model_id: str) -> _FakeCatalogModel:
        if (provider_id, model_id) not in self._known:
            raise KeyError(f"{provider_id}/{model_id}")
        return _FakeCatalogModel()


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

    def is_usable(self, provider_id: str, connection_id: str | None = None) -> bool:
        return self.has_credentials(provider_id, connection_id)


class _FakeTools:
    """Minimal live Tool Registry catalog for Project whitelist tests."""

    def __init__(self, names: set[str]) -> None:
        self.names = names

    def list_tools(self, *, include_session_scoped: bool = True) -> list[SimpleNamespace]:
        del include_session_scoped
        return [SimpleNamespace(name=name) for name in sorted(self.names)]


class _FakeTerminalManager:
    def __init__(self) -> None:
        self.closed_projects: list[str] = []

    async def close_project_scope(self, project_id: str) -> None:
        self.closed_projects.append(project_id)


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


def _write_claude_agent(repo: Path, filename: str, name: str) -> None:
    agents_dir = repo / ".claude" / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    (agents_dir / filename).write_text(
        f"---\nname: {name}\ndescription: A Claude agent.\n---\nBody.\n", encoding="utf-8"
    )


def _make_state(
    tmp_path: Path,
    *,
    cron_jobs: list | None = None,
    bootstrap_jobs: list | None = None,
) -> SimpleNamespace:
    data_dir = tmp_path / "data"
    projects = ProjectStore(data_dir)
    agents = AgentStore(data_dir)
    resolver = AgentResolver(
        agents=agents,
        projects=projects,
        model_checker=_openai_configured(),
        global_agent_defaults=lambda: {},
    )
    chat_runs = ChatRunManager()
    cron_service = SimpleNamespace(list_jobs=lambda: list(cron_jobs or []))
    bootstrap_service = SimpleNamespace(list_jobs=lambda: list(bootstrap_jobs or []))
    runtime = SimpleNamespace(
        projects=projects,
        agents=agents,
        agent_resolver=resolver,
        terminal_manager=_FakeTerminalManager(),
        cron_service=cron_service,
        bootstrap_service=bootstrap_service,
        # ``project.set_override``'s model gate reads ``runtime.models`` only for a pinned
        # ``::connection`` suffix (never in these tests), but expose it so a plain
        # model override never trips an AttributeError.
        models=_FakeModels({("openai", "gpt-5.2"), ("openai", "gpt-mini")}),
        # Mirrors tool.list's registered normal catalog. ``memory`` and
        # ``skill_manage`` are registered but intentionally not Project-eligible.
        tools=_FakeTools(
            {
                *PROJECT_DEFAULT_ALLOWED_TOOLS,
                "memory",
                "skill_manage",
            }
        ),
    )
    return SimpleNamespace(runtime=runtime, chat_runs=chat_runs)


def _build_started_runtime(tmp_path: Path) -> Runtime:
    """Start a real Runtime so the per-project skill cache is exercised end-to-end.

    The minimal ``_make_state`` runtime has no skill seam, so the skill-cache half
    of the open-time refresh needs the real runtime cache behind
    ``project_skill_names`` / ``invalidate_project_skills``.
    """
    logging.getLogger("vbot").handlers = []
    runtime = Runtime(Config(data_dir=tmp_path / "data"))
    runtime.start()
    return runtime


def _write_project_skill(repo: Path, name: str, description: str) -> None:
    """Write a project-owned skill under ``<repo>/.opencode/skills/<name>/``."""
    skill_dir = repo / ".opencode" / "skills" / name
    skill_dir.mkdir(parents=True)
    skill_dir.joinpath("SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\nUse this skill.\n",
        encoding="utf-8",
    )


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
    assert str(missing) in exc_info.value.message


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

    with pytest.raises(RpcError) as exc_info:
        _add_project(state, {"cwd": str(repo), "bogus": 1})
    assert exc_info.value.code == "invalid_request"
    assert "bogus" in exc_info.value.message


# ---------------------------------------------------------------------------
# source format: auto-detection at add, explicit set, switch, detect.
# ---------------------------------------------------------------------------


def test_add_auto_detects_claude_only_repo(tmp_path: Path) -> None:
    # No explicit source_format + exactly one format present → that one, silently.
    state = _make_state(tmp_path)
    repo = _make_repo(tmp_path, "claude-repo")
    _write_claude_agent(repo, "reviewer.md", "reviewer")

    result = _add_project(state, {"cwd": str(repo)})

    assert result["project"]["source_format"] == "claude"
    assert [member["agent_id"] for member in result["scan"]["team"]] == ["reviewer"]
    assert state.runtime.projects.get("claude-repo").source_format == "claude"


def test_add_defaults_to_opencode_when_both_formats_present(tmp_path: Path) -> None:
    # Deterministic non-interactive default (decision 2): both present → opencode.
    state = _make_state(tmp_path)
    repo = _make_repo(tmp_path, "mixed", "builder.md")
    _write_claude_agent(repo, "reviewer.md", "reviewer")

    result = _add_project(state, {"cwd": str(repo)})

    assert result["project"]["source_format"] == "opencode"
    assert [member["agent_id"] for member in result["scan"]["team"]] == ["builder"]


def test_add_defaults_to_opencode_when_neither_format_present(tmp_path: Path) -> None:
    state = _make_state(tmp_path)
    repo = _make_repo(tmp_path, "bare")

    result = _add_project(state, {"cwd": str(repo)})

    assert result["project"]["source_format"] == "opencode"


def test_add_accepts_explicit_source_format(tmp_path: Path) -> None:
    # An explicit choice wins over auto-detection.
    state = _make_state(tmp_path)
    repo = _make_repo(tmp_path, "mixed", "builder.md")
    _write_claude_agent(repo, "reviewer.md", "reviewer")

    result = _add_project(state, {"cwd": str(repo), "source_format": "claude"})

    assert result["project"]["source_format"] == "claude"
    assert [member["agent_id"] for member in result["scan"]["team"]] == ["reviewer"]


def test_add_rejects_unknown_source_format(tmp_path: Path) -> None:
    state = _make_state(tmp_path)
    repo = _make_repo(tmp_path, "vbot")

    with pytest.raises(RpcError) as exc_info:
        _add_project(state, {"cwd": str(repo), "source_format": "cursor"})

    assert exc_info.value.code == "invalid_request"
    assert "source_format" in exc_info.value.message


def test_set_source_format_switches_team_without_restart(tmp_path: Path) -> None:
    # A format switch invalidates like a cwd change, so the returned scan (and any
    # later show) reflects the other format's team immediately.
    state = _make_state(tmp_path)
    repo = _make_repo(tmp_path, "mixed", "builder.md")
    _write_claude_agent(repo, "reviewer.md", "reviewer")
    _add_project(state, {"cwd": str(repo)})

    switched = _set_project(state, {"project_id": "mixed", "source_format": "claude"})

    assert switched["project"]["source_format"] == "claude"
    assert [member["agent_id"] for member in switched["scan"]["team"]] == ["reviewer"]
    shown = _show_project(state, {"project_id": "mixed"})
    assert [member["agent_id"] for member in shown["scan"]["team"]] == ["reviewer"]


def test_detect_reports_formats_and_context_files(tmp_path: Path) -> None:
    state = _make_state(tmp_path)
    repo = _make_repo(tmp_path, "mixed", "builder.md")
    _write_claude_agent(repo, "reviewer.md", "reviewer")
    _write_claude_agent(repo, "helper.md", "helper")
    (repo / "CLAUDE.md").write_text("# Claude\n", encoding="utf-8")

    result = _detect_project(state, {"cwd": str(repo)})

    assert result["cwd_exists"] is True
    assert result["formats"]["opencode"] == {"agents": 1, "skills": 0}
    assert result["formats"]["claude"] == {"agents": 2, "skills": 0}
    assert result["context_files"] == {"agents_md": False, "claude_md": "CLAUDE.md"}


def test_detect_nonexistent_cwd_is_success_with_empty_data(tmp_path: Path) -> None:
    # The add dialog calls this while the user types — never an error envelope.
    state = _make_state(tmp_path)

    result = _detect_project(state, {"cwd": str(tmp_path / "nope")})

    assert result == {
        "cwd_exists": False,
        "formats": {},
        "context_files": {"agents_md": False, "claude_md": None},
    }


def test_detect_rejects_unknown_field(tmp_path: Path) -> None:
    state = _make_state(tmp_path)

    with pytest.raises(RpcError) as exc_info:
        _detect_project(state, {"cwd": str(tmp_path), "bogus": 1})
    assert exc_info.value.code == "invalid_request"
    assert "bogus" in exc_info.value.message


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


def test_show_reflects_a_newly_added_repo_skill(tmp_path: Path) -> None:
    # Open re-scans the Team on every call; the skill pool must keep pace. A skill
    # newly added under <cwd>/.opencode/skills surfaces after project.show in both
    # the editor pool and the resolver's effective-skills input — not only after a
    # cwd change or a restart.
    runtime = _build_started_runtime(tmp_path)
    state = SimpleNamespace(runtime=runtime)
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_project_skill(repo, "alpha", "Alpha playbook.")
    runtime.projects.create("p", "P", repo)

    # The first open primes the per-project skill cache against the current repo.
    primed = _show_project(state, {"project_id": "p"})
    assert primed["scan"]["skills"]["project"] == [
        {"name": "alpha", "description": "Alpha playbook."},
    ]

    # A new project skill lands in the repo after that first open.
    _write_project_skill(repo, "beta", "Beta playbook.")

    refreshed = _show_project(state, {"project_id": "p"})

    # The editor pool reflects the new skill (name + description carried per entry)...
    assert refreshed["scan"]["skills"]["project"] == [
        {"name": "alpha", "description": "Alpha playbook."},
        {"name": "beta", "description": "Beta playbook."},
    ]
    # ...and so does project_skill_names, which is exactly what the resolver feeds
    # into a config agent's effective skills, so the next resolve sees it too.
    assert runtime.project_skill_names("p") == frozenset({"alpha", "beta"})


def test_show_drops_team_cache_so_a_new_repo_agent_resolves(tmp_path: Path) -> None:
    # Open drops the Team cache together with the skill cache, so an agent added to
    # the repo after an earlier run resolves on the next run instead of being
    # rejected by a stale Team cache.
    state = _make_state(tmp_path)
    repo = _make_repo(tmp_path, "vbot", "builder.md")
    _add_project(state, {"cwd": str(repo), "display_name": "vBot"})
    resolver = state.runtime.agent_resolver
    # An earlier run caches the Team (builder only).
    resolver.resolve_agent("vbot", "builder")
    # A new agent is added to the repo afterwards.
    _write_agent(repo, "tester.md")

    _show_project(state, {"project_id": "vbot"})

    assert resolver.resolve_agent("vbot", "tester").id == "tester"


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


def test_set_clears_display_name_to_project_id(tmp_path: Path) -> None:
    state = _make_state(tmp_path)
    repo = _make_repo(tmp_path, "vbot")
    _add_project(state, {"cwd": str(repo), "display_name": "vBot"})

    result = _set_project(state, {"project_id": "vbot", "display_name": None})

    assert result["project"]["display_name"] == "vbot"


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

    with pytest.raises(RpcError) as exc_info:
        _set_project(state, {"project_id": "vbot"})
    assert exc_info.value.code == "invalid_request"


# ---------------------------------------------------------------------------
# Tool / Skill Whitelist fields: add defaults, set, validation, team denials.
# ---------------------------------------------------------------------------


def test_add_returns_default_whitelist_fields(tmp_path: Path) -> None:
    state = _make_state(tmp_path)
    repo = _make_repo(tmp_path, "vbot", "builder.md")

    result = _add_project(state, {"cwd": str(repo), "display_name": "vBot"})

    assert result["project"]["allowed_tools"] == list(PROJECT_DEFAULT_ALLOWED_TOOLS)
    assert result["project"]["skills_bundled_enabled"] == []
    assert result["project"]["skills_global_enabled"] == []
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
            "skills_global_enabled": ["pdf"],
            "skills_project_disabled": ["debugging"],
        },
    )

    assert result["project"]["allowed_tools"] == ["read", "grep"]
    assert result["project"]["skills_bundled_enabled"] == ["frontend-design"]
    assert result["project"]["skills_global_enabled"] == ["pdf"]
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


def test_set_rejects_tool_wildcard(tmp_path: Path) -> None:
    state = _make_state(tmp_path)
    _add_project(state, {"cwd": str(_make_repo(tmp_path, "vbot")), "display_name": "vBot"})

    with pytest.raises(RpcError) as exc_info:
        _set_project(state, {"project_id": "vbot", "allowed_tools": ["read", "*"]})

    assert exc_info.value.code == "invalid_request"


def test_set_rejects_new_unregistered_project_tool(tmp_path: Path) -> None:
    state = _make_state(tmp_path)
    _add_project(state, {"cwd": str(_make_repo(tmp_path, "vbot")), "display_name": "vBot"})

    with pytest.raises(RpcError) as exc_info:
        _set_project(
            state,
            {"project_id": "vbot", "allowed_tools": ["read", "missing_extension_tool"]},
        )

    assert exc_info.value.code == "invalid_request"
    assert "missing_extension_tool" in exc_info.value.message


def test_set_rejects_registered_but_project_excluded_tool(tmp_path: Path) -> None:
    state = _make_state(tmp_path)
    _add_project(state, {"cwd": str(_make_repo(tmp_path, "vbot")), "display_name": "vBot"})

    with pytest.raises(RpcError) as exc_info:
        _set_project(state, {"project_id": "vbot", "allowed_tools": ["read", "memory"]})

    assert exc_info.value.code == "invalid_request"
    assert "memory" in exc_info.value.message


def test_set_accepts_registered_extension_tool(tmp_path: Path) -> None:
    state = _make_state(tmp_path)
    state.runtime.tools.names.add("extension_tool")
    _add_project(state, {"cwd": str(_make_repo(tmp_path, "vbot")), "display_name": "vBot"})

    result = _set_project(
        state,
        {"project_id": "vbot", "allowed_tools": ["read", "extension_tool"]},
    )

    assert result["project"]["allowed_tools"] == ["read", "extension_tool"]


def test_set_preserves_existing_unavailable_tool_while_editing_known_tools(tmp_path: Path) -> None:
    state = _make_state(tmp_path)
    _add_project(state, {"cwd": str(_make_repo(tmp_path, "vbot")), "display_name": "vBot"})
    state.runtime.projects.update("vbot", allowed_tools=["read", "disabled_extension_tool"])

    result = _set_project(
        state,
        {
            "project_id": "vbot",
            "allowed_tools": ["read", "grep", "disabled_extension_tool"],
        },
    )

    assert result["project"]["allowed_tools"] == [
        "read",
        "grep",
        "disabled_extension_tool",
    ]


def test_show_reports_persisted_unavailable_tool_without_rejecting_project(tmp_path: Path) -> None:
    state = _make_state(tmp_path)
    _add_project(state, {"cwd": str(_make_repo(tmp_path, "vbot")), "display_name": "vBot"})
    state.runtime.projects.update("vbot", allowed_tools=["read", "disabled_extension_tool"])

    result = _show_project(state, {"project_id": "vbot"})

    assert result["project"]["allowed_tools"] == ["read", "disabled_extension_tool"]
    assert result["scan"]["report"] == {
        "clean": False,
        "findings": [
            {
                "type": "unavailable_tool",
                "detail": (
                    "Tool Whitelist entry 'disabled_extension_tool' is not a currently "
                    "registered Project tool. It remains stored but grants no access "
                    "unless the tool becomes available again."
                ),
                "agent_id": "",
                "source_path": None,
            }
        ],
    }


def test_scan_preview_includes_project_skill_pool(tmp_path: Path) -> None:
    # The scan response carries the editor's skill pool: the project's own skills,
    # plus the bundled and global opt-in pools with name collisions removed (project
    # wins) and global-home skills split out from bundled by origin.
    state = _make_state(tmp_path)
    repo = _make_repo(tmp_path, "vbot", "builder.md")
    _add_project(state, {"cwd": str(repo), "display_name": "vBot"})
    state.runtime.project_skill_names = lambda _project_id: frozenset({"refactoring", "glossary"})
    state.runtime.project_own_skills = lambda _project_id: [
        SimpleNamespace(name="refactoring", description="Refactor code safely."),
        SimpleNamespace(name="glossary", description="Maintain the glossary."),
    ]
    state.runtime.skills = SimpleNamespace(
        list_all=lambda: [
            SimpleNamespace(name="glossary", description="", origin=SKILL_ORIGIN_BUNDLED),
            SimpleNamespace(name="pdf", description="Work with PDFs.", origin=SKILL_ORIGIN_BUNDLED),
            SimpleNamespace(
                name="deploy", description="Deploy the app.", origin=SKILL_ORIGIN_GLOBAL
            ),
        ]
    )

    result = _show_project(state, {"project_id": "vbot"})

    # Each pool entry carries name + description so the editor's chips can show the
    # description on hover, like the tool pool.
    assert result["scan"]["skills"] == {
        "project": [
            {"name": "glossary", "description": "Maintain the glossary."},
            {"name": "refactoring", "description": "Refactor code safely."},
        ],
        # "glossary" is shadowed by the project skill of the same name, so it is not
        # offered again as a bundled opt-in.
        "bundled": [{"name": "pdf", "description": "Work with PDFs."}],
        # Global-home skills are a separate opt-in pool, split out by origin.
        "global": [{"name": "deploy", "description": "Deploy the app."}],
    }


def test_show_project_reloads_global_skills_from_disk(tmp_path: Path) -> None:
    # A show reloads the global skill registry so a skill hand-dropped into the global
    # skills folder surfaces in the editor pool without a server restart.
    state = _make_state(tmp_path)
    repo = _make_repo(tmp_path, "vbot", "builder.md")
    _add_project(state, {"cwd": str(repo), "display_name": "vBot"})
    reload_calls: list[bool] = []
    state.runtime.reload_skills = lambda: reload_calls.append(True)

    _show_project(state, {"project_id": "vbot"})

    assert reload_calls == [True]


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


def test_team_member_reports_effective_repo_owned_agent_targets(tmp_path: Path) -> None:
    state = _make_state(tmp_path)
    repo = tmp_path / "repos" / "vbot"
    agents_dir = repo.joinpath(*OPENCODE_AGENTS_SUBPATH)
    agents_dir.mkdir(parents=True)
    for name in ("builder", "reviewer"):
        _write_agent(repo, f"{name}.md")
    agents_dir.joinpath("orchestrator.md").write_text(
        (
            "---\nmodel: openai/gpt-5.2\npermission:\n  task:\n"
            '    "*": deny\n    reviewer: allow\n---\nBody.\n'
        ),
        encoding="utf-8",
    )
    _add_project(state, {"cwd": str(repo), "display_name": "vBot"})

    result = _show_project(state, {"project_id": "vbot"})

    members = {member["agent_id"]: member for member in result["scan"]["team"]}
    assert members["orchestrator"]["tools"] == {"subagent": {"allowed_agents": ["reviewer"]}}
    assert members["builder"]["tools"] == {
        "subagent": {"allowed_agents": ["orchestrator", "reviewer"]}
    }


# ---------------------------------------------------------------------------
# Per-agent Overrides: team response fields (overrides + effective) + set/clear handlers.
# ---------------------------------------------------------------------------


def test_team_member_reports_null_overrides_by_default(tmp_path: Path) -> None:
    state = _make_state(tmp_path)
    repo = _make_repo(tmp_path, "vbot", "builder.md")
    _add_project(state, {"cwd": str(repo), "display_name": "vBot"})

    result = _show_project(state, {"project_id": "vbot"})

    member = next(m for m in result["scan"]["team"] if m["agent_id"] == "builder")
    assert member["overrides"] is None
    # The effective block reports the model resolved from the repo (agent tier).
    assert member["effective"]["model"] == {"value": "openai/gpt-5.2", "source": "agent"}


def test_team_member_reports_overrides_value_and_effective(tmp_path: Path) -> None:
    state = _make_state(tmp_path)
    repo = _make_repo(tmp_path, "vbot", "builder.md")
    _add_project(state, {"cwd": str(repo), "display_name": "vBot"})
    state.runtime.projects.set_override("vbot", "builder", "model", "openai/gpt-mini")

    result = _show_project(state, {"project_id": "vbot"})

    member = next(m for m in result["scan"]["team"] if m["agent_id"] == "builder")
    assert member["overrides"] == {"model": "openai/gpt-mini"}
    # The override is the winning tier of the effective model chain.
    assert member["effective"]["model"] == {"value": "openai/gpt-mini", "source": "override"}


def test_set_override_model_writes_override_and_returns_scan(tmp_path: Path) -> None:
    state = _make_state(tmp_path)
    repo = _make_repo(tmp_path, "vbot", "builder.md")
    _add_project(state, {"cwd": str(repo), "display_name": "vBot"})

    result = _set_override(
        state,
        {"project_id": "vbot", "agent_id": "builder", "field": "model", "value": "openai/gpt-mini"},
    )

    member = next(m for m in result["scan"]["team"] if m["agent_id"] == "builder")
    assert member["overrides"] == {"model": "openai/gpt-mini"}
    assert state.runtime.projects.get("vbot").overrides == {"builder": {"model": "openai/gpt-mini"}}


def test_set_override_temperature_writes_override(tmp_path: Path) -> None:
    state = _make_state(tmp_path)
    repo = _make_repo(tmp_path, "vbot", "builder.md")
    _add_project(state, {"cwd": str(repo), "display_name": "vBot"})

    _set_override(
        state, {"project_id": "vbot", "agent_id": "builder", "field": "temperature", "value": 0.4}
    )

    assert state.runtime.projects.get("vbot").overrides == {"builder": {"temperature": 0.4}}


def test_set_override_thinking_effort_writes_override(tmp_path: Path) -> None:
    state = _make_state(tmp_path)
    repo = _make_repo(tmp_path, "vbot", "builder.md")
    _add_project(state, {"cwd": str(repo), "display_name": "vBot"})

    _set_override(
        state,
        {"project_id": "vbot", "agent_id": "builder", "field": "thinking_effort", "value": "high"},
    )

    assert state.runtime.projects.get("vbot").overrides == {"builder": {"thinking_effort": "high"}}


def test_set_override_rejects_agent_outside_project_team(tmp_path: Path) -> None:
    state = _make_state(tmp_path)
    repo = _make_repo(tmp_path, "vbot", "builder.md")
    _add_project(state, {"cwd": str(repo), "display_name": "vBot"})

    with pytest.raises(RpcError) as exc_info:
        _set_override(
            state,
            {
                "project_id": "vbot",
                "agent_id": "typo",
                "field": "temperature",
                "value": 0.4,
            },
        )

    assert exc_info.value.code == "invalid_request"
    assert "typo" in exc_info.value.message
    assert "vbot" in exc_info.value.message
    assert state.runtime.projects.get("vbot").overrides == {}


def test_set_override_rejects_unknown_field(tmp_path: Path) -> None:
    state = _make_state(tmp_path)
    repo = _make_repo(tmp_path, "vbot", "builder.md")
    _add_project(state, {"cwd": str(repo), "display_name": "vBot"})

    with pytest.raises(RpcError) as exc_info:
        _set_override(
            state, {"project_id": "vbot", "agent_id": "builder", "field": "nope", "value": "x"}
        )

    assert exc_info.value.code == "invalid_request"
    assert "params.field" in exc_info.value.message


def test_set_override_rejects_bad_temperature(tmp_path: Path) -> None:
    state = _make_state(tmp_path)
    repo = _make_repo(tmp_path, "vbot", "builder.md")
    _add_project(state, {"cwd": str(repo), "display_name": "vBot"})

    with pytest.raises(RpcError) as exc_info:
        _set_override(
            state,
            {"project_id": "vbot", "agent_id": "builder", "field": "temperature", "value": 3.0},
        )

    assert exc_info.value.code == "invalid_request"


def test_set_override_rejects_unusable_model(tmp_path: Path) -> None:
    state = _make_state(tmp_path)
    repo = _make_repo(tmp_path, "vbot", "builder.md")
    _add_project(state, {"cwd": str(repo), "display_name": "vBot"})

    with pytest.raises(RpcError) as exc_info:
        _set_override(
            state,
            {
                "project_id": "vbot",
                "agent_id": "builder",
                "field": "model",
                "value": "openai/ghost-model",
            },
        )

    assert exc_info.value.code == "invalid_request"


def test_set_override_rejects_unsupported_field_param(tmp_path: Path) -> None:
    state = _make_state(tmp_path)
    repo = _make_repo(tmp_path, "vbot", "builder.md")
    _add_project(state, {"cwd": str(repo), "display_name": "vBot"})

    with pytest.raises(RpcError) as exc_info:
        _set_override(
            state,
            {
                "project_id": "vbot",
                "agent_id": "builder",
                "field": "model",
                "value": "openai/gpt-mini",
                "bogus": 1,
            },
        )
    assert exc_info.value.code == "invalid_request"
    assert "bogus" in exc_info.value.message


def test_clear_override_removes_field_and_returns_scan(tmp_path: Path) -> None:
    state = _make_state(tmp_path)
    repo = _make_repo(tmp_path, "vbot", "builder.md")
    _add_project(state, {"cwd": str(repo), "display_name": "vBot"})
    state.runtime.projects.set_override("vbot", "builder", "model", "openai/gpt-mini")

    result = _clear_override(state, {"project_id": "vbot", "agent_id": "builder", "field": "model"})

    member = next(m for m in result["scan"]["team"] if m["agent_id"] == "builder")
    assert member["overrides"] is None
    assert state.runtime.projects.get("vbot").overrides == {}


def test_clear_override_absent_entry_is_noop(tmp_path: Path) -> None:
    state = _make_state(tmp_path)
    repo = _make_repo(tmp_path, "vbot", "builder.md")
    _add_project(state, {"cwd": str(repo), "display_name": "vBot"})

    result = _clear_override(state, {"project_id": "vbot", "agent_id": "builder", "field": "model"})

    assert result["project"]["project_id"] == "vbot"


def test_clear_override_rejects_unknown_field(tmp_path: Path) -> None:
    state = _make_state(tmp_path)
    repo = _make_repo(tmp_path, "vbot", "builder.md")
    _add_project(state, {"cwd": str(repo), "display_name": "vBot"})

    with pytest.raises(RpcError) as exc_info:
        _clear_override(state, {"project_id": "vbot", "agent_id": "builder", "field": "nope"})

    assert exc_info.value.code == "invalid_request"


def test_clear_override_rejects_unsupported_field_param(tmp_path: Path) -> None:
    state = _make_state(tmp_path)
    repo = _make_repo(tmp_path, "vbot", "builder.md")
    _add_project(state, {"cwd": str(repo), "display_name": "vBot"})

    with pytest.raises(RpcError) as exc_info:
        _clear_override(
            state, {"project_id": "vbot", "agent_id": "builder", "field": "model", "bogus": 1}
        )
    assert exc_info.value.code == "invalid_request"
    assert "bogus" in exc_info.value.message


def test_clear_override_unknown_project_raises(tmp_path: Path) -> None:
    state = _make_state(tmp_path)

    with pytest.raises(RpcError) as exc_info:
        _clear_override(state, {"project_id": "missing", "agent_id": "builder", "field": "model"})

    assert exc_info.value.code == "project_not_found"


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
    assert state.runtime.terminal_manager.closed_projects == ["vbot"]
    # The repo (cwd) is never touched by removal.
    assert repo.joinpath(*OPENCODE_AGENTS_SUBPATH, "builder.md").exists()


@pytest.mark.asyncio
async def test_rm_unroots_identity_agents_and_resets_default_workspaces(
    tmp_path: Path,
) -> None:
    state = _make_state(tmp_path)
    repo = _make_repo(tmp_path, "vbot", "builder.md")
    _add_project(state, {"cwd": str(repo), "display_name": "vBot"})
    custom_workspace = tmp_path / "identity-home"
    agent = state.runtime.agents.create("coder", "Coder", workspace=custom_workspace)
    Path(agent.workspace, "USER.md").write_text("user", encoding="utf-8")
    state.runtime.agents.update("coder", root_project_id="vbot")

    result = await _remove_project(
        state,
        {
            "project_id": "vbot",
            "copy_rooted_agent_identity_files": True,
        },
    )

    reset_agent = state.runtime.agents.get("coder")
    assert result["affected_agent_ids"] == ["coder"]
    assert reset_agent.root_project_id is None
    assert reset_agent.workspace == state.runtime.agents.default_workspace("coder")
    assert Path(reset_agent.workspace, "USER.md").read_text(encoding="utf-8") == "user"
    assert Path(agent.workspace, "USER.md").read_text(encoding="utf-8") == "user"
    assert repo.exists()


@pytest.mark.asyncio
async def test_rm_rolls_back_agent_reset_when_project_archive_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _make_state(tmp_path)
    repo = _make_repo(tmp_path, "vbot")
    _add_project(state, {"cwd": str(repo), "display_name": "vBot"})
    custom_workspace = tmp_path / "identity-home"
    agent = state.runtime.agents.create("coder", "Coder", workspace=custom_workspace)
    Path(agent.workspace, "USER.md").write_text("source", encoding="utf-8")
    default_workspace = Path(state.runtime.agents.default_workspace("coder"))
    default_workspace.mkdir(parents=True)
    default_workspace.joinpath("USER.md").write_text("destination", encoding="utf-8")
    state.runtime.agents.update("coder", root_project_id="vbot")

    def fail_archive(_project_id: str) -> Path:
        raise OSError("archive failed")

    monkeypatch.setattr(state.runtime.projects, "delete", fail_archive)

    with pytest.raises(OSError):
        await _remove_project(
            state,
            {
                "project_id": "vbot",
                "copy_rooted_agent_identity_files": True,
            },
        )

    restored = state.runtime.agents.get("coder")
    assert state.runtime.projects.exists("vbot")
    assert restored.root_project_id == "vbot"
    assert restored.workspace == agent.workspace
    assert Path(agent.workspace, "USER.md").read_text(encoding="utf-8") == "source"
    assert default_workspace.joinpath("USER.md").read_text(encoding="utf-8") == "destination"


@pytest.mark.asyncio
async def test_rm_blocks_identity_run_using_project_as_working_context(
    tmp_path: Path,
) -> None:
    state = _make_state(tmp_path)
    repo = _make_repo(tmp_path, "vbot")
    _add_project(state, {"cwd": str(repo), "display_name": "vBot"})
    release = asyncio.Event()

    async def execute(_run: Run) -> None:
        await release.wait()

    run = await state.chat_runs.start(
        agent_id="coder",
        session_id="s1",
        executor=execute,
        project_id=None,
        working_project_id="vbot",
    )
    try:
        with pytest.raises(RpcError) as exc:
            await _remove_project(state, {"project_id": "vbot"})
        assert exc.value.code == RPC_ERROR_PROJECT_BUSY
    finally:
        release.set()
        await run.wait()


@pytest.mark.asyncio
async def test_rm_invalidates_caches_so_readd_resolves_against_new_repo(tmp_path: Path) -> None:
    # Removing a project must drop both per-project caches keyed on its repo, so a
    # later project that reuses the same slug against a *different* repo resolves
    # against the new repo — not the removed project's stale Team/skills.
    state = _make_state(tmp_path)
    repo_a = _make_repo(tmp_path, "repo-a", "builder.md")
    repo_b = _make_repo(tmp_path, "repo-b", "tester.md")
    _add_project(state, {"cwd": str(repo_a), "display_name": "vBot"})

    resolver = state.runtime.agent_resolver
    # A run populates the Team cache for "vbot" against repo A.
    resolver.resolve_agent("vbot", "builder")
    # Spy on the skill-cache half (the minimal test runtime has no skill seam).
    skill_invalidations: list[str] = []
    state.runtime.invalidate_project_skills = skill_invalidations.append

    await _remove_project(state, {"project_id": "vbot"})

    assert skill_invalidations == ["vbot"]

    # Re-add the same slug pointing at repo B: the dropped Team cache must let repo
    # B's agent resolve, and repo A's agent must be gone with it.
    _add_project(state, {"cwd": str(repo_b), "display_name": "vBot"})
    assert resolver.resolve_agent("vbot", "tester").id == "tester"
    with pytest.raises(AgentResolutionError):
        resolver.resolve_agent("vbot", "builder")


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
async def test_rm_blocked_by_bootstrap_pointing_at_project_agent(tmp_path: Path) -> None:
    jobs = [
        SimpleNamespace(
            id="boot-1",
            agent_id="builder",
            project_id="vbot",
            status="active",
        )
    ]
    state = _make_state(tmp_path, bootstrap_jobs=jobs)
    repo = _make_repo(tmp_path, "vbot", "builder.md")
    _add_project(state, {"cwd": str(repo), "display_name": "vBot"})

    with pytest.raises(RpcError) as exc_info:
        await _remove_project(state, {"project_id": "vbot"})

    assert exc_info.value.code == "project_in_use"
    assert "bootstrap:boot-1" in exc_info.value.message


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
async def test_rm_ignores_terminal_cron_history_for_project_agent(tmp_path: Path) -> None:
    cron_jobs = [
        SimpleNamespace(
            id="job-1",
            agent_id="builder",
            project_id="vbot",
            status="missed",
        )
    ]
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

    for method in (
        "project.add",
        "project.list",
        "project.show",
        "project.set",
        "project.set_override",
        "project.clear_override",
        "project.rm",
        "project.detect",
    ):
        assert method in handlers
    # The retired override handler is gone from the method table.
    assert "project.clear_model_override" not in handlers
