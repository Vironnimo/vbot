"""In-memory completion tracking for spawned sub-agent batches.

``SubAgentBatchTracker`` is the state machine behind :class:`SubAgentCoordinator`
(in ``subagents.py``). It records reserved slots, queued and live sub-agent runs per
parent run, and fires the batch-completion trigger once every entry finishes.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from core.runs import RunStatus
from core.tools.tools import JsonObject
from core.utils.logging import get_logger

_LOGGER = get_logger("subagents")

ParentKey = tuple[str, str, str]


@dataclass
class _SubAgentEntry:
    agent_id: str
    session_id: str
    run_id: str | None
    queue_item_id: str | None = None
    complete: bool = False
    fetched: bool = False
    result: JsonObject | None = None


@dataclass
class _SubAgentBatch:
    entries: dict[str, _SubAgentEntry]
    reserved_count: int = 0
    notification_sent: bool = False


class SubAgentBatchTracker:
    """Track spawned sub-agent batches for one parent run in memory."""

    def __init__(self, trigger_service: Any) -> None:
        self._trigger_service = trigger_service
        self._batches: dict[ParentKey, _SubAgentBatch] = {}

    def register(
        self,
        parent_key: ParentKey,
        sub_agent_id: str,
        sub_session_id: str,
        sub_run_id: str,
    ) -> None:
        """Register one spawned sub-agent run under a parent run batch."""
        batch = self._batches.setdefault(parent_key, _SubAgentBatch(entries={}))
        batch.entries[sub_run_id] = _SubAgentEntry(
            agent_id=sub_agent_id,
            session_id=sub_session_id,
            run_id=sub_run_id,
        )

    def reserve_slot(self, parent_key: ParentKey, max_count: int) -> bool:
        """Reserve one sub-agent slot before async session/run work begins."""
        batch = self._batches.setdefault(parent_key, _SubAgentBatch(entries={}))
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
    ) -> None:
        """Convert one reserved slot into a live sub-agent run entry."""
        batch = self._batches.setdefault(parent_key, _SubAgentBatch(entries={}))
        if batch.reserved_count > 0:
            batch.reserved_count -= 1
        batch.entries[sub_run_id] = _SubAgentEntry(
            agent_id=sub_agent_id,
            session_id=sub_session_id,
            run_id=sub_run_id,
        )

    def register_queued(
        self,
        parent_key: ParentKey,
        sub_agent_id: str,
        sub_session_id: str,
        queue_item_id: str,
    ) -> None:
        """Convert one reserved slot into a queued sub-agent run entry."""
        batch = self._batches.setdefault(parent_key, _SubAgentBatch(entries={}))
        if batch.reserved_count > 0:
            batch.reserved_count -= 1
        batch.entries[_queue_entry_key(queue_item_id)] = _SubAgentEntry(
            agent_id=sub_agent_id,
            session_id=sub_session_id,
            run_id=None,
            queue_item_id=queue_item_id,
        )

    def mark_started(
        self,
        parent_key: ParentKey,
        queue_item_id: str,
        sub_run_id: str,
    ) -> bool:
        """Move a queued sub-agent entry to its live run id."""
        batch = self._batches.get(parent_key)
        if batch is None:
            return False
        queued_key = _queue_entry_key(queue_item_id)
        entry = batch.entries.pop(queued_key, None)
        if entry is None:
            return False
        entry.run_id = sub_run_id
        batch.entries[sub_run_id] = entry
        return True

    def remove_queued(self, parent_key: ParentKey, queue_item_id: str) -> None:
        """Remove a queued sub-agent entry that will never start."""
        batch = self._batches.get(parent_key)
        if batch is None:
            return
        batch.entries.pop(_queue_entry_key(queue_item_id), None)
        self._prune_if_empty(parent_key, batch)

    def discard_parent(self, parent_key: ParentKey) -> None:
        """Discard all in-memory tracking for a parent run."""
        self._batches.pop(parent_key, None)

    def queued_entry_for_session(
        self,
        parent_key: ParentKey,
        sub_session_id: str,
    ) -> _SubAgentEntry | None:
        """Return the latest queued entry for a sub-agent session, if any."""
        batch = self._batches.get(parent_key)
        if batch is None:
            return None
        for entry in reversed(list(batch.entries.values())):
            if entry.session_id == sub_session_id and entry.run_id is None:
                return entry
        return None

    def on_sub_agent_complete(
        self,
        parent_key: ParentKey,
        sub_run_id: str,
        result_dict: JsonObject,
    ) -> None:
        """Mark one sub-agent complete and notify the parent when the batch is done."""
        batch = self._batches.get(parent_key)
        if batch is None:
            return
        entry = batch.entries.get(sub_run_id)
        if entry is None:
            return

        entry.complete = True
        entry.result = dict(result_dict)
        if batch.notification_sent or not self._all_complete(batch):
            self._prune_if_finished(parent_key, batch)
            return

        batch.notification_sent = True
        pending_entries = [
            candidate for candidate in batch.entries.values() if not candidate.fetched
        ]
        if not pending_entries:
            self._prune_if_finished(parent_key, batch)
            return

        message = _batch_completion_message(pending_entries)
        task = asyncio.create_task(
            self._trigger_service.trigger_run(
                parent_key[0],
                message,
                session_id=parent_key[1],
                internal=True,
            )
        )
        task.add_done_callback(
            lambda completed: _log_background_task_result(
                completed,
                "Sub-agent batch completion trigger failed for "
                f"agent={parent_key[0]} session={parent_key[1]} run={parent_key[2]}",
            )
        )

    def mark_fetched(
        self,
        parent_key: ParentKey,
        sub_session_id: str,
        sub_run_id: str | None = None,
    ) -> None:
        """Mark one sub-agent result as fetched by run id within a session."""
        batch = self._batches.get(parent_key)
        if batch is None:
            return

        target_run_id = sub_run_id or self.run_id_for_session(parent_key, sub_session_id)
        if target_run_id is None:
            return
        entry = batch.entries.get(target_run_id)
        if entry is None or entry.session_id != sub_session_id:
            return
        entry.fetched = True
        self._prune_if_finished(parent_key, batch)

    def run_id_for_session(self, parent_key: ParentKey, sub_session_id: str) -> str | None:
        """Return the registered run id for a sub-agent session in a parent batch."""
        batch = self._batches.get(parent_key)
        if batch is None:
            return None
        for entry in reversed(list(batch.entries.values())):
            if entry.session_id == sub_session_id and entry.run_id is not None:
                return entry.run_id
        return None

    def spawn_count(self, parent_key: ParentKey) -> int:
        """Return the number of sub-agents spawned by the parent run."""
        batch = self._batches.get(parent_key)
        if batch is None:
            return 0
        return self._spawn_count(batch)

    @staticmethod
    def _all_complete(batch: _SubAgentBatch) -> bool:
        return (
            batch.reserved_count == 0
            and bool(batch.entries)
            and all(entry.complete for entry in batch.entries.values())
        )

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


def _log_background_task_result(task: asyncio.Task[Any], message: str) -> None:
    if task.cancelled():
        return
    error = task.exception()
    if error is None:
        return
    _LOGGER.error(
        "%s: %s",
        message,
        error,
        exc_info=(type(error), error, error.__traceback__),
    )


def _batch_completion_message(entries: list[_SubAgentEntry]) -> str:
    lines = [
        "Sub-agent batch completed. The complete final output of each sub-agent is "
        "included below. Do not call subagent_result to fetch these again.",
        "",
        "Results:",
    ]
    for entry in entries:
        lines.append("")
        lines.append(f"### {entry.agent_id} (session {entry.session_id}) — {_entry_status(entry)}")
        lines.append(_entry_result_text(entry))
    return "\n".join(lines)


def _entry_status(entry: _SubAgentEntry) -> str:
    if entry.result is not None:
        status = entry.result.get("status")
        if isinstance(status, str) and status:
            return status
    return RunStatus.COMPLETED.value


def _entry_result_text(entry: _SubAgentEntry) -> str:
    if entry.result is None:
        return "(no output)"
    result = entry.result.get("result")
    if isinstance(result, str) and result:
        return result
    note = entry.result.get("note")
    if isinstance(note, str) and note:
        return f"(no output) {note}"
    return "(no output)"


def _queue_entry_key(queue_item_id: str) -> str:
    return f"queued:{queue_item_id}"
