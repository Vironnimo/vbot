"""The Librarian: scheduled curation of each Identity Agent's own Skills.

A Librarian pass keeps one Agent's private Skill library small, current and
free of duplicates. It touches only Skills that the background rules of Skill
authoring let it change (unpinned, created by the Agent, a Reflection review or
an earlier pass; see ``core.skills``), and every change it makes is a recorded
Skill revision under the actor ``librarian`` that the user can see and revert.

A pass has two parts:

1. **Aging** (no Model): every unpinned Skill that a background Run created
   (origin ``reflection`` or ``librarian``) and whose last activity (created,
   last changed, last used in a conversation) is older than
   ``librarian.archive_after_days`` is archived with the reason ``inactive``.
   A Skill that a live Bootstrap job, Cron job or Calendar action of the Agent
   triggers by name is kept. Skills that the Agent or the user created are
   never aged.
2. **Consolidation** (``librarian.consolidate``): when at least two Skills are
   candidates and the candidates changed since the last consolidation (their
   fingerprint: names and latest revision ids), one internal Run of kind
   ``librarian`` in a new hidden Session of the Agent reads the Librarian brief
   and may merge and fix the candidates with ``skill`` and ``skill_manage``
   only.

The service checks every hour (the first check waits until startup settled)
and runs a pass for each Identity Agent that can call ``skill`` and
``skill_manage``, whose last pass is ``librarian.interval_days`` old, and that
has no active or queued Run. Passes run one at a time. ``run`` starts a pass at
once regardless of the interval; it still waits for an idle Agent.

Each Agent's state is the durable JSON document ``agents/<id>/librarian.json``:
when the last pass ran, what it did (counts, the archive revisions of aging,
the consolidation Session and Run) and the fingerprint of the last
consolidation. The report of a pass is derived from the Skill history: the
revisions aging recorded and the revisions of the consolidation Run.

Blocking reads and writes run on the ``librarian`` workers, never on the Event
Loop; Skill writes hold the Agent lifecycle guard.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from core.agents import AgentNotFoundError
from core.automation.reflection import (
    SKILL_REFLECTION_TOOL_RESTRICTION,
    callable_review_dimensions,
    tool_denial_resolver,
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
    json_object,
    validate_format_version,
    write_json_document,
)
from core.prompts.briefs import LibrarianCandidate, librarian_brief
from core.runs import RunKind
from core.settings import is_valid_agent_id
from core.skills import (
    SkillAuthoringError,
    SkillAuthoringService,
    SkillRecord,
    SkillRegistry,
    SkillWriter,
)
from core.skills._history import BACKGROUND_WRITABLE_ORIGINS
from core.skills.skills import scan_skill_resources
from core.utils.errors import VBotError
from core.utils.logging import get_logger
from core.utils.timestamps import format_canonical_timestamp, parse_timestamp
from core.utils.workers import BoundedWorkerPool

if TYPE_CHECKING:
    from core.runtime.interfaces import RuntimeServices
    from core.skills import SkillRevision
    from core.statistics.skills import SkillUse

_LOGGER = get_logger("automation.librarian")

# Blocking state, history and package work of passes and status reads.
_LIBRARIAN_WORKERS = BoundedWorkerPool(name="librarian", max_workers=2)

# The consolidation Run's dispatch boundary and its cost bound.
LIBRARIAN_TOOL_RESTRICTION = SKILL_REFLECTION_TOOL_RESTRICTION
LIBRARIAN_TOOL_ITERATION_LIMIT = 60
# Tool result for a call outside the boundary. ``tool`` is the called Tool and
# ``tools`` the allowed ones, both as the Model names them.
LIBRARIAN_TOOL_DENIAL_MESSAGE = (
    "Nothing was run: {tool} is not available in this maintenance pass. "
    "This pass can call only {tools}. Do not retry this call."
)

# The first check waits until startup has settled; later checks run hourly.
FIRST_CHECK_DELAY_SECONDS = 300.0
CHECK_INTERVAL_SECONDS = 3600.0

# Aging archives only Skills that a background Run created.
AGED_ORIGINS = frozenset({"reflection", "librarian"})
_LIBRARIAN_WRITER = SkillWriter(actor="librarian", run_kind=RunKind.LIBRARIAN.value)

LIBRARIAN_STATE_FILENAME = "librarian.json"
LIBRARIAN_STATE_FORMAT_VERSION = 1
_PASS_FIELDS = frozenset(
    {
        "started_at",
        "finished_at",
        "trigger",
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
    {"last_pass", "consolidation_fingerprint"},
    {"last_pass": json_object(_PASS_FIELDS)},
)
PassTrigger = Literal["schedule", "manual"]
ConsolidationOutcome = Literal["ran", "unchanged", "too_few", "disabled", "failed"]
_TRIGGERS = frozenset({"schedule", "manual"})
_CONSOLIDATION_OUTCOMES = frozenset({"ran", "unchanged", "too_few", "disabled", "failed"})
_COUNT_FIELDS = ("archived", "candidates", "created", "changed", "merged")
_TEXT_FIELDS = ("session_id", "run_id", "error")


class LibrarianError(VBotError):
    """A Librarian pass cannot start."""


class LibrarianBusyError(LibrarianError):
    """A pass of the Agent is running, or the Agent has active or queued Runs."""


class LibrarianUnavailableError(LibrarianError):
    """The Agent cannot be curated: it cannot call ``skill`` and ``skill_manage``."""


class LibrarianStateError(LibrarianError):
    """The Agent's ``librarian.json`` cannot be read; no pass runs until it is fixed."""


@dataclass(frozen=True)
class LibrarianPass:
    """What one finished pass did; the ``last_pass`` of the state document."""

    started_at: str
    finished_at: str
    trigger: PassTrigger
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
    """One Agent's Librarian state document."""

    last_pass: LibrarianPass | None = None
    consolidation_fingerprint: str | None = None

    def to_document(self) -> dict[str, Any]:
        document: dict[str, Any] = {}
        if self.last_pass is not None:
            document["last_pass"] = self.last_pass.to_dict()
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
    fingerprint = data.get("consolidation_fingerprint")
    if fingerprint is not None and not isinstance(fingerprint, str):
        add_error(diagnostics, "$.consolidation_fingerprint", "must be a string")
    last_pass = data.get("last_pass")
    if last_pass is None:
        return diagnostics
    if not isinstance(last_pass, dict):
        add_error(diagnostics, "$.last_pass", "must be an object")
        return diagnostics
    warn_unknown_keys(diagnostics, "$.last_pass", last_pass, _PASS_FIELDS, "field")
    for key in ("started_at", "finished_at"):
        value = last_pass.get(key)
        try:
            parse_timestamp(value if isinstance(value, str) else "")
        except ValueError:
            add_error(diagnostics, f"$.last_pass.{key}", "must be an ISO 8601 timestamp")
    if last_pass.get("trigger") not in _TRIGGERS:
        add_error(diagnostics, "$.last_pass.trigger", "must be schedule or manual")
    if last_pass.get("consolidation") not in _CONSOLIDATION_OUTCOMES:
        add_error(
            diagnostics,
            "$.last_pass.consolidation",
            f"must be one of: {', '.join(sorted(_CONSOLIDATION_OUTCOMES))}",
        )
    for key in _COUNT_FIELDS:
        value = last_pass.get(key, 0)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            add_error(diagnostics, f"$.last_pass.{key}", "must be a non-negative integer")
    revisions = last_pass.get("archived_revisions", [])
    if not isinstance(revisions, list) or any(
        isinstance(item, bool) or not isinstance(item, int) for item in revisions
    ):
        add_error(diagnostics, "$.last_pass.archived_revisions", "must be a list of integers")
    for key in _TEXT_FIELDS:
        value = last_pass.get(key)
        if value is not None and not isinstance(value, str):
            add_error(diagnostics, f"$.last_pass.{key}", "must be a string")
    return diagnostics


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
    started_at: str
    trigger: PassTrigger
    task: asyncio.Task[None] | None = field(default=None, repr=False)


def librarian_tool_denial_resolver() -> Callable[[str], str | None]:
    """Deny every Tool but ``skill`` and ``skill_manage`` in a consolidation Run."""
    return tool_denial_resolver(LIBRARIAN_TOOL_RESTRICTION, LIBRARIAN_TOOL_DENIAL_MESSAGE)


def librarian_candidates(
    authoring: SkillAuthoringService,
    root: Path,
    *,
    usage: Mapping[str, SkillUse],
    scheduled: frozenset[str],
    records: Mapping[str, SkillRecord] | None = None,
) -> tuple[LibrarianCandidate, ...]:
    """Return the Skills of the home ``root`` that a Librarian pass may change.

    A candidate is a loadable, unpinned Skill that the Agent, a Reflection
    review or an earlier pass created. ``usage`` is the Agent's Skill use by
    name and ``scheduled`` the names live automations trigger. Blocking.
    """
    if records is None:
        records = authoring.records(root)
    registry = SkillRegistry.load(root)
    candidates: list[LibrarianCandidate] = []
    for name in sorted(records):
        record = records[name]
        if record.pinned or record.origin not in BACKGROUND_WRITABLE_ORIGINS:
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
                scheduled=name in scheduled,
            )
        )
    return tuple(candidates)


class LibrarianService:
    """Scheduled and on-demand Librarian passes over Identity Agents' own Skills."""

    def __init__(
        self,
        runtime: RuntimeServices,
        *,
        authoring: SkillAuthoringService,
        skills_dir: Callable[[str], Path],
        skill_usage: Callable[[], Awaitable[Mapping[tuple[str, str], SkillUse]]],
        triggered_skill_names: Callable[[str], frozenset[str]],
        skills_changed: Callable[[str], None],
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._runtime = runtime
        self._authoring = authoring
        self._skills_dir = skills_dir
        self._skill_usage = skill_usage
        self._triggered_skill_names = triggered_skill_names
        self._skills_changed = skills_changed
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

        Raises ``AgentNotFoundError`` unless ``agent_id`` is an Identity Agent and
        :class:`LibrarianStateError` when its state document cannot be read.
        """
        agent = await self._identity_agent(agent_id)
        settings = await _LIBRARIAN_WORKERS.run(self._runtime.storage.load_librarian_settings)
        state, changes = await _LIBRARIAN_WORKERS.run(self._state_and_changes, agent_id)
        active = self._active.get(agent_id)
        last_pass = state.last_pass
        next_due = None
        if settings["enabled"] and last_pass is not None:
            next_due = _next_due(last_pass, settings["interval_days"])
        return {
            "agent_id": agent_id,
            "settings": dict(settings),
            "available": self._eligible(agent),
            "running": active is not None,
            "running_since": None if active is None else active.started_at,
            "last_pass": None if last_pass is None else last_pass.to_dict(),
            "next_due_at": next_due,
            "changes": [revision.to_dict() for revision in changes],
        }

    async def run(self, agent_id: str) -> dict[str, Any]:
        """Start a pass of ``agent_id`` now, ignoring the interval; return its status.

        Raises :class:`LibrarianBusyError` while a pass of the Agent runs or the
        Agent has active or queued Runs, :class:`LibrarianUnavailableError` when
        the Agent cannot call ``skill`` and ``skill_manage``, and the errors of
        :meth:`status`.
        """
        if self._closed:
            raise LibrarianBusyError("The Librarian is shutting down.")
        agent = await self._identity_agent(agent_id)
        # An unreadable state document refuses here, before any Skill changes.
        await _LIBRARIAN_WORKERS.run(self._read_state, agent_id)
        if not self._eligible(agent):
            raise LibrarianUnavailableError(
                f"Agent {agent_id} cannot call skill and skill_manage, so the Librarian "
                "cannot curate its Skills."
            )
        if agent_id in self._active:
            raise LibrarianBusyError(f"A Librarian pass of Agent {agent_id} is already running.")
        if self._runtime.chat_run_manager.has_activity_for_agent(agent_id, project_id=None):
            raise LibrarianBusyError(
                f"Agent {agent_id} has an active or queued Run; run the Librarian when it is idle."
            )
        active = self._begin(agent_id, "manual")
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
        """Run one pass for each due, idle and eligible Identity Agent, one at a time."""
        settings = await _LIBRARIAN_WORKERS.run(self._runtime.storage.load_librarian_settings)
        if not settings["enabled"]:
            return
        agent_ids = await _LIBRARIAN_WORKERS.run(
            lambda: [agent.id for agent in self._runtime.agents.list()]
        )
        for agent_id in agent_ids:
            if self._closed:
                return
            if agent_id in self._active:
                continue
            try:
                state = await _LIBRARIAN_WORKERS.run(self._read_state, agent_id)
            except LibrarianStateError as error:
                _LOGGER.warning("Librarian skipped an Agent (agent=%s): %s", agent_id, error)
                continue
            if (
                state.last_pass is not None
                and parse_timestamp(_next_due(state.last_pass, settings["interval_days"]))
                > self._clock()
            ):
                continue
            try:
                agent = await self._runtime.agent_resolver.resolve_agent_async(None, agent_id)
            except Exception:
                # A removed or broken Agent is skipped; the next check sees it again.
                continue
            # Checked after the last await, so no Run or manual pass starts in between.
            if (
                not self._eligible(agent)
                or agent_id in self._active
                or self._runtime.chat_run_manager.has_activity_for_agent(agent_id, project_id=None)
            ):
                continue
            await self._guarded_pass(agent_id, self._begin(agent_id, "schedule"))

    async def _identity_agent(self, agent_id: str) -> Any:
        """Resolve ``agent_id`` as an Identity Agent, never a Config Agent or Sub-Agent."""
        if not await _LIBRARIAN_WORKERS.run(self._runtime.agents.exists, agent_id):
            raise AgentNotFoundError(f"Agent not found: {agent_id}")
        return await self._runtime.agent_resolver.resolve_agent_async(None, agent_id)

    def _eligible(self, agent: Any) -> bool:
        if not getattr(agent, "workspace", None):
            return False
        return callable_review_dimensions(agent, self._runtime.tools.list_tools())[1]

    def _begin(self, agent_id: str, trigger: PassTrigger) -> _ActivePass:
        active = _ActivePass(started_at=format_canonical_timestamp(self._clock()), trigger=trigger)
        self._active[agent_id] = active
        return active

    async def _guarded_pass(self, agent_id: str, active: _ActivePass) -> None:
        try:
            await self._pass(agent_id, active)
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.warning("Librarian pass failed (agent=%s)", agent_id, exc_info=True)
        finally:
            if self._active.get(agent_id) is active:
                del self._active[agent_id]

    # -- one pass ----------------------------------------------------------------

    async def _pass(self, agent_id: str, active: _ActivePass) -> None:
        state = await _LIBRARIAN_WORKERS.run(self._read_state, agent_id)
        settings = await _LIBRARIAN_WORKERS.run(self._runtime.storage.load_librarian_settings)
        scheduled = self._triggered_skill_names(agent_id)
        usage = {
            name: use
            for (owner, name), use in (await self._skill_usage()).items()
            if owner == agent_id
        }
        cutoff = self._clock() - timedelta(days=settings["archive_after_days"])
        library = await _LIBRARIAN_WORKERS.run(
            self._age_library, agent_id, usage, scheduled, cutoff
        )
        if library.archived_names:
            self._skills_changed(agent_id)
            _LOGGER.info(
                "Librarian archived inactive Skills (agent=%s count=%d)",
                agent_id,
                len(library.archived_names),
            )
        outcome: ConsolidationOutcome
        fingerprint = state.consolidation_fingerprint
        session_id = run_id = error = None
        if not settings["consolidate"]:
            outcome = "disabled"
        elif len(library.candidates) < 2:
            outcome = "too_few"
        elif library.fingerprint == state.consolidation_fingerprint:
            outcome = "unchanged"
        else:
            outcome, session_id, run_id, error = await self._consolidate(agent_id, library)
            if outcome == "ran":
                fingerprint = await _LIBRARIAN_WORKERS.run(
                    self._fingerprint_now, agent_id, usage, scheduled
                )
        counts = (
            await _LIBRARIAN_WORKERS.run(self._run_counts, agent_id, run_id)
            if run_id is not None
            else (0, 0, 0)
        )
        finished = LibrarianPass(
            started_at=active.started_at,
            finished_at=format_canonical_timestamp(self._clock()),
            trigger=active.trigger,
            archived=len(library.archived_names),
            archived_revisions=library.archived_revisions,
            candidates=len(library.candidates),
            consolidation=outcome,
            session_id=session_id,
            run_id=run_id,
            created=counts[0],
            changed=counts[1],
            merged=counts[2],
            error=error,
        )
        await _LIBRARIAN_WORKERS.run(
            self._write_state,
            agent_id,
            LibrarianState(last_pass=finished, consolidation_fingerprint=fingerprint),
        )
        _LOGGER.info(
            "Librarian pass completed (agent=%s trigger=%s archived=%d candidates=%d "
            "consolidation=%s created=%d changed=%d merged=%d)",
            agent_id,
            active.trigger,
            finished.archived,
            finished.candidates,
            outcome,
            finished.created,
            finished.changed,
            finished.merged,
        )

    def _age_library(
        self,
        agent_id: str,
        usage: Mapping[str, SkillUse],
        scheduled: frozenset[str],
        cutoff: datetime,
    ) -> _Library:
        """Archive the inactive Skills, then describe what consolidation may change."""
        with self._runtime.agents.lifecycle_guard():
            root = self._skills_dir(agent_id)
            if not root.is_dir():
                return _Library((), (), (), _fingerprint({}))
            records = self._authoring.records(root)
            archived: list[str] = []
            revisions: list[int] = []
            for name, record in sorted(records.items()):
                if not _inactive(record, usage.get(name), cutoff) or name in scheduled:
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
            candidates = librarian_candidates(
                self._authoring, root, usage=usage, scheduled=scheduled, records=records
            )
            return _Library(
                tuple(archived),
                tuple(revisions),
                candidates,
                _fingerprint(self._latest_revisions(root, candidates)),
            )

    def _fingerprint_now(
        self, agent_id: str, usage: Mapping[str, SkillUse], scheduled: frozenset[str]
    ) -> str:
        """Fingerprint the candidates as a consolidation Run left them."""
        with self._runtime.agents.lifecycle_guard():
            root = self._skills_dir(agent_id)
            if not root.is_dir():
                return _fingerprint({})
            candidates = librarian_candidates(
                self._authoring, root, usage=usage, scheduled=scheduled
            )
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
        self, agent_id: str, library: _Library
    ) -> tuple[ConsolidationOutcome, str | None, str | None, str | None]:
        """Run the consolidation Run; return its outcome, Session, Run and error."""
        brief = await _LIBRARIAN_WORKERS.run(
            librarian_brief,
            self._runtime.storage,
            library.candidates,
            limit=LIBRARIAN_TOOL_ITERATION_LIMIT,
        )
        sessions = self._runtime.chat_sessions
        session = await sessions.create_async(agent_id, run_kind=RunKind.LIBRARIAN)
        try:
            run = await self._runtime.streaming_chat_loop.start_run(
                agent_id,
                brief,
                session_id=session.id,
                internal=True,
                tool_restriction=LIBRARIAN_TOOL_RESTRICTION,
                tool_denial_resolver=librarian_tool_denial_resolver(),
                max_tool_iterations=LIBRARIAN_TOOL_ITERATION_LIMIT,
                run_kind=RunKind.LIBRARIAN,
                contributes_to_agent_activity=False,
            )
        except Exception as error:
            # Nothing ran in the new Session: remove it rather than leave it empty.
            with suppress(Exception):
                await _LIBRARIAN_WORKERS.run(sessions.delete, session.address)
            _LOGGER.warning("Librarian consolidation did not start (agent=%s): %s", agent_id, error)
            return "failed", None, None, str(error)
        try:
            await run.wait()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            _LOGGER.warning("Librarian consolidation failed (agent=%s): %s", agent_id, error)
            return "failed", session.id, run.id, str(error) or type(error).__name__
        return "ran", session.id, run.id, None

    def _run_counts(self, agent_id: str, run_id: str) -> tuple[int, int, int]:
        """Count the Skills a consolidation Run created, changed and merged away."""
        revisions = [
            revision
            for revision in self._home_history(agent_id)
            if revision.actor == "librarian" and revision.run_id == run_id
        ]
        created = {revision.skill for revision in revisions if revision.kind == "create"}
        merged = {revision.skill for revision in revisions if revision.reason == "absorbed"}
        changed = {revision.skill for revision in revisions} - created - merged
        return len(created), len(changed), len(merged)

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
        return LibrarianState(
            last_pass=None if last_pass is None else LibrarianPass.from_dict(last_pass),
            consolidation_fingerprint=data.get("consolidation_fingerprint"),
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

    def _state_and_changes(self, agent_id: str) -> tuple[LibrarianState, list[SkillRevision]]:
        """Return the state and the Skill revisions of its last pass, newest first."""
        state = self._read_state(agent_id)
        last_pass = state.last_pass
        if last_pass is None:
            return state, []
        archived = set(last_pass.archived_revisions)
        changes = [
            revision
            for revision in self._home_history(agent_id)
            if revision.id in archived
            or (
                last_pass.run_id is not None
                and revision.actor == "librarian"
                and revision.run_id == last_pass.run_id
            )
        ]
        return state, changes

    def _home_history(self, agent_id: str) -> list[SkillRevision]:
        """The newest revisions of the Agent's private Skill home, newest first."""
        with self._runtime.agents.lifecycle_guard():
            root = self._skills_dir(agent_id)
            if not root.is_dir():
                return []
            return self._authoring.history(root, limit=_ALL_REVISIONS)


# A history limit that reads every revision of a home.
_ALL_REVISIONS = 1_000_000


def _inactive(record: SkillRecord, use: SkillUse | None, cutoff: datetime) -> bool:
    """Whether aging archives ``record``: background-made, unpinned and idle since ``cutoff``."""
    if record.pinned or record.origin not in AGED_ORIGINS:
        return False
    moments = [record.created_at, record.changed_at, None if use is None else use.last_activated]
    try:
        last_activity = max(parse_timestamp(moment) for moment in moments if moment)
    except ValueError:
        # A time that cannot be read never ages a Skill.
        return False
    return last_activity < cutoff


def _fingerprint(latest_revisions: Mapping[str, int]) -> str:
    """Fingerprint the candidates by name and latest revision id."""
    payload = json.dumps(sorted(latest_revisions.items()), separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _next_due(last_pass: LibrarianPass, interval_days: int) -> str:
    finished = parse_timestamp(last_pass.finished_at)
    return format_canonical_timestamp(finished + timedelta(days=interval_days))


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
    "LIBRARIAN_STATE_FILENAME",
    "LIBRARIAN_STATE_FORMAT",
    "LIBRARIAN_TOOL_DENIAL_MESSAGE",
    "LIBRARIAN_TOOL_ITERATION_LIMIT",
    "LIBRARIAN_TOOL_RESTRICTION",
    "LibrarianBusyError",
    "LibrarianError",
    "LibrarianPass",
    "LibrarianService",
    "LibrarianState",
    "LibrarianStateError",
    "LibrarianUnavailableError",
    "librarian_candidates",
    "librarian_tool_denial_resolver",
    "validate_librarian_state_file",
]
