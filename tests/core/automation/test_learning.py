"""Contracts of the Memory and Skill changes of one Run and their undo."""

from __future__ import annotations

import errno
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

import core.memory._file_backend as memory_module
import core.skills._revert as skill_revert_module
from core.automation import (
    LearningChanges,
    LearningError,
    LearningRunActiveError,
    LearningUndoConflictError,
    LearningUndoError,
    LearningUndoFailedError,
)
from core.memory import MemoryError, MemoryScope, MemoryService, MemoryWriter
from core.skills import (
    HUMAN_WRITER,
    SkillAuthoringError,
    SkillAuthoringService,
    SkillReference,
    SkillWriter,
)

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
        self.project_root = tmp_path / "projects"
        self.projects: dict[str, Path] = {}
        self.skill_homes: dict[str, Path] = {}
        self.memory = MemoryService(history_root=agents, project_history_root=self.project_root)
        self.skills = SkillAuthoringService()
        self.running: set[str] = set()
        self.learning = LearningChanges(
            memory=self.memory,
            skills=self.skills,
            skill_home=lambda agent_id: self.home,
            run_active=self.running.__contains__,
            project_memories=lambda: self.projects,
            skill_homes=lambda: self.skill_homes,
        )

    def shared_home(self, owner_id: str = "owner") -> Path:
        home = self.home.parent.parent / owner_id / "skills"
        self.skill_homes[owner_id] = home
        return home

    def project(self, project_id: str = "sample") -> Path:
        path = self.project_root / project_id
        path.mkdir(parents=True, exist_ok=True)
        self.projects[project_id] = path
        return path

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
    project = agent.project()
    agent.memory.add_entry(
        project,
        "project",
        "Changes need user review.",
        writer=MemoryWriter(agent_id=AGENT_ID, project_id="sample", run_id=REVIEW),
    )
    # A shared history keeps other Agents' contributions separate, even if a
    # caller supplied the same Run label outside the globally unique Run API.
    agent.memory.add_entry(
        project,
        "project",
        "Another Agent's change.",
        writer=MemoryWriter(agent_id="other-agent", project_id="sample", run_id=REVIEW),
    )

    changes = agent.learning.of_run(AGENT_ID, REVIEW)

    assert changes.to_dict() == {
        "agent_id": AGENT_ID,
        "run_id": REVIEW,
        "summary": {"memory": 3, "skills": 3, "undone": False},
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
                "store": "memory",
                "kind": "added",
                "revisions": [1],
                "undone": False,
                "scope": "project",
                "text": "Changes need user review.",
                "project_id": "sample",
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
        REVIEW: {"memory": 3, "skills": 3, "undone": False},
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
    project = agent.project()
    agent.memory.add_entry(
        project,
        "project",
        "Changes need user review.",
        writer=MemoryWriter(agent_id=AGENT_ID, project_id="sample", run_id=REVIEW),
    )
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
    assert result.changes.summary.to_dict() == {"memory": 3, "skills": 3, "undone": True}
    assert all(change.undone for change in result.changes.changes)
    assert agent.entries() == {"user": [], "agent": ["Uses pytest.", "Unrelated."]}
    assert agent.memory.list_entries(project, "project") == []
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


def test_shared_skill_changes_keep_their_owner_across_reload_and_undo(agent: _Agent) -> None:
    review(agent)
    shared = agent.shared_home()
    agent.skills.create(
        shared, "deploy", skill_document("deploy", "Original."), writer=OTHER_SKILLS
    )
    agent.skills.edit(
        shared,
        "deploy",
        skill_document("deploy", "Learned."),
        writer=replace(REVIEW_SKILLS, subject_created_at="original"),
    )
    # Learning reads all registered homes, even when a share was withdrawn or
    # the owner renamed since the Run; revision numbers belong to each home.
    renamed = shared.parent.with_name("renamed")
    shared.parent.rename(renamed)
    agent.skill_homes = {"renamed": renamed / "skills"}
    fresh = LearningChanges(
        memory=agent.memory,
        skills=SkillAuthoringService(),
        skill_home=lambda agent_id: agent.home,
        skill_homes=lambda: agent.skill_homes,
        run_active=lambda run_id: False,
        subject_created_at=lambda agent_id: "original",
    )

    changes = fresh.of_run(AGENT_ID, REVIEW)
    same_name = [change for change in changes.changes if change.skill == "deploy"]
    assert [(change.skill_owner_id, change.revisions) for change in same_name] == [
        (None, (3, 4)),
        ("renamed", (2,)),
    ]
    assert same_name[1].to_dict()["skill_owner_id"] == "renamed"
    assert fresh.summaries(AGENT_ID, [REVIEW])[REVIEW].skills == 4

    result = fresh.undo(AGENT_ID, REVIEW, workspace=agent.workspace)
    assert result.changes.summary.undone
    assert not (agent.home / "deploy").exists()
    assert "Original." in (renamed / "skills" / "deploy" / "SKILL.md").read_text(encoding="utf-8")
    assert fresh.undo(AGENT_ID, REVIEW, workspace=agent.workspace).skills_changed is False


@pytest.mark.parametrize("operation", ["read", "summaries", "undo"])
def test_learning_requires_the_runs_actual_subject(agent: _Agent, operation: str) -> None:
    review(agent)
    shared = agent.shared_home()
    agent.skills.create(
        shared,
        "deploy",
        skill_document("deploy"),
        writer=replace(REVIEW_SKILLS, subject_created_at="original"),
    )
    identities = {AGENT_ID: "original", "different": "another"}
    learning = LearningChanges(
        memory=agent.memory,
        skills=agent.skills,
        skill_home=lambda agent_id: agent.home,
        skill_homes=lambda: agent.skill_homes,
        run_active=lambda run_id: False,
        subject_created_at=identities.get,
    )
    before = agent.files()
    shared_before = (shared / "deploy" / "SKILL.md").read_bytes()
    for subject in ("different", AGENT_ID):
        identities[AGENT_ID] = "replacement"
        with pytest.raises(LearningError):
            if operation == "read":
                learning.of_run(subject, REVIEW)
            elif operation == "summaries":
                learning.summaries(subject, [REVIEW])
            else:
                learning.undo(subject, REVIEW, workspace=agent.workspace)
    identities[AGENT_ID] = "original"
    assert learning.of_run(AGENT_ID, REVIEW).summary.skills == 4
    assert agent.files() == before
    assert (shared / "deploy" / "SKILL.md").read_bytes() == shared_before


@pytest.mark.parametrize("proof", ["own_only", "unknown", "personal_memory", "validated_pass"])
def test_legacy_shared_changes_need_evidence_before_any_undo(agent: _Agent, proof: str) -> None:
    agent.skills.edit(
        agent.home, "notes", skill_document("notes", "Learned."), writer=REVIEW_SKILLS
    )
    shared = agent.shared_home()
    if proof != "own_only":
        agent.skills.create(shared, "deploy", skill_document("deploy"), writer=REVIEW_SKILLS)
    if proof == "personal_memory":
        agent.memory.add_entry(agent.workspace, "agent", "Learned fact.", writer=REVIEW_MEMORY)
    learning = LearningChanges(
        memory=agent.memory,
        skills=agent.skills,
        skill_home=lambda agent_id: agent.home,
        skill_homes=lambda: agent.skill_homes,
        run_active=lambda run_id: False,
        subject_created_at=lambda agent_id: "original",
    )
    before = agent.files()
    if proof in {"unknown", "validated_pass"}:
        for read in (
            lambda: learning.of_run(AGENT_ID, REVIEW),
            lambda: learning.summaries(AGENT_ID, [REVIEW]),
            lambda: learning.undo(AGENT_ID, REVIEW, workspace=agent.workspace),
        ):
            with pytest.raises(LearningError):
                read()
        assert agent.files() == before
        assert (shared / "deploy" / "SKILL.md").is_file()
    if proof == "unknown":
        return
    admissions: list[str] = []
    result = learning.undo(
        AGENT_ID,
        REVIEW,
        workspace=agent.workspace,
        validate=(lambda: admissions.append("exact_pass")) if proof == "validated_pass" else None,
    )
    assert result.changes.summary.undone
    assert "Learned." not in (agent.home / "notes" / "SKILL.md").read_text(encoding="utf-8")
    assert not (shared / "deploy").exists()
    assert admissions == (["exact_pass"] if proof == "validated_pass" else [])


def test_explicit_skill_revisions_join_run_undo_in_their_own_history(agent: _Agent) -> None:
    aged = agent.skills.delete(
        agent.home, "old", writer=SkillWriter(actor="librarian", run_kind="librarian")
    )
    agent.skills.edit(
        agent.home, "notes", skill_document("notes", "Learned."), writer=REVIEW_SKILLS
    )
    shared = agent.shared_home()
    agent.skills.create(
        shared, "deploy", skill_document("deploy", "Original."), writer=OTHER_SKILLS
    )
    agent.skills.create(shared, "other", skill_document("other"), writer=OTHER_SKILLS)
    learned = agent.skills.edit(
        shared, "deploy", skill_document("deploy", "Learned."), writer=REVIEW_SKILLS
    )
    assert aged.revision == learned.revision == 3
    revisions = {AGENT_ID: [int(aged.revision)]}

    result = agent.learning.undo(
        AGENT_ID, REVIEW, workspace=agent.workspace, skill_revisions=revisions
    )

    assert result.changes.summary.undone
    assert (agent.home / "old" / "SKILL.md").is_file()
    assert "Original." in (shared / "deploy" / "SKILL.md").read_text(encoding="utf-8")
    assert not agent.learning.undo(
        AGENT_ID, REVIEW, workspace=agent.workspace, skill_revisions=revisions
    ).skills_changed
    # Explicit revisions never disappear just because their owner left the catalog.
    with pytest.raises(LearningUndoError):
        agent.learning.undo(
            AGENT_ID, REVIEW, workspace=agent.workspace, skill_revisions={"missing": [3]}
        )


@pytest.mark.parametrize("failure_store", ["shared", "memory"])
def test_undo_failure_restores_every_completed_skill_home(
    agent: _Agent, monkeypatch: pytest.MonkeyPatch, failure_store: str
) -> None:
    review(agent)
    shared = agent.shared_home()
    agent.skills.create(
        shared, "deploy", skill_document("deploy", "Original."), writer=OTHER_SKILLS
    )
    agent.skills.edit(shared, "deploy", skill_document("deploy", "Learned."), writer=REVIEW_SKILLS)
    agent.skills.create(agent.home, "aged", skill_document("aged"), writer=OTHER_SKILLS)
    aged = agent.skills.delete(agent.home, "aged", writer=SkillWriter(actor="librarian"))
    revisions = {AGENT_ID: [int(aged.revision or 0)]}
    own_before = agent.skill_files()
    shared_before = (shared / "deploy" / "SKILL.md").read_bytes()
    revert = agent.skills.revert

    def fail_shared(home: Path, revisions: list[int], **kwargs: Any) -> Any:
        if home == shared:
            raise SkillAuthoringError("controlled shared-home failure")
        return revert(home, revisions, **kwargs)

    with monkeypatch.context() as patch:
        if failure_store == "shared":
            patch.setattr(agent.skills, "revert", fail_shared)
        else:
            _fail_the_memory_revert(patch, agent)
        with pytest.raises(LearningError):
            agent.learning.undo(
                AGENT_ID, REVIEW, workspace=agent.workspace, skill_revisions=revisions
            )

    assert agent.skill_files() == own_before
    assert (shared / "deploy" / "SKILL.md").read_bytes() == shared_before
    assert not agent.learning.of_run(AGENT_ID, REVIEW).summary.undone
    assert agent.learning.undo(
        AGENT_ID, REVIEW, workspace=agent.workspace, skill_revisions=revisions
    ).changes.summary.undone
    assert (agent.home / "aged" / "SKILL.md").is_file()


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


def _change_project_memory_later(agent: _Agent) -> None:
    project = agent.project()
    agent.memory.add_entry(
        project,
        "project",
        "Changes need user review.",
        writer=MemoryWriter(agent_id=AGENT_ID, project_id="sample", run_id=REVIEW),
    )
    agent.memory.replace_matching(
        project,
        "project",
        "Changes need",
        "Changes need a separate review Session.",
        writer=MemoryWriter(agent_id="other-agent", project_id="sample", run_id=REVIEW),
    )


def _change_shared_skill_later(agent: _Agent) -> None:
    home = agent.shared_home()
    agent.skills.create(home, "deploy", skill_document("deploy"), writer=REVIEW_SKILLS)
    agent.skills.edit(
        home, "deploy", skill_document("deploy", "Later user edit."), writer=HUMAN_WRITER
    )


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
            _change_project_memory_later,
            {
                "store": "memory",
                "revision": 1,
                "later": "internal",
                "scope": "project",
                "project_id": "sample",
            },
            id="project-entry-changed-by-another-agent",
        ),
        pytest.param(
            _change_skill_later,
            {"store": "skill", "revision": 4, "later": "human", "skill": "deploy"},
            id="skill-file-changed",
        ),
        pytest.param(
            _change_shared_skill_later,
            {
                "store": "skill",
                "revision": 1,
                "later": "human",
                "skill": "deploy",
                "skill_owner_id": "owner",
            },
            id="shared-skill-changed-by-owner",
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
    assert not [
        r
        for project_id in agent.projects
        for r in agent.memory.recorded_revisions(project_id=project_id)
        if r.kind == "revert"
    ]


def _fail_the_memory_revert(patch: pytest.MonkeyPatch, agent: _Agent) -> None:
    def fail(*args: object, **kwargs: object) -> None:
        raise MemoryError("disk full")

    patch.setattr(agent.memory, "revert", fail)


def _fail_the_second_project_revert(patch: pytest.MonkeyPatch, agent: _Agent) -> None:
    for project_id in ("first", "second"):
        agent.memory.add_entry(
            agent.project(project_id),
            "project",
            "Changes need user review.",
            writer=MemoryWriter(agent_id=AGENT_ID, project_id=project_id, run_id=REVIEW),
        )
    revert = agent.memory.revert

    def fail(workspace: Path, revisions: list[int], **kwargs: Any) -> Any:
        if kwargs["writer"].project_id == "second":
            raise MemoryError("disk full")
        return revert(workspace, revisions, **kwargs)

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
    # Content writes fail before changing bytes; the prior archive restore can
    # still be undone without writing those unchanged files again.
    def atomic_write_bytes(*args: object, **kwargs: object) -> None:
        raise OSError(errno.ENOSPC, "No space left on device")

    patch.setattr(skill_revert_module, "atomic_write_bytes", atomic_write_bytes)


@pytest.mark.parametrize(
    ("break_writes", "error_type", "user_memory_undone"),
    [
        pytest.param(
            _fail_the_memory_revert,
            LearningUndoFailedError,
            False,
            id="memory-write-fails",
        ),
        pytest.param(
            _fail_the_second_project_revert,
            LearningUndoFailedError,
            False,
            id="project-write-fails-restores-personal-and-earlier-project",
        ),
        pytest.param(
            _fail_memory_writes_after_the_first,
            LearningUndoFailedError,
            True,
            id="memory-left-part-way",
        ),
        pytest.param(
            _fail_skill_writes,
            LearningUndoError,
            False,
            id="skill-write-fails-before-effect",
        ),
    ],
)
def test_an_undo_that_fails_while_writing_says_what_changed_and_can_run_again(
    agent: _Agent,
    monkeypatch: pytest.MonkeyPatch,
    break_writes: Callable[[pytest.MonkeyPatch, _Agent], None],
    error_type: type[LearningError],
    user_memory_undone: bool,
) -> None:
    review(agent)
    skill_files = agent.skill_files()
    entries = agent.entries()

    with monkeypatch.context() as patch:
        break_writes(patch, agent)
        with pytest.raises(error_type):
            agent.learning.undo(AGENT_ID, REVIEW, workspace=agent.workspace, actor="rpc")

    assert agent.skill_files() == skill_files
    assert agent.entries() == ({**entries, "user": []} if user_memory_undone else entries)
    assert all(
        [entry.content for entry in agent.memory.list_entries(path, "project")]
        == ["Changes need user review."]
        for path in agent.projects.values()
    )
    assert agent.learning.of_run(AGENT_ID, REVIEW).summary.undone is False
    # What the failed attempt left recorded belongs to the review itself, so it
    # never blocks undoing the rest.
    result = agent.learning.undo(AGENT_ID, REVIEW, workspace=agent.workspace, actor="rpc")
    assert result.changes.summary.undone is True
    assert not (agent.home / "deploy").exists()
