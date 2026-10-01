"""project.rm: archive, rooted identity Agents, cache invalidation and refusals."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core.projects.resolver import AgentResolutionError
from core.projects.scanners.opencode import OPENCODE_AGENTS_SUBPATH
from core.runs import Run, RunAdmission
from core.sessions import ArchiveEntryFilter, SessionAddress
from server.rpc.errors import RPC_ERROR_PROJECT_BUSY
from tests.server.rpc.project_methods_test_support import _make_repo, _make_state
from tests.server.rpc_test_support import call, rpc_error, rpc_result


async def _vbot_state(
    tmp_path: Path, *agents: str, **jobs: list[Any]
) -> tuple[SimpleNamespace, Path]:
    state = _make_state(tmp_path, **jobs)
    repo = _make_repo(tmp_path, "vbot", *agents)
    await rpc_result(state, "project.add", cwd=str(repo), display_name="vBot")
    return state, repo


@pytest.mark.asyncio
async def test_rm_archives_project(tmp_path: Path) -> None:
    state, repo = await _vbot_state(tmp_path, "builder.md")

    result = await rpc_result(state, "project.rm", project_id="vbot")

    entry = state.runtime.sessions.archive_ledger.entry(result["archive_entry_id"])
    assert entry is not None and (entry.kind, entry.subject_id) == ("project", "vbot")
    assert (result["session_count"], result["purged"], result["purge_pending"]) == (0, False, False)
    assert "archive_path" not in result
    assert not state.runtime.projects.exists("vbot")
    assert state.runtime.terminal_manager.closed_projects == ["vbot"]
    # The repo (cwd) is never touched by removal.
    assert repo.joinpath(*OPENCODE_AGENTS_SUBPATH, "builder.md").exists()


@pytest.mark.asyncio
async def test_rm_unroots_identity_agents_and_resets_default_workspaces(
    tmp_path: Path,
) -> None:
    state, repo = await _vbot_state(tmp_path, "builder.md")
    agent = state.runtime.agents.create("coder", "Coder", workspace=tmp_path / "identity-home")
    Path(agent.workspace, "USER.md").write_text("user", encoding="utf-8")
    state.runtime.agents.update("coder", root_project_id="vbot")

    result = await rpc_result(
        state, "project.rm", project_id="vbot", copy_rooted_agent_identity_files=True
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
    state, _repo = await _vbot_state(tmp_path)
    agent = state.runtime.agents.create("coder", "Coder", workspace=tmp_path / "identity-home")
    Path(agent.workspace, "USER.md").write_text("source", encoding="utf-8")
    default_workspace = Path(state.runtime.agents.default_workspace("coder"))
    default_workspace.mkdir(parents=True)
    default_workspace.joinpath("USER.md").write_text("destination", encoding="utf-8")
    state.runtime.agents.update("coder", root_project_id="vbot")

    def fail_archive(_project_id: str, _tree: Path) -> Any:
        raise OSError("archive failed")

    monkeypatch.setattr(state.runtime.projects, "archive_files", fail_archive)

    with pytest.raises(OSError):
        await call(state, "project.rm", project_id="vbot", copy_rooted_agent_identity_files=True)

    restored = state.runtime.agents.get("coder")
    assert state.runtime.projects.exists("vbot")
    assert restored.root_project_id == "vbot"
    assert restored.workspace == agent.workspace
    assert Path(agent.workspace, "USER.md").read_text(encoding="utf-8") == "source"
    assert default_workspace.joinpath("USER.md").read_text(encoding="utf-8") == "destination"
    # The failed archive leaves no entry behind.
    assert not state.runtime.sessions.archive_ledger.page(ArchiveEntryFilter()).entries


@pytest.mark.asyncio
async def test_rm_invalidates_caches_so_readd_resolves_against_new_repo(tmp_path: Path) -> None:
    # Removing a project must drop both per-project caches keyed on its repo, so a
    # later project that reuses the same slug against a *different* repo resolves
    # against the new repo, not the removed project's stale Team/skills.
    state = _make_state(tmp_path)
    repo_a = _make_repo(tmp_path, "repo-a", "builder.md")
    repo_b = _make_repo(tmp_path, "repo-b", "tester.md")
    await rpc_result(state, "project.add", cwd=str(repo_a), display_name="vBot")
    resolver = state.runtime.agent_resolver
    # A run populates the Team cache for "vbot" against repo A.
    resolver.resolve_agent("vbot", "builder")
    # Spy on the skill-cache half (the minimal test runtime has no skill seam).
    skill_invalidations: list[str] = []
    state.runtime.invalidate_project_skills = skill_invalidations.append

    await rpc_result(state, "project.rm", project_id="vbot")
    await rpc_result(state, "project.add", cwd=str(repo_b), display_name="vBot")

    assert skill_invalidations == ["vbot"]
    assert resolver.resolve_agent("vbot", "tester").id == "tester"
    with pytest.raises(AgentResolutionError):
        resolver.resolve_agent("vbot", "builder")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("address", "admission"),
    [
        pytest.param(
            SessionAddress(project_id="vbot", agent_id="builder", session_id="s1"),
            None,
            id="project-agent-run",
        ),
        pytest.param(
            SessionAddress(project_id=None, agent_id="coder", session_id="s1"),
            RunAdmission(working_project_id="vbot"),
            id="identity-run-working-in-project",
        ),
    ],
)
async def test_rm_is_refused_while_a_run_uses_the_project(
    tmp_path: Path, address: SessionAddress, admission: RunAdmission | None
) -> None:
    state, _repo = await _vbot_state(tmp_path, "builder.md")
    # Create the canonical Session so the busy check has an owner to match.
    state.runtime.sessions.create("builder", session_id="s1", project_id="vbot")
    release = asyncio.Event()

    async def hold_run(_run: Run) -> str:
        await release.wait()
        return "done"

    run = await state.chat_runs.start(
        address, hold_run, **({"admission": admission} if admission else {})
    )
    try:
        error = await rpc_error(state, "project.rm", project_id="vbot")
    finally:
        release.set()
        assert await run.wait() == "done"

    assert error["code"] == RPC_ERROR_PROJECT_BUSY
    assert state.runtime.projects.exists("vbot")


def _job(**fields: Any) -> SimpleNamespace:
    """A live Cron or Bootstrap job of the Project Agent ``builder@vbot`` unless *fields* differ."""
    return SimpleNamespace(
        **{
            "id": "job-1",
            "name": "Report",
            "agent_id": "builder",
            "project_id": "vbot",
            "session_id": None,
            "status": "active",
            **fields,
        }
    )


def _cron_job(**fields: Any) -> dict[str, list[Any]]:
    return {"cron_jobs": [_job(**fields)]}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("jobs", "project_id", "code", "named"),
    [
        pytest.param(_cron_job(), "vbot", "project_in_use", "cron:job-1", id="cron"),
        pytest.param(
            {"bootstrap_jobs": [_job(id="boot-1")]},
            "vbot",
            "project_in_use",
            "bootstrap:boot-1",
            id="bootstrap",
        ),
        pytest.param(
            {"calendar_actions": [{"id": "act-1", "event_id": "evt-1", "target": "builder@vbot"}]},
            "vbot",
            "project_in_use",
            "calendar:act-1",
            id="calendar",
        ),
        pytest.param({}, "ghost", "project_not_found", "", id="unknown-project"),
    ],
)
async def test_rm_refusals_keep_the_project(
    tmp_path: Path, jobs: dict[str, list[Any]], project_id: str, code: str, named: str
) -> None:
    state, _repo = await _vbot_state(tmp_path, "builder.md", **jobs)

    error = await rpc_error(state, "project.rm", project_id=project_id)

    assert error["code"] == code
    assert named in error["message"]
    assert state.runtime.projects.exists("vbot")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "jobs",
    [
        # A bare job targets the identity Agent, not this Project's same-named
        # Team Agent.
        pytest.param(_cron_job(project_id=None), id="bare-identity-job"),
        pytest.param(_cron_job(status="missed"), id="terminal-history"),
        pytest.param(_cron_job(project_id="other"), id="other-project"),
        # An action that can no longer fire, for example of a past one-time event.
        pytest.param(
            {
                "calendar_actions": [
                    {"id": "act-1", "event_id": "evt-1", "target": "builder@vbot", "spent": True}
                ]
            },
            id="calendar-used-up",
        ),
    ],
)
async def test_rm_ignores_automations_that_never_start_a_run_for_the_project(
    tmp_path: Path, jobs: dict[str, list[Any]]
) -> None:
    state, _repo = await _vbot_state(tmp_path, "builder.md", **jobs)

    result = await rpc_result(state, "project.rm", project_id="vbot")

    assert result["archive_entry_id"].startswith("arc_")


def _usage_import_fails() -> None:
    raise OSError("usage ledger unavailable")


@pytest.mark.asyncio
@pytest.mark.parametrize("usage_import", ["works", "fails"])
async def test_rm_permanent_deletes_the_anchor_and_sessions_or_keeps_them_archived(
    tmp_path: Path, usage_import: str
) -> None:
    state = _make_state(tmp_path)
    if usage_import == "fails":
        state.runtime.import_usage = _usage_import_fails
    repo = _make_repo(tmp_path, "vbot", "builder.md")
    await rpc_result(state, "project.add", cwd=str(repo), display_name="vBot")
    state.runtime.sessions.create("builder", session_id="s1", project_id="vbot")

    result = await rpc_result(state, "project.rm", project_id="vbot", permanent=True)

    ledger = state.runtime.sessions.archive_ledger
    assert not state.runtime.projects.exists("vbot")
    assert result["session_count"] == 1
    if usage_import == "works":
        assert (result["purged"], result["purge_pending"], result["purge_reason"]) == (
            True,
            False,
            None,
        )
        assert not ledger.page(ArchiveEntryFilter()).entries
        assert not any((tmp_path / "data" / "archive").rglob("*"))
    else:
        # The Project is archived and stays restorable; nothing was deleted.
        assert (result["purged"], result["purge_pending"], result["purge_reason"]) == (
            False,
            False,
            "usage_import_failed",
        )
        entry = ledger.entry(result["archive_entry_id"])
        assert entry is not None and entry.state == "archived"
    assert repo.exists()
