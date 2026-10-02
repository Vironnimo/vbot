"""Learning-change RPCs: the Memory and Skill changes of one Run and their undo."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from core.memory import MemoryWriter
from core.skills import SkillWriter
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


def _reviewed_agent(tmp_path: Path) -> tuple[Any, Path]:
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
    return state, agent_dir


@pytest.mark.asyncio
async def test_learning_changes_lists_a_runs_changes_and_undo_takes_them_back(
    tmp_path: Path,
) -> None:
    state, agent_dir = _reviewed_agent(tmp_path)

    listed = await rpc_result(state, "learning.changes", agent_id="coder", run_id=REVIEW)
    undone = await rpc_result(state, "learning.undo", agent_id="coder", run_id=REVIEW)

    assert listed["summary"] == {"memory": 1, "skills": 1, "undone": False}
    assert [(change["store"], change["kind"]) for change in listed["changes"]] == [
        ("memory", "added"),
        ("skill", "created"),
    ]
    assert undone["summary"] == {"memory": 1, "skills": 1, "undone": True}
    assert (agent_dir / "workspace" / "USER.md").read_text(encoding="utf-8") == ""
    assert not (agent_dir / "skills" / "deploy").exists()
    assert resource_changes(state) == [
        {"kind": "memories", "scope": {"agent_id": "coder"}},
        {"kind": "skills"},
    ]
    # A finished undo leaves nothing to take back and announces nothing.
    again = await rpc_result(state, "learning.undo", agent_id="coder", run_id=REVIEW)
    assert again == undone
    assert len(resource_changes(state)) == 2


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
