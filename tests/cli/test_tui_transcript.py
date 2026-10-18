"""Long conversations paint only what shows and retain operator-owned state."""

from __future__ import annotations

import asyncio
from typing import Any, override

import pytest
from textual.widgets import TextArea

from cli.tui.app import ConversationApp
from cli.tui.state import Conversation
from tests.cli.test_tui import Server


@pytest.mark.asyncio
async def test_long_run_viewport_keeps_disclosures_and_reading_position(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ready = asyncio.Event()

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
    app = ConversationApp(server, agent_id="main")
    bootstrap = app.navigation.bootstrap

    async def signal_ready() -> None:
        await bootstrap()
        ready.set()

    monkeypatch.setattr(app.navigation, "bootstrap", signal_ready)
    async with app.run_test(size=(100, 35)) as pilot:
        await ready.wait()
        await pilot.pause()
        view = app.conversation
        transcript = app.transcript
        assert view is not None
        # Only the screens around the viewport are painted; the rest are heights.
        painted = [row for row in transcript._rows if row.lines is not None]
        assert len(painted) < 200
        assert transcript.displays("tool-999")

        # Opening a step keeps its header on the same screen line.
        header = transcript._offsets[transcript._index["tool-999"]] - transcript.scroll_y
        transcript.toggle("tool-999")
        await pilot.pause()
        assert "tool-999" in view.expanded_rows
        assert transcript._offsets[transcript._index["tool-999"]] - transcript.scroll_y == header

        transcript.scroll_home(animate=False, immediate=True)
        await pilot.pause()
        anchor = transcript.reading_anchor()
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
        assert transcript.reading_anchor() == anchor
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
        assert transcript.reading_anchor() == anchor

        # Away from the tail a click on the floating control returns there,
        # which then gives its line back to the conversation.
        bottom = transcript.scrollable_content_region.height - 1
        assert "Jump to latest" in transcript.render_line(bottom).text
        await pilot.click(transcript, offset=(transcript.size.width - 8, bottom + 1))
        await pilot.pause()
        assert transcript.is_vertical_scroll_end
        assert not view.unread_output
        assert "Jump to latest" not in transcript.render_line(bottom).text

        # Disclosure state belongs to the Conversation and survives repainting.
        row = transcript._rows[transcript._index["tool-999"]]
        assert row.lines is not None and len(row.lines) > 1
        transcript.toggle("tool-999")
        assert "tool-999" not in view.expanded_rows
        assert len(row.lines) == 1


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
        worker = app._start(lambda: app.navigation.open(view))
        await blocked.wait()
        assert app.transcript.displays("cached-text")
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
        app.navigation.views[view.key] = view
        app.navigation.cache_history(view)
    assert len(app.navigation.history_cache) == 8
    assert not views[0].items
    assert views[0].draft == "draft-0"
    assert app.navigation.views[views[0].key] is views[0]
    assert views[-1].items


def test_background_events_target_only_the_displayed_address() -> None:
    app = ConversationApp(Server())
    app.conversation = Conversation("coder@project", session="one")
    assert app.navigation.event_targets_view(
        {"agent_id": "coder", "project_id": "project", "session_id": "one"}
    )
    assert not app.navigation.event_targets_view(
        {"agent_id": "coder", "project_id": "other", "session_id": "one"}
    )
    assert not app.navigation.event_targets_view(
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
            "before": "step-8",
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


@pytest.mark.asyncio
async def test_earlier_run_steps_load_once_their_gap_comes_into_view() -> None:
    def step(position: int) -> dict[str, Any]:
        return {
            "id": f"step-{position}",
            "type": "tool_call",
            "name": "read",
            "status": "success",
            "position": position,
        }

    def gap(before: int) -> dict[str, Any]:
        return {
            "id": "run-gap",
            "type": "history_gap",
            "run_id": "run",
            "before": f"step-{before}",
            "position": -1,
        }

    class WindowedServer(Server):
        @override
        async def rpc(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
            if method == "chat.timeline_children":
                self.calls.append((method, dict(params or {})))
                return {
                    "run_id": "run",
                    "before": (params or {})["before"],
                    "history_generation": "one",
                    "items": [step(0), step(1)],
                }
            return await super().rpc(method, params)

    server = WindowedServer()
    server.items = [
        {
            "id": "run",
            "type": "assistant_run",
            "run_id": "run",
            "status": "completed",
            "window_start": 2,
            "items": [gap(2), step(2), step(3)],
        }
    ]
    app = ConversationApp(server, agent_id="main", session_id="session-one")
    async with app.run_test(size=(100, 35)) as pilot:
        for _ in range(10):
            await pilot.pause()
            if app.transcript.displays("step-0"):
                break
        pages = [params for method, params in server.calls if method == "chat.timeline_children"]
        assert [page["before"] for page in pages] == ["step-2"]
        assert app.transcript.displays("step-0")
        assert not app.transcript.displays("run-gap")


@pytest.mark.asyncio
async def test_double_and_triple_click_select_and_copy_a_word_and_its_paragraph() -> None:
    server = Server()
    server.items = [
        {
            "id": "answer",
            "type": "message",
            "message": {"role": "assistant", "content": "Open cli/tui/app.py now.\n\nNext."},
        }
    ]
    copied: list[str] = []

    class CopyingApp(ConversationApp):
        @override
        def copy_to_clipboard(self, text: str) -> None:
            copied.append(text)

    app = CopyingApp(server, agent_id="main", session_id="session-one")
    async with app.run_test(size=(100, 35)) as pilot:
        transcript = app.transcript
        for _ in range(10):
            await pilot.pause()
            if transcript.displays("answer"):
                break
        lines = range(transcript._offsets[-1])
        line = next(y for y in lines if "app.py" in transcript._text_line(y))
        x = transcript._text_line(line).index("app.py")
        offset = (x + transcript.styles.padding.left, line - round(transcript.scroll_y) + 1)

        await pilot.double_click(transcript, offset=offset)
        await pilot.pause()
        # A path stays whole; the sentence's full stop is not part of it.
        assert copied[-1] == "cli/tui/app.py"

        await pilot.triple_click(transcript, offset=offset)
        await pilot.pause()
        assert copied[-1] == "Open cli/tui/app.py now."
        assert copied.count("Open cli/tui/app.py now.") == 1
