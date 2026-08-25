"""In-memory completion tracking for spawned Sub-Agent batches.

``SubAgentBatchTracker`` is the state machine behind :class:`SubAgentCoordinator`
(in ``subagents.py``). It records reserved slots, queued and live sub-agent runs per
Parent Run, and submits each ready result to shared Run-boundary delivery.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from core.projects import format_agent_address
from core.runs import RunStatus
from core.sessions import SessionAddress
from core.tools.tools import JsonObject
from core.utils.logging import get_logger

_LOGGER = get_logger("subagents")

ParentKey = tuple[str, str, str]


@dataclass
class _SubAgentEntry:
    agent_id: str
    session_id: str
    run_id: str | None
    work_id: str = ""
    project_id: str | None = None
    activity_file: str | None = None
    queue_item_id: str | None = None
    complete: bool = False
    fetched: bool = False
    result: JsonObject | None = None
    completion_notice_id: str | None = None

    def __post_init__(self) -> None:
        if not self.work_id:
            self.work_id = self.run_id or self.queue_item_id or ""


@dataclass
class _SubAgentBatch:
    entries: dict[str, _SubAgentEntry]
    reserved_count: int = 0
    # The Parent Run's project, captured when the batch is first created. The
    # completion delivery continues the Parent under this project so a
    # project (config) agent resolves on its Team instead of falling through to
    # the identity path. ``None`` keeps the identity/global layout.
    project_id: str | None = None


class SubAgentBatchTracker:
    """Track spawned sub-agent batches for one parent run in memory."""

    def __init__(self, trigger_service: Any, *, sessions: Any | None = None) -> None:
        self._trigger_service = trigger_service
        self._sessions = sessions
        self._batches: dict[ParentKey, _SubAgentBatch] = {}

    def register(
        self,
        parent_key: ParentKey,
        sub_agent_id: str,
        sub_session_id: str,
        sub_run_id: str,
        project_id: str | None = None,
        activity_file: str | None = None,
        *,
        work_id: str | None = None,
    ) -> None:
        """Register one spawned sub-agent run under a parent run batch."""
        work_id = work_id or sub_run_id
        batch = self._batches.setdefault(parent_key, _SubAgentBatch(entries={}))
        batch.entries[work_id] = _SubAgentEntry(
            work_id=work_id,
            agent_id=sub_agent_id,
            project_id=project_id,
            session_id=sub_session_id,
            run_id=sub_run_id,
            activity_file=activity_file,
        )

    def reserve_slot(
        self, parent_key: ParentKey, max_count: int, project_id: str | None = None
    ) -> bool:
        """Reserve one sub-agent slot before async session/run work begins.

        The first reservation for a Parent Run records that Run's ``project_id``
        on the batch, so later completion delivery continues the Parent
        under the same project (``None`` keeps the identity layout).
        """
        batch = self._batches.get(parent_key)
        if batch is None:
            batch = _SubAgentBatch(entries={}, project_id=project_id)
            self._batches[parent_key] = batch
        if self._spawn_count(batch) >= max_count:
            self._prune_if_empty(parent_key, batch)
            return False
        batch.reserved_count += 1
        return True

    def release_slot(self, parent_key: ParentKey) -> None:
        """Release one previously reserved sub-agent slot."""
        batch = self._batches.get(parent_key)
        if batch is None:
            return
        if batch.reserved_count > 0:
            batch.reserved_count -= 1
        self._prune_if_empty(parent_key, batch)

    def register_reserved(
        self,
        parent_key: ParentKey,
        sub_agent_id: str,
        sub_session_id: str,
        sub_run_id: str,
        project_id: str | None = None,
        activity_file: str | None = None,
        *,
        work_id: str | None = None,
    ) -> None:
        """Convert one reserved slot into a live sub-agent run entry."""
        work_id = work_id or sub_run_id
        batch = self._batches.setdefault(parent_key, _SubAgentBatch(entries={}))
        if batch.reserved_count > 0:
            batch.reserved_count -= 1
        batch.entries[work_id] = _SubAgentEntry(
            work_id=work_id,
            agent_id=sub_agent_id,
            project_id=project_id,
            session_id=sub_session_id,
            run_id=sub_run_id,
            activity_file=activity_file,
        )

    def register_queued(
        self,
        parent_key: ParentKey,
        sub_agent_id: str,
        sub_session_id: str,
        queue_item_id: str,
        project_id: str | None = None,
        activity_file: str | None = None,
        *,
        work_id: str | None = None,
    ) -> None:
        """Convert one reserved slot into a queued sub-agent run entry."""
        work_id = work_id or queue_item_id
        batch = self._batches.setdefault(parent_key, _SubAgentBatch(entries={}))
        if batch.reserved_count > 0:
            batch.reserved_count -= 1
        batch.entries[work_id] = _SubAgentEntry(
            work_id=work_id,
            agent_id=sub_agent_id,
            project_id=project_id,
            session_id=sub_session_id,
            run_id=None,
            queue_item_id=queue_item_id,
            activity_file=activity_file,
        )

    def mark_started(
        self,
        parent_key: ParentKey,
        queue_item_id: str,
        sub_run_id: str,
    ) -> bool:
        """Attach the started Run to a queued entry without changing its public id."""
        batch = self._batches.get(parent_key)
        if batch is None:
            return False
        entry = next(
            (
                candidate
                for candidate in batch.entries.values()
                if candidate.queue_item_id == queue_item_id
            ),
            None,
        )
        if entry is None:
            return False
        entry.run_id = sub_run_id
        return True

    def remove_queued(self, parent_key: ParentKey, queue_item_id: str) -> None:
        """Remove a queued Sub-Agent entry that will never start."""
        batch = self._batches.get(parent_key)
        if batch is None:
            return
        work_id = next(
            (
                candidate_work_id
                for candidate_work_id, entry in batch.entries.items()
                if entry.queue_item_id == queue_item_id
            ),
            None,
        )
        if work_id is None or batch.entries.pop(work_id, None) is None:
            return
        self._prune_if_empty(parent_key, batch)
        self._prune_if_finished(parent_key, batch)

    def queued_entry_for_session(
        self,
        parent_key: ParentKey,
        sub_session_id: str,
        *,
        sub_agent_id: str | None = None,
        project_id: str | None = None,
    ) -> _SubAgentEntry | None:
        """Return the latest queued entry for a sub-agent session, if any."""
        batch = self._batches.get(parent_key)
        if batch is None:
            return None
        for entry in reversed(list(batch.entries.values())):
            if (
                entry.session_id == sub_session_id
                and entry.run_id is None
                and _entry_matches_target(entry, sub_agent_id, project_id)
            ):
                return entry
        return None

    def on_sub_agent_complete(
        self,
        parent_key: ParentKey,
        sub_run_id: str,
        result_dict: JsonObject,
    ) -> None:
        """Mark one Sub-Agent complete and submit it for Run-boundary delivery."""
        batch = self._batches.get(parent_key)
        if batch is None:
            return
        entry = next(
            (candidate for candidate in batch.entries.values() if candidate.run_id == sub_run_id),
            None,
        )
        if entry is None or entry.complete:
            return

        entry.complete = True
        entry.result = dict(result_dict)
        if entry.fetched:
            self._prune_if_finished(parent_key, batch)
            return

        notice_id = f"subagent:{parent_key[2]}:{entry.work_id}"
        entry.completion_notice_id = notice_id
        delivery = self._trigger_service.submit_completion(
            parent_key[0],
            parent_key[1],
            notice_id=notice_id,
            origin_run_id=parent_key[2],
            body=_entry_completion_message(entry),
            project_id=batch.project_id,
            on_persisted=lambda: self._acknowledge_delivered_entry(
                parent_key,
                entry.work_id,
                entry,
            ),
        )
        delivery.add_done_callback(
            lambda completed: self._on_completion_delivery_done(
                parent_key,
                entry.work_id,
                entry,
                completed,
                "Sub-Agent completion delivery failed for "
                f"agent={parent_key[0]} session={parent_key[1]} "
                f"run={parent_key[2]} work={entry.work_id}",
            )
        )

    def _on_completion_delivery_done(
        self,
        parent_key: ParentKey,
        work_id: str,
        delivered_entry: _SubAgentEntry,
        delivery: asyncio.Future[None],
        failure_message: str,
    ) -> None:
        if not delivery.cancelled() and delivery.exception() is not None:
            batch = self._batches.get(parent_key)
            if batch is not None and batch.entries.get(work_id) is delivered_entry:
                # A failed Future is terminal: transient start/write failures
                # remain pending inside completion delivery. If the Parent
                # Session no longer exists, release this process-local ownership
                # without marking the undelivered Child result as read.
                batch.entries.pop(work_id, None)
                self._prune_if_empty(parent_key, batch)
                self._prune_if_finished(parent_key, batch)
        _log_background_task_result(delivery, failure_message)

    def _acknowledge_delivered_entry(
        self,
        parent_key: ParentKey,
        work_id: str,
        delivered_entry: _SubAgentEntry,
    ) -> None:
        """Mark one exact child result fetched after its Parent note is persisted."""
        batch = self._batches.get(parent_key)
        if batch is None or batch.entries.get(work_id) is not delivered_entry:
            return
        delivered_entry.fetched = True
        delivered_entry.completion_notice_id = None
        if self._sessions is not None and delivered_entry.run_id is not None:
            try:
                self._sessions.mark_terminal_run_read(
                    SessionAddress(
                        project_id=delivered_entry.project_id,
                        agent_id=delivered_entry.agent_id,
                        session_id=delivered_entry.session_id,
                    ),
                    delivered_entry.run_id,
                )
            except Exception:
                _LOGGER.warning(
                    "Failed to acknowledge delivered sub-agent result (agent=%s session=%s run=%s)",
                    delivered_entry.agent_id,
                    delivered_entry.session_id,
                    delivered_entry.run_id,
                    exc_info=True,
                )
        self._prune_if_finished(parent_key, batch)

    def mark_fetched(
        self,
        parent_key: ParentKey,
        sub_session_id: str,
        sub_run_id: str | None = None,
        *,
        sub_agent_id: str | None = None,
        project_id: str | None = None,
    ) -> None:
        """Mark one sub-agent result as fetched by run id within a session."""
        batch = self._batches.get(parent_key)
        if batch is None:
            return

        target_run_id = sub_run_id or self.run_id_for_session(
            parent_key,
            sub_session_id,
            sub_agent_id=sub_agent_id,
            project_id=project_id,
        )
        if target_run_id is None:
            return
        entry = next(
            (
                candidate
                for candidate in batch.entries.values()
                if candidate.run_id == target_run_id
            ),
            None,
        )
        if (
            entry is None
            or entry.session_id != sub_session_id
            or not _entry_matches_target(entry, sub_agent_id, project_id)
        ):
            return
        self._cancel_completion_notice(parent_key, batch, entry)
        entry.fetched = True
        self._prune_if_finished(parent_key, batch)

    def _cancel_completion_notice(
        self,
        parent_key: ParentKey,
        batch: _SubAgentBatch,
        entry: _SubAgentEntry,
    ) -> None:
        notice_id = entry.completion_notice_id
        if notice_id is None:
            return
        self._trigger_service.cancel_completion(
            parent_key[0],
            parent_key[1],
            notice_id=notice_id,
            project_id=batch.project_id,
        )
        entry.completion_notice_id = None

    def run_id_for_session(
        self,
        parent_key: ParentKey,
        sub_session_id: str,
        *,
        sub_agent_id: str | None = None,
        project_id: str | None = None,
    ) -> str | None:
        """Return the registered run id for a sub-agent session in a parent batch."""
        batch = self._batches.get(parent_key)
        if batch is None:
            return None
        for entry in reversed(list(batch.entries.values())):
            if (
                entry.session_id == sub_session_id
                and entry.run_id is not None
                and _entry_matches_target(entry, sub_agent_id, project_id)
            ):
                return entry.run_id
        return None

    def owned_entry(
        self,
        parent_agent_id: str,
        parent_session_id: str,
        parent_project_id: str | None,
        work_id: str,
    ) -> tuple[ParentKey, _SubAgentEntry] | None:
        """Return one public work handle owned by the calling Parent Session."""
        for parent_key, batch in reversed(list(self._batches.items())):
            if (
                parent_key[0] != parent_agent_id
                or parent_key[1] != parent_session_id
                or batch.project_id != parent_project_id
            ):
                continue
            entry = batch.entries.get(work_id)
            if entry is not None:
                return parent_key, entry
        return None

    def activity_file_for_session(
        self,
        parent_key: ParentKey,
        sub_session_id: str,
        *,
        sub_run_id: str | None = None,
        sub_agent_id: str | None = None,
        project_id: str | None = None,
    ) -> str | None:
        """Return the matching queued or live entry's activity-file path."""
        batch = self._batches.get(parent_key)
        if batch is None:
            return None
        for entry in reversed(list(batch.entries.values())):
            if (
                entry.session_id == sub_session_id
                and (sub_run_id is None or entry.run_id == sub_run_id)
                and _entry_matches_target(entry, sub_agent_id, project_id)
            ):
                return entry.activity_file
        return None

    def spawn_count(self, parent_key: ParentKey) -> int:
        """Return the number of sub-agents spawned by the parent run."""
        batch = self._batches.get(parent_key)
        if batch is None:
            return 0
        return self._spawn_count(batch)

    def references_identity_agent(self, agent_id: str) -> bool:
        """Return whether an open batch still addresses an Identity Agent.

        A batch can outlive its parent Run while child work is queued or active;
        its completion callback still needs the parent's original address. Both
        the parent and child sides therefore block an Identity Agent rename until
        the relation has been fully delivered and pruned.
        """
        for parent_key, batch in self._batches.items():
            if batch.project_id is None and parent_key[0] == agent_id:
                return True
            if any(
                entry.project_id is None and entry.agent_id == agent_id
                for entry in batch.entries.values()
            ):
                return True
        return False

    @staticmethod
    def _spawn_count(batch: _SubAgentBatch) -> int:
        return len(batch.entries) + batch.reserved_count

    def _prune_if_empty(self, parent_key: ParentKey, batch: _SubAgentBatch) -> None:
        if batch.reserved_count == 0 and not batch.entries:
            self._batches.pop(parent_key, None)

    def _prune_if_finished(self, parent_key: ParentKey, batch: _SubAgentBatch) -> None:
        if (
            batch.reserved_count == 0
            and bool(batch.entries)
            and all(entry.complete and entry.fetched for entry in batch.entries.values())
        ):
            self._batches.pop(parent_key, None)


def _log_background_task_result(
    delivery: asyncio.Future[Any],
    message: str,
) -> None:
    if delivery.cancelled():
        return
    error = delivery.exception()
    if error is None:
        return
    _LOGGER.error(
        "%s: %s",
        message,
        error,
        exc_info=(type(error), error, error.__traceback__),
    )


def _entry_completion_message(entry: _SubAgentEntry) -> str:
    lines: list[str] = []
    address = format_agent_address(entry.agent_id, entry.project_id)
    lines.append(
        f"### Sub-Agent {address} (id {entry.work_id}, session {entry.session_id}) — "
        f"{_entry_status(entry)}"
    )
    if entry.activity_file is not None:
        lines.append(f"Activity file: {entry.activity_file}")
    lines.append(_entry_result_text(entry))
    return "\n".join(lines)


def _entry_status(entry: _SubAgentEntry) -> str:
    if entry.result is not None:
        if entry.result.get("cancelled_by_user"):
            return "cancelled by user"
        if entry.result.get("interrupted"):
            cause = entry.result.get("interruption_cause")
            if isinstance(cause, str) and cause:
                return f"interrupted ({cause})"
            return "interrupted"
        status = entry.result.get("status")
        if isinstance(status, str) and status:
            return status
    return RunStatus.COMPLETED.value


def _entry_result_text(entry: _SubAgentEntry) -> str:
    if entry.result is None:
        return "(no output)"
    if entry.result.get("cancelled_by_user"):
        result = entry.result.get("result")
        if isinstance(result, str) and result:
            return result
        return "Cancelled by the user"
    result = entry.result.get("result")
    if isinstance(result, str) and result:
        note = entry.result.get("note")
        if entry.result.get("interrupted") and isinstance(note, str) and note:
            return f"{result}\n\n{note}"
        return result
    note = entry.result.get("note")
    if isinstance(note, str) and note:
        return f"(no output) {note}"
    return "(no output)"


def _entry_matches_target(
    entry: _SubAgentEntry,
    sub_agent_id: str | None,
    project_id: str | None,
) -> bool:
    if sub_agent_id is None:
        return True
    return entry.agent_id == sub_agent_id and entry.project_id == project_id
