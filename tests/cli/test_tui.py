"""Terminal accessor contracts at the mounted UI and HTTP boundaries."""

from __future__ import annotations

import asyncio
import copy
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, override

import httpx
import pytest
from textual.widgets import Collapsible, TextArea

from cli.tui.app import ConversationApp
from cli.tui.dialogs import Picker, TextDialog
from cli.tui.rendering import TimelineEntry, file_links, tool_detail
from cli.tui.state import Conversation
from cli.tui.transport import ClientError, TerminalClient

type JsonObject = dict[str, Any]


class Server(TerminalClient):
    """Only the transport is fake: the complete Textual App and input run normally."""

    @override
    def __init__(self) -> None:
        self.url = "http://test.invalid"
        self.calls: list[tuple[str, JsonObject]] = []
        self.frames: asyncio.Queue[JsonObject] = asyncio.Queue()
        self.bus: asyncio.Queue[JsonObject] = asyncio.Queue()
        self.sent = asyncio.Event()
        self.release_send = asyncio.Event()
        self.release_send.set()
        self.fail_send = False
        self.active = False
        self.items: list[JsonObject] = []
        self.queued = [
            {
                "id": "queued-one",
                "content": "Queue text",
                "editable": True,
                "steerable": True,
                "steering": False,
            }
        ]

    @override
    async def rpc(self, method: str, params: JsonObject | None = None) -> JsonObject:
        params = params or {}
        self.calls.append((method, copy.deepcopy(params)))
        if method == "agent.list":
            return {"agents": [{"id": "main", "name": "Main", "current_session_id": "session-one"}]}
        if method == "project.list":
            return {"projects": []}
        if method == "chat.timeline":
            result: JsonObject = {
                "items": copy.deepcopy(self.items),
                "session_id": params.get("session_id"),
                "timeline_sequence": 0,
                "has_more": False,
                "history_generation": "one",
            }
            if self.active:
                result["active_run"] = {
                    "run_id": "run-one",
                    "timeline_url": "/api/runs/run-one/timeline",
                    "status": "running",
                }
            return result
        if method == "session.get":
            return {"session": {"id": params["session_id"], "title": "Test conversation"}}
        if method == "chat.queue_list":
            return {"items": copy.deepcopy(self.queued)}
        if method == "extensions.requests":
            return {"requests": []}
        if method == "chat.work":
            return {"run_active": self.active, "background_work_running": False, "running": []}
        if method == "chat.stream":
            self.sent.set()
            await self.release_send.wait()
            if self.fail_send:
                raise ClientError("Unknown test outcome", state="unknown")
            self.active = True
            return {"session_id": params.get("session_id", "new-session"), "run_id": "run-one"}
        if method == "chat.queue_update":
            self.queued[0]["content"] = params["content"]
            return {"item": self.queued[0]}
        if method == "chat.queue_remove":
            self.queued = []
            return {"ok": True}
        if method == "chat.queue_steer":
            self.queued[0]["steering"] = True
            return {"item": self.queued[0]}
        if method == "session.list":
            return {
                "sessions": [
                    {"id": "session-one", "agent_address": "main", "title": "One"},
                    {"id": "session-two", "agent_address": "main", "title": "Two"},
                ]
            }
        if method in {"chat.cancel", "chat.stop_all", "session.mark_read"}:
            return {"ok": True}
        raise AssertionError(f"Unexpected RPC: {method}")

    @override
    async def timeline(self, path: str) -> AsyncIterator[JsonObject]:
        while True:
            yield await self.frames.get()

    @override
    async def events(self) -> AsyncIterator[JsonObject]:
        while True:
            yield await self.bus.get()


def snapshot() -> JsonObject:
    return {
        "type": "timeline_snapshot",
        "run_id": "run-one",
        "sequence": 1,
        "state": {"status": "running"},
        "terminal": False,
        "items": [
            {
                "id": "run-item",
                "run_id": "run-one",
                "runId": "run-one",
                "type": "assistant_run",
                "status": "running",
                "items": [
                    {"id": "output", "type": "assistant_output", "content": "Hello"},
                    {"id": "reason", "type": "reasoning", "content": "Visible reasoning"},
                    {
                        "id": "tool",
                        "type": "tool_call",
                        "name": "read",
                        "status": "completed",
                        "arguments": {"path": "a.py"},
                        "result": '{"ok":true,"data":{}}',
                    },
                ],
            }
        ],
    }


@pytest.mark.asyncio
async def test_terminal_send_stream_navigation_and_detach() -> None:
    server = Server()
    app = ConversationApp(server, agent_id="main")
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        editor = app.query_one("#composer", TextArea)
        editor.load_text("A multiline\nmessage")
        await pilot.press("ctrl+s")
        await server.sent.wait()
        await pilot.pause()
        await server.frames.put(snapshot())
        await pilot.pause()
        await server.frames.put(
            {
                "type": "timeline_update",
                "run_id": "run-one",
                "sequence": 2,
                "base_sequence": 1,
                "run": {"id": "run-item"},
                "child_updates": [{"id": "output", "append": {"content": " **world**"}, "set": {}}],
                "state": {"session_usage": {"input_tokens": 7, "output_tokens": 3}},
                "terminal": False,
            }
        )
        await pilot.pause()
        assert app.conversation is not None
        assert app.conversation.items[0]["items"][0]["content"] == "Hello **world**"
        await app._render()
        assert len(app.query(Collapsible)) == 2
        editor.load_text("Retain this draft")
        await pilot.press("ctrl+p")
        await pilot.pause()
        assert isinstance(app.screen, Picker)
        await pilot.press("down", "enter")
        await pilot.pause()
        assert app.conversation.session == "session-two"
        await pilot.press("ctrl+b")
        await pilot.pause()
        assert app.conversation.session == "session-one"
        assert editor.text == "Retain this draft"
        await pilot.press("ctrl+q")
    sends = [params for method, params in server.calls if method == "chat.stream"]
    assert sends == [
        {
            "agent_id": "main",
            "session_id": "session-one",
            "reply_surface": "tui",
            "content": "A multiline\nmessage",
        }
    ]
    assert not any(method in {"chat.cancel", "chat.stop_all"} for method, _ in server.calls)


@pytest.mark.asyncio
async def test_queue_edit_shortcut_does_not_send_the_hidden_draft() -> None:
    server = Server()
    app = ConversationApp(server, agent_id="main")
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.query_one("#composer", TextArea).load_text("Do not send")
        await pilot.press("f6")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        # Steer, edit, remove are independently selectable actions.
        await pilot.press("down", "enter")
        await pilot.pause()
        assert isinstance(app.screen, TextDialog)
        app.screen.query_one(TextArea).load_text("Edited queue")
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert server.queued[0]["content"] == "Edited queue"
        assert not server.sent.is_set()
        assert app.query_one("#composer", TextArea).text == "Do not send"


@pytest.mark.asyncio
@pytest.mark.parametrize("unknown", [False, True])
async def test_pending_submission_preserves_later_input_and_never_replays(unknown: bool) -> None:
    server = Server()
    server.fail_send = unknown
    server.release_send.clear()
    app = ConversationApp(server, agent_id="main")
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        editor = app.query_one("#composer", TextArea)
        assert app.conversation is not None
        first_attachment = {"attachment_id": "first", "filename": "first.txt", "kind": "file"}
        later_attachment = {"attachment_id": "later", "filename": "later.txt", "kind": "file"}
        app.conversation.attachments.append(first_attachment)
        app.conversation.mentions.append("first.txt")
        editor.load_text("First input")
        await pilot.press("ctrl+s")
        await server.sent.wait()
        editor.load_text("Next input")
        app.conversation.attachments.append(later_attachment)
        app.conversation.mentions.append("later.txt")
        await pilot.pause()
        server.release_send.set()
        await pilot.pause()
        assert editor.text == "Next input"
        assert app.conversation is not None
        assert app.conversation.uncertain is unknown
        if unknown:
            assert app.conversation.uncertain_submission is not None
            assert app.conversation.uncertain_submission["text"] == "First input"
        await server.bus.put({"type": "connection_ready"})
        await pilot.pause()
        assert len([method for method, _ in server.calls if method == "chat.stream"]) == 1
        if unknown:
            # An empty current composer must still expose recovery of the original.
            editor.load_text("")
            app.conversation.attachments.clear()
            await pilot.press("ctrl+s")
            await pilot.pause()
            assert isinstance(app.screen, Picker)
            await pilot.press("escape")
            editor.load_text("Next input")
            app.conversation.attachments.extend([first_attachment, later_attachment])
            await pilot.press("ctrl+s")
            await pilot.pause()
            await pilot.press("down", "down", "down", "enter")
            await pilot.pause()
            assert editor.text == "First input"
            assert app.conversation.attachments == [first_attachment]
            assert app.conversation.mentions == ["first.txt"]
            assert app.saved_drafts == [
                {
                    "text": "Next input",
                    "attachments": [first_attachment, later_attachment],
                    "mentions": ["first.txt", "later.txt"],
                }
            ]
            editor.load_text("")
            app.conversation.attachments.clear()
            app.conversation.mentions.clear()
            await pilot.press("alt+up")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            assert editor.text == "Next input"
            assert app.conversation.attachments == [first_attachment, later_attachment]
            assert app.conversation.mentions == ["first.txt", "later.txt"]
        else:
            assert app.conversation.attachments == [later_attachment]
            assert app.conversation.mentions == ["later.txt"]


def test_timeline_patch_order_gap_and_pagination_contract() -> None:
    view = Conversation("main", "session-one")
    view.event(snapshot())
    update = {
        "type": "timeline_update",
        "run_id": "run-one",
        "sequence": 3,
        "base_sequence": 1,
        "run": {"id": "run-item"},
        "upsert": [
            {
                "id": "user",
                "run_id": "run-one",
                "type": "message",
                "message": {"role": "user", "content": "Start"},
            }
        ],
        "item_order": ["user", "run-item"],
        "remove_children": ["reason"],
        "child_updates": [{"id": "output", "append": {"content": " there"}}],
        "state": {"session_usage": {"output_tokens": 9}},
    }
    assert view.event(update)
    assert not view.event(update)
    assert [item["id"] for item in view.items] == ["user", "run-item"]
    assert view.items[1]["items"][0]["content"] == "Hello there"
    assert view.facts["session_usage"]["output_tokens"] == 9
    view.page(
        {
            "items": [
                {
                    "id": "older",
                    "type": "message",
                    "message": {"role": "user", "content": "Earlier"},
                }
            ],
            "has_more": True,
            "next_before": "before",
        },
        older=True,
    )
    view.page(
        {
            "items": [view.items[-1]],
            "replace_run_ids": ["run-one"],
            "incremental": True,
            "next_after": "after",
        }
    )
    assert [item["id"] for item in view.items] == ["older", "run-item"]
    assert view.next_before == "before"


@pytest.mark.asyncio
async def test_project_drafts_keep_distinct_text_and_working_directory() -> None:
    server = Server()
    app = ConversationApp(server, agent_id="main", project_id="project-one")
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        assert app.conversation is not None and app.conversation.session is None
        editor = app.query_one("#composer", TextArea)
        editor.load_text("First Project draft")
        await pilot.pause()
        await app._open(Conversation("main", project="project-two", project_selected=True))
        editor.load_text("Second Project draft")
        await pilot.pause()
        await app._open(Conversation("main", project_selected=True))
        assert editor.text == ""
        await app._open(Conversation("main", project="project-one", project_selected=True))
        assert editor.text == "First Project draft"
        assert (
            app.conversation.submission(editor.text)["new_session"]["working_project_id"]
            == "project-one"
        )
        await app._open(Conversation("main", project="project-two", project_selected=True))
        assert editor.text == "Second Project draft"


@pytest.mark.asyncio
async def test_render_and_stream_changes_during_an_awaited_mount(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = Server()
    app = ConversationApp(server, agent_id="main")
    original = TimelineEntry.update_item
    changed = False
    render_blocked = asyncio.Event()
    render_cancelled = asyncio.Event()

    async def update(widget: TimelineEntry, item: JsonObject) -> None:
        nonlocal changed
        if item["id"] == "detach-during-mount":
            render_blocked.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                render_cancelled.set()
                raise
        await original(widget, item)
        if not changed and app.conversation:
            changed = True
            app.conversation.items.append(
                {
                    "id": "later",
                    "type": "message",
                    "message": {"role": "user", "content": "Arrived during rendering"},
                }
            )
            app.render_dirty = True

    monkeypatch.setattr(TimelineEntry, "update_item", update)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        assert app.conversation is not None
        app.conversation.event(snapshot())
        await app._render()
        await app._render()
        assert {"run-item", "later"} == set(app.rows)
        assert len(app.query(Collapsible)) == 2
        app.conversation.items[0]["items"].append(
            {"id": "detach-during-mount", "type": "reasoning", "content": "Still rendering"}
        )
        app.render_dirty = True
        app._render_if_dirty()
        await render_blocked.wait()
        await pilot.press("ctrl+q")
    assert render_cancelled.is_set()


@pytest.mark.asyncio
async def test_stale_refresh_does_not_replace_stream_and_next_run_resets_sequence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = asyncio.Event()
    release = asyncio.Event()

    class SlowServer(Server):
        delay = False
        recovering = False

        @override
        async def rpc(self, method: str, params: JsonObject | None = None) -> JsonObject:
            if self.recovering and method == "chat.timeline":
                return {
                    "items": [],
                    "active_run": {"run_id": "run-three"},
                    "timeline_sequence": 1,
                    "incremental": True,
                }
            result = await super().rpc(method, params)
            if self.delay and method == "session.get":
                captured.set()
                await release.wait()
            return result

        @override
        async def timeline(self, path: str) -> AsyncIterator[JsonObject]:
            if self.recovering:
                return
            async for frame in super().timeline(path):
                yield frame

    server = SlowServer()
    app = ConversationApp(server, agent_id="main")
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        view = app.conversation
        assert view is not None
        view.event(snapshot())
        server.delay = True
        refresh = asyncio.create_task(app._refresh(view))
        await captured.wait()
        view.event(
            {
                "type": "timeline_update",
                "run_id": "run-one",
                "sequence": 20,
                "base_sequence": 1,
                "child_updates": [{"id": "output", "append": {"content": " current"}}],
            }
        )
        release.set()
        await refresh
        assert view.items[0]["items"][0]["content"] == "Hello current"
        view.page(
            {
                "items": [],
                "active_run": {"run_id": "run-two"},
                "timeline_sequence": 1,
                "incremental": True,
            }
        )
        assert (view.stream_run, view.sequence) == ("run-two", 1)

        # A reconnect's accepted snapshot retires older in-flight history reads,
        # including when each read represents a different Run and sequence space.
        original_sleep = asyncio.sleep

        async def skip_reconnect_delay(delay: float) -> None:
            await original_sleep(0 if delay == 0.5 else delay)

        monkeypatch.setattr(asyncio, "sleep", skip_reconnect_delay)
        captured.clear()
        release.clear()
        server.active = True
        refresh = asyncio.create_task(app._refresh(view))
        await captured.wait()
        server.recovering = True
        await app._follow(view, "/closed-stream")
        release.set()
        await refresh
        assert (view.stream_run, view.sequence) == ("run-three", 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("token,method", [("/he", "chat.commands"), ("@src", "files.list")])
async def test_delayed_completion_cannot_edit_another_conversation(token: str, method: str) -> None:
    captured = asyncio.Event()
    release = asyncio.Event()

    class SlowCatalog(Server):
        @override
        async def rpc(self, name: str, params: JsonObject | None = None) -> JsonObject:
            if name == method:
                captured.set()
                await release.wait()
                return {
                    "items": [{"name": "help", "type": "command"}],
                    "files": ["src/app.py"],
                }
            return await super().rpc(name, params)

    app = ConversationApp(SlowCatalog(), agent_id="main")
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        editor = app.query_one("#composer", TextArea)
        editor.load_text(token)
        editor.move_cursor((0, len(token)))
        worker = app.action_complete()
        await captured.wait()
        await app._open(Conversation("main", project="other", project_selected=True))
        editor.load_text("Other conversation draft")
        release.set()
        await worker.wait()
        assert not isinstance(app.screen, Picker)
        assert editor.text == "Other conversation draft"


@pytest.mark.asyncio
async def test_transport_preserves_send_uncertainty_and_local_files(tmp_path: Path) -> None:
    calls: list[httpx.Request] = []

    async def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path == "/api/rpc":
            raise httpx.ReadError("lost", request=request)
        return httpx.Response(200, content=b"file contents")

    async with TerminalClient("http://example.invalid:8420") as client:
        await client.http.aclose()
        client.http = httpx.AsyncClient(
            base_url=client.url, transport=httpx.MockTransport(respond), trust_env=False
        )
        with pytest.raises(ClientError) as failure:
            await client.rpc("chat.stream", {"content": "once"})
        assert failure.value.state == "unknown"
        assert len(calls) == 1
        destination = tmp_path / "download.txt"
        await client.download("/api/files/capability", destination)
        assert destination.read_bytes() == b"file contents"
        with pytest.raises(FileExistsError):
            await client.download("/api/files/capability", destination)
        with pytest.raises(ClientError):
            await client.download(
                "https://elsewhere.invalid/api/files/capability", tmp_path / "other.txt"
            )
        assert destination.read_bytes() == b"file contents"


def test_tool_details_preserve_diffs_and_offer_media_downloads() -> None:
    item = {
        "id": "tool",
        "type": "tool_call",
        "arguments": {"secret": "not visible", "path": "a.py"},
        "display": {
            "hidden_argument_keys": ["secret"],
            "details": [
                {
                    "type": "file_changes",
                    "files": [
                        {
                            "path": "a.py",
                            "change": "updated",
                            "hunks": [{"old_start": 1, "new_start": 1, "lines": ["-old", "+new"]}],
                        }
                    ],
                }
            ],
            "media": [{"url": "/api/files/output", "filename": "result.png"}],
        },
    }
    detail = tool_detail(item)
    assert "-old" in detail and "+new" in detail
    assert "not visible" not in detail
    assert file_links([item]) == [("result.png", "/api/files/output")]
