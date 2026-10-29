"""Learning-change RPCs: the Memory and Skill changes of one Run and their undo."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from core.automation import LearningChanges
from core.memory import MemoryService, MemoryWriter
from core.skills import HUMAN_WRITER, SkillWriter
from tests.server.rpc_test_support import (
    StubAdapter,
    make_state,
    resource_changes,
    rpc_error,
    rpc_result,
)

REVIEW = "run-review"
_MEMORY = MemoryWriter(agent_id="coder", actor="tool", run_id=REVIEW, run_kind="reflection")
_SKILLS = SkillWriter(actor="reflection", run_id=REVIEW, run_kind="reflection")


def _reviewed_agent(
    tmp_path: Path, *, with_project: bool = False, with_shared: bool = False
) -> tuple[Any, Path]:
    """A state whose Agent ``coder`` has one review's Memory entry and Skill."""
    state = make_state(tmp_path, StubAdapter())
    agent_dir = tmp_path / "agents" / "coder"
    agent_dir.mkdir(parents=True)
    workspace = agent_dir / "workspace"
    state.runtime.agents.update("coder", workspace=str(workspace))
    state.runtime.memory.add_entry(workspace, "user", "Prefers short answers.", writer=_MEMORY)
    state.runtime.skill_authoring.create(
        agent_dir / "skills",
        "deploy",
        "---\nname: deploy\ndescription: Deploy the app.\n---\n\n# Steps\n",
        writer=_SKILLS,
    )
    if with_project:
        project = tmp_path / "projects" / "sample"
        project.mkdir(parents=True)
        state.runtime.memory = MemoryService(
            history_root=tmp_path / "agents", project_history_root=tmp_path / "projects"
        )
        state.runtime.memory.add_entry(
            project,
            "project",
            "Changes need user review.",
            writer=MemoryWriter(agent_id="coder", project_id="sample", run_id=REVIEW),
        )
    if with_shared:
        shared = tmp_path / "agents" / "owner" / "skills"
        state.runtime.skill_authoring.create(
            shared,
            "deploy",
            "---\nname: deploy\ndescription: Deploy.\n---\n\nOriginal.\n",
            writer=HUMAN_WRITER,
        )
        state.runtime.skill_authoring.write_file(
            shared, "deploy", "references/learned.md", "Learned.\n", writer=_SKILLS
        )
    if with_project or with_shared:
        state.runtime.learning_changes = LearningChanges(
            memory=state.runtime.memory,
            skills=state.runtime.skill_authoring,
            skill_home=lambda agent_id: tmp_path / "agents" / agent_id / "skills",
            run_active=lambda run_id: state.runtime.chat_run_manager.is_running(run_id),
            project_memories=lambda: {"sample": project} if with_project else {},
            skill_homes=lambda: {"owner": shared} if with_shared else {},
        )
    return state, agent_dir


@pytest.mark.asyncio
@pytest.mark.parametrize("with_project", [False, True], ids=["personal", "shared-project"])
@pytest.mark.parametrize("with_shared", [False, True], ids=["own-skills", "shared-skills"])
async def test_learning_changes_lists_a_runs_changes_and_undo_takes_them_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    with_project: bool,
    with_shared: bool,
) -> None:
    state, agent_dir = _reviewed_agent(tmp_path, with_project=with_project, with_shared=with_shared)
    invalidated: list[str | None] = []
    monkeypatch.setattr(state.runtime, "invalidate_agent_skills", invalidated.append)

    listed = await rpc_result(state, "learning.changes", agent_id="coder", run_id=REVIEW)
    # A Run still running cannot be undone yet.
    with monkeypatch.context() as patch:
        patch.setattr(state.runtime.chat_run_manager, "is_running", lambda run_id: True)
        running = await rpc_error(state, "learning.undo", agent_id="coder", run_id=REVIEW)
    assert (running["code"], running["data"]) == ("active_run", {"run_id": REVIEW})
    assert resource_changes(state) == []
    undone = await rpc_result(state, "learning.undo", agent_id="coder", run_id=REVIEW)

    memory_count = 2 if with_project else 1
    skill_count = 2 if with_shared else 1
    assert listed["summary"] == {"memory": memory_count, "skills": skill_count, "undone": False}
    assert [(change["store"], change["kind"]) for change in listed["changes"]] == [
        ("memory", "added"),
        *([("memory", "added")] if with_project else []),
        ("skill", "created"),
        *([("skill", "file_written")] if with_shared else []),
    ]
    if with_project:
        assert listed["changes"][1]["project_id"] == "sample"
        assert (tmp_path / "projects" / "sample" / "MEMORY.md").read_text(encoding="utf-8") == ""
    if with_shared:
        assert listed["changes"][-1]["skill_owner_id"] == "owner"
        assert not (tmp_path / "agents" / "owner" / "skills" / "deploy" / "references").exists()
    assert invalidated == (["coder", "owner"] if with_shared else ["coder"])
    assert undone["summary"] == {"memory": memory_count, "skills": skill_count, "undone": True}
    assert (agent_dir / "workspace" / "USER.md").read_text(encoding="utf-8") == ""
    assert not (agent_dir / "skills" / "deploy").exists()
    assert resource_changes(state) == [
        {"kind": "memories", "scope": {"agent_id": "coder"}},
        *([{"kind": "memories", "scope": {"project_id": "sample"}}] if with_project else []),
        {"kind": "skills"},
    ]
    # A finished undo leaves nothing to take back and announces nothing.
    again = await rpc_result(state, "learning.undo", agent_id="coder", run_id=REVIEW)
    assert again == undone
    assert len(resource_changes(state)) == (3 if with_project else 2)


@pytest.mark.asyncio
async def test_learning_undo_refuses_a_later_change_and_names_it(tmp_path: Path) -> None:
    state, agent_dir = _reviewed_agent(tmp_path)
    await rpc_result(
        state,
        "memory.replace",
        agent_id="coder",
        scope="user",
        entry_id=1,
        content="Prefers tables.",
    )
    announced = len(resource_changes(state))

    error = await rpc_error(state, "learning.undo", agent_id="coder", run_id=REVIEW)

    assert error["code"] == "learning_undo_conflict"
    later = error["data"].pop("later")
    assert (later["revision"], later["actor"]) == (2, "rpc")
    assert error["data"] == {
        "store": "memory",
        "revision": 1,
        "scope": "user",
        "text": "Prefers short answers.",
    }
    # Nothing changed, in either store.
    assert (agent_dir / "skills" / "deploy").is_dir()
    assert len(resource_changes(state)) == announced


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "params",
    [
        pytest.param({"agent_id": "coder", "run_id": ""}, id="empty-run-id"),
        pytest.param({"agent_id": "coder", "run_id": REVIEW, "scope": "agent"}, id="unknown-field"),
    ],
)
async def test_learning_rpcs_reject_invalid_requests(
    tmp_path: Path, params: dict[str, str]
) -> None:
    state, _agent_dir = _reviewed_agent(tmp_path)

    for method in ("learning.changes", "learning.undo"):
        assert (await rpc_error(state, method, **params))["code"] == "invalid_request"
