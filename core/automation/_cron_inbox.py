"""What jobs that run on arrivals keep for their next Run: webhook requests and emails.

A job whose Runs start when something arrives instead of at a time (schedule
types ``webhook`` and ``email``) stores each arrival before the sender is told
it was taken. The arrivals waiting for a Run live in
``cron/<kind directory>/<sha256 of job id>.json``: runtime state outside the
JSON document contract, like once-fire claims and watch baselines, so it
carries no ``format_version``. At most ``INBOX_MAX_ITEMS`` arrivals and
``INBOX_MAX_CHARS`` characters wait; the oldest are dropped first and counted,
and the next Run is told how many it does not see.

The job's task (``_cron_scheduler``) starts a Run while arrivals wait; the Run
takes them all once it holds a Run slot and forgets them once it is admitted,
so arrivals during a Run start one follow-up Run. A Run that is not admitted
leaves its arrivals waiting for the next attempt. A process that stops after
admission but before the arrivals were forgotten on disk runs them again after
the restart.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol

from core.automation._cron_jobs import CronStorageError, _parse_utc_timestamp
from core.utils.atomic import atomic_write_text
from core.utils.log_conditions import LoggedConditions
from core.utils.logging import get_logger
from core.utils.workers import settle_before_cancelling

if TYPE_CHECKING:
    from core.automation.cron import CronService

#: How many arrivals, and how many characters of them, wait for one Run.
INBOX_MAX_ITEMS = 10
INBOX_MAX_CHARS = 16_000

#: How many keys of taken arrivals a job remembers, so a repeated hand-over is
#: recognized (email Message-IDs). The oldest are forgotten first.
INBOX_MAX_KEYS = 1024

_LOGGER = get_logger("automation.cron")


class InboxItem(Protocol):
    """One stored arrival as its Run receives it."""

    @property
    def seq(self) -> int:
        """Its number among the job's arrivals; increases by one per arrival."""
        ...

    @property
    def received_at(self) -> str:
        """When vBot received it, UTC."""
        ...

    @property
    def size(self) -> int:
        """The characters it takes in its Run's note, near enough for the waiting cap."""
        ...

    def to_dict(self) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class InboxKind[Item: InboxItem]:
    """What differs between the arrivals of one schedule type."""

    schedule_type: str
    """The schedule type of the jobs that keep these arrivals."""
    label: str
    """What the arrivals are called in log lines and errors, such as ``webhook requests``."""
    dir_name: str
    """The directory under ``cron/`` that keeps the jobs' state files."""
    items_key: str
    """The state file's key of the waiting arrivals."""
    parse_item: Callable[[Any], Item]
    """Reads one stored arrival; raises ``ValueError`` for an invalid one."""
    log_fields: tuple[str, str]
    """The ``Cron job fired`` line's names of a Run's arrivals and of those dropped."""


@dataclass(frozen=True, slots=True)
class InboxBatch[Item: InboxItem]:
    """The arrivals one Run receives and how many earlier ones it does not see."""

    items: tuple[Item, ...]
    dropped: int

    @property
    def seqs(self) -> frozenset[int]:
        return frozenset(item.seq for item in self.items)


@dataclass(slots=True)
class InboxState[Item: InboxItem]:
    """What a job keeps between arrivals and Runs."""

    job_id: str
    next_seq: int = 0
    last_received_at: str | None = None
    dropped: int = 0
    """Arrivals dropped unseen since the last Run started."""
    items: list[Item] = field(default_factory=list)
    """The arrivals waiting for a Run, oldest first."""
    keys: list[str] = field(default_factory=list)
    """Keys of the latest arrivals taken, oldest first, at most ``INBOX_MAX_KEYS``."""

    def copy(self) -> InboxState[Item]:
        return InboxState(
            self.job_id,
            self.next_seq,
            self.last_received_at,
            self.dropped,
            list(self.items),
            list(self.keys),
        )

    def add(
        self, item: Item, *, taken: frozenset[int] = frozenset(), key: str | None = None
    ) -> None:
        """Queue an arrival, dropping the oldest waiting ones beyond the caps.

        ``taken`` are the arrivals a starting Run already took; they never count
        toward the caps and are never dropped here. The newest arrival is always
        kept. ``key`` is remembered so the same arrival is not taken twice.
        """
        self.items.append(item)
        self.next_seq = max(self.next_seq, item.seq + 1)
        self.last_received_at = item.received_at
        if key is not None:
            self.keys.append(key)
            del self.keys[:-INBOX_MAX_KEYS]
        while True:
            waiting = [entry for entry in self.items if entry.seq not in taken]
            if len(waiting) <= 1 or (
                len(waiting) <= INBOX_MAX_ITEMS
                and sum(entry.size for entry in waiting) <= INBOX_MAX_CHARS
            ):
                return
            self.items.remove(waiting[0])
            self.dropped += 1

    def waiting(self, *, taken: frozenset[int] = frozenset()) -> list[Item]:
        return [item for item in self.items if item.seq not in taken]

    def consume(self, batch: InboxBatch[Item]) -> None:
        """Forget the arrivals of a Run that started; later drops stay counted."""
        seqs = batch.seqs
        self.items = [item for item in self.items if item.seq not in seqs]
        self.dropped = max(self.dropped - batch.dropped, 0)

    def to_dict(self, items_key: str) -> dict[str, Any]:
        data: dict[str, Any] = {
            "job_id": self.job_id,
            "next_seq": self.next_seq,
            "last_received_at": self.last_received_at,
            "dropped": self.dropped,
            items_key: [item.to_dict() for item in self.items],
        }
        if self.keys:
            data["keys"] = list(self.keys)
        return data


@dataclass(frozen=True, slots=True)
class InboxCounts:
    """What a job shows about the arrivals it keeps."""

    last_received_at: str | None
    waiting: int
    """Arrivals waiting for the next Run."""
    dropped: int
    """Arrivals dropped unseen since the last Run started."""
    error: str | None = None
    """Why the waiting arrivals cannot be read; None while they can."""


class InboxWriteError(CronStorageError):
    """An arrival could not be stored; its sender must hand it over again."""


#: What became of an arrival: stored, refused by the caller's check, or a key
#: the job already took.
InboxAdded = Literal["added", "refused", "duplicate"]


class CronInbox[Item: InboxItem]:
    """The stored arrivals and Run batches of the jobs of one schedule type."""

    def __init__(self, owner: CronService, kind: InboxKind[Item]) -> None:
        self._owner = owner
        self._kind = kind
        self._states: dict[str, InboxState[Item]] = {}
        self._state_errors: dict[str, str] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._wakes: dict[str, asyncio.Event] = {}
        self._taken: dict[str, InboxBatch[Item]] = {}
        self._conditions = LoggedConditions()

    @property
    def kind(self) -> InboxKind[Item]:
        return self._kind

    @property
    def schedule_type(self) -> str:
        return self._kind.schedule_type

    @property
    def state_dir(self) -> Path:
        return self._owner._cron_dir / self._kind.dir_name

    def counts(self, job_id: str) -> InboxCounts:
        state = self._states.get(job_id)
        taken = self._taken_seqs(job_id)
        return InboxCounts(
            last_received_at=None if state is None else state.last_received_at,
            waiting=0 if state is None else len(state.waiting(taken=taken)),
            dropped=0 if state is None else state.dropped,
            error=self._state_errors.get(job_id),
        )

    def load_states(self, job_ids: list[str]) -> None:
        """Read the stored arrivals of these jobs before their tasks start. Blocking."""
        for job_id in job_ids:
            with suppress(CronStorageError):
                self._states[job_id] = self._read_state(job_id)

    def wake(self, job_id: str) -> asyncio.Event:
        """The event a job's task waits on; set when an arrival is stored."""
        return self._wakes.setdefault(job_id, asyncio.Event())

    async def waiting(self, job_id: str) -> int:
        state = await self._state(job_id)
        return len(state.waiting(taken=self._taken_seqs(job_id)))

    async def discard(self, job_id: str) -> None:
        """Forget the job's waiting arrivals and the keys it took. A failed removal is logged."""
        async with self._lock(job_id):
            self._states.pop(job_id, None)
            self._state_errors.pop(job_id, None)
            self._taken.pop(job_id, None)
            await self._owner._writer.call_async(
                partial(remove_state, self._kind, self.state_dir, job_id)
            )
        self._wakes.pop(job_id, None)
        self._locks.pop(job_id, None)

    async def add(
        self,
        job_id: str,
        make: Callable[[int], Item],
        *,
        key: str | None = None,
        wanted: Callable[[], bool] = lambda: True,
    ) -> tuple[InboxAdded, InboxState[Item] | None]:
        """Store one arrival for the job's next Run and wake its task.

        ``make`` builds the arrival from its sequence number. ``wanted`` is
        checked under the job's lock, after any earlier arrival settled; an
        arrival it refuses is not stored. A ``key`` the job already took is a
        ``duplicate``. Raises ``CronStorageError`` when the waiting arrivals
        cannot be read and ``InboxWriteError`` when the arrival could not be
        stored. Once its save starts, caller cancellation waits for both
        persistence and publication.
        """
        async with self._lock(job_id):
            if not wanted():
                return "refused", None
            state = await self._state(job_id)
            if key is not None and key in state.keys:
                return "duplicate", state
            updated = state.copy()
            updated.add(make(state.next_seq), taken=self._taken_seqs(job_id), key=key)
            await settle_before_cancelling(self._commit_added(updated))
        return "added", updated

    async def _commit_added(self, state: InboxState[Item]) -> None:
        """Persist and publish a stored arrival before honoring caller cancellation."""
        try:
            await self._write(state)
        except CronStorageError as error:
            raise InboxWriteError(str(error)) from error
        self._states[state.job_id] = state
        self.wake(state.job_id).set()
        self._owner._notify_changed()

    async def take(self, job_id: str) -> InboxBatch[Item] | None:
        """Take the waiting arrivals for a Run about to start; None when none wait."""
        async with self._lock(job_id):
            state = await self._state(job_id)
            waiting = state.waiting(taken=self._taken_seqs(job_id))
            if not waiting:
                return None
            batch = InboxBatch(tuple(waiting), state.dropped)
            self._taken[job_id] = batch
            return batch

    def release(self, job_id: str, batch: InboxBatch[Item]) -> None:
        """Give back the arrivals of a Run that was not admitted; they wait for the next one."""
        if self._taken.get(job_id) is batch:
            self._taken.pop(job_id, None)

    async def consume(self, job_id: str, batch: InboxBatch[Item]) -> None:
        """Forget the arrivals of an admitted Run. A failed write is logged.

        Memory moves on at once; the arrivals stay on disk only until the next
        successful write, or run again after a restart.
        """
        async with self._lock(job_id):
            if self._taken.get(job_id) is batch:
                self._taken.pop(job_id, None)
            state = self._states.get(job_id)
            if state is None:
                return
            updated = state.copy()
            updated.consume(batch)
            self._states[job_id] = updated
            try:
                await self._write(updated)
            except CronStorageError as error:
                _LOGGER.error(
                    "Cron %s of a started Run not removed; they run again after a restart "
                    "(job=%s): %s",
                    self._kind.label,
                    job_id,
                    error,
                )
        self._owner._notify_changed()

    async def _state(self, job_id: str) -> InboxState[Item]:
        state = self._states.get(job_id)
        if state is None:
            previous_error = self._state_errors.get(job_id)
            try:
                state = await self._owner._writer.call_async(partial(self._read_state, job_id))
            finally:
                if previous_error != self._state_errors.get(job_id):
                    self._owner._notify_changed()
            # Another caller may have loaded or changed it meanwhile.
            state = self._states.setdefault(job_id, state)
        return state

    def _read_state(self, job_id: str) -> InboxState[Item]:
        try:
            state = read_state(self._kind, self.state_dir, job_id)
        except CronStorageError as error:
            self._state_errors[job_id] = str(error)
            if self._conditions.started(("state", job_id)):
                _LOGGER.warning(
                    "Cron %s unavailable; retained for retry (job=%s)", self._kind.label, job_id
                )
            raise
        self._state_errors.pop(job_id, None)
        if self._conditions.ended(("state", job_id)):
            _LOGGER.info("Cron %s readable again (job=%s)", self._kind.label, job_id)
        return state

    async def _write(self, state: InboxState[Item]) -> None:
        await self._owner._writer.call_async(
            partial(
                write_state,
                self.state_dir,
                state.job_id,
                state.to_dict(self._kind.items_key),
            )
        )

    def _taken_seqs(self, job_id: str) -> frozenset[int]:
        batch = self._taken.get(job_id)
        return frozenset() if batch is None else batch.seqs

    def _lock(self, job_id: str) -> asyncio.Lock:
        return self._locks.setdefault(job_id, asyncio.Lock())


def state_path(directory: Path, job_id: str) -> Path:
    digest = hashlib.sha256(job_id.encode("utf-8")).hexdigest()
    return directory / f"{digest}.json"


def read_state[Item: InboxItem](
    kind: InboxKind[Item], directory: Path, job_id: str
) -> InboxState[Item]:
    """Read stored arrivals; only a missing file means an empty queue."""
    path = state_path(directory, job_id)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return _parse_state(kind, payload, job_id)
    except FileNotFoundError:
        return InboxState(job_id)
    except (OSError, UnicodeError, ValueError, TypeError) as error:
        raise CronStorageError(
            f"Waiting {kind.label} could not be read; retrying without replacing them."
        ) from error


def write_state(directory: Path, job_id: str, payload: dict[str, Any]) -> None:
    """Persist ``payload``, an ``InboxState.to_dict``, atomically.

    Blocking; raises ``CronStorageError``.
    """
    path = state_path(directory, job_id)
    serialized = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, serialized)
    except OSError as error:
        raise CronStorageError(f"Cannot write {path}: {error}") from error


def remove_state(kind: InboxKind[Any], directory: Path, job_id: str) -> None:
    path = state_path(directory, job_id)
    try:
        path.unlink(missing_ok=True)
    except OSError as error:
        _LOGGER.warning("Cannot remove Cron %s for job=%s: %s", kind.label, job_id, error)


def _parse_state[Item: InboxItem](
    kind: InboxKind[Item], payload: Any, job_id: str
) -> InboxState[Item]:
    if not isinstance(payload, dict) or payload.get("job_id") != job_id:
        raise ValueError("job_id mismatch")
    next_seq, dropped = payload.get("next_seq"), payload.get("dropped")
    last_received_at = payload.get("last_received_at")
    for name, value in (("next_seq", next_seq), ("dropped", dropped)):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{name} must be a whole number")
    if last_received_at is not None:
        if not isinstance(last_received_at, str):
            raise ValueError("last_received_at must be a timestamp or null")
        _parse_utc_timestamp(last_received_at, field_name="last_received_at")
    items = payload.get(kind.items_key)
    if not isinstance(items, list):
        raise ValueError(f"{kind.items_key} must be a list")
    keys = payload.get("keys", [])
    if not isinstance(keys, list) or not all(isinstance(key, str) for key in keys):
        raise ValueError("keys must be a list of text")
    parsed = [kind.parse_item(item) for item in items]
    assert isinstance(next_seq, int) and isinstance(dropped, int)
    if any(item.seq >= next_seq for item in parsed):
        raise ValueError("an item's seq is not below next_seq")
    return InboxState(job_id, next_seq, last_received_at, dropped, parsed, keys[-INBOX_MAX_KEYS:])
