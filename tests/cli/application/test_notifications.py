"""Tray toasts: which events deserve one, what it says, and when it disappears."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from cli.application.monitor import MonitorStatus
from cli.application.notifications import Notifier, Toast, run_toast_kind
from cli.application.state import Operation

_CONNECTED = MonitorStatus("http://127.0.0.1:8420", "connected", True)
_REFUSED = MonitorStatus("http://127.0.0.1:8420", "refused")


class _Sink:
    def __init__(self) -> None:
        self.shown: list[Toast] = []
        self.dismissed: list[str] = []

    def show(self, toast: Toast) -> None:
        self.shown.append(toast)

    def dismiss(self, key: str) -> None:
        self.dismissed.append(key)


def _server(settings: dict[str, bool] | None = None):
    """Answer the RPCs a notifier makes, like a server with one Session."""

    async def rpc(method: str, params: dict[str, Any]) -> dict[str, Any] | None:
        if method == "settings.values":
            return {"settings": {"notifications": settings or {}}}
        if method == "session.get":
            titled = params == {"agent_id": "coder", "session_id": "ses_1"}
            return {"session": {"title": "Fix login" if titled else "", "auto_title": None}}
        if method == "agent.get":
            return {"name": "Coder"} if params == {"id": "coder"} else None
        return None

    return rpc


def _notifier(
    sink: _Sink,
    *,
    settings: dict[str, bool] | None = None,
    owns_server: bool = True,
    update_active: bool = False,
) -> Notifier:
    return Notifier(
        sink,
        _server(settings),
        owns_server=owns_server,
        update_active=lambda: update_active,
        hold_seconds=0,
    )


async def _settle() -> None:
    current = asyncio.current_task()
    while pending := [task for task in asyncio.all_tasks() if task is not current]:
        await asyncio.gather(*pending, return_exceptions=True)


def _run_event(event_type: str = "run_completed", **payload: object) -> dict[str, Any]:
    fields = {"run_id": "run_1", "run_kind": "user", "agent_id": "coder", "session_id": "ses_1"}
    return {"type": event_type, "payload": {**fields, **payload}}


def _read(run_id: str = "run_1") -> dict[str, Any]:
    scope = {"agent_id": "coder", "session_id": "ses_1", "read_run_id": run_id}
    return {"type": "resource_changed", "payload": {"kind": "sessions", "scope": scope}}


@pytest.mark.parametrize(
    ("event_type", "payload", "kind"),
    [
        pytest.param("run_completed", {"run_kind": "user"}, "run_completed", id="user-done"),
        pytest.param("run_failed", {"run_kind": "system"}, "run_failed", id="system-failed"),
        pytest.param("run_interrupted", {"run_kind": "user"}, "run_failed", id="interrupted"),
        pytest.param("run_cancelled", {"run_kind": "user"}, None, id="cancelled"),
        pytest.param("run_completed", {"run_kind": "cron"}, None, id="cron-done"),
        pytest.param("run_failed", {"run_kind": "calendar"}, "automation_failed", id="calendar"),
        pytest.param("run_failed", {"run_kind": "subagent"}, None, id="subagent"),
        pytest.param(
            "run_failed",
            {"run_kind": "user", "contributes_to_agent_activity": False},
            None,
            id="internal-continuation",
        ),
    ],
)
def test_only_foreground_results_and_automation_failures_deserve_a_toast(
    event_type: str, payload: dict[str, Any], kind: str | None
):
    assert run_toast_kind(event_type, payload) == kind


@pytest.mark.parametrize(
    ("event", "expected"),
    [
        pytest.param(
            _run_event(),
            Toast(
                "run:run_1",
                "run_completed",
                "Coder finished",
                "Fix login",
                False,
                ("coder", "ses_1"),
            ),
            id="agent-finished",
        ),
        pytest.param(
            _run_event("run_failed", project_id="site", error="Provider refused\ntrace"),
            Toast(
                "run:run_1",
                "run_failed",
                "coder failed",
                "New Session\nProvider refused",
                True,
                ("coder@site", "ses_1"),
            ),
            id="project-agent-failed",
        ),
        pytest.param(
            _run_event("run_failed", run_kind="cron"),
            Toast(
                "run:run_1",
                "automation_failed",
                "Cron Run failed · Coder",
                "Fix login",
                True,
                ("coder", "ses_1"),
            ),
            id="cron-failed",
        ),
    ],
)
def test_run_toast_names_the_agent_and_session_and_opens_it(event: dict[str, Any], expected: Toast):
    sink = _Sink()

    async def scenario() -> None:
        notifier = _notifier(sink)
        notifier.event_received(event)
        await _settle()
        # A replayed event after a reconnect never repeats the toast.
        notifier.event_received(event)
        await _settle()

    asyncio.run(scenario())

    assert sink.shown == [expected]


@pytest.mark.parametrize(
    ("settings", "read_at", "shown", "dismissed"),
    [
        pytest.param({"run_completed": False}, None, 0, [], id="switched-off"),
        pytest.param({}, "during-hold", 0, [], id="session-already-open"),
        pytest.param({}, "after-toast", 1, ["run:run_1"], id="session-opened-later"),
    ],
)
def test_run_toast_respects_its_setting_and_the_read_acknowledgement(
    settings: dict[str, bool], read_at: str | None, shown: int, dismissed: list[str]
):
    sink = _Sink()

    async def scenario() -> None:
        notifier = _notifier(sink, settings=settings)
        notifier.status_changed(_CONNECTED)
        await _settle()
        notifier.event_received(_run_event())
        if read_at == "during-hold":
            notifier.event_received(_read())
        await _settle()
        if read_at == "after-toast":
            notifier.event_received(_read())

    asyncio.run(scenario())

    assert len(sink.shown) == shown
    assert sink.dismissed == dismissed


@pytest.mark.parametrize(
    ("close_code", "setup", "toasted"),
    [
        pytest.param(1006, {}, True, id="server-vanished"),
        pytest.param(1012, {}, False, id="cooperative-stop"),
        pytest.param(1006, {"expected": True}, False, id="tray-stopped-it"),
        pytest.param(1006, {"update_active": True}, False, id="update-restart"),
        pytest.param(1006, {"owns_server": False}, False, id="remote-server"),
        pytest.param(1006, {"settings": {"server_stopped": False}}, False, id="switched-off"),
    ],
)
def test_server_stop_toast_only_for_an_unexpected_loss_and_leaves_on_reconnect(
    close_code: int, setup: dict[str, Any], toasted: bool
):
    sink = _Sink()

    async def scenario() -> None:
        notifier = _notifier(
            sink,
            settings=setup.get("settings"),
            owns_server=setup.get("owns_server", True),
            update_active=setup.get("update_active", False),
        )
        notifier.status_changed(_CONNECTED)
        await _settle()
        if setup.get("expected"):
            notifier.expect_server_stop()
        notifier.connection_lost(close_code)
        notifier.status_changed(_REFUSED)
        notifier.status_changed(_CONNECTED)
        await _settle()

    asyncio.run(scenario())

    assert [(toast.key, toast.title, toast.failure) for toast in sink.shown] == (
        [("server", "vBot server stopped", True)] if toasted else []
    )
    assert sink.dismissed == (["server"] if toasted else [])


@pytest.mark.parametrize(
    ("operation", "title", "body", "failure"),
    [
        pytest.param(
            Operation(
                "upd_1",
                "completed",
                previous_version="rel_a",
                candidate_version="rel_b",
                previous_label="0.4.2",
                target_label="0.5.0",
            ),
            "vBot updated",
            "Done.\nVersion: 0.4.2 -> 0.5.0",
            False,
            id="updated",
        ),
        pytest.param(
            Operation("upd_1", "completed", previous_version="rel_a", candidate_version="rel_a"),
            "vBot is up to date",
            "Done.",
            False,
            id="already-current",
        ),
        pytest.param(
            Operation("upd_1", "prepared", target_label="0.5.0"),
            "vBot update prepared",
            "Done.\nVersion: 0.5.0",
            False,
            id="prepared",
        ),
        pytest.param(
            Operation("upd_1", "rolled_back", "Health check failed", target_label="0.5.0"),
            "vBot update failed",
            "Done.\nHealth check failed",
            True,
            id="rolled-back",
        ),
    ],
)
def test_update_result_toast_describes_the_outcome(
    operation: Operation, title: str, body: str, failure: bool
):
    sink = _Sink()

    _notifier(sink).update_finished(operation, "Done.")

    assert sink.shown == [Toast("update:upd_1", "update_result", title, body, failure)]
