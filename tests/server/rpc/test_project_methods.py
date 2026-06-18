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

import pytest

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


def _write_agent(repo: Path, filename: str, *, model: str = "openai/gpt-5.2") -> None:
    agents_dir = repo.joinpath(*OPENCODE_AGENTS_SUBPATH)
    agents_dir.mkdir(parents=True, exist_ok=True)
    front = f"description: An agent.\nmodel: {model}\n" if model else "description: An agent.\n"
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
        agents=SimpleNamespace(),  # identity path is unused in these tests
        projects=projects,
        model_checker=_openai_configured(),
        global_default_model=lambda: "",
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
    cron_jobs = [SimpleNamespace(id="job-1", agent_id="builder")]
    state = _make_state(tmp_path, cron_jobs=cron_jobs)
    repo = _make_repo(tmp_path, "vbot", "builder.md")
    _add_project(state, {"cwd": str(repo), "display_name": "vBot"})

    with pytest.raises(RpcError) as exc_info:
        await _remove_project(state, {"project_id": "vbot"})

    assert exc_info.value.code == "project_in_use"
    assert "cron:job-1" in exc_info.value.message
    assert state.runtime.projects.exists("vbot")


@pytest.mark.asyncio
async def test_rm_ignores_cron_pointing_at_other_project_agent(tmp_path: Path) -> None:
    # A cron job at an agent that is NOT on this project's Team does not block.
    cron_jobs = [SimpleNamespace(id="job-1", agent_id="stranger")]
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
