"""Contracts of a writable Skill home's history, archive, pins and background rules."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest

from core.skills._history import SkillHistory
from core.skills.authoring import (
    HUMAN_WRITER,
    SkillAuthoringError,
    SkillAuthoringService,
    SkillProtectedError,
    SkillRevertConflictError,
    SkillWriter,
)

AGENT = SkillWriter(actor="agent", session_id="s-1", run_id="r-1", run_kind="user")
REFLECTION = SkillWriter(actor="reflection", session_id="s-2", run_id="r-2", run_kind="reflection")


def skill_document(name: str = "demo", body: str = "# Demo\n", author: str | None = None) -> str:
    metadata = f"metadata:\n  vbot:\n    author: {author}\n" if author else ""
    return f"---\nname: {name}\ndescription: Do a demo task.\n{metadata}---\n\n{body}"


@pytest.fixture
def service() -> SkillAuthoringService:
    return SkillAuthoringService()


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path / "skills"


def history_lines(root: Path) -> list[dict[str, Any]]:
    content = (root.parent / "skill-history.jsonl").read_text(encoding="utf-8")
    return [json.loads(line) for line in content.splitlines()]


def summary(service: SkillAuthoringService, root: Path, name: str | None = None) -> list[tuple]:
    return [
        (revision.id, revision.skill, revision.kind, revision.actor)
        for revision in service.history(root, name)
    ]


def test_every_write_records_its_writer_and_files(
    service: SkillAuthoringService, root: Path
) -> None:
    service.create(root, "demo", skill_document(), writer=AGENT)
    service.write_file(root, "demo", "references/notes.md", "notes\r\n", writer=AGENT)
    service.rewrite(root, "demo", "SKILL.md", lambda text: text + "More.\n", writer=HUMAN_WRITER)
    result = service.remove_file(root, "demo", "references/notes.md", writer=HUMAN_WRITER)

    assert result.revision == 4
    assert summary(service, root) == [
        (4, "demo", "change", "human"),
        (3, "demo", "change", "human"),
        (2, "demo", "change", "agent"),
        (1, "demo", "create", "agent"),
    ]
    assert summary(service, root, "demo")[:1] == [(4, "demo", "change", "human")]
    assert len(service.history(root, limit=2)) == 2
    first, second, *_ = history_lines(root)
    assert (first["session_id"], first["run_id"], first["run_kind"]) == ("s-1", "r-1", "user")
    # A file change records the hash before, the hash after and the exact text after.
    [created] = second["files"]
    assert (created["path"], created["change"], created["before"]) == (
        "references/notes.md",
        "created",
        None,
    )
    assert created["text"] == "notes\n"
    removed = history_lines(root)[3]["files"][0]
    assert (removed["change"], removed["before"], removed["after"]) == (
        "deleted",
        created["after"],
        None,
    )
    record = service.record(root, "demo")
    assert record is not None
    assert (record.origin, record.pinned, record.changed_by) == ("agent", False, "human")
    assert record.created_at == first["at"]


@pytest.mark.parametrize(
    ("author", "origin"),
    [("agent", "agent"), ("human", "human"), (None, "human")],
    ids=["agent-author", "human-author", "no-author"],
)
def test_a_skill_found_without_history_gets_a_baseline(
    service: SkillAuthoringService, root: Path, author: str | None, origin: str
) -> None:
    package = root / "found"
    package.mkdir(parents=True)
    (package / "SKILL.md").write_text(skill_document("found", author=author), encoding="utf-8")

    records = service.records(root)

    assert records["found"].origin == origin
    assert summary(service, root) == [(1, "found", "baseline", "external")]
    # Origin stays fixed at creation; a later human edit does not change it.
    service.edit(root, "found", skill_document("found"), writer=HUMAN_WRITER)
    assert service.records(root)["found"].origin == origin


def test_changes_outside_vbot_are_recorded_before_the_next_write(
    service: SkillAuthoringService, root: Path
) -> None:
    service.create(root, "demo", skill_document(), writer=AGENT)
    (root / "demo" / "SKILL.md").write_text(skill_document(body="Edited by hand.\n"))
    (root / "demo" / "assets").mkdir()
    (root / "demo" / "assets" / "logo.bin").write_bytes(b"\xff\x00")

    service.write_file(root, "demo", "references/a.md", "a\n", writer=AGENT)

    assert summary(service, root) == [
        (3, "demo", "change", "agent"),
        (2, "demo", "external", "external"),
        (1, "demo", "create", "agent"),
    ]
    external = history_lines(root)[1]["files"]
    assert [(item["path"], item["change"], "text" in item) for item in external] == [
        ("SKILL.md", "updated", True),
        # Binary content is recorded by its hash only.
        ("assets/logo.bin", "created", False),
    ]


@pytest.mark.parametrize(
    "damage",
    [
        pytest.param(b'{"v": 1, "id": "broken"}\nnot json\n', id="invalid-lines"),
        pytest.param(b'{"v":1,"id":2,"at":"', id="incomplete-last-line"),
    ],
)
def test_unreadable_history_lines_are_skipped(
    service: SkillAuthoringService,
    root: Path,
    damage: bytes,
    caplog: pytest.LogCaptureFixture,
) -> None:
    service.create(root, "demo", skill_document(), writer=AGENT)
    with (root.parent / "skill-history.jsonl").open("ab") as handle:
        handle.write(damage)
    SkillHistory._logs.clear()

    with caplog.at_level(logging.WARNING, logger="vbot.skills.history"):
        service.write_file(root, "demo", "references/a.md", "a\n", writer=AGENT)
        SkillHistory._logs.clear()
        assert summary(service, root) == [
            (2, "demo", "change", "agent"),
            (1, "demo", "create", "agent"),
        ]

    assert any("unreadable Skill history lines" in record.message for record in caplog.records)


def test_a_failing_history_never_fails_an_attended_write(
    service: SkillAuthoringService, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service.create(root, "demo", skill_document(author="agent"), writer=AGENT)

    def failing(*_args: object, **_kwargs: object) -> None:
        raise OSError("disk full")

    with monkeypatch.context() as patch:
        patch.setattr(SkillHistory, "append", failing)
        patch.setattr(SkillHistory, "observe", failing)
        written = service.write_file(root, "demo", "references/a.md", "a\n", writer=AGENT)
        # A background writer cannot know the origin, so it is refused.
        with pytest.raises(SkillProtectedError) as refused:
            service.write_file(root, "demo", "references/b.md", "b\n", writer=REFLECTION)

    assert written.revision is None
    assert (root / "demo" / "references" / "a.md").is_file()
    assert refused.value.reason == "unknown"
    # The missed change is noticed as an external one.
    assert summary(service, root)[0] == (2, "demo", "external", "external")


@pytest.mark.parametrize(
    ("setup", "reason"),
    [
        pytest.param("user", "user", id="user-created"),
        pytest.param("pinned", "pinned", id="pinned"),
    ],
)
def test_background_writers_leave_user_and_pinned_skills_alone(
    service: SkillAuthoringService, root: Path, setup: str, reason: str
) -> None:
    writer = HUMAN_WRITER if setup == "user" else AGENT
    service.create(root, "demo", skill_document(), writer=writer)
    if setup == "pinned":
        service.set_pinned(root, "demo", True, writer=HUMAN_WRITER)
    before = (root / "demo" / "SKILL.md").read_bytes()

    for action in (
        lambda: service.edit(root, "demo", skill_document(body="x\n"), writer=REFLECTION),
        lambda: service.write_file(root, "demo", "references/a.md", "a", writer=REFLECTION),
        lambda: service.delete(root, "demo", writer=REFLECTION),
    ):
        with pytest.raises(SkillProtectedError) as refused:
            action()
        assert refused.value.reason == reason

    assert (root / "demo" / "SKILL.md").read_bytes() == before
    # Attended Agents are not limited by pins.
    service.write_file(root, "demo", "references/a.md", "a\n", writer=AGENT)


def test_background_writers_change_skills_agents_created(
    service: SkillAuthoringService, root: Path
) -> None:
    service.create(root, "agent-made", skill_document("agent-made"), writer=AGENT)
    service.create(root, "learned", skill_document("learned"), writer=REFLECTION)

    service.edit(root, "agent-made", skill_document("agent-made", body="New.\n"), writer=REFLECTION)
    service.delete(root, "agent-made", writer=REFLECTION, absorbed_into="learned")

    record = service.record(root, "learned")
    assert record is not None and record.origin == "reflection"
    [archived] = service.archived(root)
    assert (archived.name, archived.reason, archived.absorbed_into, archived.archived_by) == (
        "agent-made",
        "absorbed",
        "learned",
        "reflection",
    )


def test_pins_are_recorded_and_only_people_pin(service: SkillAuthoringService, root: Path) -> None:
    service.create(root, "demo", skill_document(), writer=AGENT)

    pinned = service.set_pinned(root, "demo", True, writer=HUMAN_WRITER)
    unchanged = service.set_pinned(root, "demo", True, writer=HUMAN_WRITER)

    assert (pinned.revision, unchanged.revision) == (2, None)
    assert service.records(root)["demo"].pinned is True
    with pytest.raises(SkillAuthoringError, match="Only a person"):
        service.set_pinned(root, "demo", False, writer=AGENT)
    service.set_pinned(root, "demo", False, writer=HUMAN_WRITER)
    assert summary(service, root)[:2] == [
        (3, "demo", "unpin", "human"),
        (2, "demo", "pin", "human"),
    ]


def test_archived_skills_are_restored_with_their_provenance_or_purged(
    service: SkillAuthoringService, root: Path
) -> None:
    service.create(root, "demo", skill_document(), writer=AGENT)
    service.set_pinned(root, "demo", True, writer=HUMAN_WRITER)
    first = service.delete(root, "demo", writer=HUMAN_WRITER)
    service.create(root, "demo", skill_document(body="Second.\n"), writer=HUMAN_WRITER)

    with pytest.raises(SkillAuthoringError, match="already exists"):
        service.restore(root, str(first.archive_id), writer=HUMAN_WRITER)

    second = service.delete(root, "demo", writer=HUMAN_WRITER)
    restored = service.restore(root, str(first.archive_id), writer=HUMAN_WRITER)

    assert restored.name == "demo"
    assert "Second." not in (root / "demo" / "SKILL.md").read_text(encoding="utf-8")
    record = service.record(root, "demo")
    assert record is not None and (record.origin, record.pinned) == ("agent", True)
    assert [entry.archive_id for entry in service.archived(root)] == [second.archive_id]

    purged = service.purge(root, str(second.archive_id))

    assert (purged.name, service.archived(root)) == ("demo", [])
    with pytest.raises(SkillAuthoringError, match="not found"):
        service.restore(root, str(second.archive_id), writer=HUMAN_WRITER)
    with pytest.raises(SkillAuthoringError, match="Illegal archive id"):
        service.purge(root, "../skills")


@pytest.mark.parametrize(
    ("absorbed_into", "match"),
    [("demo", "cannot absorb itself"), ("missing", "Skill 'missing' not found")],
)
def test_absorbed_into_names_another_skill_of_the_home(
    service: SkillAuthoringService, root: Path, absorbed_into: str, match: str
) -> None:
    service.create(root, "demo", skill_document(), writer=AGENT)

    with pytest.raises(SkillAuthoringError, match=match):
        service.delete(root, "demo", writer=AGENT, absorbed_into=absorbed_into)

    assert (root / "demo" / "SKILL.md").is_file()


def test_archived_skill_finds_an_absorbed_name_after_its_files_are_purged(
    service: SkillAuthoringService, root: Path
) -> None:
    service.create(root, "old", skill_document("old"), writer=AGENT)
    service.create(root, "new", skill_document("new"), writer=AGENT)
    archived = service.delete(root, "old", writer=AGENT, absorbed_into="new")
    service.purge(root, str(archived.archive_id))

    entry = service.archived_skill(root, "old")

    assert entry is not None
    assert (entry.absorbed_into, entry.available) == ("new", False)
    assert service.archived_skill(root, "never") is None


def test_revert_returns_files_to_their_earlier_text(
    service: SkillAuthoringService, root: Path
) -> None:
    notes = root / "demo" / "references" / "a.md"
    service.create(root, "demo", skill_document(), writer=AGENT)
    service.write_file(root, "demo", "references/a.md", "one\n", writer=HUMAN_WRITER)
    notes.write_bytes(b"one\r\n")
    service.write_file(root, "demo", "references/a.md", "two\n", writer=AGENT)
    service.write_file(root, "demo", "references/b.md", "b\n", writer=AGENT)
    assert [revision.kind for revision in service.history(root)][:3] == [
        "change",
        "change",
        "external",
    ]

    [reverted] = service.revert(root, [4], writer=HUMAN_WRITER)

    # The exact earlier bytes come back, CRLF included.
    assert notes.read_bytes() == b"one\r\n"
    assert (reverted.kind, reverted.reverts, reverted.actor) == ("revert", (4,), "human")
    # A revert is a revision of its own and can be reverted.
    service.revert(root, [reverted.id], writer=HUMAN_WRITER)
    assert notes.read_bytes() == b"two\r\n"
    # Reverting the creation of a file removes it.
    service.revert(root, [5], writer=HUMAN_WRITER)
    assert not (root / "demo" / "references" / "b.md").exists()


def test_revert_refuses_to_overwrite_a_later_change_and_changes_nothing(
    service: SkillAuthoringService, root: Path
) -> None:
    service.create(root, "demo", skill_document(), writer=AGENT)
    service.create(root, "other", skill_document("other"), writer=AGENT)
    service.write_file(root, "other", "references/a.md", "first\n", writer=AGENT)
    service.write_file(root, "demo", "references/a.md", "first\n", writer=AGENT)
    service.write_file(root, "demo", "references/a.md", "second\n", writer=AGENT)
    before = len(service.history(root))

    with pytest.raises(SkillRevertConflictError) as conflict:
        service.revert(root, [3, 4], writer=HUMAN_WRITER)

    assert (conflict.value.revision, conflict.value.later) == (4, 5)
    assert "revision 5" in str(conflict.value)
    assert (root / "other" / "references" / "a.md").is_file()
    assert len(service.history(root)) == before
    # Reverting the later change together with it succeeds.
    service.revert(root, [3, 4, 5], writer=HUMAN_WRITER)
    assert not (root / "demo" / "references").exists()
    assert not (root / "other" / "references").exists()


def test_revert_moves_skills_between_the_home_and_the_archive_and_flips_pins(
    service: SkillAuthoringService, root: Path
) -> None:
    service.create(root, "demo", skill_document(), writer=AGENT)
    pinned = service.set_pinned(root, "demo", True, writer=HUMAN_WRITER)
    service.revert(root, [int(pinned.revision or 0)], writer=HUMAN_WRITER)
    assert service.records(root)["demo"].pinned is False
    deleted = service.delete(root, "demo", writer=HUMAN_WRITER)

    service.revert(root, [int(deleted.revision or 0)], writer=HUMAN_WRITER)

    record = service.record(root, "demo")
    assert record is not None and (record.origin, record.pinned) == ("agent", False)
    assert service.archived(root) == []
    # Undoing the creation would discard every later change, so it names the first.
    with pytest.raises(SkillRevertConflictError) as conflict:
        service.revert(root, [1], writer=HUMAN_WRITER)
    assert conflict.value.later == 2
    # Reverting it with every later revision undoes them newest first.
    service.revert(root, [revision.id for revision in service.history(root)], writer=HUMAN_WRITER)
    assert not (root / "demo").exists()
    assert [entry.reason for entry in service.archived(root)] == ["deleted"]


def test_revert_refuses_what_the_history_cannot_restore(
    service: SkillAuthoringService, root: Path
) -> None:
    package = root / "found"
    (package / "assets").mkdir(parents=True)
    (package / "SKILL.md").write_text(skill_document("found"), encoding="utf-8")
    (package / "assets" / "data.bin").write_bytes(b"\xff\x00")
    service.records(root)
    (package / "assets" / "data.bin").write_bytes(b"\xff\x01")
    service.edit(root, "found", skill_document("found", body="x\n"), writer=HUMAN_WRITER)

    with pytest.raises(SkillAuthoringError, match="first recorded state"):
        service.revert(root, [1], writer=HUMAN_WRITER)
    with pytest.raises(SkillAuthoringError, match="no earlier text of assets/data.bin"):
        service.revert(root, [2], writer=HUMAN_WRITER)
    with pytest.raises(SkillAuthoringError, match="does not exist"):
        service.revert(root, [99], writer=HUMAN_WRITER)
