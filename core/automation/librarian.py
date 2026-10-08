"""The Librarian: scheduled curation of each Identity Agent's own Skills.

A Librarian pass keeps one Agent's private Skill library small, current and
free of duplicates. It never touches a Skill the user pinned (the background
rule of Skill authoring; see ``core.skills``), and every change it makes is a
recorded Skill revision under the actor ``librarian`` that the user can see
and revert.

A pass has two parts:

1. **Aging** (no Model): every unpinned Skill of the Agent, whoever created
   it, whose last activity (created, last changed by the user, an attended
   Agent or an outside edit, last used in a conversation) is older than
   ``librarian.archive_after_days`` is archived with the reason ``inactive``.
   Changes by background Runs do not keep a Skill. Use of a shared Skill by
   the Agents it is shared with counts as use. A Skill that a live Bootstrap
   job or Cron job of the Agent triggers by name is kept: its
   use is recorded only when the automation runs, which can be rarer than the
   aging period.
2. **Consolidation** (``librarian.consolidate``): when at least two Skills are
   candidates and the candidates changed since the last consolidation (their
   fingerprint: names and latest revision ids), the built-in Librarian Agent
   (``core.agents``: its own Model and Model settings, only the Tools ``skill``
   and ``skill_manage``) gets one internal Run of kind ``librarian`` in a new
   Session of its own. The Session's metadata binds it to the Agent
   (``SKILL_AGENT_ID_KEY``), so the Librarian's Tools and System Prompt work on
   that Agent's Skills there; it is titled ``Skills of <Agent name> · <date>``.
   The Run reads the Librarian brief and may merge and fix the candidates. The
   Session is kept: the user can open it and go on talking with the Librarian
   about that Agent's Skills.

The service checks every hour (the first check waits until startup settled)
and runs a pass for each Identity Agent of the user that has Skills of its own,
whose own ``librarian_enabled`` switch is on, whose last pass is
``librarian.interval_days`` old and that has no active or queued Run, while the
Librarian is available and has no active or queued Run itself. An Agent without
a pass is due one interval after the first check that saw it, so a new or
upgraded installation gets a full interval before its first pass. Passes run
one at a time. ``run`` starts a pass at once regardless of the interval and of
``librarian.enabled``; it refuses while a pass runs or the Agent or the
Librarian is not idle, and when no pass can run for the Agent at all
(``UnscheduledReason``).

Each Agent's state is the durable JSON document ``agents/<id>/librarian.json``:
when the schedule first saw the Agent, its recent passes (``last_pass`` and the
``earlier_passes`` before it, at most ``LIBRARIAN_PASS_HISTORY`` together), what
each did (counts, the archive revisions of aging, the consolidation Session and
Run) and the fingerprint of the last consolidation. A running pass keeps a
running record there, written when it starts, after aging archived Skills and
once the consolidation Run started; a pass that ends without a final record
(vBot stopped during it) is reported from that record as interrupted. A pass
that fails records itself as failed. Either way the next scheduled pass comes
one interval later, as after a completed pass. The report of a pass is derived
from the Skill history: the revisions aging recorded and the revisions of the
consolidation Run. vBot never deletes the Session of a pass that started its
Run.

Blocking reads and writes run on the ``librarian`` workers, never on the Event
Loop; Skill writes hold the Agent lifecycle guard.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from core.agents import (
    LIBRARIAN_AGENT_ID,
    SKILL_AGENT_ID_KEY,
    AgentNotFoundError,
    LibrarianProblem,
    is_builtin_agent,
    is_librarian,
    librarian_problem_message,
)
from core.config_validation import (
    JsonDiagnostic,
    JsonValidationReport,
    add_error,
    format_report_diagnostics,
    read_json_file,
    validate_json_file,
    warn_unknown_keys,
)
from core.json_documents import (
    JsonDocumentFormat,
    json_document,
    json_list,
    json_object,
    validate_format_version,
    write_json_document,
)
from core.prompts.briefs import LibrarianCandidate, librarian_brief
from core.runs import RunKind
from core.sessions import SESSION_AUTO_TITLE_INITIALIZED_KEY, SESSION_AUTO_TITLE_KEY
from core.settings import is_valid_agent_id
from core.skills import (
    SkillAuthoringError,
    SkillAuthoringService,
    SkillRecord,
    SkillRegistry,
    SkillWriter,
)
from core.skills._history import EXTERNAL_ACTOR
from core.skills.skills import scan_skill_resources
from core.statistics.skills import SkillUse
from core.utils.errors import VBotError
from core.utils.logging import get_logger
from core.utils.timestamps import format_canonical_timestamp, parse_timestamp
from core.utils.workers import BoundedWorkerPool

if TYPE_CHECKING:
    from core.runtime.interfaces import RuntimeServices
    from core.skills import SkillRevision

_LOGGER = get_logger("automation.librarian")

# Blocking state, history and package work of passes and status reads.
_LIBRARIAN_WORKERS = BoundedWorkerPool(name="librarian", max_workers=2)

# The first check waits until startup has settled; later checks run hourly.
FIRST_CHECK_DELAY_SECONDS = 300.0
CHECK_INTERVAL_SECONDS = 3600.0

# Writers whose changes count as activity for aging: background Runs' do not.
_ATTENDED_ACTORS = frozenset({"human", "agent", EXTERNAL_ACTOR})
_LIBRARIAN_WRITER = SkillWriter(actor="librarian", run_kind=RunKind.LIBRARIAN.value)

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


@dataclass(frozen=True)
class _Library:
    """One Agent's Skill library after aging, as consolidation sees it."""

    archived_names: tuple[str, ...]
    archived_revisions: tuple[int, ...]
    candidates: tuple[LibrarianCandidate, ...]
    fingerprint: str


@dataclass
class _ActivePass:
    """A running pass and how far it got.

    ``base`` is the settled state the pass started from, once it was read.
    """

    started_at: str
    trigger: PassTrigger
    # The name of the Agent whose Skills the pass curates, for its Session title and brief.
    agent_name: str = ""
    task: asyncio.Task[None] | None = field(default=None, repr=False)
    base: LibrarianState | None = None
    # Every Agent's Skill use by ``(agent id, name)``, read for this pass or its check.
    usage: Mapping[tuple[str, str], SkillUse] | None = field(default=None, repr=False)
    archived: int = 0
    archived_revisions: tuple[int, ...] = ()
    candidates: int = 0
    consolidation: ConsolidationOutcome | None = None
    session_id: str | None = None
    run_id: str | None = None

    def record(
        self,
        finished_at: str,
        outcome: PassOutcome,
        *,
        counts: tuple[int, int, int] = (0, 0, 0),
        error: str | None = None,
    ) -> LibrarianPass:
        """The pass's record as of ``finished_at``."""
        return LibrarianPass(
            started_at=self.started_at,
            finished_at=finished_at,
            trigger=self.trigger,
            outcome=outcome,
            archived=self.archived,
            archived_revisions=self.archived_revisions,
            candidates=self.candidates,
            consolidation=self.consolidation or "failed",
            session_id=self.session_id,
            run_id=self.run_id,
            created=counts[0],
            changed=counts[1],
            merged=counts[2],
            error=error,
        )


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


class LibrarianService:
    """Scheduled and on-demand Librarian passes over Identity Agents' own Skills.

    Passes run as the built-in Librarian Agent (``core.agents``), one at a time.
    """

    def __init__(
        self,
        runtime: RuntimeServices,
        *,
        authoring: SkillAuthoringService,
        skills_dir: Callable[[str], Path],
        skill_usage: Callable[[], Awaitable[Mapping[tuple[str, str], SkillUse]]],
        triggered_skill_names: Callable[[str], frozenset[str]],
        shared_skill_receivers: Callable[[str], Mapping[str, frozenset[str]]],
        skills_changed: Callable[[str], None],
        status_changed: Callable[[], None] = lambda: None,
        reviewing: Callable[[str], bool] = lambda _agent_id: False,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """Curate the homes ``skills_dir`` names with ``authoring``.

        ``skill_usage`` reads every Agent's Skill use by ``(agent id, name)``;
        ``triggered_skill_names`` names the Skills an Agent's live automations
        trigger, and ``shared_skill_receivers`` (blocking) maps each Skill it
        shares to the Agents it shares it with. ``skills_changed`` runs after
        aging archived Skills of an Agent (consolidation writes report themselves
        through ``skill_manage``), and
        ``status_changed`` whenever a pass starts or ends. ``reviewing(agent_id)``
        says whether a Reflection of the Agent is starting or running; a pass
        waits for it like for any other Run of the Agent.
        """
        self._runtime = runtime
        self._authoring = authoring
        self._skills_dir = skills_dir
        self._skill_usage = skill_usage
        self._triggered_skill_names = triggered_skill_names
        self._shared_skill_receivers = shared_skill_receivers
        self._skills_changed = skills_changed
        self._status_changed = status_changed
        self._reviewing = reviewing
        self._clock = clock or (lambda: datetime.now(UTC))
        self._active: dict[str, _ActivePass] = {}
        self._task: asyncio.Task[None] | None = None
        self._closed = False

    # -- lifecycle ---------------------------------------------------------------

    def start(self) -> None:
        """Start the hourly check; call inside the serving Event Loop."""
        if self._task is not None and not self._task.done():
            return
        self._closed = False
        self._task = asyncio.get_running_loop().create_task(self._run(), name="librarian")

    def stop(self) -> None:
        """Stop checking and cancel every pass in progress."""
        self._closed = True
        for task in self._tasks():
            task.cancel()

    async def aclose(self) -> None:
        """Stop, then wait until every cancelled pass has ended."""
        tasks = self._tasks()
        self.stop()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._task = None
        self._active.clear()

    def _tasks(self) -> list[asyncio.Task[None]]:
        tasks = [self._task] if self._task is not None else []
        tasks.extend(active.task for active in self._active.values() if active.task is not None)
        return tasks

    # -- public surface ----------------------------------------------------------

    async def status(self, agent_id: str) -> dict[str, Any]:
        """Return an Agent's Librarian settings, state and the changes of its last pass.

        ``available`` says whether a pass can run for the Agent at all.
        ``unscheduled_reason`` names why it gets no scheduled pass, ``None``
        while it gets them: ``librarian_unavailable`` (the Librarian Agent is
        unavailable; ``librarian_problem`` says why), ``agent_disabled`` (its
        ``librarian_enabled`` is off), ``no_skills`` (it has no Skills of its own)
        or ``schedule_disabled`` (``librarian.enabled`` is off; a pass started by
        hand still runs). ``next_due_at`` is set only while the Agent gets
        scheduled passes. ``running_session_id`` names the Librarian's Session
        of the running pass once its consolidation Run started. ``passes`` are
        its recent passes, newest first, the first being ``last_pass``; a pass
        whose consolidation Run started names the Librarian's Session and Run.

        Raises ``AgentNotFoundError`` unless ``agent_id`` is an Identity Agent,
        :class:`LibrarianUnavailableError` for the Librarian itself and
        :class:`LibrarianStateError` when the state document cannot be read.
        """
        agent = await self._identity_agent(agent_id)
        settings = await _LIBRARIAN_WORKERS.run(self._runtime.storage.load_librarian_settings)
        state = await _LIBRARIAN_WORKERS.run(self._read_state, agent_id)
        active = self._active.get(agent_id)
        if active is None:
            state = state.settled()
        passes = list(state.passes)
        last_pass = state.last_pass
        changes = await _LIBRARIAN_WORKERS.run(self._pass_revisions, agent_id, last_pass)
        if last_pass is not None and last_pass.outcome == "interrupted":
            # An interrupted pass never counted what its consolidation Run did.
            created, changed, merged = _counts(changes, last_pass.run_id)
            last_pass = replace(last_pass, created=created, changed=changed, merged=merged)
            passes[0] = last_pass
        problem, blocked = await _LIBRARIAN_WORKERS.run(self._blocked, agent_id, agent)
        unscheduled = blocked or (None if settings["enabled"] else "schedule_disabled")
        next_due = None
        if unscheduled is None:
            # Until a check sees the Agent, its first pass is about one interval away.
            seen = state.first_seen_at or format_canonical_timestamp(self._clock())
            next_due = _next_due(replace(state, first_seen_at=seen), settings["interval_days"])
        return {
            "agent_id": agent_id,
            "settings": dict(settings),
            "available": blocked is None,
            "unscheduled_reason": unscheduled,
            "librarian_problem": problem,
            "running": active is not None,
            "running_since": None if active is None else active.started_at,
            "running_session_id": None if active is None else active.session_id,
            "last_pass": None if last_pass is None else last_pass.to_dict(),
            "passes": [record.to_dict() for record in passes],
            "next_due_at": next_due,
            "changes": [revision.to_dict() for revision in changes],
        }

    async def overview(self) -> dict[str, Any]:
        """Return whether the Librarian is available and its recent passes over all Agents.

        ``problem`` says why the Librarian is unavailable, ``None`` while it is
        available. ``running`` names the Agent whose pass runs, if one does, and
        ``running_session_id`` the Librarian's Session of that pass once its
        consolidation Run started.
        ``passes`` are the recent passes of every Identity Agent, newest first
        (at most ``LIBRARIAN_OVERVIEW_PASSES``), each with its ``agent_id`` and
        ``agent_name``; an Agent whose state cannot be read is left out.
        """
        settings = await _LIBRARIAN_WORKERS.run(self._runtime.storage.load_librarian_settings)
        problem, passes = await _LIBRARIAN_WORKERS.run(self._overview_passes, set(self._active))
        running = next(iter(self._active.items()), None)
        return {
            "agent_id": LIBRARIAN_AGENT_ID,
            "available": problem is None,
            "problem": problem,
            "settings": dict(settings),
            "running": None if running is None else running[0],
            "running_session_id": None if running is None else running[1].session_id,
            "passes": passes,
        }

    async def run(self, agent_id: str) -> dict[str, Any]:
        """Start a pass of ``agent_id`` now, ignoring the interval; return its status.

        Raises :class:`LibrarianUnavailableError` when no pass can run for the
        Agent (see :meth:`status`), :class:`LibrarianBusyError` while a pass
        runs or the Agent or the Librarian has active or queued Runs, and the
        errors of :meth:`status`.
        """
        if self._closed:
            raise LibrarianBusyError("The Librarian is shutting down.")
        agent = await self._identity_agent(agent_id)
        # An unreadable state document refuses here, before any Skill changes.
        await _LIBRARIAN_WORKERS.run(self._read_state, agent_id)
        problem, reason = await _LIBRARIAN_WORKERS.run(self._blocked, agent_id, agent)
        if reason is not None:
            raise LibrarianUnavailableError(_unavailable_message(agent_id, reason, problem))
        if agent_id in self._active:
            raise LibrarianBusyError(f"A Librarian pass of Agent {agent_id} is already running.")
        if self._active:
            raise LibrarianBusyError(
                f"A Librarian pass of Agent {next(iter(self._active))} is running; passes run "
                "one at a time, so start this one when it has finished."
            )
        if self._agent_active(agent_id):
            raise LibrarianBusyError(
                f"Agent {agent_id} has an active or queued Run; run the Librarian when it is idle."
            )
        if self._librarian_active():
            raise LibrarianBusyError(
                "The Librarian has an active or queued Run; start the pass when it is idle."
            )
        active = self._begin(agent_id, "manual", agent)
        active.task = asyncio.get_running_loop().create_task(
            self._guarded_pass(agent_id, active), name=f"librarian-pass:{agent_id}"
        )
        return await self.status(agent_id)

    # -- schedule ----------------------------------------------------------------

    async def _run(self) -> None:
        delay = FIRST_CHECK_DELAY_SECONDS
        try:
            while not self._closed:
                await _wait(delay)
                delay = CHECK_INTERVAL_SECONDS
                try:
                    await self._check()
                except Exception:
                    _LOGGER.warning(
                        "Librarian check failed; the next check retries it", exc_info=True
                    )
        except asyncio.CancelledError:
            return

    async def _check(self) -> None:
        """Run one pass for each due, idle and eligible Identity Agent, one at a time.

        One Agent whose check fails is skipped; the others are still checked.
        """
        settings = await _LIBRARIAN_WORKERS.run(self._runtime.storage.load_librarian_settings)
        if not settings["enabled"]:
            return
        if await _LIBRARIAN_WORKERS.run(self._runtime.agents.librarian_problem) is not None:
            return
        # The roster: the user's Agents, never the Librarian itself.
        agent_ids = await _LIBRARIAN_WORKERS.run(
            lambda: [agent.id for agent in self._runtime.agents.list()]
        )
        # Skill use is read once per check, and again after a consolidation Run,
        # during which the other Agents can use Skills for minutes.
        usage: Mapping[tuple[str, str], SkillUse] | None = None
        for agent_id in agent_ids:
            if self._closed:
                return
            try:
                usage = await self._check_agent(agent_id, settings, usage)
            except Exception:
                _LOGGER.warning(
                    "Librarian check skipped an Agent (agent=%s)", agent_id, exc_info=True
                )

    async def _check_agent(
        self,
        agent_id: str,
        settings: Mapping[str, Any],
        usage: Mapping[tuple[str, str], SkillUse] | None,
    ) -> Mapping[tuple[str, str], SkillUse] | None:
        """Run the pass of ``agent_id`` when it is due, idle and eligible.

        ``usage`` is the Skill use read earlier in this check, if any. Returns the
        Skill use the next Agent's pass can reuse, ``None`` when it is stale.
        """
        if agent_id in self._active:
            return usage
        try:
            state = await _LIBRARIAN_WORKERS.run(self._read_state, agent_id)
        except LibrarianStateError as error:
            _LOGGER.warning("Librarian skipped an Agent (agent=%s): %s", agent_id, error)
            return usage
        state = state.settled()
        if state.last_pass is None and state.first_seen_at is None:
            # The first pass comes one interval after the Agent was first seen.
            seen = replace(state, first_seen_at=format_canonical_timestamp(self._clock()))
            await _LIBRARIAN_WORKERS.run(self._write_state, agent_id, seen)
            return usage
        due = _next_due(state, settings["interval_days"])
        if due is not None and parse_timestamp(due) > self._clock():
            return usage
        try:
            agent = await self._runtime.agent_resolver.resolve_agent_async(None, agent_id)
        except Exception:
            # A removed or broken Agent is skipped; the next check sees it again.
            return usage
        _problem, blocked = await _LIBRARIAN_WORKERS.run(self._blocked, agent_id, agent)
        if blocked is not None or self._busy(agent_id):
            return usage
        if usage is None:
            usage = await self._skill_usage()
        # Checked again after the last await, so no Run or manual pass starts in between.
        if self._busy(agent_id):
            return usage
        active = self._begin(agent_id, "schedule", agent)
        active.usage = usage
        await self._guarded_pass(agent_id, active)
        return usage if active.run_id is None else None

    def running(self, agent_id: str) -> bool:
        """Whether a pass of the Agent runs."""
        return agent_id in self._active

    def _busy(self, agent_id: str) -> bool:
        """Whether a pass runs, or the Agent or the Librarian has an active or queued Run."""
        return bool(self._active) or self._agent_active(agent_id) or self._librarian_active()

    def _agent_active(self, agent_id: str) -> bool:
        """Whether the Agent has an active or queued Run, or a review is starting.

        Only Runs without a Project count: a Project Run executes a Config Agent,
        which never loads an Identity Agent's own Skills nor changes them.
        """
        return self._runtime.chat_run_manager.has_activity_for_agent(
            agent_id, project_id=None
        ) or self._reviewing(agent_id)

    def _librarian_active(self) -> bool:
        """Whether the Librarian has an active or queued Run, a pass's or the user's."""
        return self._runtime.chat_run_manager.has_activity_for_agent(
            LIBRARIAN_AGENT_ID, project_id=None
        )

    async def _identity_agent(self, agent_id: str) -> Any:
        """Resolve ``agent_id`` as an Identity Agent of the user, never a Config Agent,
        a Sub-Agent or the Librarian."""
        if not await _LIBRARIAN_WORKERS.run(self._runtime.agents.exists, agent_id):
            raise AgentNotFoundError(f"Agent not found: {agent_id}")
        agent = await self._runtime.agent_resolver.resolve_agent_async(None, agent_id)
        if is_librarian(agent):
            raise LibrarianUnavailableError(
                "The Librarian maintains the Skills of other Agents and gets no pass itself."
            )
        if is_builtin_agent(agent):
            raise AgentNotFoundError(f"Agent not found: {agent_id}")
        return agent

    def _blocked(
        self, agent_id: str, agent: Any
    ) -> tuple[LibrarianProblem | None, UnscheduledReason | None]:
        """Why the Librarian is unavailable, and why no pass can run for the Agent.

        Each is ``None`` when there is no such reason. Blocking.
        """
        problem = self._runtime.agents.librarian_problem()
        if problem is not None:
            return problem, "librarian_unavailable"
        if getattr(agent, "librarian_enabled", True) is False:
            return None, "agent_disabled"
        if not self._has_own_skills(agent_id):
            return None, "no_skills"
        return None, None

    def _has_own_skills(self, agent_id: str) -> bool:
        """Whether the Agent's private Skill home holds a Skill. Blocking."""
        root = self._skills_dir(agent_id)
        return root.is_dir() and bool(SkillRegistry.load(root).list_all())

    def _overview_passes(
        self, running: set[str]
    ) -> tuple[LibrarianProblem | None, list[dict[str, Any]]]:
        """The Librarian's problem and the recent passes over all Agents. Blocking."""
        problem = self._runtime.agents.librarian_problem()
        passes: list[dict[str, Any]] = []
        for agent in self._runtime.agents.list():
            try:
                state = self._read_state(agent.id)
            except LibrarianStateError:
                continue
            if agent.id not in running:
                state = state.settled()
            passes.extend(
                {"agent_id": agent.id, "agent_name": agent.name, **record.to_dict()}
                for record in state.passes
            )
        passes.sort(key=lambda record: str(record["started_at"]), reverse=True)
        return problem, passes[:LIBRARIAN_OVERVIEW_PASSES]

    def _begin(self, agent_id: str, trigger: PassTrigger, agent: Any) -> _ActivePass:
        active = _ActivePass(
            started_at=format_canonical_timestamp(self._clock()),
            trigger=trigger,
            agent_name=str(getattr(agent, "name", "") or agent_id),
        )
        self._active[agent_id] = active
        self._announce()
        return active

    def _announce(self) -> None:
        """Tell observers that a pass started or ended."""
        try:
            self._status_changed()
        except Exception:
            _LOGGER.warning("Librarian status callback failed", exc_info=True)

    async def _guarded_pass(self, agent_id: str, active: _ActivePass) -> None:
        try:
            await self._pass(agent_id, active)
        except asyncio.CancelledError:
            # The running record written so far reports the pass as interrupted.
            raise
        except Exception as error:
            _LOGGER.warning("Librarian pass failed (agent=%s)", agent_id, exc_info=True)
            await self._record_failure(agent_id, active, str(error) or type(error).__name__)
        finally:
            if self._active.get(agent_id) is active:
                del self._active[agent_id]
                self._announce()

    # -- one pass ----------------------------------------------------------------

    async def _pass(self, agent_id: str, active: _ActivePass) -> None:
        # A running record that an interrupted pass left behind becomes the last pass.
        active.base = (await _LIBRARIAN_WORKERS.run(self._read_state, agent_id)).settled()
        await self._write_progress(agent_id, active)
        settings = await _LIBRARIAN_WORKERS.run(self._runtime.storage.load_librarian_settings)
        # Live automations load Skills by name and record use only when they run.
        scheduled = self._triggered_skill_names(agent_id)
        receivers = await _LIBRARIAN_WORKERS.run(self._shared_skill_receivers, agent_id)
        if active.usage is None:
            active.usage = await self._skill_usage()
        usage = _library_usage(active.usage, agent_id, receivers)
        cutoff = self._clock() - timedelta(days=settings["archive_after_days"])
        library = await _LIBRARIAN_WORKERS.run(
            self._age_library, agent_id, usage, scheduled, cutoff
        )
        active.archived = len(library.archived_names)
        active.archived_revisions = library.archived_revisions
        active.candidates = len(library.candidates)
        if library.archived_names:
            self._skills_changed(agent_id)
            _LOGGER.info(
                "Librarian archived inactive Skills (agent=%s count=%d)",
                agent_id,
                len(library.archived_names),
            )
            await self._write_progress(agent_id, active)
        fingerprint = active.base.consolidation_fingerprint
        error = None
        if not settings["consolidate"]:
            active.consolidation = "disabled"
        elif len(library.candidates) < 2:
            active.consolidation = "too_few"
        elif library.fingerprint == fingerprint:
            active.consolidation = "unchanged"
        else:
            error = await self._consolidate(agent_id, library, active)
            if active.consolidation == "ran":
                fingerprint = await _LIBRARIAN_WORKERS.run(self._fingerprint_now, agent_id, usage)
        finished = await self._finish(agent_id, active, "completed", fingerprint, error)
        _LOGGER.info(
            "Librarian pass completed (agent=%s trigger=%s archived=%d candidates=%d "
            "consolidation=%s created=%d changed=%d merged=%d)",
            agent_id,
            active.trigger,
            finished.archived,
            finished.candidates,
            finished.consolidation,
            finished.created,
            finished.changed,
            finished.merged,
        )

    async def _write_progress(self, agent_id: str, active: _ActivePass) -> None:
        """Record how far the pass got, as the record it has if vBot stops now."""
        assert active.base is not None
        running = active.record(format_canonical_timestamp(self._clock()), "interrupted")
        await _LIBRARIAN_WORKERS.run(
            self._write_state, agent_id, replace(active.base, running_pass=running)
        )

    async def _finish(
        self,
        agent_id: str,
        active: _ActivePass,
        outcome: PassOutcome,
        fingerprint: str | None,
        error: str | None,
    ) -> LibrarianPass:
        """Write the pass's final record as the last pass of the Agent's state."""
        assert active.base is not None
        counts = (
            await _LIBRARIAN_WORKERS.run(self._run_counts, agent_id, active.run_id)
            if active.run_id is not None
            else (0, 0, 0)
        )
        finished = active.record(
            format_canonical_timestamp(self._clock()), outcome, counts=counts, error=error
        )
        state = replace(active.base.recorded(finished), consolidation_fingerprint=fingerprint)
        await _LIBRARIAN_WORKERS.run(self._write_state, agent_id, state)
        return finished

    async def _record_failure(self, agent_id: str, active: _ActivePass, error: str) -> None:
        """Record a pass that an error stopped; a pass over an unreadable state records none."""
        if active.base is None:
            return
        try:
            await self._finish(
                agent_id, active, "failed", active.base.consolidation_fingerprint, error
            )
        except Exception:
            _LOGGER.warning(
                "Librarian could not record a failed pass (agent=%s)", agent_id, exc_info=True
            )

    def _age_library(
        self,
        agent_id: str,
        usage: Mapping[str, SkillUse],
        scheduled: frozenset[str],
        cutoff: datetime,
    ) -> _Library:
        """Archive the inactive Skills, then describe what consolidation may change."""
        # Holding off other Skill writes keeps the activity read below true for
        # the archives: a person's change lands before it, which keeps the
        # Skill, or after the archive, never unseen in between.
        with self._runtime.agents.lifecycle_guard(), self._authoring.exclusive():
            root = self._skills_dir(agent_id)
            if not root.is_dir():
                return _Library((), (), (), _fingerprint({}))
            records = self._authoring.records(root)
            changed = _attended_changes(self._authoring.history(root, limit=_ALL_REVISIONS))
            archived: list[str] = []
            revisions: list[int] = []
            for name, record in sorted(records.items()):
                if name in scheduled:
                    continue
                if not _inactive(record, usage.get(name), changed.get(name), cutoff):
                    continue
                try:
                    result = self._authoring.delete(
                        root, name, writer=_LIBRARIAN_WRITER, reason="inactive"
                    )
                except (SkillAuthoringError, OSError) as error:
                    # One Skill that cannot move never stops the rest of the pass.
                    _LOGGER.warning(
                        "Librarian kept an inactive Skill (agent=%s skill=%s): %s",
                        agent_id,
                        name,
                        error,
                    )
                    continue
                archived.append(name)
                if result.revision is not None:
                    revisions.append(result.revision)
            if archived:
                records = self._authoring.records(root)
            candidates = librarian_candidates(self._authoring, root, usage=usage, records=records)
            return _Library(
                tuple(archived),
                tuple(revisions),
                candidates,
                _fingerprint(self._latest_revisions(root, candidates)),
            )

    def _fingerprint_now(self, agent_id: str, usage: Mapping[str, SkillUse]) -> str:
        """Fingerprint the candidates as a consolidation Run left them."""
        with self._runtime.agents.lifecycle_guard():
            root = self._skills_dir(agent_id)
            if not root.is_dir():
                return _fingerprint({})
            candidates = librarian_candidates(self._authoring, root, usage=usage)
            return _fingerprint(self._latest_revisions(root, candidates))

    def _latest_revisions(
        self, root: Path, candidates: Sequence[LibrarianCandidate]
    ) -> dict[str, int]:
        names = {candidate.name for candidate in candidates}
        latest: dict[str, int] = {}
        for revision in self._authoring.history(root, limit=_ALL_REVISIONS):
            if revision.skill in names:
                latest.setdefault(revision.skill, revision.id)
        return {name: latest.get(name, 0) for name in names}

    async def _consolidate(
        self, agent_id: str, library: _Library, active: _ActivePass
    ) -> str | None:
        """Run the Librarian's consolidation Run; return why it failed, if it did.

        The Run executes in a new Session of the Librarian bound to the Agent,
        with the Librarian's own Model, Model settings and Tools. Sets the pass's
        consolidation outcome, Session and Run, and records them before awaiting
        the Run.
        """
        brief = await _LIBRARIAN_WORKERS.run(
            librarian_brief,
            self._runtime.storage,
            library.candidates,
            agent_id=agent_id,
            agent_name=active.agent_name,
        )
        title = f"Skills of {active.agent_name} · {self._clock().astimezone().date().isoformat()}"
        sessions = self._runtime.chat_sessions
        session = await sessions.create_async(
            LIBRARIAN_AGENT_ID,
            run_kind=RunKind.LIBRARIAN,
            # The Librarian maintains Skills in its own Workspace, never in a Project.
            working_project_id=None,
            metadata={
                SKILL_AGENT_ID_KEY: agent_id,
                SESSION_AUTO_TITLE_KEY: title,
                SESSION_AUTO_TITLE_INITIALIZED_KEY: True,
            },
        )
        try:
            run = await self._runtime.chat_loop.start_run(
                LIBRARIAN_AGENT_ID,
                brief,
                session_id=session.id,
                internal=True,
                run_kind=RunKind.LIBRARIAN,
            )
        except Exception as error:
            # Nothing ran in the new Session: remove it rather than leave it empty.
            with suppress(Exception):
                await _LIBRARIAN_WORKERS.run(sessions.delete, session.address)
            _LOGGER.warning("Librarian consolidation did not start (agent=%s): %s", agent_id, error)
            active.consolidation = "failed"
            return str(error) or type(error).__name__
        active.session_id, active.run_id = session.id, run.id
        active.consolidation = "failed"
        await self._write_progress(agent_id, active)
        # Observers can open the Session while its Run works.
        self._announce()
        try:
            await run.wait()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            _LOGGER.warning("Librarian consolidation failed (agent=%s): %s", agent_id, error)
            return str(error) or type(error).__name__
        active.consolidation = "ran"
        return None

    def _run_counts(self, agent_id: str, run_id: str) -> tuple[int, int, int]:
        """Count the Skills a consolidation Run created, changed and merged away."""
        return _counts(self._recorded_revisions(agent_id), run_id)

    # -- state -------------------------------------------------------------------

    def _state_path(self, agent_id: str) -> Path:
        if not is_valid_agent_id(agent_id):
            raise LibrarianError(f"Invalid Agent id: {agent_id!r}")
        return self._runtime.storage.data_dir / "agents" / agent_id / LIBRARIAN_STATE_FILENAME

    def _read_state(self, agent_id: str) -> LibrarianState:
        """Return the stored state, empty when the document is missing.

        Raises :class:`LibrarianStateError` for a document that cannot be read:
        no pass runs over a state it would overwrite.
        """
        path = self._state_path(agent_id)
        report, data = read_json_file(path, _validate_state_document, missing_ok=True)
        if not report.exists:
            return LibrarianState()
        if not report.ok or not isinstance(data, dict):
            details = "; ".join(format_report_diagnostics(report))
            raise LibrarianStateError(f"{path}: {details}")
        last_pass = data.get("last_pass")
        running_pass = data.get("running_pass")
        return LibrarianState(
            last_pass=None if last_pass is None else LibrarianPass.from_dict(last_pass),
            consolidation_fingerprint=data.get("consolidation_fingerprint"),
            first_seen_at=data.get("first_seen_at"),
            running_pass=None if running_pass is None else LibrarianPass.from_dict(running_pass),
            earlier_passes=tuple(
                LibrarianPass.from_dict(record) for record in data.get("earlier_passes", ())
            )[: LIBRARIAN_PASS_HISTORY - 1],
        )

    def _write_state(self, agent_id: str, state: LibrarianState) -> None:
        """Write the state while the Agent exists; a removed Agent keeps none."""
        with self._runtime.agents.lifecycle_guard():
            if not self._runtime.agents.exists(agent_id):
                return
            write_json_document(
                self._state_path(agent_id),
                state.to_document(),
                LIBRARIAN_STATE_FORMAT,
                data_dir=self._runtime.storage.data_dir,
            )

    def _pass_revisions(self, agent_id: str, record: LibrarianPass | None) -> list[SkillRevision]:
        """The Skill revisions a pass recorded, newest first."""
        if record is None:
            return []
        archived = set(record.archived_revisions)
        return [
            revision
            for revision in reversed(self._recorded_revisions(agent_id))
            if revision.id in archived or _by_run(revision, record.run_id)
        ]

    def _recorded_revisions(self, agent_id: str) -> list[SkillRevision]:
        """The revisions the history of the Agent's private Skill home holds, oldest first.

        Reads the history only, without the Agent lifecycle guard: a home that is
        gone or unreadable has none.
        """
        try:
            return self._authoring.recorded_revisions(self._skills_dir(agent_id))
        except SkillAuthoringError:
            return []


# A history limit that reads every revision of a home.
_ALL_REVISIONS = 1_000_000


def _attended_changes(history: Sequence[SkillRevision]) -> dict[str, str]:
    """The time of each Skill's newest file change by an attended writer."""
    changed: dict[str, str] = {}
    for revision in history:
        if revision.files and revision.kind != "baseline" and revision.actor in _ATTENDED_ACTORS:
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
    usage: Mapping[tuple[str, str], SkillUse],
    agent_id: str,
    receivers: Mapping[str, frozenset[str]],
) -> dict[str, SkillUse]:
    """The use of ``agent_id``'s Skills by name, its shared Skills' receivers included.

    A receiver's use of a shared Skill's name counts toward the owner's Skill:
    the Sessions add up and the latest activation wins.
    """
    library = {name: use for (owner, name), use in usage.items() if owner == agent_id}
    for name, agents in receivers.items():
        uses = [use for agent in agents if (use := usage.get((agent, name))) is not None]
        own = library.get(name)
        if own is not None:
            uses.append(own)
        if uses:
            library[name] = SkillUse(
                last_activated=max((use.last_activated for use in uses), key=parse_timestamp),
                count=sum(use.count for use in uses),
            )
    return library


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


def _next_due(state: LibrarianState, interval_days: int) -> str | None:
    """When the next scheduled pass is due, ``None`` before a check saw the Agent.

    It is one interval after the last pass, or after the Agent was first seen.
    """
    since = state.last_pass.finished_at if state.last_pass is not None else state.first_seen_at
    if since is None:
        return None
    return format_canonical_timestamp(parse_timestamp(since) + timedelta(days=interval_days))


def _unavailable_message(
    agent_id: str, reason: UnscheduledReason, problem: LibrarianProblem | None
) -> str:
    """Why ``run`` refuses a pass of ``agent_id``, for the user."""
    if problem is not None:
        return librarian_problem_message(problem)
    if reason == "agent_disabled":
        return (
            f"The Librarian is off for Agent {agent_id} (librarian_enabled is false), "
            "so it does not curate its Skills."
        )
    return f"Agent {agent_id} has no Skills of its own, so the Librarian has nothing to curate."


def _date(timestamp: str) -> str:
    """The ISO date of a timestamp, in UTC."""
    try:
        return parse_timestamp(timestamp).date().isoformat()
    except ValueError:
        return timestamp[:10]


async def _wait(seconds: float) -> None:
    """Sleep between checks (the schedule's test seam)."""
    await asyncio.sleep(seconds)


__all__ = [
    "CHECK_INTERVAL_SECONDS",
    "FIRST_CHECK_DELAY_SECONDS",
    "LIBRARIAN_OVERVIEW_PASSES",
    "LIBRARIAN_PASS_HISTORY",
    "LIBRARIAN_STATE_FILENAME",
    "LIBRARIAN_STATE_FORMAT",
    "LibrarianBusyError",
    "LibrarianError",
    "LibrarianPass",
    "LibrarianService",
    "LibrarianState",
    "LibrarianStateError",
    "LibrarianUnavailableError",
    "UnscheduledReason",
    "librarian_candidates",
    "validate_librarian_state_file",
]
