"""Mounted operator flows and file delivery contracts for the terminal accessor."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, override
from unittest.mock import AsyncMock, call

import httpx
import pytest
from textual.app import App, ComposeResult
from textual.widgets import Button, Checkbox, DirectoryTree, Input, Select, SelectionList, TextArea

from cli.tui.app import ConversationApp
from cli.tui.dialogs import ExtensionDialog, PathDialog, Picker
from cli.tui.transport import ClientError, TerminalClient
from tests.cli.test_tui import Server

type JsonObject = dict[str, Any]


@pytest.mark.asyncio
@pytest.mark.parametrize("flow", ["form", "cancel", "leave", "url", "oauth"])
async def test_extension_dialog_routes_typed_answers_and_preserves_unanswered_requests(
    flow: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = Server()
    original_rpc = server.rpc
    answered = False
    request: JsonObject = {
        "id": "request-one",
        "extension": "mcp",
        "kind": "oauth" if flow == "oauth" else "elicitation",
        "response_operation": "respond",
        "payload": {
            "message": "Choose settings",
            "requestedSchema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "count": {"type": "integer"},
                    "enabled": {"type": "boolean"},
                    "level": {"enum": [1, 2]},
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "metadata": {"type": "object"},
                    "selections": {"type": "array", "items": {"enum": ["first", "second"]}},
                },
                "required": ["name"],
            },
        },
    }
    if flow in {"url", "oauth"}:
        request["payload"]["url"] = "https://example.invalid/authorize?state=one"
    if flow == "url":
        request["payload"]["mode"] = "url"

    async def respond(method: str, params: JsonObject | None = None) -> JsonObject:
        nonlocal answered
        if method == "extensions.requests":
            return {"requests": [] if answered else [request]}
        if method == "extensions.operation":
            answered = True
            return {"ok": True}
        return await original_rpc(method, params)

    rpc = AsyncMock(side_effect=respond)
    monkeypatch.setattr(server, "rpc", rpc)
    app = ConversationApp(server, agent_id="main")
    async with app.run_test(size=(120, 45)) as pilot:
        await pilot.pause()
        app.query_one("#composer", TextArea).load_text("Unsent conversation draft")
        await pilot.press("f7")
        await pilot.pause()
        assert isinstance(app.screen, Picker)
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, ExtensionDialog)
        expected: JsonObject | None
        if flow == "form":
            app.screen.query_one("#accept", Button).press()
            await pilot.pause()
            assert isinstance(app.screen, ExtensionDialog)
            assert not answered
            app.screen.query_one("#field-0", Input).value = "Ada"
            app.screen.query_one("#field-1", Input).value = "7"
            app.screen.query_one("#field-2", Checkbox).value = True
            app.screen.query_one("#field-3", Select).value = "1"
            app.screen.query_one("#field-4", TextArea).load_text('["one", "two"]')
            app.screen.query_one("#field-6", SelectionList).select(1)
            expected = {
                "action": "accept",
                "content": {
                    "name": "Ada",
                    "count": 7,
                    "enabled": True,
                    "level": 2,
                    "tags": ["one", "two"],
                    "selections": ["second"],
                },
            }
            app.screen.query_one("#accept", Button).press()
        elif flow == "cancel":
            expected = {"action": "cancel"}
            await pilot.click("#cancel")
        elif flow == "leave":
            expected = None
            await pilot.press("escape")
        else:
            assert app.screen.query_one(".request-url", TextArea).text == request["payload"]["url"]
            expected = {"action": "accept"}
            if flow == "oauth":
                redirect = "http://localhost/callback?code=accepted&state=one"
                app.screen.query_one("#redirect", Input).value = redirect
                expected["redirect_url"] = redirect
            await pilot.click("#accept")
        await pilot.pause()
        responses = [
            entry for entry in rpc.await_args_list if entry.args[0] == "extensions.operation"
        ]
        assert responses == (
            [
                call(
                    "extensions.operation",
                    {
                        "name": "mcp",
                        "operation": "respond",
                        "arguments": {"request_id": "request-one", "response": expected},
                    },
                )
            ]
            if expected is not None
            else []
        )
        assert answered is (expected is not None)
        assert app.query_one("#composer", TextArea).text == "Unsent conversation draft"
        assert not server.sent.is_set()


@pytest.mark.asyncio
async def test_extension_server_refusal_reopens_form_with_values_and_authoritative_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = Server()
    original_rpc = server.rpc
    accepted = False
    submissions = 0
    request: JsonObject = {
        "id": "request-validation",
        "extension": "mcp",
        "kind": "elicitation",
        "response_operation": "respond",
        "payload": {
            "message": "Choose settings",
            "requestedSchema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "count": {"type": "integer", "minimum": 5},
                },
                "required": ["name", "count"],
            },
        },
    }

    async def respond(method: str, params: JsonObject | None = None) -> JsonObject:
        nonlocal accepted, submissions
        if method == "extensions.requests":
            return {"requests": [] if accepted else [request]}
        if method == "extensions.operation":
            submissions += 1
            if submissions == 1:
                raise ClientError(
                    "MCP input response does not satisfy the requested schema: count does not "
                    "meet the schema's 'minimum' rule",
                    code="invalid_request",
                )
            accepted = True
            return {"ok": True}
        return await original_rpc(method, params)

    rpc = AsyncMock(side_effect=respond)
    monkeypatch.setattr(server, "rpc", rpc)
    app = ConversationApp(server, agent_id="main")
    async with app.run_test(size=(120, 45)) as pilot:
        await pilot.pause()
        await pilot.press("f7", "enter")
        await pilot.pause()
        assert isinstance(app.screen, ExtensionDialog)
        app.screen.query_one("#field-0", Input).value = "Ada"
        app.screen.query_one("#field-1", Input).value = "2"
        await pilot.click("#accept")
        await pilot.pause()
        assert isinstance(app.screen, ExtensionDialog)
        assert app.screen.query_one("#field-0", Input).value == "Ada"
        assert app.screen.query_one("#field-1", Input).value == "2"
        assert "minimum" in app.screen.problem
        assert submissions == 1
        app.screen.query_one("#field-1", Input).value = "5"
        await pilot.click("#accept")
        await pilot.pause()
        assert submissions == 2
        assert accepted


@pytest.mark.asyncio
async def test_unknown_extension_response_is_explicit_and_never_retried_automatically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = Server()
    original_rpc = server.rpc
    submissions = 0
    request: JsonObject = {
        "id": "request-unknown",
        "extension": "mcp",
        "kind": "elicitation",
        "response_operation": "respond",
        "payload": {
            "message": "Choose settings",
            "requestedSchema": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        },
    }

    async def respond(method: str, params: JsonObject | None = None) -> JsonObject:
        nonlocal submissions
        if method == "extensions.requests":
            return {"requests": [request]}
        if method == "extensions.operation":
            submissions += 1
            raise ClientError("The response outcome is unknown.", state="unknown")
        return await original_rpc(method, params)

    rpc = AsyncMock(side_effect=respond)
    monkeypatch.setattr(server, "rpc", rpc)
    app = ConversationApp(server, agent_id="main")
    async with app.run_test(size=(120, 45)) as pilot:
        await pilot.pause()
        await pilot.press("f7", "enter")
        await pilot.pause()
        assert isinstance(app.screen, ExtensionDialog)
        app.screen.query_one("#field-0", Input).value = "Ada"
        await pilot.click("#accept")
        await pilot.pause()
        assert isinstance(app.screen, ExtensionDialog)
        assert app.screen.outcome_unknown
        assert "outcome is unknown" in app.screen.problem.lower()
        assert app.screen.query_one("#field-0", Input).value == "Ada"
        assert app.screen.query_one("#accept", Button).label == "Respond again"
        assert submissions == 1
        await pilot.press("escape")
        await pilot.pause()
        assert submissions == 1


class _PathDialogHost(App[None]):
    @override
    def compose(self) -> ComposeResult:
        yield TextArea("host")


@pytest.mark.asyncio
async def test_path_dialog_accepts_existing_open_file_and_nonexistent_safe_destination(
    tmp_path: Path,
) -> None:
    existing = tmp_path / "source.txt"
    existing.write_text("source", encoding="utf-8")
    destination = tmp_path / "new.txt"
    app = _PathDialogHost()
    async with app.run_test(size=(100, 35)) as pilot:
        open_result: list[str | None] = []
        await app.push_screen(
            PathDialog("Upload local file", str(existing), must_exist=True),
            callback=open_result.append,
        )
        await pilot.pause()
        await pilot.click("#choose")
        await pilot.pause()
        assert open_result == [str(existing)]

        save_result: list[str | None] = []
        await app.push_screen(
            PathDialog("Download destination", str(existing), must_exist=False),
            callback=save_result.append,
        )
        await pilot.pause()
        await pilot.click("#choose")
        await pilot.pause()
        assert isinstance(app.screen, PathDialog)
        assert "already exists" in app.screen.problem
        app.screen.query_one("#path", Input).value = str(destination)
        assert app.screen.query_one("#path", Input).value == str(destination)
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert save_result == [str(destination)]
        assert not destination.exists()
        assert existing.read_text(encoding="utf-8") == "source"


@pytest.mark.asyncio
async def test_path_dialog_save_mode_treats_a_folder_as_the_base_not_the_answer(
    tmp_path: Path,
) -> None:
    folder = tmp_path / "results"
    folder.mkdir()
    app = _PathDialogHost()
    async with app.run_test(size=(100, 35)) as pilot:
        result: list[str | None] = []
        await app.push_screen(
            PathDialog("Download destination", str(tmp_path / "result.txt"), must_exist=False),
            callback=result.append,
        )
        await pilot.pause()
        dialog = app.screen
        assert isinstance(dialog, PathDialog)

        # A folder where a file name belongs is a mistake the operator can
        # correct here, not an answer: say so and keep the dialog open.
        dialog.query_one("#path", Input).value = str(folder)
        await pilot.press("enter")
        await pilot.pause()
        assert result == []
        assert isinstance(app.screen, PathDialog)
        assert "This is a folder" in dialog.problem
        assert f"{os.sep}name.txt" in dialog.problem

        # Picking a folder in the tree keeps it as the base for the file name
        # the operator types next, instead of naming the folder as the file.
        tree = dialog.query_one(DirectoryTree)
        dialog.post_message(DirectoryTree.DirectorySelected(tree.root, folder))
        await pilot.pause()
        assert dialog.query_one("#path", Input).value == str(folder) + os.sep

        destination = folder / "result.txt"
        dialog.query_one("#path", Input).value = str(destination)
        await pilot.press("enter")
        await pilot.pause()
        assert result == [str(destination)]
        assert not destination.exists()


@pytest.mark.asyncio
async def test_path_dialog_keeps_unavailable_file_distinct_from_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable(_path: Path) -> bool:
        raise PermissionError("access denied")

    monkeypatch.setattr("cli.tui.dialogs.is_file_strict", unavailable)
    app = _PathDialogHost()
    async with app.run_test(size=(100, 35)) as pilot:
        result: list[str | None] = []
        await app.push_screen(
            PathDialog("Upload local file", str(tmp_path / "private.txt"), must_exist=True),
            callback=result.append,
        )
        await pilot.pause()
        await pilot.click("#choose")
        await pilot.pause()
        assert result == []
        assert isinstance(app.screen, PathDialog)
        assert app.screen.problem == "Cannot check this path: access denied"


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["history", "live"])
async def test_activity_inspects_and_stops_the_selected_child_not_its_parent(
    source: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = Server()
    child = {
        "id": "child-task",
        "agent_id": "worker",
        "project_id": "project-one",
        "session_id": "child-session",
        "status": "running",
    }
    server.items = (
        [
            {
                "id": "spawn-tool",
                "type": "tool_call",
                "name": "subagent",
                "arguments": {"description": "Investigate the bug"},
                "result": {"ok": True, "data": child},
            }
        ]
        if source == "history"
        else []
    )
    original_rpc = server.rpc

    async def respond(method: str, params: JsonObject | None = None) -> JsonObject:
        if method == "chat.work" and source == "live":
            return {"running": [{"kind": "subagent", "label": "Investigate the bug", **child}]}
        if method == "subagent.inspect":
            return child
        return await original_rpc(method, params)

    rpc = AsyncMock(side_effect=respond)
    monkeypatch.setattr(server, "rpc", rpc)
    app = ConversationApp(server, agent_id="main")
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("f5")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, Picker)
        await pilot.press("down", "down", "enter")
        await pilot.pause()
        calls = [
            entry
            for entry in rpc.await_args_list
            if entry.args[0] in {"subagent.inspect", "chat.stop_all"}
        ]
        assert calls == [
            call(
                "subagent.inspect",
                {
                    "agent_id": "worker@project-one",
                    "session_id": "child-session",
                    "id": "child-task",
                },
            ),
            call(
                "chat.stop_all", {"agent_id": "worker@project-one", "session_id": "child-session"}
            ),
        ]
        assert app.conversation is not None
        assert (app.conversation.agent, app.conversation.session) == ("main", "session-one")


@pytest.mark.asyncio
async def test_provider_default_thinking_is_saved_with_the_explicit_reset_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = Server()
    original_rpc = server.rpc

    async def respond(method: str, params: JsonObject | None = None) -> JsonObject:
        if method == "agent.list":
            return {
                "agents": [
                    {
                        "id": "main",
                        "name": "Main",
                        "current_session_id": "session-one",
                        "effective": {
                            "model": {"value": "openai/gpt-5.2", "source": "agent"},
                            "thinking_effort": {"value": "high", "source": "agent"},
                        },
                    }
                ]
            }
        if method == "model.get":
            return {
                "model": {
                    "capabilities": {"reasoning": {"supported": True, "mandatory": False}},
                    "usable_connections": ["default"],
                    "wire_profiles": {"default": {"reasoning_ladder": ["low", "high"]}},
                }
            }
        if method == "session.set_agent_overrides":
            return {"agent_overrides": {"thinking_effort_default": True}}
        return await original_rpc(method, params)

    rpc = AsyncMock(side_effect=respond)
    monkeypatch.setattr(server, "rpc", rpc)
    app = ConversationApp(server, agent_id="main")
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("f4")
        await pilot.pause()
        await pilot.press("down", "enter")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        rpc.assert_any_await("model.get", {"model": "openai/gpt-5.2"})
        rpc.assert_any_await(
            "session.set_agent_overrides",
            {
                "agent_id": "main",
                "session_id": "session-one",
                "agent_overrides": {"thinking_effort": None, "thinking_effort_default": True},
            },
        )
        assert app.conversation is not None
        assert app.conversation.overrides == {"thinking_effort_default": True}


@pytest.mark.asyncio
async def test_file_picker_keeps_local_uploads_separate_from_server_mentions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = Server()
    original_rpc = server.rpc
    local_path = tmp_path / "local.txt"
    local_path.write_text("local upload bytes", encoding="utf-8")
    attachment = {
        "attachment_id": "upload-one",
        "filename": "local.txt",
        "media_type": "text/plain",
    }
    upload = AsyncMock(return_value=attachment)
    monkeypatch.setattr(server, "upload", upload)
    download = AsyncMock()
    monkeypatch.setattr(server, "download", download)
    server.items = [
        {
            "id": "answer",
            "type": "message",
            "message": {"role": "assistant", "content": "[report.txt](/api/attachments/report)"},
        }
    ]

    async def respond(method: str, params: JsonObject | None = None) -> JsonObject:
        if method == "files.list":
            return {"root": "/server/project", "files": ["src/server.py"]}
        return await original_rpc(method, params)

    rpc = AsyncMock(side_effect=respond)
    monkeypatch.setattr(server, "rpc", rpc)
    app = ConversationApp(server, agent_id="main")
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("f8")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, PathDialog)
        app.screen.query_one("#path", Input).value = str(local_path)
        await pilot.press("ctrl+s")
        await pilot.pause()
        upload.assert_awaited_once_with(local_path)
        await pilot.press("f8")
        await pilot.pause()
        await pilot.press("down", "enter")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert app.conversation is not None
        assert app.conversation.attachments == [attachment]
        assert app.conversation.mentions == ["src/server.py"]
        assert app.query_one("#composer", TextArea).text == "@src/server.py "
        rpc.assert_any_await("files.list", {"agent_id": "main", "session_id": "session-one"})
        await pilot.press("f8")
        await pilot.pause()
        await pilot.press("down", "down", "enter")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, PathDialog)
        destination = tmp_path / "report.txt"
        app.screen.query_one("#path", Input).value = str(destination)
        await pilot.press("ctrl+s")
        await pilot.pause()
        download.assert_awaited_once_with("/api/attachments/report", destination)
        await pilot.press("ctrl+s")
        await server.sent.wait()
        await pilot.pause()
        rpc.assert_any_await(
            "chat.stream",
            {
                "agent_id": "main",
                "session_id": "session-one",
                "reply_surface": "tui",
                "content": [
                    {"type": "text", "text": "@src/server.py "},
                    {"type": "file", **attachment},
                ],
                "file_mentions": ["src/server.py"],
            },
        )


@pytest.mark.asyncio
async def test_file_transport_uploads_bytes_and_preserves_existing_downloads(
    tmp_path: Path,
) -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/upload":
            return httpx.Response(
                200,
                json={"attachment_id": "one", "filename": "source.txt", "media_type": "text/plain"},
            )
        if request.url.path == "/api/files/file-token":
            return httpx.Response(200, content=b"original file")
        assert request.url.path == "/api/attachments/one"
        return httpx.Response(200, content=b"downloaded bytes")

    async with TerminalClient("http://test.invalid") as client:
        await client.http.aclose()
        client.http = httpx.AsyncClient(
            base_url=client.url, transport=httpx.MockTransport(respond), trust_env=False
        )
        source = tmp_path / "source.txt"
        source.write_text("local bytes", encoding="utf-8")
        assert (await client.upload(source))["attachment_id"] == "one"
        assert requests[0].method == "POST"
        assert b'filename="source.txt"' in requests[0].content
        assert b"local bytes" in requests[0].content
        destination = tmp_path / "download.txt"
        await client.download("/api/attachments/one", destination)
        assert destination.read_bytes() == b"downloaded bytes"
        destination.write_bytes(b"keep existing")
        with pytest.raises(FileExistsError):
            await client.download("/api/attachments/one", destination)
        assert destination.read_bytes() == b"keep existing"
        original = tmp_path / "original.txt"
        await client.download("/api/files/file-token?version=1&empty=&download=false", original)
        assert original.read_bytes() == b"original file"
        assert requests[-1].url.params["version"] == "1"
        assert requests[-1].url.params["empty"] == ""
        assert requests[-1].url.params["download"] == "true"
        before = len(requests)
        with pytest.raises(ClientError, match="another server"):
            await client.download("https://elsewhere.invalid/api/attachments/one", destination)
        assert len(requests) == before
