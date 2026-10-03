"""Contracts of the Memory and Skill changes of one Run and their undo."""

from __future__ import annotations

import errno
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

import core.memory.memory as memory_module
import core.skills._revert as skill_revert_module
from core.automation import (
    LearningChanges,
    LearningRunActiveError,
    LearningUndoConflictError,
    LearningUndoFailedError,
)
from core.memory import MemoryError, MemoryScope, MemoryService, MemoryWriter
from core.skills import HUMAN_WRITER, SkillAuthoringService, SkillReference, SkillWriter

AGENT_ID = "coder"
REVIEW = "run-review"
OTHER = "run-other"
REVIEW_MEMORY = MemoryWriter(
    agent_id=AGENT_ID, actor="tool", session_id="fork", run_id=REVIEW, run_kind="reflection"
)
REVIEW_SKILLS = SkillWriter(
    actor="reflection", session_id="fork", run_id=REVIEW, run_kind="reflection"
)
OTHER_MEMORY = MemoryWriter(agent_id=AGENT_ID, actor="tool", run_id=OTHER, run_kind="user")
OTHER_SKILLS = SkillWriter(actor="agent", run_id=OTHER, run_kind="user")
PERSON = MemoryWriter(agent_id=AGENT_ID, actor="rpc")


class _Agent:
    def __init__(self, tmp_path: Path) -> None:
        agents = tmp_path / "agents"
        (agents / AGENT_ID).mkdir(parents=True)
        self.workspace = agents / AGENT_ID / "workspace"
        self.home = agents / AGENT_ID / "skills"
        self.memory = MemoryService(history_root=agents)
        self.skills = SkillAuthoringService()
        self.running: set[str] = set()
        self.learning = LearningChanges(
            memory=self.memory,
            skills=self.skills,
            skill_home=lambda agent_id: self.home,
            run_active=self.running.__contains__,
        )

    def files(self) -> dict[str, bytes]:
        roots = (self.workspace, self.home)
        return {
            str(path.relative_to(root.parent)): path.read_bytes()
            for root in roots
            if root.exists()
            for path in sorted(root.rglob("*"))
            if path.is_file()
        }

    def skill_files(self) -> dict[str, bytes]:
        return {name: data for name, data in self.files().items() if name.startswith("skills")}

    def entries(self) -> dict[MemoryScope, list[str]]:
        return self.memory.entries_at(self.workspace, AGENT_ID, None)


def skill_document(name: str, body: str = "# Steps\n") -> str:
    return f"---\nname: {name}\ndescription: Do {name}.\n---\n\n{body}"


@pytest.fixture
def agent(tmp_path: Path) -> _Agent:
    agent = _Agent(tmp_path)
    agent.memory.add_entry(agent.workspace, "agent", "Uses pytest.", writer=OTHER_MEMORY)
    agent.skills.create(agent.home, "notes", skill_document("notes"), writer=OTHER_SKILLS)
    agent.skills.create(agent.home, "old", skill_document("old"), writer=OTHER_SKILLS)
    return agent


def review(agent: _Agent) -> None:
    """What one combined review changes: two Memory entries and three Skills."""
    agent.memory.add_entry(agent.workspace, "user", "Prefers " + "x" * 200, writer=REVIEW_MEMORY)
    agent.memory.replace_matching(
        agent.workspace, "agent", "pytest", "Uses pytest with xdist.", writer=REVIEW_MEMORY
    )
    agent.skills.create(agent.home, "deploy", skill_document("deploy"), writer=REVIEW_SKILLS)
    agent.skills.write_file(
        agent.home, "deploy", "references/hosts.md", "hosts\n", writer=REVIEW_SKILLS
    )
    agent.skills.edit(
        agent.home, "notes", skill_document("notes", "# Better\n"), writer=REVIEW_SKILLS
    )
    agent.skills.delete(
        agent.home,
        "old",
        writer=REVIEW_SKILLS,
        absorbed_into="deploy",
        followed=[SkillReference("cron", "job-1", "Daily report")],
    )


def test_the_changes_of_a_run_come_from_both_histories(agent: _Agent) -> None:
    review(agent)
    agent.memory.add_entry(agent.workspace, "agent", "Unrelated.", writer=OTHER_MEMORY)

    changes = agent.learning.of_run(AGENT_ID, REVIEW)

    assert changes.to_dict() == {
        "agent_id": AGENT_ID,
        "run_id": REVIEW,
        "summary": {"memory": 2, "skills": 3, "undone": False},
        "changes": [
            {
                "store": "memory",
                "kind": "added",
                "revisions": [2],
                "undone": False,
                "scope": "user",
                "text": "Prefers " + "x" * 151 + "…",
            },
            {
                "store": "memory",
                "kind": "replaced",
                "revisions": [3],
                "undone": False,
                "scope": "agent",
                "text": "Uses pytest with xdist.",
            },
            {
                "store": "skill",
                "kind": "created",
                "revisions": [3, 4],
                "undone": False,
                "skill": "deploy",
                "files": ["SKILL.md", "references/hosts.md"],
            },
            {
                "store": "skill",
                "kind": "changed",
                "revisions": [5],
                "undone": False,
                "skill": "notes",
                "files": ["SKILL.md"],
            },
            {
                "store": "skill",
                "kind": "archived",
                "revisions": [6],
                "undone": False,
                "skill": "old",
                "files": [],
                "absorbed_into": "deploy",
                "followed": [{"kind": "cron", "id": "job-1", "name": "Daily report"}],
            },
        ],
    }
    summaries = agent.learning.summaries(AGENT_ID, [REVIEW, OTHER, "run-without-changes"])
    assert {run: summary.to_dict() for run, summary in summaries.items()} == {
        REVIEW: {"memory": 2, "skills": 3, "undone": False},
        OTHER: {"memory": 2, "skills": 2, "undone": False},
        "run-without-changes": {"memory": 0, "skills": 0, "undone": False},
    }


def test_the_changes_of_a_run_are_its_net_effect(agent: _Agent) -> None:
    tidy = MemoryWriter(agent_id=AGENT_ID, actor="tool", run_id="run-tidy", run_kind="reflection")
    tidy_skills = SkillWriter(actor="reflection", run_id="run-tidy", run_kind="reflection")
    # The User Memory changes cancel out, and so does the pytest wording.
    agent.memory.add_entry(agent.workspace, "user", "Temp note.", writer=tidy)
    agent.memory.replace_matching(
        agent.workspace, "agent", "pytest", "Uses pytest daily.", writer=tidy
    )
    agent.memory.replace_matching(agent.workspace, "agent", "daily", "Uses pytest.", writer=tidy)
    agent.memory.remove_matching(agent.workspace, "user", "Temp note.", writer=tidy)
    agent.memory.add_entry(agent.workspace, "agent", "Draft.", writer=tidy)
    agent.memory.replace_matching(agent.workspace, "agent", "Draft", "Uses uv.", writer=tidy)
    agent.skills.edit(agent.home, "notes", skill_document("notes", "# Tidy\n"), writer=tidy_skills)

    changes = agent.learning.of_run(AGENT_ID, "run-tidy")

    assert [(c.store, c.kind, c.text or c.skill) for c in changes.changes] == [
        ("memory", "added", "Uses uv."),
        ("skill", "changed", "notes"),
    ]
    assert changes.summary.to_dict() == {"memory": 1, "skills": 1, "undone": False}
    result = agent.learning.undo(AGENT_ID, "run-tidy", workspace=agent.workspace, actor="rpc")
    assert result.changes.summary.to_dict() == {"memory": 1, "skills": 1, "undone": True}
    assert agent.entries() == {"user": [], "agent": ["Uses pytest."]}


def test_undo_takes_back_every_change_of_the_run_as_a_person(agent: _Agent) -> None:
    before = agent.skill_files()
    review(agent)
    agent.memory.add_entry(agent.workspace, "agent", "Unrelated.", writer=OTHER_MEMORY)
    files = agent.files()
    agent.running.add(REVIEW)

    # A Run still running may change more, so it cannot be undone yet.
    with pytest.raises(LearningRunActiveError, match="still running"):
        agent.learning.undo(AGENT_ID, REVIEW, workspace=agent.workspace, actor="rpc")
    assert agent.files() == files
    agent.running.clear()
    result = agent.learning.undo(AGENT_ID, REVIEW, workspace=agent.workspace, actor="rpc")

    assert (result.memory_changed, result.skills_changed) == (True, True)
    assert result.changes.summary.to_dict() == {"memory": 2, "skills": 3, "undone": True}
    assert all(change.undone for change in result.changes.changes)
    assert agent.entries() == {"user": [], "agent": ["Uses pytest.", "Unrelated."]}
    # Every Skill package is back as it was; the archived one returned home.
    assert agent.skill_files() == before
    memory_reverts = [r for r in agent.memory.recorded_revisions(AGENT_ID) if r.kind == "revert"]
    assert sorted((r.scope, r.actor, r.reverts) for r in memory_reverts) == [
        ("agent", "rpc", (3,)),
        ("user", "rpc", (2,)),
    ]
    skill_reverts = [r for r in agent.skills.recorded_revisions(agent.home) if r.kind == "revert"]
    assert sorted((r.actor, r.reverts) for r in skill_reverts) == [
        ("human", (3,)),
        ("human", (4,)),
        ("human", (5,)),
        ("human", (6,)),
    ]
    # Nothing is left to take back.
    again = agent.learning.undo(AGENT_ID, REVIEW, workspace=agent.workspace, actor="rpc")
    assert (again.memory_changed, again.skills_changed) == (False, False)
    assert agent.learning.of_run(AGENT_ID, OTHER).summary.undone is False


def _replace_entry_later(agent: _Agent) -> None:
    agent.memory.replace_matching(
        agent.workspace, "agent", "xdist", "Uses pytest with 4 workers.", writer=PERSON
    )


def _remove_entry_later(agent: _Agent) -> None:
    agent.memory.remove_matching(agent.workspace, "user", "Prefers", writer=PERSON)


def _edit_memory_by_hand(agent: _Agent) -> None:
    path = agent.workspace / "MEMORY.md"
    path.write_text("- Uses pytest with xdist, edited.\n", encoding="utf-8")


def _change_skill_later(agent: _Agent) -> None:
    agent.skills.write_file(agent.home, "deploy", "references/hosts.md", "b\n", writer=HUMAN_WRITER)


@pytest.mark.parametrize(
    ("change_later", "expected"),
    [
        pytest.param(
            _replace_entry_later,
            {
                "store": "memory",
                "revision": 3,
                "later": "rpc",
                "scope": "agent",
                "text": "Uses pytest with xdist.",
            },
            id="memory-entry-replaced",
        ),
        pytest.param(
            _remove_entry_later,
            {"store": "memory", "revision": 2, "later": "rpc", "scope": "user"},
            id="memory-entry-already-removed",
        ),
        pytest.param(
            _edit_memory_by_hand,
            {"store": "memory", "revision": 3, "later": "external", "scope": "agent"},
            id="memory-file-edited-outside-vbot",
        ),
        pytest.param(
            _change_skill_later,
            {"store": "skill", "revision": 4, "later": "human", "skill": "deploy"},
            id="skill-file-changed",
        ),
    ],
)
def test_undo_refuses_when_a_later_change_touched_a_change_of_the_run(
    agent: _Agent, change_later: Callable[[_Agent], None], expected: dict[str, object]
) -> None:
    review(agent)
    change_later(agent)
    files = agent.files()

    with pytest.raises(LearningUndoConflictError) as refused:
        agent.learning.undo(AGENT_ID, REVIEW, workspace=agent.workspace, actor="rpc")

    facts = refused.value.to_dict()
    facts["later"] = facts["later"]["actor"]
    assert {key: facts[key] for key in expected} == expected
    assert "Nothing was undone" in str(refused.value)
    assert agent.files() == files
    assert not [r for r in agent.memory.recorded_revisions(AGENT_ID) if r.kind == "revert"]
    assert not [r for r in agent.skills.recorded_revisions(agent.home) if r.kind == "revert"]


def _fail_the_memory_revert(patch: pytest.MonkeyPatch, agent: _Agent) -> None:
    def fail(*args: object, **kwargs: object) -> None:
        raise MemoryError("disk full")

    patch.setattr(agent.memory, "revert", fail)


def _fail_memory_writes_after_the_first(patch: pytest.MonkeyPatch, agent: _Agent) -> None:
    # USER.md is reverted, then writing MEMORY.md and restoring USER.md fail.
    write = memory_module.atomic_write_bytes
    writes: list[Path] = []

    def atomic_write_bytes(path: Path, data: bytes, **kwargs: Any) -> None:
        writes.append(path)
        if len(writes) > 1:
            raise OSError(errno.ENOSPC, "No space left on device")
        write(path, data, **kwargs)

    patch.setattr(memory_module, "atomic_write_bytes", atomic_write_bytes)


def _fail_skill_writes(patch: pytest.MonkeyPatch, agent: _Agent) -> None:
    # The Skill revert fails and cannot take its own steps back either.
    def atomic_write_bytes(*args: object, **kwargs: object) -> None:
        raise OSError(errno.ENOSPC, "No space left on device")

    patch.setattr(skill_revert_module, "atomic_write_bytes", atomic_write_bytes)


@pytest.mark.parametrize(
    ("break_writes", "message", "user_memory_undone"),
    [
        pytest.param(
            _fail_the_memory_revert,
            "The undo failed and nothing was changed: disk full",
            False,
            id="memory-write-fails",
        ),
        pytest.param(
            _fail_memory_writes_after_the_first,
            r"The undo stopped part-way: the User Memory changes were undone, the other Memory "
            r"changes were not \(.*No space left on device\)\. The Skill changes were not "
            r"undone\. Undo again to finish\.",
            True,
            id="memory-left-part-way",
        ),
        pytest.param(
            _fail_skill_writes,
            r"The undo failed part-way\. The revert failed and could not be undone completely "
            r"\(.*No space left on device\)\. Check Skill notes\. The Memory changes were "
            r"not undone\.",
            False,
            id="skill-steps-not-taken-back",
        ),
    ],
)
def test_an_undo_that_fails_while_writing_says_what_changed_and_can_run_again(
    agent: _Agent,
    monkeypatch: pytest.MonkeyPatch,
    break_writes: Callable[[pytest.MonkeyPatch, _Agent], None],
    message: str,
    user_memory_undone: bool,
) -> None:
    review(agent)
    skill_files = agent.skill_files()
    entries = agent.entries()

    with monkeypatch.context() as patch:
        break_writes(patch, agent)
        with pytest.raises(LearningUndoFailedError) as failed:
            agent.learning.undo(AGENT_ID, REVIEW, workspace=agent.workspace, actor="rpc")

    assert re.fullmatch(message, str(failed.value))
    assert agent.skill_files() == skill_files
    assert agent.entries() == ({**entries, "user": []} if user_memory_undone else entries)
    assert agent.learning.of_run(AGENT_ID, REVIEW).summary.undone is False
    # What the failed attempt left recorded belongs to the review itself, so it
    # never blocks undoing the rest.
    result = agent.learning.undo(AGENT_ID, REVIEW, workspace=agent.workspace, actor="rpc")
    assert result.changes.summary.undone is True
    assert not (agent.home / "deploy").exists()
