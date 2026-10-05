"""Append-only revision history of one writable Skill home.

Each writable home (an Identity Agent's ``agents/<id>/skills/`` or the global
``<data_dir>/skills/``) has one JSON Lines file beside it, ``skill-history.jsonl``,
outside every scan root. Every line is one revision of one Skill with a
sequential id. A revision lists the package files it changed: the change kind,
a SHA-256 of the bytes before and after, and the text after the change when it
is UTF-8 text of at most 1 MiB. The text before a change is never stored again:
it is the stored text of an earlier revision with that hash, so a history holds
each written version of a file once.

``baseline`` records a Skill found without history (its whole package, origin
from ``metadata.vbot.author``); ``external`` records files changed outside the
Skill write owner, noticed before the next write or history read of that Skill.
An ``archive`` revision of a Skill merged into another one lists what moved to
that Skill with it (``followed``): its shares and the automations that triggered
it.
A line that is not a readable revision (a torn write, a hand edit) is skipped
with a warning; an append after a last line without its newline starts a new
line, so the fragment stays separate and the new revision stays readable.

Callers hold the authoring write lock; the history file has its own lock,
always taken last. The parsed log is cached per file and validated by size and
modification time; stored texts stay on disk and are read back by line offset.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar, Literal

from core.skills._packages import excluded, is_redirect
from core.skills.requirements import REQUIREMENTS_METADATA_KEY
from core.skills.skill_validator import parse_skill_front_matter, split_skill_document
from core.utils.logging import get_logger
from core.utils.timestamps import format_canonical_timestamp, utc_now_timestamp

_LOGGER = get_logger("skills.history")

HISTORY_FILE_NAME = "skill-history.jsonl"
SKILL_DOCUMENT = "SKILL.md"
# ``metadata.vbot.author`` of a written ``SKILL.md``: ``human`` or ``agent``.
PROVENANCE_AUTHOR_KEY = "author"
ARCHIVE_DIR_NAME = "skill-archive"
_FORMAT_VERSION = 1
# Text larger than this (or not UTF-8) is recorded by hash only.
MAX_STORED_TEXT_BYTES = 1024 * 1024

SkillActor = Literal["human", "agent", "reflection", "librarian"]
SkillRevisionKind = Literal[
    "baseline", "create", "change", "external", "archive", "restore", "revert", "pin", "unpin"
]
SkillFileChangeKind = Literal["created", "updated", "deleted"]
# ``published``: the Skill moved into another home, such as a private Skill made global.
SkillArchiveReason = Literal["deleted", "absorbed", "inactive", "published"]

SKILL_ACTORS: tuple[SkillActor, ...] = ("human", "agent", "reflection", "librarian")
# Actors that write without a person attending: they never change a Skill the
# user pinned.
BACKGROUND_ACTORS: frozenset[str] = frozenset({"reflection", "librarian"})
ARCHIVE_REASONS: tuple[SkillArchiveReason, ...] = ("deleted", "absorbed", "inactive", "published")
# ``baseline`` and ``external`` revisions are written by nobody vBot knows.
EXTERNAL_ACTOR = "external"

_KINDS = frozenset(
    {"baseline", "create", "change", "external", "archive", "restore", "revert", "pin", "unpin"}
)
SkillReferenceKind = Literal["shared", "bootstrap", "cron", "calendar"]
_REFERENCE_KINDS = frozenset({"shared", "bootstrap", "cron", "calendar"})
_ACTORS = frozenset({*SKILL_ACTORS, EXTERNAL_ACTOR})
_CHANGES = frozenset({"created", "updated", "deleted"})
_REASONS = frozenset(ARCHIVE_REASONS)


class SkillHistoryError(OSError):
    """The history file cannot be read or written."""


@dataclass(frozen=True)
class SkillFileRecord:
    """One package file a revision changed.

    ``before``/``after`` are SHA-256 digests of the bytes (``None`` for an absent
    side); ``stored`` says the line holds the text after the change.
    """

    path: str
    change: SkillFileChangeKind
    before: str | None
    after: str | None
    stored: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "change": self.change}


@dataclass(frozen=True)
class SkillReference:
    """Something outside a Skill's package that names the Skill.

    ``shared`` is the share with one receiver Agent (``id`` its Agent id,
    ``name`` its name); ``bootstrap``, ``cron`` and ``calendar`` are an
    automation of the owning Agent whose texts trigger the Skill (``id`` the job
    or Calendar action id, ``name`` the job name or the event title).
    """

    kind: SkillReferenceKind
    id: str
    name: str

    def to_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "id": self.id, "name": self.name}


@dataclass(frozen=True)
class SkillRevision:
    """One recorded change of one Skill in one home.

    ``actor`` is ``human``, ``agent``, ``reflection``, ``librarian`` or
    ``external`` (baseline and external revisions). ``live`` is set when the
    revision moved the Skill into or out of the home; ``archive_id`` names the
    archived package involved. ``origin``/``created_at``/``pinned`` carry a
    Skill's provenance where a revision starts or restores it. ``followed``
    lists, on the archive revision of a Skill merged into ``absorbed_into``,
    what moved to that Skill with it.
    """

    id: int
    at: str
    skill: str
    kind: SkillRevisionKind
    actor: str
    files: tuple[SkillFileRecord, ...] = ()
    session_id: str | None = None
    run_id: str | None = None
    run_kind: str | None = None
    origin: str | None = None
    created_at: str | None = None
    pinned: bool | None = None
    live: bool | None = None
    reason: str | None = None
    absorbed_into: str | None = None
    archive_id: str | None = None
    reverts: tuple[int, ...] = ()
    followed: tuple[SkillReference, ...] = ()

    @property
    def moves(self) -> bool:
        """Whether the revision started, ended or moved the Skill's live package."""
        return self.kind in ("baseline", "create") or self.live is not None

    def to_dict(self) -> dict[str, Any]:
        """Return the public projection: no file texts or hashes.

        ``live`` appears only on a revision that moved the Skill into (true) or
        out of (false) the home.
        """
        data: dict[str, Any] = {
            "id": self.id,
            "at": self.at,
            "skill": self.skill,
            "kind": self.kind,
            "actor": self.actor,
            "files": [record.to_dict() for record in self.files],
        }
        for key in (
            "session_id",
            "run_id",
            "run_kind",
            "origin",
            "pinned",
            "live",
            "reason",
            "absorbed_into",
            "archive_id",
        ):
            value = getattr(self, key)
            if value is not None:
                data[key] = value
        if self.reverts:
            data["reverts"] = list(self.reverts)
        if self.followed:
            data["followed"] = [reference.to_dict() for reference in self.followed]
        return data


@dataclass(frozen=True)
class SkillRecord:
    """Provenance and state of one live Skill in a writable home.

    ``origin`` is who created it (``human``, ``agent``, ``reflection`` or
    ``librarian``); ``created_at`` is its first revision; ``changed_at`` and
    ``changed_by`` name its last change of package files, if any.
    """

    name: str
    origin: str
    pinned: bool
    created_at: str
    changed_at: str | None
    changed_by: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "origin": self.origin,
            "pinned": self.pinned,
            "created_at": self.created_at,
            "changed_at": self.changed_at,
            "changed_by": self.changed_by,
        }


@dataclass(frozen=True)
class SkillArchiveInfo:
    """What the history knows about one archived package."""

    archive_id: str
    skill: str
    revision: int
    at: str
    actor: str
    reason: str | None
    absorbed_into: str | None
    origin: str
    created_at: str
    pinned: bool
    restored: bool = False


@dataclass(frozen=True)
class FileState:
    """One package file as found on disk."""

    digest: str
    text: str | None


@dataclass
class _Lineage:
    live: bool
    origin: str
    created_at: str
    pinned: bool = False
    changed_at: str | None = None
    changed_by: str | None = None
    files: dict[str, str] = field(default_factory=dict)


@dataclass
class _Log:
    signature: tuple[int, int] | None
    revisions: list[SkillRevision] = field(default_factory=list)
    by_id: dict[int, SkillRevision] = field(default_factory=dict)
    # Byte span of each revision's line, for reading its stored texts back.
    spans: dict[int, tuple[int, int]] = field(default_factory=dict)
    lineages: dict[str, _Lineage] = field(default_factory=dict)
    archives: dict[str, SkillArchiveInfo] = field(default_factory=dict)


class SkillHistory:
    """The revision history of one writable Skill home (``<home>/../skill-history.jsonl``)."""

    _locks: ClassVar[dict[str, threading.RLock]] = {}
    _locks_guard: ClassVar[threading.Lock] = threading.Lock()
    _logs: ClassVar[dict[str, _Log]] = {}

    def __init__(self, home: Path) -> None:
        self.home = Path(home)
        self.path = self.home.parent / HISTORY_FILE_NAME
        self.archive_root = self.home.parent / ARCHIVE_DIR_NAME
        self._key = os.path.normcase(os.path.abspath(self.path))

    # -- reading ---------------------------------------------------------------

    def revisions(self, skill: str | None = None) -> list[SkillRevision]:
        """Return the revisions of *skill* (all when ``None``), oldest first."""
        with self._lock():
            revisions = self._load().revisions
            return [r for r in revisions if skill is None or r.skill == skill]

    def revision(self, revision_id: int) -> SkillRevision | None:
        with self._lock():
            return self._load().by_id.get(revision_id)

    def record(self, skill: str) -> SkillRecord | None:
        """Return the record of *skill* while the history knows it as live."""
        with self._lock():
            lineage = self._load().lineages.get(skill)
            return _record(skill, lineage) if lineage is not None and lineage.live else None

    def records(self) -> dict[str, SkillRecord]:
        with self._lock():
            return {
                name: _record(name, lineage)
                for name, lineage in self._load().lineages.items()
                if lineage.live
            }

    def files(self, skill: str) -> dict[str, str] | None:
        """Return the recorded file digests of a live *skill*, or ``None``."""
        with self._lock():
            lineage = self._load().lineages.get(skill)
            return dict(lineage.files) if lineage is not None and lineage.live else None

    def archive(self, archive_id: str) -> SkillArchiveInfo | None:
        with self._lock():
            return self._load().archives.get(archive_id)

    def archives(self) -> list[SkillArchiveInfo]:
        with self._lock():
            return list(self._load().archives.values())

    def text(self, skill: str, path: str, digest: str) -> str | None:
        """Return the stored text of *path* whose bytes hash to *digest*, if any.

        Any revision of *skill* that stored that file version serves; the text is
        read back from its line on disk.
        """
        with self._lock():
            log = self._load()
            for revision in reversed(log.revisions):
                if revision.skill != skill:
                    continue
                for record in revision.files:
                    if record.path == path and record.after == digest and record.stored:
                        text = self._stored_text(log, revision.id, path)
                        if text is not None and _digest(text.encode("utf-8")) == digest:
                            return text
            return None

    # -- writing ---------------------------------------------------------------

    def observe(self, skill: str, package: Path | None) -> SkillRecord | None:
        """Bring the history up to the package on disk; return the live record.

        A package the history does not know as live starts a ``baseline`` lineage
        whose origin is ``agent`` when its ``SKILL.md`` declares
        ``metadata.vbot.author: agent`` and ``human`` otherwise; files that differ
        from the recorded state become one ``external`` revision; a recorded live
        Skill whose package is gone ends with an ``external`` revision that is
        not live.
        """
        with self._lock():
            log = self._load()
            lineage = log.lineages.get(skill)
            if package is None:
                if lineage is not None and lineage.live:
                    removed, _ = diff_states(lineage.files, {})
                    self._append(
                        log,
                        skill=skill,
                        kind="external",
                        actor=EXTERNAL_ACTOR,
                        files=removed,
                        texts={},
                        live=False,
                    )
                return None
            found = package_state(package)
            if lineage is None or not lineage.live:
                records, texts = diff_states({}, found)
                self._append(
                    log,
                    skill=skill,
                    kind="baseline",
                    actor=EXTERNAL_ACTOR,
                    files=records,
                    texts=texts,
                    at=_modified_at(package),
                    origin=declared_origin(package),
                )
            else:
                records, texts = diff_states(lineage.files, found)
                if records:
                    self._append(
                        log,
                        skill=skill,
                        kind="external",
                        actor=EXTERNAL_ACTOR,
                        files=records,
                        texts=texts,
                        at=_modified_at(package),
                    )
            lineage = log.lineages[skill]
            return _record(skill, lineage)

    def append(
        self,
        *,
        skill: str,
        kind: SkillRevisionKind,
        actor: str,
        files: Iterable[SkillFileRecord] = (),
        texts: Mapping[str, str] | None = None,
        session_id: str | None = None,
        run_id: str | None = None,
        run_kind: str | None = None,
        origin: str | None = None,
        created_at: str | None = None,
        pinned: bool | None = None,
        live: bool | None = None,
        reason: str | None = None,
        absorbed_into: str | None = None,
        archive_id: str | None = None,
        reverts: Iterable[int] = (),
        followed: Iterable[SkillReference] = (),
    ) -> SkillRevision:
        """Append one revision; ``texts`` maps a file path to its stored text after."""
        with self._lock():
            return self._append(
                self._load(),
                skill=skill,
                kind=kind,
                actor=actor,
                files=tuple(files),
                texts=dict(texts or {}),
                session_id=session_id,
                run_id=run_id,
                run_kind=run_kind,
                origin=origin,
                created_at=created_at,
                pinned=pinned,
                live=live,
                reason=reason,
                absorbed_into=absorbed_into,
                archive_id=archive_id,
                reverts=tuple(reverts),
                followed=tuple(followed),
            )

    def changes(
        self, skill: str, package: Path | None, paths: Iterable[str] | None = None
    ) -> tuple[list[SkillFileRecord], dict[str, str]]:
        """Describe how *package* differs from the recorded state of *skill*.

        With *paths*, only those package files are compared.
        """
        with self._lock():
            lineage = self._load().lineages.get(skill)
            known = dict(lineage.files) if lineage is not None and lineage.live else {}
            if paths is None:
                found = package_state(package) if package is not None else {}
            else:
                selected = set(paths)
                known = {path: digest for path, digest in known.items() if path in selected}
                found = {}
                for path in selected:
                    state = file_state(package / path) if package is not None else None
                    if state is not None:
                        found[path] = state
            return diff_states(known, found)

    # -- internals ---------------------------------------------------------------

    def _lock(self) -> threading.RLock:
        with self._locks_guard:
            return self._locks.setdefault(self._key, threading.RLock())

    def _load(self) -> _Log:
        signature = _signature(self.path)
        cached = self._logs.get(self._key)
        if cached is not None and cached.signature == signature:
            return cached
        log = _Log(signature=signature)
        if signature is not None:
            try:
                content = self.path.read_bytes()
            except OSError as exc:
                raise SkillHistoryError(f"failed to read Skill history {self.path}: {exc}") from exc
            skipped = 0
            start = 0
            for line in content.split(b"\n"):
                end = start + len(line)
                if line.strip():
                    revision = _parse_revision(line)
                    if revision is None or revision.id in log.by_id:
                        skipped += 1
                    else:
                        _advance(log, revision, (start, end))
                start = end + 1
            if skipped:
                _LOGGER.warning(
                    "Skipped %d unreadable Skill history lines in %s", skipped, self.path
                )
        self._logs[self._key] = log
        return log

    def _append(
        self,
        log: _Log,
        *,
        skill: str,
        kind: SkillRevisionKind,
        actor: str,
        files: Sequence[SkillFileRecord],
        texts: dict[str, str],
        at: str | None = None,
        session_id: str | None = None,
        run_id: str | None = None,
        run_kind: str | None = None,
        origin: str | None = None,
        created_at: str | None = None,
        pinned: bool | None = None,
        live: bool | None = None,
        reason: str | None = None,
        absorbed_into: str | None = None,
        archive_id: str | None = None,
        reverts: tuple[int, ...] = (),
        followed: tuple[SkillReference, ...] = (),
    ) -> SkillRevision:
        stored = tuple(
            SkillFileRecord(
                record.path,
                record.change,
                record.before,
                record.after,
                stored=record.after is not None and record.path in texts,
            )
            for record in files
        )
        revision = SkillRevision(
            id=max(log.by_id, default=0) + 1,
            at=at or utc_now_timestamp(),
            skill=skill,
            kind=kind,
            actor=actor,
            files=stored,
            session_id=session_id,
            run_id=run_id,
            run_kind=run_kind,
            origin=origin,
            created_at=created_at,
            pinned=pinned,
            live=live,
            reason=reason,
            absorbed_into=absorbed_into,
            archive_id=archive_id,
            reverts=reverts,
            followed=followed,
        )
        line = json.dumps(_line(revision, texts), ensure_ascii=False, separators=(",", ":"))
        data = (line + "\n").encode("utf-8")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a+b") as handle:
                end = handle.seek(0, os.SEEK_END)
                if end:
                    handle.seek(end - 1)
                    if handle.read(1) != b"\n":
                        # A torn last line stays its own (skipped) line instead of
                        # swallowing this revision.
                        data = b"\n" + data
                        end += 1
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            raise SkillHistoryError(f"failed to write Skill history {self.path}: {exc}") from exc
        _advance(log, revision, (end, end + len(line.encode("utf-8"))))
        log.signature = _signature(self.path)
        return revision

    def _stored_text(self, log: _Log, revision_id: int, path: str) -> str | None:
        span = log.spans.get(revision_id)
        if span is None:
            return None
        start, end = span
        try:
            with self.path.open("rb") as handle:
                handle.seek(start)
                line = handle.read(end - start)
            data = json.loads(line.decode("utf-8"))
        except OSError, ValueError, RecursionError:
            return None
        for item in data.get("files", []) if isinstance(data, dict) else []:
            if isinstance(item, dict) and item.get("path") == path:
                text = item.get("text")
                return text if isinstance(text, str) else None
        return None


def package_files(package: Path) -> dict[str, Path]:
    """Return a package's regular files by POSIX path relative to it.

    Links (Windows junctions included) are neither entered nor recorded;
    generated and VCS directories and the install receipt are skipped.
    """
    files: dict[str, Path] = {}
    for base, directories, names in package.walk():
        directories[:] = sorted(
            name
            for name in directories
            if not _redirect(base / name)
            and not excluded((base / name).relative_to(package).as_posix())
        )
        for name in sorted(names):
            path = base / name
            relative = path.relative_to(package).as_posix()
            if excluded(relative) or _redirect(path) or not path.is_file():
                continue
            files[relative] = path
    return files


def package_state(package: Path) -> dict[str, FileState]:
    """Return the digest, and the text when storable, of every package file."""
    state: dict[str, FileState] = {}
    for relative, path in package_files(package).items():
        try:
            data = path.read_bytes()
        except OSError:
            continue
        state[relative] = FileState(_digest(data), _storable_text(data))
    return state


def file_state(path: Path) -> FileState | None:
    """Return one file's state, or ``None`` when it is absent."""
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return None
    return FileState(_digest(data), _storable_text(data))


def text_digest(text: str) -> str:
    return _digest(text.encode("utf-8"))


def document_fields(skill_file: Path) -> dict[str, Any]:
    """Return the front matter fields of a ``SKILL.md``, or ``{}`` when unreadable."""
    try:
        data = skill_file.read_bytes()
        if len(data) > MAX_STORED_TEXT_BYTES:
            return {}
        front_matter, _, _ = split_skill_document(data.decode("utf-8"))
        fields, _ = parse_skill_front_matter(front_matter)
    except OSError, UnicodeDecodeError:
        return {}
    return fields if isinstance(fields, dict) else {}


def declared_origin(package: Path) -> str:
    """Return ``agent`` when ``SKILL.md`` declares an Agent author, else ``human``."""
    metadata = document_fields(package / SKILL_DOCUMENT).get("metadata")
    vbot = metadata.get(REQUIREMENTS_METADATA_KEY) if isinstance(metadata, dict) else None
    author = vbot.get(PROVENANCE_AUTHOR_KEY) if isinstance(vbot, dict) else None
    return "agent" if author == "agent" else "human"


def diff_states(
    known: Mapping[str, str], found: Mapping[str, FileState]
) -> tuple[list[SkillFileRecord], dict[str, str]]:
    records: list[SkillFileRecord] = []
    texts: dict[str, str] = {}
    for path in sorted(set(known) | set(found)):
        before = known.get(path)
        current = found.get(path)
        after = current.digest if current is not None else None
        if before == after:
            continue
        change: SkillFileChangeKind = (
            "created" if before is None else "deleted" if after is None else "updated"
        )
        records.append(SkillFileRecord(path, change, before, after))
        if current is not None and current.text is not None:
            texts[path] = current.text
    return records, texts


def _record(name: str, lineage: _Lineage) -> SkillRecord:
    return SkillRecord(
        name=name,
        origin=lineage.origin,
        pinned=lineage.pinned,
        created_at=lineage.created_at,
        changed_at=lineage.changed_at,
        changed_by=lineage.changed_by,
    )


def _advance(log: _Log, revision: SkillRevision, span: tuple[int, int]) -> None:
    """Apply one revision to the replayed state."""
    log.revisions.append(revision)
    log.by_id[revision.id] = revision
    log.spans[revision.id] = span
    lineage = log.lineages.get(revision.skill)
    if revision.kind in ("baseline", "create") or lineage is None:
        lineage = _Lineage(
            live=revision.live is not False,
            origin=revision.origin or "human",
            created_at=revision.created_at or revision.at,
        )
        log.lineages[revision.skill] = lineage
    for record in revision.files:
        if record.after is None:
            lineage.files.pop(record.path, None)
        else:
            lineage.files[record.path] = record.after
    if revision.files and revision.kind != "baseline":
        lineage.changed_at = revision.at
        lineage.changed_by = revision.actor
    if revision.live is True:
        lineage.live = True
        archived = log.archives.get(revision.archive_id or "")
        if archived is not None:
            log.archives[archived.archive_id] = replace(archived, restored=True)
        if revision.origin is not None and revision.kind != "create":
            lineage.origin = revision.origin
            lineage.created_at = revision.created_at or lineage.created_at
    if revision.pinned is not None:
        lineage.pinned = revision.pinned
    if revision.live is False:
        if revision.archive_id is not None:
            log.archives[revision.archive_id] = SkillArchiveInfo(
                archive_id=revision.archive_id,
                skill=revision.skill,
                revision=revision.id,
                at=revision.at,
                actor=revision.actor,
                reason=revision.reason,
                absorbed_into=revision.absorbed_into,
                origin=lineage.origin,
                created_at=lineage.created_at,
                pinned=lineage.pinned,
            )
        lineage.live = False
        lineage.files = {}


def _line(revision: SkillRevision, texts: Mapping[str, str]) -> dict[str, Any]:
    files: list[dict[str, Any]] = []
    for record in revision.files:
        item: dict[str, Any] = {
            "path": record.path,
            "change": record.change,
            "before": record.before,
            "after": record.after,
        }
        if record.stored:
            item["text"] = texts[record.path]
        files.append(item)
    data: dict[str, Any] = {
        "v": _FORMAT_VERSION,
        "id": revision.id,
        "at": revision.at,
        "skill": revision.skill,
        "kind": revision.kind,
        "actor": revision.actor,
        "files": files,
    }
    for key in (
        "session_id",
        "run_id",
        "run_kind",
        "origin",
        "created_at",
        "pinned",
        "live",
        "reason",
        "absorbed_into",
        "archive_id",
    ):
        value = getattr(revision, key)
        if value is not None:
            data[key] = value
    if revision.reverts:
        data["reverts"] = list(revision.reverts)
    if revision.followed:
        data["followed"] = [reference.to_dict() for reference in revision.followed]
    return data


def _parse_revision(line: bytes) -> SkillRevision | None:
    """Parse one history line, or ``None`` when it is not a readable revision.

    Bytes that are not UTF-8, invalid JSON, another format version, an unknown
    kind or actor, or a missing or mistyped field all make a line unreadable.
    """
    try:
        data = json.loads(line.decode("utf-8"))
        if not isinstance(data, dict) or data.get("v") != _FORMAT_VERSION:
            return None
        return SkillRevision(
            id=_integer(data["id"]),
            at=_text(data["at"]),
            skill=_text(data["skill"]),
            kind=_member(data["kind"], _KINDS),
            actor=_member(data["actor"], _ACTORS),
            files=tuple(_parse_file(item) for item in _array(data.get("files", []))),
            session_id=_optional_text(data.get("session_id")),
            run_id=_optional_text(data.get("run_id")),
            run_kind=_optional_text(data.get("run_kind")),
            origin=_optional_member(data.get("origin"), frozenset(SKILL_ACTORS)),
            created_at=_optional_text(data.get("created_at")),
            pinned=_optional_bool(data.get("pinned")),
            live=_optional_bool(data.get("live")),
            reason=_optional_member(data.get("reason"), _REASONS),
            absorbed_into=_optional_text(data.get("absorbed_into")),
            archive_id=_optional_text(data.get("archive_id")),
            reverts=tuple(_integer(item) for item in _array(data.get("reverts", []))),
            followed=tuple(_parse_reference(item) for item in _array(data.get("followed", []))),
        )
    # ValueError includes undecodable bytes and invalid JSON; RecursionError is
    # deeply nested JSON.
    except KeyError, TypeError, ValueError, RecursionError:
        return None


def _parse_reference(data: Any) -> SkillReference:
    if not isinstance(data, dict):
        raise ValueError("malformed Skill reference")
    return SkillReference(
        kind=_member(data["kind"], _REFERENCE_KINDS),
        id=_text(data["id"]),
        name=_text(data["name"]),
    )


def _parse_file(data: Any) -> SkillFileRecord:
    if not isinstance(data, dict):
        raise ValueError("malformed Skill file change")
    text = data.get("text")
    if text is not None and not isinstance(text, str):
        raise ValueError("malformed Skill file text")
    return SkillFileRecord(
        path=_text(data["path"]),
        change=_member(data["change"], _CHANGES),
        before=_optional_text(data.get("before")),
        after=_optional_text(data.get("after")),
        stored=text is not None,
    )


def _member(value: Any, allowed: frozenset[str]) -> Any:
    if not isinstance(value, str) or value not in allowed:
        raise ValueError("unexpected value")
    return value


def _optional_member(value: Any, allowed: frozenset[str]) -> Any:
    return None if value is None else _member(value, allowed)


def _array(value: Any) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError("expected a list")
    return value


def _integer(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("expected an integer")
    return value


def _text(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("expected a string")
    return value


def _optional_text(value: Any) -> str | None:
    return None if value is None else _text(value)


def _optional_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if not isinstance(value, bool):
        raise ValueError("expected a boolean")
    return value


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _storable_text(data: bytes) -> str | None:
    if len(data) > MAX_STORED_TEXT_BYTES:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _redirect(path: Path) -> bool:
    try:
        return is_redirect(path)
    except OSError:
        return True


def _signature(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise SkillHistoryError(f"failed to read Skill history {path}: {exc}") from exc
    return stat.st_size, stat.st_mtime_ns


def _modified_at(package: Path) -> str:
    """Date a noticed state by its newest file, falling back to now."""
    try:
        newest = max(
            (path.stat().st_mtime for path in package_files(package).values()), default=None
        )
    except OSError:
        newest = None
    if newest is None:
        return utc_now_timestamp()
    return format_canonical_timestamp(datetime.fromtimestamp(newest, tz=UTC))
