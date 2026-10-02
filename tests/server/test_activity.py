"""Background activity: what Settings lists, how entries end, and the activity RPCs."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from server.activity import COMPLETED_VISIBLE_S, RECALL_BACKLOG_SHOWN, ActivityMonitor
from server.rpc.methods import dispatch_rpc


class _Work:
    """What the Runtime's owners report, as a controllable fake Runtime."""

    def __init__(self) -> None:
        self.speech: list[dict[str, Any]] = []
        self.embeddings: list[dict[str, Any]] = []
        self.whatsapp: list[dict[str, Any]] = []
        self.recall_status: dict[str, Any] = {"state": "idle", "indexed": 0, "waiting": 0}
        self.now = 0.0
        self.published: list[list[dict[str, Any]]] = []
        self.runtime = SimpleNamespace(
            speech=SimpleNamespace(local_activities=lambda: self.speech),
            embeddings=SimpleNamespace(local_activities=lambda: self.embeddings),
            channel_service=SimpleNamespace(setup_activities=lambda: self.whatsapp),
            recall=SimpleNamespace(index_status=self._index_status),
        )
        self.monitor = ActivityMonitor(self.runtime, self.published.append, clock=lambda: self.now)

    async def _index_status(self) -> SimpleNamespace:
        return SimpleNamespace(to_dict=lambda: self.recall_status)

    def states(self) -> list[tuple[str, str]]:
        return [(entry["id"], entry["state"]) for entry in self.monitor.activities]


_INSTALL = {
    "target": "local/parakeet",
    "label": "Parakeet",
    "task_type": "speech_to_text",
    "state": "running",
    "phase": "downloading",
    "progress": {"completed": 10, "total": 40},
}


def test_finished_installation_shows_as_completed_briefly_and_only_changes_publish() -> None:
    work = _Work()
    work.monitor.update()
    assert work.published == [] and work.monitor.activities == []

    work.speech = [_INSTALL]
    work.monitor.update()
    work.monitor.update()
    assert work.published == [
        [
            {
                "id": "local_setup:local/parakeet",
                "kind": "local_model_install",
                "label": "Parakeet",
                "state": "running",
                "phase": "downloading",
                "error": "",
                "message": "",
                "target": "local/parakeet",
                "task_type": "speech_to_text",
                "progress": {"completed": 10, "total": 40, "unit": "bytes"},
            }
        ]
    ]

    # A WhatsApp installation runs alongside; running entries come first.
    finished = {key: value for key, value in _INSTALL.items() if key != "progress"}
    work.speech = [{**finished, "state": "completed", "phase": ""}]
    work.whatsapp = [{"channel_id": "wa", "state": "running"}]
    work.monitor.update()
    completed = work.monitor.activities[1]
    assert work.states() == [
        ("whatsapp_setup:wa", "running"),
        ("local_setup:local/parakeet", "completed"),
    ]
    assert "progress" not in completed and completed["phase"] == ""

    # Finished work stays reported, but shows only briefly after it ran.
    work.now = COMPLETED_VISIBLE_S
    work.monitor.update()
    assert work.states() == [("whatsapp_setup:wa", "running")]

    # Work that stops without finishing, such as a cancelled installation, leaves at once.
    work.whatsapp = []
    work.monitor.update()
    assert work.monitor.activities == []
    assert len(work.published) == 4


def test_failed_and_restart_entries_stay_until_dismissed_or_run_again() -> None:
    work = _Work()
    work.speech = [{**_INSTALL, "state": "failed", "error": "download_failed"}]
    work.embeddings = [
        {
            "target": "local/granite",
            "label": "Granite",
            "task_type": "text_embedding",
            "state": "action_required",
            "phase": "restart_required",
        }
    ]
    work.monitor.update()
    work.now = 10 * COMPLETED_VISIBLE_S
    work.monitor.update()
    assert work.states() == [
        ("local_setup:local/granite", "action_required"),
        ("local_setup:local/parakeet", "failed"),
    ]

    # Dismissing hides an entry even while its owner still reports it.
    assert [entry["id"] for entry in work.monitor.dismiss("local_setup:local/parakeet")] == [
        "local_setup:local/granite"
    ]
    work.monitor.update()
    assert work.states() == [("local_setup:local/granite", "action_required")]

    # Retrying shows it again, and a later failure is not hidden by the old dismissal.
    work.speech = [_INSTALL]
    work.monitor.update()
    work.speech = [{**_INSTALL, "state": "failed", "error": "download_failed"}]
    work.monitor.update()
    assert ("local_setup:local/parakeet", "failed") in work.states()

    # A completed entry can be dismissed before it expires.
    work.speech = [_INSTALL]
    work.monitor.update()
    work.speech = [{**_INSTALL, "state": "completed"}]
    work.monitor.update()
    assert ("local_setup:local/parakeet", "completed") in work.states()
    work.monitor.dismiss("local_setup:local/parakeet")
    assert work.states() == [("local_setup:local/granite", "action_required")]


def test_recall_indexing_shows_only_a_large_pass_and_failures() -> None:
    work = _Work()
    status = {"state": "indexing", "indexed": 5, "waiting": RECALL_BACKLOG_SHOWN - 1}
    work.monitor.set_recall_status(status)
    work.monitor.update()
    assert work.monitor.activities == []

    work.monitor.set_recall_status(
        {"state": "indexing", "indexed": 0, "waiting": RECALL_BACKLOG_SHOWN, "eta_seconds": 30}
    )
    work.monitor.update()
    (entry,) = work.monitor.activities
    assert entry["kind"] == "recall_index" and entry["state"] == "running"
    assert entry["progress"] == {"completed": 0, "total": RECALL_BACKLOG_SHOWN, "unit": "items"}
    assert entry["eta_seconds"] == 30

    # Once shown, the pass stays until it ends, also while it waits to retry,
    # then shows as completed.
    work.monitor.set_recall_status({"state": "indexing", "indexed": 45, "waiting": 5})
    work.monitor.update()
    assert work.monitor.activities[0]["progress"]["total"] == 50
    work.monitor.set_recall_status({"state": "retrying", "indexed": 45, "waiting": 5})
    work.monitor.update()
    (retrying,) = work.monitor.activities
    assert (retrying["state"], retrying["phase"]) == ("running", "retrying")
    work.monitor.set_recall_status({"state": "idle", "indexed": 50, "waiting": 0})
    work.monitor.update()
    assert work.states() == [("recall_index", "completed")]

    work.monitor.set_recall_status(
        {
            "state": "error",
            "indexed": 50,
            "waiting": 3,
            "last_error": {"code": "embedding_unavailable", "message": "Model missing"},
        }
    )
    work.monitor.update()
    (failed,) = work.monitor.activities
    assert (failed["state"], failed["error"], failed["message"]) == (
        "failed",
        "embedding_unavailable",
        "Model missing",
    )


@pytest.mark.asyncio
async def test_activity_rpcs_list_and_dismiss_what_the_running_monitor_publishes() -> None:
    work = _Work()
    work.recall_status = {
        "state": "error",
        "indexed": 0,
        "waiting": 1,
        "last_error": {"code": "embedding_unavailable", "message": ""},
    }
    work.monitor.start()
    try:
        async with asyncio.timeout(2):
            while not work.published:
                await asyncio.sleep(0)
        state = SimpleNamespace(activity=work.monitor)

        async def call(method: str, **params: Any) -> dict[str, Any]:
            return await dispatch_rpc(state, {"method": f"activity.{method}", "params": params})

        listed = (await call("list"))["result"]["activities"]
        assert [entry["id"] for entry in listed] == ["recall_index"]
        assert (await call("dismiss", id="recall_index"))["result"] == {"activities": []}
        assert (await call("list"))["result"] == {"activities": []}
        assert work.published[-1] == []
        for method, params in (
            ("list", {"all": True}),
            ("dismiss", {}),
            ("dismiss", {"id": 3}),
            ("dismiss", {"id": "recall_index", "forever": True}),
        ):
            assert (await call(method, **params))["error"]["code"] == "invalid_request"
    finally:
        await work.monitor.aclose()
