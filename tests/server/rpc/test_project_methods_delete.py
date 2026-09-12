"""Tests for project methods delete."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.projects.resolver import (
    AgentResolutionError,
)
from core.projects.scanners.opencode import OPENCODE_AGENTS_SUBPATH
from core.runs import Run, RunAdmission
from core.sessions import SessionAddress
from server.rpc.errors import RPC_ERROR_PROJECT_BUSY, RpcError
from server.rpc.methods import build_method_handlers
from server.rpc.project_methods import (
    _add_project,
    _remove_project,
)
from tests.server.rpc.project_methods_test_support import (
    _make_repo,
    _make_state,
)


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
        SessionAddress(project_id=None, agent_id="coder", session_id="s1"),
        execute,
        admission=RunAdmission(working_project_id="vbot"),
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
    # Create the canonical Session so the busy check has an owner to match.
    state.runtime.sessions.create("builder", session_id="s1", project_id="vbot")

    release = asyncio.Event()

    async def hold_run(_run: Run) -> str:
        await release.wait()
        return "done"

    run = await state.chat_runs.start(
        SessionAddress(project_id="vbot", agent_id="builder", session_id="s1"),
        hold_run,
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
