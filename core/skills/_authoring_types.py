"""Skill writers, authoring results, protection and mutation errors."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from core.skills._history import BACKGROUND_ACTORS, SkillActor
from core.skills._packages import MAX_DOWNLOAD_BYTES
from core.utils.errors import VBotError

PROVENANCE_SOURCE_KEY = "source"


SKILL_ARCHIVE_MAX_BYTES = MAX_DOWNLOAD_BYTES


# ``metadata.vbot.author`` stamped into a written ``SKILL.md``.
SkillAuthor = Literal["agent", "human"]


SkillFileChangeKind = Literal["created", "updated", "deleted"]


SkillProtection = Literal["pinned", "unknown"]


class SkillAuthoringError(VBotError):
    """Raised when a Skill write fails validation or path confinement."""

    def __init__(self, message: str, *, diagnostics: Sequence[str] | None = None) -> None:
        super().__init__(message)
        self.diagnostics: list[str] = list(diagnostics) if diagnostics else [message]


class SkillProtectedError(SkillAuthoringError):
    """A background writer tried to change a Skill it may not change.

    ``reason`` is ``pinned`` (the user pinned it) or ``unknown`` (its history
    cannot be read, so whether the user pinned it is unknown).
    """

    def __init__(self, skill_name: str, reason: SkillProtection) -> None:
        explanation = {
            "pinned": "the user pinned it",
            "unknown": "its history cannot be read",
        }[reason]
        super().__init__(
            f"Skill '{skill_name}' is protected from background changes: {explanation}."
        )
        self.skill_name = skill_name
        self.reason = reason


class SkillMergeAccessError(SkillAuthoringError):
    """A merge would remove access to the instructions it is meant to preserve.

    Each blocker names an Agent, its optional Project Context, and the reason.
    Names are display names; this exception does not persist references.
    """

    def __init__(self, blocked: Sequence[tuple[str, str | None, str]]) -> None:
        super().__init__("The merged Skill would not be accessible to its previous users.")
        self.blocked = tuple(blocked)


class SkillRevertConflictError(SkillAuthoringError):
    """A later revision changed the same part of the Skill as a reverted one."""

    def __init__(self, message: str, *, revision: int, later: int, skill_name: str) -> None:
        super().__init__(message)
        self.revision = revision
        self.later = later
        self.skill_name = skill_name


class SkillRevertIncompleteError(SkillAuthoringError):
    """A revert failed while changing packages and could not undo every step.

    ``skill_names`` names the Skills whose packages may be left part-way.
    """

    def __init__(self, message: str, *, skill_names: Sequence[str]) -> None:
        super().__init__(message)
        self.skill_names = tuple(skill_names)


@dataclass(frozen=True)
class SkillWriter:
    """Who performs a Skill write; recorded with its revision.

    ``actor`` is ``human`` (a person through the WebUI, CLI or RPC), ``agent``
    (an attended Agent's Tool call), ``reflection`` or ``librarian`` (background
    Runs). A written ``SKILL.md`` is stamped ``metadata.vbot.author: human`` for
    a person and ``agent`` otherwise.
    """

    actor: SkillActor = "human"
    session_id: str | None = None
    run_id: str | None = None
    run_kind: str | None = None

    @property
    def background(self) -> bool:
        return self.actor in BACKGROUND_ACTORS

    @property
    def author(self) -> SkillAuthor:
        return "human" if self.actor == "human" else "agent"


HUMAN_WRITER = SkillWriter()


@dataclass(frozen=True)
class ArchivedSkill:
    """One package in a home's archive.

    ``available`` is false for an absorbed Skill whose archived files were
    purged: the history still knows where its instructions went.

    ``absorbed_into`` is the Skill that absorbed this one when it was archived.
    ``holder`` is the Skill of the home that holds its instructions now:
    ``absorbed_into`` while that Skill exists, otherwise the Skill that later
    absorbed it, following each later merge; ``holder_since`` is when the
    instructions reached ``holder``. Both are ``None`` when the merges end at a
    Skill that was archived for another reason, or lead back to a Skill they
    already passed.
    """

    archive_id: str
    name: str
    archived_at: str
    reason: str | None
    absorbed_into: str | None
    archived_by: str | None
    origin: str
    description: str
    available: bool = True
    holder: str | None = None
    holder_since: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "archive_id": self.archive_id,
            "name": self.name,
            "archived_at": self.archived_at,
            "reason": self.reason,
            "absorbed_into": self.absorbed_into,
            "archived_by": self.archived_by,
            "origin": self.origin,
            "description": self.description,
        }


# A changed file larger than this reports no text; its change is still reported.
MAX_CHANGE_TEXT_BYTES = 1024 * 1024


@dataclass(frozen=True)
class SkillFileChange:
    """One file a Skill mutation changed, by its package-relative path.

    ``before``/``after`` hold its text with LF line endings; ``None`` means the
    file was absent on that side or is not UTF-8 text.
    """

    path: str
    change: SkillFileChangeKind
    before: str | None
    after: str | None


@dataclass(frozen=True)
class SkillWriteResult:
    """Outcome of one successful direct Skill mutation."""

    name: str
    operation: str
    path: Path
    warnings: list[str] = field(default_factory=list)
    changes: tuple[SkillFileChange, ...] = ()
    # The recorded history revision, ``None`` when nothing was recorded.
    revision: int | None = None
    archive_id: str | None = None
