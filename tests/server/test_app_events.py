"""Tests for the app's bridges from core change callbacks to the server event bus."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast, override

from fastapi.testclient import TestClient  # type: ignore[import-not-found]

from core.automation.cron import CronService
from core.recall import IndexStatus
from core.sessions import SessionAddress
from core.tools.command_manager import CommandNotFoundError
from server.app import create_app
from tests.server.rpc_test_support import StubAdapter, StubRuntime
from tests.server.rpc_test_support_runtime import StubTerminalManager

JsonObject = dict[str, Any]


class _RecordingTerminalManager(StubTerminalManager):
    def __init__(self) -> None:
        self.changed_callbacks: list[Callable[[str], None]] = []

    @override
    def add_changed_callback(self, callback: Callable[[str], None]) -> Any:
        self.changed_callbacks.append(callback)
        return lambda: self.changed_callbacks.remove(callback)


class _RecordingCommandManager:
    """The CommandManager's change callbacks and the background commands they report."""

    def __init__(self, owner: SessionAddress) -> None:
        self.changed_callbacks: list[Callable[[str], None]] = []
        self.owner = owner
        self.reports: dict[str, SimpleNamespace] = {}

    def add_changed_callback(self, callback: Callable[[str], None]) -> Any:
        self.changed_callbacks.append(callback)
        return lambda: self.changed_callbacks.remove(callback)

    def report(self, command_id: str) -> SimpleNamespace:
        try:
            return self.reports[command_id]
        except KeyError:
            raise CommandNotFoundError(command_id) from None

    def change(self, command_id: str, status: str | None, *still_running: str) -> None:
        """The command now has *status* (None: forgotten) and processes still running."""
        if status is None:
            self.reports.pop(command_id, None)
        else:
            self.reports[command_id] = SimpleNamespace(
                owner=self.owner, background=True, status=status, still_running=still_running
            )
        for callback in list(self.changed_callbacks):
            callback(command_id)


def test_core_change_callbacks_publish_server_events(tmp_path: Path) -> None:
    runtime = StubRuntime(tmp_path, StubAdapter())
    terminal_manager = _RecordingTerminalManager()
    runtime.terminal_manager = terminal_manager
    address = SessionAddress(project_id=None, agent_id="coder", session_id="session-one")
    command_manager = _RecordingCommandManager(address)
    runtime.command_manager = command_manager  # type: ignore[assignment]
    cron_service = CronService(cast(Any, SimpleNamespace()), tmp_path)
    runtime.cron_service = cron_service  # type: ignore[attr-defined]
    session_scope: JsonObject = {
        "project_id": None,
        "agent_id": "coder",
        "session_id": "session-one",
    }
    app = create_app(runtime=cast(Any, runtime))

    async def finish(_run: Any) -> str:
        return "done"

    with TestClient(app) as client:
        # Trigger each change on the app's Event Loop, where core callbacks fire.
        portal = client.portal
        assert portal is not None
        bus = app.state.event_bus

        def events_after(sequence: int) -> list[tuple[str, JsonObject]]:
            return [(e["type"], e["payload"]) for e in bus.events if e["sequence"] > sequence]

        def publishes(action: Callable[[], object]) -> list[tuple[str, JsonObject]]:
            async def on_loop() -> list[tuple[str, JsonObject]]:
                before = bus.last_sequence
                result = action()
                if inspect.isawaitable(result):
                    await result
                return events_after(before)

            return portal.call(on_loop)

        async def run_outside_rpc() -> tuple[str, list[tuple[str, JsonObject]]]:
            before = bus.last_sequence
            run = await app.state.chat_runs.start(address, finish)
            await run.wait()
            # The bridge publishes from its own task; wait for the Session invalidation.
            invalidated = (
                "resource_changed",
                {"kind": "sessions", "scope": {**session_scope, "run_id": run.id}},
            )
            async with asyncio.timeout(2):
                while invalidated not in events_after(before):
                    await asyncio.sleep(0)
            return run.id, events_after(before)

        run_id, run_events = portal.call(run_outside_rpc)
        title_events = publishes(lambda: runtime.chat_sessions.set_auto_title(address, "Title"))
        read_events = publishes(
            lambda: runtime.chat_sessions.mark_terminal_run_read(address, run_id)
        )
        cron_events = publishes(
            lambda: cron_service.create_job(
                agent_id="coder",
                prompt="Scheduled work",
                schedule_type="cron",
                cron_expression="0 9 * * *",
            )
        )

        terminal_events = publishes(lambda: terminal_manager.changed_callbacks[0]("term_one"))

        # A background command's state is published once per change of its
        # status or of whether processes it started still run; a forgotten
        # command publishes nothing.
        changes: list[tuple[str | None, tuple[str, ...]]] = [
            ("running", ()),
            ("running", ()),
            ("completed", ("node (pid 7)",)),
            ("completed", ("node (pid 7)",)),
            ("completed", ()),
            (None, ()),
        ]
        command_events = [
            publishes(partial(command_manager.change, "cmd_one", status, *still_running))
            for status, still_running in changes
        ]
        # An Agent's Skill authoring Tool reports its package changes.
        skill_events = publishes(lambda: runtime.skill_changed_callbacks[0]())
        # A local catalog sweep publishes a changed Model catalog.
        model_events = publishes(lambda: runtime.model_catalog_changed_callbacks[0]())
        index_status = IndexStatus(semantic_enabled=True, state="indexing", waiting=4)
        index_events = publishes(lambda: runtime.recall.listeners[0](index_status))

    # Runs started outside RPC reach /ws too, and invalidate their exact Session.
    assert [event_type for event_type, _payload in run_events] == [
        "run_started",
        "run_completed",
        "resource_changed",
        "resource_changed",
    ]
    assert all(payload["run_id"] == run_id for _type, payload in run_events[:2])
    assert [payload for _type, payload in run_events[2:]] == [
        {"kind": "debug_traces"},
        {"kind": "sessions", "scope": {**session_scope, "run_id": run_id}},
    ]
    assert title_events == [("resource_changed", {"kind": "sessions", "scope": session_scope})]
    # The acknowledged Run lets other windows clear their unread marker.
    read_scope = {**session_scope, "read_run_id": run_id}
    assert read_events == [("resource_changed", {"kind": "sessions", "scope": read_scope})]
    assert cron_events == [("resource_changed", {"kind": "cron"})]

    assert terminal_events == [
        ("resource_changed", {"kind": "terminals", "scope": {"terminal_id": "term_one"}})
    ]

    def status_changed(status: str, *, still_running: bool = False) -> tuple[str, JsonObject]:
        return (
            "command_status_changed",
            {
                "command_id": "cmd_one",
                "session_id": "session-one",
                "status": status,
                "still_running": still_running,
            },
        )

    assert command_events == [
        [status_changed("running")],
        [],
        [status_changed("completed", still_running=True)],
        [],
        [status_changed("completed")],
        [],
    ]
    assert skill_events == [("resource_changed", {"kind": "skills"})]
    assert model_events == [("resource_changed", {"kind": "models"})]
    assert index_events == [("recall_index_status", index_status.to_dict())]
    # App shutdown releases the bridges.
    assert terminal_manager.changed_callbacks == []
    assert command_manager.changed_callbacks == []
    assert runtime.skill_changed_callbacks == []
    assert runtime.model_catalog_changed_callbacks == []
    assert runtime.recall.listeners == []
