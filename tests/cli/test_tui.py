"""Terminal accessor contracts at the mounted UI and HTTP boundaries."""

from __future__ import annotations

import asyncio
import copy
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, override

import httpx
import pytest
from textual.widgets import TextArea
from websockets.exceptions import ConnectionClosedError
from websockets.frames import Close

import cli.tui.app as app_module
import cli.tui.transport as transport_module
from cli._server_target import resolve_instance
from cli.tui.app import ConversationApp
from cli.tui.dialogs import Picker, TextDialog
from cli.tui.drafts import draft_record
from cli.tui.rendering import file_links, tool_detail
from cli.tui.state import Conversation
from cli.tui.transport import ClientError, TerminalClient
from tests.cli.cli_test_support import make_instance

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
        if method == "chat.commands":
            return {
                "items": [
                    {
                        "name": "help",
                        "description": "Show the keyboard controls.",
                        "type": "command",
                        "argument": "none",
                    },
                    {
                        "name": "model",
                        "description": "Set the Model.",
                        "type": "command",
                        "argument": "required",
                    },
                ]
            }
        if method == "model.list":
            return {
                "models": [
                    {
                        "id": "openai/gpt-5.2",
                        "name": "GPT-5.2",
                        "provider_id": "openai",
                        "model_id": "gpt-5.2",
                    }
                ]
            }
        if method == "files.list":
            return {"files": ["first.txt", "later.txt"]}
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
        editor.load_text("Retain this draft")
        app.transcript.focus()
        await pilot.press("enter")
        await pilot.pause()
        # Enter in the conversation opens a step; it never sends the draft.
        assert app.conversation.expanded_rows
        assert editor.text == "Retain this draft"
        await pilot.press("escape")
        assert app.focused is editor
        assert len([method for method, _ in server.calls if method == "chat.stream"]) == 1
        editor.focus()
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
        editor.load_text("First input @first.txt")
        await pilot.press("ctrl+s")
        await server.sent.wait()
        editor.load_text("Next input @later.txt")
        app.conversation.attachments.append(later_attachment)
        app.conversation.mentions.append("later.txt")
        await pilot.pause()
        server.release_send.set()
        await pilot.pause()
        assert editor.text == "Next input @later.txt"
        assert app.conversation is not None
        assert app.conversation.uncertain is unknown
        if unknown:
            assert app.conversation.uncertain_submission is not None
            assert app.conversation.uncertain_submission["text"] == "First input @first.txt"
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
            editor.load_text("Next input @later.txt")
            app.conversation.mentions.append("later.txt")
            app.conversation.attachments.extend([first_attachment, later_attachment])
            await pilot.press("ctrl+s")
            await pilot.pause()
            await pilot.press("down", "down", "down", "enter")
            await pilot.pause()
            assert editor.text == "First input @first.txt"
            assert app.conversation.attachments == [first_attachment]
            assert app.conversation.mentions == ["first.txt"]
            assert len(app.saved_drafts) == 1
            saved = app.saved_drafts[0]
            assert saved["text"] == "Next input @later.txt"
            assert saved["attachments"] == [first_attachment, later_attachment]
            assert saved["mentions"] == ["later.txt"]
            assert (saved["agent"], saved["session"]) == ("main", "session-one")
            assert saved["draft_id"] != app.conversation.draft_id
            editor.load_text("")
            app.conversation.attachments.clear()
            app.conversation.mentions.clear()
            await pilot.press("alt+up")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            assert editor.text == "Next input @later.txt"
            assert app.conversation.attachments == [first_attachment, later_attachment]
            assert app.conversation.mentions == ["later.txt"]
        else:
            assert app.conversation.attachments == [later_attachment]
            assert app.conversation.mentions == ["later.txt"]


def test_timeline_patch_order_gap_and_pagination_contract() -> None:
    view = Conversation("main", "session-one")
    view.event(snapshot())
    previous = view.items
    original_content = previous[0]["items"][0]["content"]
    unchanged_tool = previous[0]["items"][2]
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
    assert previous[0]["items"][0]["content"] == original_content
    assert view.items[1]["items"][1] is unchanged_tool
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
@pytest.mark.parametrize("action", ["older", "queue"])
async def test_delayed_conversation_reads_cannot_change_a_new_selection(action: str) -> None:
    captured = asyncio.Event()
    release = asyncio.Event()

    class DelayedServer(Server):
        delay = False

        @override
        async def rpc(self, method: str, params: JsonObject | None = None) -> JsonObject:
            response = await super().rpc(method, params)
            if self.delay and (
                (action == "older" and method == "chat.timeline" and (params or {}).get("before"))
                or (action == "queue" and method == "chat.queue_list")
            ):
                captured.set()
                await release.wait()
                if action == "older":
                    response["items"] = [
                        {"id": "old-row", "type": "message", "message": {"content": "Old"}}
                    ]
            return response

    server = DelayedServer()
    app = ConversationApp(server, agent_id="main")
    async with app.run_test(size=(100, 35)) as pilot:
        await pilot.pause()
        previous = app.conversation
        assert previous is not None
        previous.next_before = "older"
        server.delay = True
        worker = app.action_older() if action == "older" else app.action_queue()
        await captured.wait()
        await app._open(Conversation("main", project="another", project_selected=True))
        release.set()
        await worker.wait()
        assert app.conversation is not previous
        assert not previous.items
        assert not isinstance(app.screen, Picker)
        assert "old-row" not in app.transcript.rows


@pytest.mark.asyncio
async def test_transport_preserves_send_uncertainty_and_local_files(tmp_path: Path) -> None:
    calls: list[httpx.Request] = []

    async def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path == "/api/rpc":
            raise httpx.ReadError("lost", request=request)
        return httpx.Response(200, content=b"file contents")

    target = make_instance(tmp_path, host="example.invalid")
    async with TerminalClient(target, transport=httpx.MockTransport(respond)) as client:
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "code", "close", "close_code", "explained"),
    [
        pytest.param(
            401,
            "auth_required",
            4403,
            "access_denied",
            "vbot devices pair https://vbot.example.com",
            id="refused",
        ),
        pytest.param(
            503,
            "devices_unavailable",
            4503,
            "devices_unavailable",
            "device store is unavailable",
            id="device-store-unavailable",
        ),
    ],
)
async def test_transport_explains_access_refusals(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    code: str,
    close: int,
    close_code: str,
    explained: str,
) -> None:
    async def respond(request: httpx.Request) -> httpx.Response:
        error = {"code": code, "message": "Refused"}
        return httpx.Response(status, json={"ok": False, "error": error})

    class RefusedSocket:
        async def __aenter__(self) -> RefusedSocket:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        def __aiter__(self) -> RefusedSocket:
            return self

        async def __anext__(self) -> str:
            raise ConnectionClosedError(Close(close, "Refused"), None)

    monkeypatch.setattr(transport_module, "connect", lambda *_args, **_kwargs: RefusedSocket())
    target = resolve_instance(data_dir=tmp_path / "data", url="https://vbot.example.com")
    async with TerminalClient(target, transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(ClientError) as rpc_refusal:
            await client.rpc("agent.list")
        with pytest.raises(ClientError) as stream_refusal:
            async for _event in client.events():
                pass

    assert rpc_refusal.value.code == code
    assert explained in str(rpc_refusal.value)
    assert stream_refusal.value.code == close_code


@pytest.mark.asyncio
async def test_events_stop_after_a_repeated_access_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    class RefusingServer(Server):
        attempts = 0

        @override
        async def events(self) -> AsyncIterator[JsonObject]:
            RefusingServer.attempts += 1
            raise ClientError("Sign in with: vbot devices pair", code="auth_required")
            yield {}

    async def no_delay(_seconds: float) -> None:
        return

    monkeypatch.setattr(app_module, "_sleep", no_delay)
    app = ConversationApp(RefusingServer(), agent_id="main")
    async with app.run_test(size=(100, 35)) as pilot:
        await pilot.pause()
        await pilot.pause()

        # One immediate retry reads the credential again; then the stream stays closed.
        assert RefusingServer.attempts == 2
        assert not app.connected
        assert "vbot devices pair" in app.last_notice


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


@pytest.mark.asyncio
@pytest.mark.parametrize("reconnect", [False, True])
async def test_catch_up_drains_all_pages_before_completion_is_acknowledged(
    reconnect: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class CatchUpServer(Server):
        catching_up = False

        @override
        async def rpc(self, method: str, params: JsonObject | None = None) -> JsonObject:
            params = params or {}
            if method == "session.get" and self.catching_up:
                return {
                    "session": {
                        "title": "Current",
                        "latest_completion_run_id": "latest",
                        "has_unread_completion": True,
                    }
                }
            if method == "chat.timeline":
                self.calls.append((method, params))
                if not self.catching_up:
                    return {"items": [], "next_after": "zero", "history_generation": "g"}
                cursor = params.get("after")
                run = "earlier" if cursor == "zero" else "latest"
                return {
                    "items": [
                        {
                            "id": run,
                            "type": "assistant_run",
                            "run_id": run,
                            "status": "completed",
                            "items": [
                                {"id": f"{run}-text", "type": "assistant_output", "content": run}
                            ],
                        }
                    ],
                    "incremental": True,
                    "replace_run_ids": [run],
                    "next_after": "one" if run == "earlier" else "two",
                    "has_newer": run == "earlier",
                    "history_generation": "g",
                }
            return await super().rpc(method, params)

        @override
        async def timeline(self, path: str) -> AsyncIterator[JsonObject]:
            if False:
                yield {}

    async def no_delay(_seconds: float) -> None:
        return

    monkeypatch.setattr(app_module, "_sleep", no_delay)
    server = CatchUpServer()
    app = ConversationApp(server, agent_id="main")
    async with app.run_test(size=(100, 35)) as pilot:
        await pilot.pause()
        view = app.conversation
        assert view is not None
        server.catching_up = True
        server.calls.clear()
        if reconnect:
            await app._follow(view, "/closed")
            assert [row["id"] for row in view.items] == ["earlier", "latest"]
            # The normal refresh also owns Session metadata and read receipts.
            view.facts["next_after"] = "zero"
        await app._refresh(view)
        assert view.facts["has_newer"] is False
        assert [row["id"] for row in view.items] == ["earlier", "latest"]
        assert [
            params["after"] for method, params in server.calls if method == "chat.timeline"
        ] == (["zero", "one"] * (2 if reconnect else 1))
        assert not any(method == "session.mark_read" for method, _ in server.calls)
        modal = TextDialog("Inspecting something else", read_only=True)
        await app.push_screen(modal)
        await app._render()
        await pilot.pause()
        assert not any(method == "session.mark_read" for method, _ in server.calls)
        modal.dismiss(None)
        app.app_focus = True
        await pilot.pause()
        app._completion_if_visible()
        await pilot.pause()
        assert [
            (method, params) for method, params in server.calls if method == "session.mark_read"
        ] == [
            (
                "session.mark_read",
                {"agent_id": "main", "session_id": "session-one", "run_id": "latest"},
            )
        ]


@pytest.mark.asyncio
async def test_lost_first_response_recovers_exact_session_without_resending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checkpoints: list[JsonObject] = []

    class AcceptedServer(Server):
        admitted: str | None = None
        reveal = False

        @override
        async def rpc(self, method: str, params: JsonObject | None = None) -> JsonObject:
            params = params or {}
            if method == "chat.stream":
                assert (
                    checkpoints
                    and checkpoints[-1]["new_session"]["session_id"]
                    == params["new_session"]["session_id"]
                )
                self.calls.append((method, copy.deepcopy(params)))
                self.admitted = params["new_session"]["session_id"]
                raise ClientError("Lost response", state="unknown")
            if method == "chat.timeline" and params.get("session_id") == self.admitted:
                if not self.reveal:
                    raise ClientError("Not found yet", code="session_not_found")
                return {"items": [], "runs": [{"run_id": "accepted", "complete": True}]}
            return await super().rpc(method, params)

    server = AcceptedServer()
    app = ConversationApp(server, agent_id="main", project_id="project-one")

    async def checkpoint() -> None:
        assert app.conversation is not None and app.conversation.uncertain
        assert app.conversation.uncertain_submission is not None
        checkpoints.append(copy.deepcopy(app.conversation.uncertain_submission["request"]))

    monkeypatch.setattr(app, "persist_drafts", checkpoint)
    async with app.run_test(size=(100, 35)) as pilot:
        await pilot.pause()
        editor = app.query_one("#composer", TextArea)
        editor.load_text("First request")
        worker = app.action_send()
        await worker.wait()
        view = app.conversation
        assert view is not None and view.uncertain and view.session is None
        assert view.pending_session_id == server.admitted
        assert checkpoints[-1]["new_session"]["working_project_id"] == "project-one"
        editor.load_text("Following request")
        await pilot.pause()
        server.reveal = True
        await app._refresh(view)
        assert view.session == server.admitted
        assert not view.uncertain and view.pending_session_id is None
        assert editor.text == "Following request"
        assert len([method for method, _ in server.calls if method == "chat.stream"]) == 1
        assert "new_session" not in view.submission(editor.text)


@pytest.mark.asyncio
async def test_send_checkpoint_failure_keeps_input_without_admitting_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = Server()
    app = ConversationApp(server, agent_id="main")

    async def failing_checkpoint() -> None:
        raise OSError("Unavailable local draft storage")

    monkeypatch.setattr(app, "persist_drafts", failing_checkpoint)
    async with app.run_test(size=(100, 35)) as pilot:
        await pilot.pause()
        app.query_one("#composer", TextArea).load_text("Retain this request")
        await app.action_send().wait()
        assert not server.sent.is_set()
        assert app.conversation is not None
        assert app.conversation.draft == "Retain this request"
        assert not app.conversation.uncertain


@pytest.mark.asyncio
async def test_sessionless_command_does_not_adopt_its_proposed_session() -> None:
    class CommandServer(Server):
        @override
        async def rpc(self, method: str, params: JsonObject | None = None) -> JsonObject:
            if method == "chat.stream":
                return {"command_handled": True, "data": {"command": "status"}}
            return await super().rpc(method, params)

    app = ConversationApp(CommandServer(), agent_id="main", project_id="project-one")
    async with app.run_test(size=(100, 35)) as pilot:
        await pilot.pause()
        app.query_one("#composer", TextArea).load_text("/status")
        await app.action_send().wait()
        assert app.conversation is not None
        assert app.conversation.session is None
        assert app.conversation.pending_session_id is None
        assert app.conversation.draft == "" and not app.conversation.uncertain


@pytest.mark.asyncio
async def test_scoped_rename_move_and_deletion_keep_local_drafts() -> None:
    app = ConversationApp(Server(), agent_id="main")
    async with app.run_test(size=(100, 35)) as pilot:
        await pilot.pause()
        view = app.conversation
        assert view is not None
        app.query_one("#composer", TextArea).load_text("Unsent context")
        await pilot.pause()
        displaced = draft_record(view)
        assert displaced is not None
        app.recovered_drafts["displaced"] = displaced
        app.recovered_drafts["other-session"] = {**displaced, "session": "other-session"}
        saved_edit = {
            **app._saved_input(view),
            "operation": "chat.edit",
            "message_id": "message-one",
            "uncertain": True,
            "request": {"agent_id": "main", "session_id": "session-one"},
        }
        app.saved_drafts.append(saved_edit)
        old_key = view.key
        await app._resource_changed(
            {"kind": "agents", "scope": {"old_agent_id": "main", "new_agent_id": "renamed"}}
        )
        assert view.agent == "renamed" and old_key not in app.views
        assert displaced["agent"] == saved_edit["agent"] == "renamed"
        await app._resource_changed(
            {
                "kind": "sessions",
                "scope": {
                    "agent_id": "renamed",
                    "session_id": "session-one",
                    "moved_to_agent_id": "worker@project",
                },
            }
        )
        assert app.conversation is view and view.agent == "worker@project"
        assert view.draft == "Unsent context"
        assert displaced["agent"] == saved_edit["agent"] == "worker@project"
        assert app.recovered_drafts["other-session"]["agent"] == "renamed"
        assert saved_edit["request"]["agent_id"] == "main"
        await app._resource_changed(
            {
                "kind": "sessions",
                "scope": {
                    "agent_id": "worker",
                    "project_id": "another-project",
                    "deleted_session_id": "session-one",
                },
            }
        )
        assert view.available
        await app._resource_changed(
            {
                "kind": "sessions",
                "scope": {
                    "agent_id": "worker",
                    "project_id": "project",
                    "deleted_session_id": "session-one",
                    "next_session_id": None,
                },
            }
        )
        assert not view.available and app.conversation is not view
        saved = app.saved_drafts[-1]
        assert (saved["agent"], saved["session"], saved["text"]) == (
            "worker@project",
            "session-one",
            "Unsent context",
        )
