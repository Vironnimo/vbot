"""Librarian Skill candidates, usage, aging and report calculations."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from core.prompts.briefs import LibrarianCandidate
from core.skills import SkillAuthoringService, SkillRecord, SkillRegistry
from core.skills.skills import scan_skill_resources
from core.statistics.skills import SkillActivationUse, SkillUse
from core.utils.timestamps import parse_timestamp

if TYPE_CHECKING:
    from core.skills import SkillRevision


@dataclass(frozen=True)
class _Library:
    """One Agent's Skill library after aging, as consolidation sees it."""

    archived_names: tuple[str, ...]
    archived_revisions: tuple[int, ...]
    candidates: tuple[LibrarianCandidate, ...]
    fingerprint: str
    latest_revision: int = 0
    aging_source_fingerprint: str | None = None


def librarian_candidates(
    authoring: SkillAuthoringService,
    root: Path,
    *,
    usage: Mapping[str, SkillUse],
    records: Mapping[str, SkillRecord] | None = None,
) -> tuple[LibrarianCandidate, ...]:
    """Return the Skills of the home ``root`` that a Librarian pass may change.

    A candidate is a loadable Skill the user has not pinned, whoever created
    it. ``usage`` is the Agent's Skill use by name. Blocking.
    """
    if records is None:
        records = authoring.records(root)
    registry = SkillRegistry.load(root)
    candidates: list[LibrarianCandidate] = []
    for name in sorted(records):
        record = records[name]
        if record.pinned:
            continue
        try:
            skill = registry.get(name)
            text = skill.path.read_text(encoding="utf-8")
        except KeyError, OSError, UnicodeDecodeError:
            continue
        use = usage.get(name)
        candidates.append(
            LibrarianCandidate(
                name=name,
                description=skill.description,
                origin=record.origin,
                created=_date(record.created_at),
                changed=_date(record.changed_at or record.created_at),
                last_used=None if use is None else _date(use.last_activated),
                uses=0 if use is None else use.count,
                skill_md_chars=len(text),
                support_files=tuple(scan_skill_resources(skill.path.parent)),
            )
        )
    return tuple(candidates)


def _attended_changes(history: Sequence[SkillRevision]) -> dict[str, str]:
    """The time of each Skill's newest file change by an attended writer."""
    changed: dict[str, str] = {}
    for revision in history:
        if revision.files and revision.kind != "baseline" and revision.attended:
            changed.setdefault(revision.skill, revision.at)
    return changed


def _inactive(
    record: SkillRecord, use: SkillUse | None, changed_at: str | None, cutoff: datetime
) -> bool:
    """Whether aging archives ``record``: unpinned and idle since ``cutoff``.

    ``changed_at`` is the Skill's newest change by an attended writer.
    """
    if record.pinned:
        return False
    moments = [record.created_at, changed_at, None if use is None else use.last_activated]
    try:
        last_activity = max(parse_timestamp(moment) for moment in moments if moment)
    except ValueError:
        # A time that cannot be read never ages a Skill.
        return False
    return last_activity < cutoff


def _library_usage(
    usage: Sequence[SkillActivationUse],
    agent_id: str,
    receivers: Mapping[str, frozenset[str]],
) -> dict[str, SkillUse]:
    """Actual private-package use, with conservative matching for legacy records.

    New records follow their resolved package regardless of who used it. Old
    records have no package evidence, so keep the former owner/receiver rule.
    One Session counts once even when it has both old and qualified activations.
    """
    selected: dict[str, dict[int, str]] = {}
    for use in usage:
        package = use.package
        if package is not None:
            if package.source != "agent" or package.location != agent_id:
                continue
            name = package.name
        else:
            name = use.name
            if use.agent_id != agent_id and use.agent_id not in receivers.get(name, frozenset()):
                continue
        sessions = selected.setdefault(name, {})
        previous = sessions.get(use.session_key)
        if previous is None or parse_timestamp(use.last_activated) > parse_timestamp(previous):
            sessions[use.session_key] = use.last_activated
    return {
        name: SkillUse(
            last_activated=max(sessions.values(), key=parse_timestamp), count=len(sessions)
        )
        for name, sessions in selected.items()
    }


def _by_run(revision: SkillRevision, run_id: str | None) -> bool:
    """Whether ``revision`` is a write of the consolidation Run ``run_id``."""
    return run_id is not None and revision.actor == "librarian" and revision.run_id == run_id


def _counts(revisions: Sequence[SkillRevision], run_id: str | None) -> tuple[int, int, int]:
    """Count the Skills the consolidation Run ``run_id`` created, changed and merged away."""
    written = [revision for revision in revisions if _by_run(revision, run_id)]
    created = {revision.skill for revision in written if revision.kind == "create"}
    merged = {revision.skill for revision in written if revision.reason == "absorbed"}
    changed = {revision.skill for revision in written} - created - merged
    return len(created), len(changed), len(merged)


def _fingerprint(latest_revisions: Mapping[str, int]) -> str:
    """Fingerprint the candidates by name and latest revision id."""
    payload = json.dumps(sorted(latest_revisions.items()), separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _latest_revisions(
    candidates: Sequence[LibrarianCandidate], history: Sequence[SkillRevision]
) -> dict[str, int]:
    """Return each candidate's newest revision from a newest-first history."""
    names = {candidate.name for candidate in candidates}
    latest: dict[str, int] = {}
    for revision in history:
        if revision.skill in names:
            latest.setdefault(revision.skill, revision.id)
    return {name: latest.get(name, 0) for name in names}


def _date(timestamp: str) -> str:
    """The ISO date of a timestamp, in UTC."""
    try:
        return parse_timestamp(timestamp).date().isoformat()
    except ValueError:
        return timestamp[:10]
