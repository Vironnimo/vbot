"""Background activity: the long-running server work Settings lists in one place.

The monitor collects, once per second, what the owners of such work report
from memory: local Model installations (speech and embeddings), the
WhatsApp support installation of each Channel, and a large semantic Recall
indexing pass. It publishes the list as the ``activity_status`` event
whenever it changes; ``activity.list`` returns it on request.

Each entry is ``{id, kind, label, state, phase, error, message, progress?,
target?, task_type?, eta_seconds?}``:

- ``state`` is ``running``, ``failed``, ``action_required`` (a restart is
  due) or ``completed``. Owners keep reporting finished work as
  ``completed``; the list shows it for ``COMPLETED_VISIBLE_S`` seconds only
  when the same work was running a moment before. Work that stops running
  without finishing or failing, such as a cancelled installation, leaves
  the list.
- ``failed`` and ``action_required`` entries stay until the work runs again
  or a client dismisses them; dismissing hides an entry for every client
  until it runs again and never changes the work itself.
- ``progress`` is ``{completed, total, unit}`` with ``unit`` ``bytes`` or
  ``items``.

The list is server memory only: a restart starts empty.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any

from core.utils.logging import get_logger

_LOGGER = get_logger("server.activity")

TICK_S = 1.0
COMPLETED_VISIBLE_S = 8.0
# Indexing this many waiting texts or more is shown; smaller passes finish in seconds.
RECALL_BACKLOG_SHOWN = 50
_STATE_ORDER = {"running": 0, "action_required": 1, "failed": 2, "completed": 3}

JsonObject = dict[str, Any]


class ActivityMonitor:
    """Collect background activity from a started Runtime and publish its changes."""

    def __init__(
        self,
        runtime: Any,
        publish: Callable[[list[JsonObject]], None],
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._runtime = runtime
        self._publish = publish
        self._clock = clock
        self._recall: JsonObject | None = None
        self._recall_pass_shown = False
        self._running: dict[str, JsonObject] = {}
        self._completed: dict[str, tuple[float, JsonObject]] = {}
        self._dismissed: set[str] = set()
        self._activities: list[JsonObject] = []
        self._task: asyncio.Task[None] | None = None

    @property
    def activities(self) -> list[JsonObject]:
        """The list as last published."""
        return self._activities

    def set_recall_status(self, status: JsonObject) -> None:
        """Take the semantic Recall index status (the ``recall.status`` shape)."""
        self._recall = status

    def dismiss(self, activity_id: str) -> list[JsonObject]:
        """Hide a failed, completed or restart entry until its work runs again."""
        self._dismissed.add(activity_id)
        self._completed.pop(activity_id, None)
        self.update()
        return self._activities

    def update(self) -> None:
        """Collect the current activity and publish the list if it changed."""
        now = self._clock()
        current = self._collect()
        for entry in current:
            if entry["state"] == "completed" and entry["id"] in self._running:
                self._completed[entry["id"]] = (now, entry)
        current = [entry for entry in current if entry["state"] != "completed"]
        ids = {entry["id"] for entry in current}
        self._running = {entry["id"]: entry for entry in current if entry["state"] == "running"}
        # Running work clears its dismissal, and so does work that is no longer reported.
        self._dismissed &= ids - self._running.keys()
        self._completed = {
            activity_id: (since, entry)
            for activity_id, (since, entry) in self._completed.items()
            if activity_id not in ids
            and activity_id not in self._dismissed
            and now - since < COMPLETED_VISIBLE_S
        }
        shown = [entry for entry in current if entry["id"] not in self._dismissed]
        shown.extend(entry for _since, entry in self._completed.values())
        shown.sort(key=lambda entry: (_STATE_ORDER[entry["state"]], entry["label"], entry["id"]))
        if shown != self._activities:
            self._activities = shown
            self._publish(shown)

    def _collect(self) -> list[JsonObject]:
        entries: list[JsonObject] = []
        for owner in ("speech", "embeddings"):
            try:
                reports = getattr(self._runtime, owner).local_activities()
            except RuntimeError:
                continue
            for report in reports:
                entries.append(
                    _entry(f"local_setup:{report['target']}", "local_model_install", report)
                )
        try:
            setups = self._runtime.channel_service.setup_activities()
        except RuntimeError:
            setups = []
        for report in setups:
            channel = report["channel_id"]
            entries.append(
                _entry(
                    f"whatsapp_setup:{channel}",
                    "whatsapp_setup",
                    {**report, "label": channel, "target": channel},
                )
            )
        recall = self._recall_activity()
        if recall is not None:
            entries.append(recall)
        return entries

    def _recall_activity(self) -> JsonObject | None:
        status = self._recall
        if status is None:
            return None
        if status.get("state") == "error":
            failure = status.get("last_error") or {}
            return _entry(
                "recall_index",
                "recall_index",
                {
                    "label": "",
                    "state": "failed",
                    "error": failure.get("code", ""),
                    "message": failure.get("message", ""),
                },
            )
        state = status.get("state")
        if state not in {"indexing", "retrying"}:
            self._recall_pass_shown = False
            # A shown pass that ends with nothing waiting finished.
            return (
                _entry("recall_index", "recall_index", {"label": "", "state": "completed"})
                if state == "idle"
                else None
            )
        waiting = int(status.get("waiting") or 0)
        # Once shown, a pass stays until it ends, also while it waits to retry,
        # so its entry does not flicker.
        if not self._recall_pass_shown and waiting < RECALL_BACKLOG_SHOWN:
            return None
        self._recall_pass_shown = True
        indexed = int(status.get("indexed") or 0)
        report: JsonObject = {
            "label": "",
            "state": "running",
            "phase": state,
            "progress": {"completed": indexed, "total": indexed + waiting},
        }
        if state == "indexing" and status.get("eta_seconds") is not None:
            report["eta_seconds"] = status["eta_seconds"]
        return _entry("recall_index", "recall_index", report, unit="items")

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="background-activity")

    async def _run(self) -> None:
        if self._recall is None:
            try:
                self._recall = (await self._runtime.recall.index_status()).to_dict()
            except Exception:
                _LOGGER.debug("Recall index status unavailable for background activity")
        while True:
            try:
                self.update()
            except Exception:
                _LOGGER.warning("Background activity update failed", exc_info=True)
            await asyncio.sleep(TICK_S)

    async def aclose(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


def _entry(activity_id: str, kind: str, report: JsonObject, *, unit: str = "bytes") -> JsonObject:
    entry: JsonObject = {
        "id": activity_id,
        "kind": kind,
        "label": report.get("label", ""),
        "state": report["state"],
        "phase": report.get("phase", ""),
        "error": report.get("error", ""),
        "message": report.get("message", ""),
    }
    for field in ("target", "task_type", "eta_seconds"):
        if field in report:
            entry[field] = report[field]
    if "progress" in report:
        entry["progress"] = {**report["progress"], "unit": unit}
    return entry
