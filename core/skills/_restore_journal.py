"""The durable intent for a single interrupted Skill restore in one home."""

from __future__ import annotations

import json
import stat
from dataclasses import asdict, dataclass
from pathlib import Path
from uuid import UUID

from core.skills._authoring_types import SkillAuthoringError, SkillWriter
from core.skills._history import SKILL_ACTORS
from core.utils.atomic import atomic_write_bytes, read_replaced_text
from core.utils.file_status import is_link_status, stat_or_none


@dataclass(frozen=True)
class RestoreIntent:
    transaction: str
    archive_id: str
    name: str
    device: int
    inode: int
    origin: str
    created_at: str | None
    pinned: bool
    writer: SkillWriter

    def matches(self, path: Path) -> bool:
        status = stat_or_none(path, follow_symlinks=False)
        return (
            status is not None
            and not is_link_status(status)
            and stat.S_ISDIR(status.st_mode)
            and (status.st_dev, status.st_ino) == (self.device, self.inode)
        )


def intent_path(home: Path) -> Path:
    return home.parent / "skill-restore.json"


def require_idle(home: Path, *names: str) -> None:
    intent = read_intent(home)
    if intent is not None and (not names or intent.name in names):
        raise SkillAuthoringError(
            f"Skill '{intent.name}' has a pending restore. "
            f"Ask the user to retry restoring archive '{intent.archive_id}'."
        )


def write_intent(home: Path, intent: RestoreIntent) -> None:
    payload = {"format_version": 1, **asdict(intent)}
    atomic_write_bytes(
        intent_path(home), (json.dumps(payload) + "\n").encode("utf-8"), replace_existing=False
    )


def read_intent(home: Path) -> RestoreIntent | None:
    path = intent_path(home)
    try:
        status = stat_or_none(path, follow_symlinks=False)
        if status is None:
            return None
        if is_link_status(status) or not stat.S_ISREG(status.st_mode):
            raise ValueError("expected a regular file")
        data = json.loads(read_replaced_text(path))
        if not isinstance(data, dict) or type(data.get("format_version")) is not int:
            raise ValueError("invalid restore journal")
        if data.pop("format_version") != 1:
            raise ValueError("unsupported restore journal version")
        writer = SkillWriter(**data.pop("writer"))
        if writer.actor not in SKILL_ACTORS or any(
            value is not None and not isinstance(value, str)
            for value in (
                writer.session_id,
                writer.run_id,
                writer.run_kind,
                writer.subject_created_at,
            )
        ):
            raise ValueError("invalid restore writer")
        intent = RestoreIntent(**data, writer=writer)
        if (
            not isinstance(intent.transaction, str)
            or UUID(hex=intent.transaction).hex != intent.transaction
            or not isinstance(intent.name, str)
            or not isinstance(intent.archive_id, str)
            or type(intent.device) is not int
            or type(intent.inode) is not int
            or intent.origin not in SKILL_ACTORS
            or (intent.created_at is not None and not isinstance(intent.created_at, str))
            or type(intent.pinned) is not bool
        ):
            raise ValueError("invalid restore journal")
        return intent
    except (OSError, ValueError, TypeError, KeyError, RecursionError) as error:
        raise SkillAuthoringError(
            "The pending Skill restore could not be checked. Ask the user to inspect the Skill "
            "restore journal before changing Skills in this home."
        ) from error
