"""Durable intent and cursor for a Skill History revert."""

from __future__ import annotations

import json
import stat
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast
from uuid import UUID

from core.skills._authoring_types import SkillAuthoringError
from core.skills._history import SkillRevision, _line, _parse_revision
from core.utils.atomic import atomic_write_bytes, read_replaced_text
from core.utils.file_status import exists_strict, is_link_status, stat_or_none

RevertAction = Literal["archive", "restore", "content", "pin"]
PackageIdentity = tuple[int, int]


@dataclass(frozen=True)
class RevertStep:
    action: RevertAction
    identity: PackageIdentity
    revision: SkillRevision
    texts: dict[str, str]


@dataclass(frozen=True)
class RevertJournal:
    transaction: str
    steps: tuple[RevertStep, ...]

    @property
    def revisions(self) -> tuple[int, ...]:
        return tuple(step.revision.reverts[0] for step in self.steps)


def _journal_path(root: Path) -> Path:
    return root.parent / "skill-revert.json"


def _cursor_path(root: Path) -> Path:
    return root.parent / "skill-revert-progress.json"


def revert_pending(root: Path) -> bool:
    return exists_strict(_journal_path(root), follow_symlinks=False)


def require_revert_idle(root: Path) -> None:
    if revert_pending(root):
        raise SkillAuthoringError(
            "A Skill revert is pending in this home. Ask the user to retry the same revisions."
        )


def read_journal(root: Path) -> RevertJournal | None:
    try:
        data = _read_document(_journal_path(root))
        if data is None:
            return None
        transaction = data["transaction"]
        if not isinstance(transaction, str) or UUID(hex=transaction).hex != transaction:
            raise ValueError("invalid transaction")
        steps = tuple(_parse_step(step, transaction) for step in data["steps"])
        journal = RevertJournal(transaction, steps)
        if not steps or tuple(sorted(set(journal.revisions), reverse=True)) != journal.revisions:
            raise ValueError("invalid revision sequence")
        return journal
    except (OSError, ValueError, TypeError, KeyError, RecursionError) as error:
        raise SkillAuthoringError(
            "The pending Skill revert could not be read. Inspect the Skill revert journal."
        ) from error


def write_journal(root: Path, journal: RevertJournal) -> None:
    _write_document(
        _journal_path(root),
        {
            "transaction": journal.transaction,
            "steps": [
                {
                    "action": step.action,
                    "identity": step.identity,
                    "revision": _line(step.revision, step.texts),
                }
                for step in journal.steps
            ],
        },
        replace_existing=False,
    )


def read_cursor(root: Path, journal: RevertJournal) -> int:
    try:
        data = _read_document(_cursor_path(root))
        if data is None or data["transaction"] != journal.transaction:
            return 0
        next_step = data["next_step"]
        if type(next_step) is not int or not 0 <= next_step <= len(journal.steps):
            raise ValueError("invalid next step")
        return next_step
    except (OSError, ValueError, TypeError, KeyError, RecursionError) as error:
        raise SkillAuthoringError(
            "The progress of the pending Skill revert could not be read."
        ) from error


def write_cursor(root: Path, journal: RevertJournal, next_step: int) -> None:
    _write_document(
        _cursor_path(root), {"transaction": journal.transaction, "next_step": next_step}
    )


def finish_journal(root: Path) -> None:
    _journal_path(root).unlink()
    # A remaining cursor is tagged with this transaction and ignored by a later one.
    with suppress(OSError):
        _cursor_path(root).unlink(missing_ok=True)


def _parse_step(data: dict[str, Any], transaction: str) -> RevertStep:
    action = data["action"]
    identity = data["identity"]
    body = data["revision"]
    revision = _parse_revision(json.dumps(body).encode())
    if (
        action not in ("archive", "restore", "content", "pin")
        or not isinstance(identity, list)
        or len(identity) != 2
        or not all(type(number) is int for number in identity)
        or revision is None
        or revision.kind != "revert"
        or len(revision.reverts) != 1
        or revision.revert_transaction != transaction
        or revision.live is not {"archive": False, "restore": True}.get(action)
    ):
        raise ValueError("invalid step")
    texts = {
        item["path"]: item["text"] for item in body["files"] if isinstance(item.get("text"), str)
    }
    if action == "content" and any(
        record.after is not None and record.path not in texts for record in revision.files
    ):
        raise ValueError("missing pending file content")
    return RevertStep(cast(RevertAction, action), (identity[0], identity[1]), revision, texts)


def _read_document(path: Path) -> dict[str, Any] | None:
    status = stat_or_none(path, follow_symlinks=False)
    if status is None:
        return None
    if is_link_status(status) or not stat.S_ISREG(status.st_mode):
        raise ValueError("expected a regular file")
    data = json.loads(read_replaced_text(path))
    if not isinstance(data, dict) or type(data.get("format_version")) is not int:
        raise ValueError("invalid operation record")
    if data["format_version"] != 1:
        raise ValueError("unsupported operation record version")
    return data


def _write_document(path: Path, data: dict[str, Any], *, replace_existing: bool = True) -> None:
    atomic_write_bytes(
        path,
        json.dumps({"format_version": 1, **data}, ensure_ascii=True).encode() + b"\n",
        replace_existing=replace_existing,
    )
