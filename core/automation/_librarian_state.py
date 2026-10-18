"""Durable Librarian pass records, validation and scheduling state."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import timedelta
from pathlib import Path
from typing import Any, Literal

from core.config_validation import (
    JsonDiagnostic,
    JsonValidationReport,
    add_error,
    validate_json_file,
    warn_unknown_keys,
)
from core.json_documents import (
    JsonDocumentFormat,
    json_document,
    json_list,
    json_object,
    validate_format_version,
)
from core.utils.errors import VBotError
from core.utils.timestamps import format_canonical_timestamp, parse_timestamp

LIBRARIAN_STATE_FILENAME = "librarian.json"


LIBRARIAN_STATE_FORMAT_VERSION = 1


# How many passes an Agent's state keeps, the last pass included.
LIBRARIAN_PASS_HISTORY = 10


# How many recent passes over all Agents the overview lists.
LIBRARIAN_OVERVIEW_PASSES = 20


_PASS_FIELDS = frozenset(
    {
        "started_at",
        "finished_at",
        "trigger",
        "outcome",
        "archived",
        "archived_revisions",
        "candidates",
        "consolidation",
        "session_id",
        "run_id",
        "created",
        "changed",
        "merged",
        "error",
    }
)


LIBRARIAN_STATE_SHAPE = json_document(
    {"first_seen_at", "last_pass", "earlier_passes", "running_pass", "consolidation_fingerprint"},
    {
        "last_pass": json_object(_PASS_FIELDS),
        "earlier_passes": json_list(json_object(_PASS_FIELDS), key="started_at"),
        "running_pass": json_object(_PASS_FIELDS),
    },
)


PassTrigger = Literal["schedule", "manual"]


# How a pass ended: it finished, an error stopped it, or vBot stopped during it.
PassOutcome = Literal["completed", "failed", "interrupted"]


ConsolidationOutcome = Literal["ran", "unchanged", "too_few", "disabled", "failed"]


# Why an Agent gets no scheduled pass, in the order status reports them: the
# Librarian is unavailable, the Agent's own switch is off, it has no Skills of its
# own, or scheduled passes are off. Only the last still allows a manual pass.
UnscheduledReason = Literal[
    "librarian_unavailable", "agent_disabled", "no_skills", "schedule_disabled"
]


_TRIGGERS = frozenset({"schedule", "manual"})


_OUTCOMES = frozenset({"completed", "failed", "interrupted"})


_CONSOLIDATION_OUTCOMES = frozenset({"ran", "unchanged", "too_few", "disabled", "failed"})


_COUNT_FIELDS = ("archived", "candidates", "created", "changed", "merged")


_TEXT_FIELDS = ("session_id", "run_id", "error")


class LibrarianError(VBotError):
    """A Librarian pass cannot start."""


class LibrarianBusyError(LibrarianError):
    """A pass of the Agent is running, or the Agent has active or queued Runs."""


class LibrarianUnavailableError(LibrarianError):
    """No pass can run for the Agent: the Librarian is unavailable, the Agent's switch
    is off, it has no Skills of its own, or it is the Librarian."""


class LibrarianStateError(LibrarianError):
    """The Agent's ``librarian.json`` cannot be read; no pass runs until it is fixed."""


@dataclass(frozen=True)
class LibrarianPass:
    """What one pass did: the ``last_pass`` of the state document.

    The ``running_pass`` of a running pass has the same form: how far the pass
    got by ``finished_at``, with the outcome ``interrupted`` it has when vBot
    stops before the pass ends. ``consolidation`` is ``failed`` when the
    consolidation step did not finish, also when the pass stopped before it.
    ``error`` says why the pass or its consolidation Run failed.
    """

    started_at: str
    finished_at: str
    trigger: PassTrigger
    outcome: PassOutcome = "completed"
    archived: int = 0
    archived_revisions: tuple[int, ...] = ()
    candidates: int = 0
    consolidation: ConsolidationOutcome = "disabled"
    session_id: str | None = None
    run_id: str | None = None
    created: int = 0
    changed: int = 0
    merged: int = 0
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "trigger": self.trigger,
            "outcome": self.outcome,
            "archived": self.archived,
            "archived_revisions": list(self.archived_revisions),
            "candidates": self.candidates,
            "consolidation": self.consolidation,
            "created": self.created,
            "changed": self.changed,
            "merged": self.merged,
        }
        for key in _TEXT_FIELDS:
            value = getattr(self, key)
            if value is not None:
                data[key] = value
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> LibrarianPass:
        return cls(
            started_at=str(data["started_at"]),
            finished_at=str(data["finished_at"]),
            trigger=data["trigger"],
            outcome=data.get("outcome", "completed"),
            archived=int(data.get("archived", 0)),
            archived_revisions=tuple(int(item) for item in data.get("archived_revisions", ())),
            candidates=int(data.get("candidates", 0)),
            consolidation=data["consolidation"],
            session_id=data.get("session_id"),
            run_id=data.get("run_id"),
            created=int(data.get("created", 0)),
            changed=int(data.get("changed", 0)),
            merged=int(data.get("merged", 0)),
            error=data.get("error"),
        )


@dataclass(frozen=True)
class LibrarianState:
    """One Agent's Librarian state document.

    ``first_seen_at`` is when a scheduled check first saw the Agent; it dates
    the first scheduled pass while ``last_pass`` is ``None``. ``earlier_passes``
    are the passes before the last one, newest first. ``running_pass`` is the
    record of the pass in progress (see :class:`LibrarianPass`).
    """

    last_pass: LibrarianPass | None = None
    consolidation_fingerprint: str | None = None
    first_seen_at: str | None = None
    running_pass: LibrarianPass | None = None
    earlier_passes: tuple[LibrarianPass, ...] = ()

    @property
    def passes(self) -> tuple[LibrarianPass, ...]:
        """The recorded passes, newest first, at most ``LIBRARIAN_PASS_HISTORY``."""
        newest = () if self.last_pass is None else (self.last_pass,)
        return (*newest, *self.earlier_passes)[:LIBRARIAN_PASS_HISTORY]

    def recorded(self, record: LibrarianPass) -> LibrarianState:
        """This state with ``record`` as the last pass, the older passes kept up to the bound."""
        return replace(
            self,
            last_pass=record,
            running_pass=None,
            earlier_passes=self.passes[: LIBRARIAN_PASS_HISTORY - 1],
        )

    def settled(self) -> LibrarianState:
        """The state while no pass runs: a running record left behind is the last pass."""
        if self.running_pass is None:
            return self
        return self.recorded(self.running_pass)

    def to_document(self) -> dict[str, Any]:
        document: dict[str, Any] = {}
        if self.first_seen_at is not None:
            document["first_seen_at"] = self.first_seen_at
        if self.last_pass is not None:
            document["last_pass"] = self.last_pass.to_dict()
        if self.earlier_passes:
            document["earlier_passes"] = [record.to_dict() for record in self.earlier_passes]
        if self.running_pass is not None:
            document["running_pass"] = self.running_pass.to_dict()
        if self.consolidation_fingerprint is not None:
            document["consolidation_fingerprint"] = self.consolidation_fingerprint
        return document


def validate_librarian_state_file(path: str | Path) -> JsonValidationReport:
    """Validate one ``agents/<id>/librarian.json`` without consuming it."""
    return validate_json_file(path, _validate_state_document, missing_ok=True)


def _validate_state_document(data: Any) -> list[JsonDiagnostic]:
    diagnostics: list[JsonDiagnostic] = []
    if not isinstance(data, dict):
        add_error(diagnostics, "$", "must be a JSON object")
        return diagnostics
    if not validate_format_version(diagnostics, data, LIBRARIAN_STATE_FORMAT_VERSION):
        return diagnostics
    warn_unknown_keys(diagnostics, "$", data, LIBRARIAN_STATE_SHAPE.fields, "field")
    first_seen = data.get("first_seen_at")
    if first_seen is not None and not _is_timestamp(first_seen):
        add_error(diagnostics, "$.first_seen_at", "must be an ISO 8601 timestamp")
    fingerprint = data.get("consolidation_fingerprint")
    if fingerprint is not None and not isinstance(fingerprint, str):
        add_error(diagnostics, "$.consolidation_fingerprint", "must be a string")
    for key in ("last_pass", "running_pass"):
        if data.get(key) is not None:
            _validate_pass(diagnostics, f"$.{key}", data[key])
    earlier = data.get("earlier_passes", [])
    if not isinstance(earlier, list):
        add_error(diagnostics, "$.earlier_passes", "must be a list")
    else:
        for index, record in enumerate(earlier):
            _validate_pass(diagnostics, f"$.earlier_passes[{index}]", record)
    return diagnostics


def _validate_pass(diagnostics: list[JsonDiagnostic], path: str, record: Any) -> None:
    if not isinstance(record, dict):
        add_error(diagnostics, path, "must be an object")
        return
    warn_unknown_keys(diagnostics, path, record, _PASS_FIELDS, "field")
    for key in ("started_at", "finished_at"):
        if not _is_timestamp(record.get(key)):
            add_error(diagnostics, f"{path}.{key}", "must be an ISO 8601 timestamp")
    if record.get("trigger") not in _TRIGGERS:
        add_error(diagnostics, f"{path}.trigger", "must be schedule or manual")
    if record.get("outcome", "completed") not in _OUTCOMES:
        add_error(diagnostics, f"{path}.outcome", "must be completed, failed or interrupted")
    if record.get("consolidation") not in _CONSOLIDATION_OUTCOMES:
        add_error(
            diagnostics,
            f"{path}.consolidation",
            f"must be one of: {', '.join(sorted(_CONSOLIDATION_OUTCOMES))}",
        )
    for key in _COUNT_FIELDS:
        value = record.get(key, 0)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            add_error(diagnostics, f"{path}.{key}", "must be a non-negative integer")
    revisions = record.get("archived_revisions", [])
    if not isinstance(revisions, list) or any(
        isinstance(item, bool) or not isinstance(item, int) for item in revisions
    ):
        add_error(diagnostics, f"{path}.archived_revisions", "must be a list of integers")
    for key in _TEXT_FIELDS:
        value = record.get(key)
        if value is not None and not isinstance(value, str):
            add_error(diagnostics, f"{path}.{key}", "must be a string")


def _is_timestamp(value: Any) -> bool:
    try:
        parse_timestamp(value if isinstance(value, str) else "")
    except ValueError:
        return False
    return True


LIBRARIAN_STATE_FORMAT = JsonDocumentFormat(
    name="Librarian state",
    version=LIBRARIAN_STATE_FORMAT_VERSION,
    shape=LIBRARIAN_STATE_SHAPE,
    validate=_validate_state_document,
)


def _next_due(state: LibrarianState, interval_days: int) -> str | None:
    """When the next scheduled pass is due, ``None`` before a check saw the Agent.

    It is one interval after the last pass, or after the Agent was first seen.
    """
    since = state.last_pass.finished_at if state.last_pass is not None else state.first_seen_at
    if since is None:
        return None
    return format_canonical_timestamp(parse_timestamp(since) + timedelta(days=interval_days))
