"""Long conversations keep bounded widgets and retain operator-owned state."""

from __future__ import annotations

import asyncio
from typing import Any, override

import pytest
from textual.widgets import TextArea

from cli.tui.app import ConversationApp
from cli.tui.state import Conversation
from tests.cli.test_tui import Server


@pytest.mark.asyncio
async def test_long_run_viewport_keeps_disclosures_and_reading_position() -> None:
    ready = asyncio.Event()

    class ReadyApp(ConversationApp):
        @override
        async def _bootstrap(self) -> None:
            await super()._bootstrap()
            ready.set()

    server = Server()
    server.items = [
        {
            "id": "long-run",
            "type": "assistant_run",
            "status": "completed",
            "items": [
                {
                    "id": f"tool-{i}",
                    "type": "tool_call",
                    "name": "read",
                    "status": "success",
                    "result": "large result\n" * 100,
                }
                for i in range(1000)
            ],
        }
    ]
    app = ReadyApp(server, agent_id="main")
    async with app.run_test(size=(100, 35)) as pilot:
        await ready.wait()
        await pilot.pause()
        view = app.conversation
        assert view is not None
        assert len(app.rows) < 80
        assert len(list(app.screen.walk_children())) < 400
        row = app.rows["tool-999"]
        assert row.body is None
        assert row.fold is not None
        row.fold.collapsed = False
        await pilot.pause()
        assert row.body is not None
        app.transcript.scroll_home(animate=False, immediate=True)
        await app.transcript._refresh_window()
        await pilot.pause()
        assert "tool-999" not in app.rows
        anchor = app.transcript.reading_anchor()
        view.items = [
            *view.items,
            {
                "id": "new-output",
                "type": "message",
                "message": {"role": "assistant", "content": "New output"},
            },
        ]
        await app._render()
        await pilot.pause()
        assert app.transcript.reading_anchor() == anchor
        assert view.unread_output
        view.items = [
            {
                "id": "older-user",
                "type": "message",
                "message": {"role": "user", "content": "Earlier"},
            },
            *view.items,
        ]
        await app._render()
        await pilot.pause()
        assert app.transcript.reading_anchor() == anchor
        app.action_latest()
        app.transcript.scroll_end(animate=False, immediate=True)
        await app.transcript._refresh_window()
        await pilot.pause()
        restored = app.rows["tool-999"]
        assert restored is not row
        assert restored.fold is not None and not restored.fold.collapsed
        assert restored.body is not None


@pytest.mark.asyncio
async def test_cached_session_is_visible_while_its_authoritative_refresh_waits() -> None:
    blocked, release = asyncio.Event(), asyncio.Event()

    class DelayedServer(Server):
        delay = False

        @override
        async def rpc(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
            if self.delay and method == "chat.timeline":
                blocked.set()
                await release.wait()
            return await super().rpc(method, params)

    server = DelayedServer()
    server.items = [
        {
            "id": "cached-text",
            "type": "message",
            "message": {"role": "assistant", "content": "Saved answer"},
        }
    ]
    app = ConversationApp(server, agent_id="main")
    async with app.run_test(size=(100, 35)) as pilot:
        await pilot.pause()
        view = app.conversation
        assert view is not None
        app.query_one(TextArea).load_text("Keep this draft")
        server.delay = True
        worker = app._start(lambda: app._open(view))
        await blocked.wait()
        assert "cached-text" in app.rows
        assert app.query_one(TextArea).text == "Keep this draft"
        release.set()
        await worker.wait()


def test_history_cache_evicts_content_without_losing_drafts_or_navigation() -> None:
    app = ConversationApp(Server())
    views = [
        Conversation(
            "main", session=f"s{i}", draft=f"draft-{i}", items=[{"id": str(i)}], cache_bytes=1
        )
        for i in range(12)
    ]
    for view in views:
        app.views[view.key] = view
        app._cache_history(view)
    assert len(app.history_cache) == 8
    assert not views[0].items
    assert views[0].draft == "draft-0"
    assert app.views[views[0].key] is views[0]
    assert views[-1].items


def test_background_events_target_only_the_displayed_address() -> None:
    app = ConversationApp(Server())
    app.conversation = Conversation("coder@project", session="one")
    assert app._event_targets_view(
        {"agent_id": "coder", "project_id": "project", "session_id": "one"}
    )
    assert not app._event_targets_view(
        {"agent_id": "coder", "project_id": "other", "session_id": "one"}
    )
    assert not app._event_targets_view(
        {"agent_id": "coder", "project_id": "project", "session_id": "two"}
    )


def test_windowed_history_pages_and_reconnect_keep_the_loaded_prefix() -> None:
    def child(position: int) -> dict[str, Any]:
        return {
            "id": f"step-{position}",
            "type": "assistant_output",
            "content": str(position),
            "position": position,
        }

    def run(start: int, end: int) -> dict[str, Any]:
        return {
            "id": "run",
            "run_id": "run",
            "type": "assistant_run",
            "window_start": start,
            "items": [
                {"id": "gap", "type": "history_gap", "position": -1, "before": f"step-{start}"},
                *[child(i) for i in range(start, end)],
            ],
        }

    view = Conversation("main", session="one")
    view.page({"history_generation": "g", "items": [run(8, 10)]})
    view.child_page(
        {
            "run_id": "run",
            "items": [
                {"id": "gap", "type": "history_gap", "position": -1, "before": "step-6"},
                child(6),
                child(7),
            ],
        }
    )
    view.event({"type": "timeline_snapshot", "run_id": "run", "sequence": 2, "items": [run(9, 11)]})
    assert [item["position"] for item in view.items[0]["items"]] == [-1, 6, 7, 8, 9, 10]
    view.event(
        {
            "type": "timeline_update",
            "run_id": "run",
            "sequence": 3,
            "base_sequence": 2,
            "windowed": True,
            "run": {"id": "run"},
            "children": [child(11)],
            "positions": {"step-6": 7, "step-7": 6},
        }
    )
    assert view.items[0]["items"][-1]["position"] == 11
    assert [item["id"] for item in view.items[0]["items"]][1:3] == ["step-7", "step-6"]
    view.page({"history_generation": "other", "items": [run(10, 12)]})
    assert [item["position"] for item in view.items[0]["items"]] == [-1, 10, 11]
