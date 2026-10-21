"""Session activity as the server reports it: the status row and the Session picker."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, override

import pytest

import cli.tui.activity as activity_module
from cli.tui.app import ConversationApp
from cli.tui.dialogs import Picker
from tests.cli.test_tui import Server

type JsonObject = dict[str, Any]


@pytest.mark.asyncio
async def test_status_row_follows_the_server_reported_session_activity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        activity_module, "_now", lambda: datetime(2026, 10, 21, 10, 4, 12, tzinfo=UTC)
    )
    server = Server()
    server.queued = []
    working = {
        "project_id": None,
        "agent_id": "main",
        "session_id": "session-one",
        "state": "working",
        "work_started_at": "2026-10-21T10:00:00.000000Z",
        "waiting_for": {"subagents": 0, "commands": 0, "results": 0},
    }
    waiting = {**working, "state": "waiting", "waiting_for": {"subagents": 2, "commands": 1}}
    idle = {**working, "state": "idle", "work_started_at": None}
    app = ConversationApp(server, agent_id="main")
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        view = app.conversation
        assert view is not None and not view.facts.get("active_run")
        await server.bus.put({"type": "connection_ready", "session_activity": [working]})
        await pilot.pause()
        # The span counts from the Session leaving idle, across Runs.
        message = app._status_message(view)[0]
        assert "Working · 4m 12s" in message and "Ctrl+X" in message
        await server.bus.put({"type": "session_activity_changed", "payload": waiting})
        await server.bus.put(
            {
                "type": "session_activity_changed",
                "payload": {**idle, "session_id": "session-two"},
            }
        )
        await pilot.pause()
        message = app._status_message(view)[0]
        assert "Waiting for 2 Sub-Agents, 1 command · 4m 12s" in message and "F10" in message
        await server.bus.put({"type": "session_activity_changed", "payload": idle})
        await pilot.pause()
        assert app._status_message(view) == ("", "info")
        # A hello starts over: a Session it does not list is idle.
        await server.bus.put({"type": "session_activity_changed", "payload": working})
        await server.bus.put({"type": "connection_ready", "session_activity": []})
        await pilot.pause()
        assert app._status_message(view) == ("", "info")


@pytest.mark.asyncio
async def test_session_picker_marks_working_waiting_and_unread_only_when_idle() -> None:
    class ActivityServer(Server):
        @override
        async def rpc(self, method: str, params: JsonObject | None = None) -> JsonObject:
            if method == "session.list":
                rows: list[tuple[str, JsonObject]] = [
                    ("working", {"activity_state": "working", "has_unread_completion": True}),
                    ("waiting", {"activity_state": "waiting", "has_active_run": False}),
                    ("idle-unread", {"activity_state": "idle", "has_unread_completion": True}),
                    ("idle", {"activity_state": "idle"}),
                    # A server that does not report Session activity.
                    ("older-server", {"has_active_run": True}),
                ]
                return {
                    "sessions": [
                        {"id": name, "agent_address": "main", "title": name, **fields}
                        for name, fields in rows
                    ]
                }
            return await super().rpc(method, params)

    app = ConversationApp(ActivityServer(), agent_id="main")
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("f2")
        await pilot.pause()
        assert isinstance(app.screen, Picker)
        labels = {value["id"]: label for label, value in app.screen.choices}
        await pilot.press("escape")

    assert labels == {
        "working": "● working · main",
        "waiting": "○ waiting · main",
        "idle-unread": "• idle-unread · main",
        "idle": "idle · main",
        "older-server": "● older-server · main",
    }
