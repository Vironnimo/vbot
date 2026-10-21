"""Mounted Actions contracts: server controls, address guards and edit recovery."""

from __future__ import annotations

import asyncio
import copy
from typing import Any, override

import pytest
from textual.widgets import OptionList, TextArea

from cli.tui import actions
from cli.tui.app import ConversationApp
from cli.tui.dialogs import Picker, TextDialog
from cli.tui.state import Conversation, JsonObject
from cli.tui.transport import ClientError
from tests.cli.test_tui import Server


class ActionServer(Server):
    @override
    def __init__(self) -> None:
        super().__init__()
        self.controls = {"compaction": "idle", "background_tool_call_ids": ["call-one"]}
        self.items = [
            {
                "id": "user",
                "type": "message",
                "message": {
                    "id": "user",
                    "role": "user",
                    "content": "Original request",
                    "editable": True,
                },
            },
            {
                "id": "run",
                "type": "assistant_run",
                "run_id": "run-one",
                "status": "running",
                "items": [
                    {
                        "id": "tool",
                        "type": "tool_call",
                        "toolCallId": "call-one",
                        "name": "bash",
                        "status": "running",
                    },
                ],
            },
        ]
        self.edit_error: ClientError | None = None
        self.edit_started = asyncio.Event()
        self.release_edit = asyncio.Event()
        self.release_edit.set()

    @override
    async def rpc(self, method: str, params: JsonObject | None = None) -> JsonObject:
        params = params or {}
        if method in {
            "session.rename",
            "session.fork",
            "chat.edit",
            "chat.cancel_tool_call",
            "chat.control_run",
        }:
            self.calls.append((method, copy.deepcopy(params)))
            if method == "session.fork":
                return {"session": {"id": "forked", "agent_id": params["agent_id"]}}
            if method == "chat.edit":
                self.edit_started.set()
                await self.release_edit.wait()
                if self.edit_error:
                    raise self.edit_error
                self.active = True
                return {"run_id": "run-one", "session_id": params["session_id"]}
            return {"ok": True}
        result = await super().rpc(method, params)
        if method == "chat.timeline" and self.active:
            result["active_run"]["controls"] = copy.deepcopy(self.controls)
        return result


async def choose(app: ConversationApp, pilot: Any, value: Any) -> None:
    screen = app.screen
    assert isinstance(screen, Picker)
    index = next(
        index for index, (_, candidate) in enumerate(screen.filtered) if candidate == value
    )
    screen.query_one(OptionList).highlighted = index
    await pilot.press("enter")
    await pilot.pause()


async def open_actions(app: ConversationApp, pilot: Any) -> Any:
    worker = app._start(lambda: actions.menu(app))
    await pilot.pause()
    assert isinstance(app.screen, Picker)
    return worker


async def edit_dialog(app: ConversationApp, pilot: Any) -> Any:
    worker = await open_actions(app, pilot)
    await choose(app, pilot, "edit")
    screen = app.screen
    assert isinstance(screen, Picker)
    await choose(app, pilot, screen.choices[0][1])
    assert isinstance(app.screen, TextDialog)
    return worker


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["rename", "fork"])
async def test_session_actions_use_the_selected_address_and_keep_its_draft(operation: str) -> None:
    server = ActionServer()
    app = ConversationApp(server, agent_id="main")
    async with app.run_test(size=(100, 35)) as pilot:
        await pilot.pause()
        original = app.conversation
        assert original is not None
        app.query_one("#composer", TextArea).load_text("Unsent original draft")
        await open_actions(app, pilot)
        await choose(app, pilot, operation)
        if operation == "rename":
            app.screen.query_one(TextArea).load_text("Release")
            await pilot.press("ctrl+s")
            await pilot.pause()
            assert (
                "session.rename",
                {"agent_id": "main", "session_id": "session-one", "title": "Release"},
            ) in server.calls
            assert app.conversation is original
        else:
            assert (
                "session.fork",
                {"agent_id": "main", "session_id": "session-one"},
            ) in server.calls
            assert app.conversation is not None and app.conversation.session == "forked"
            assert original in app.navigation.backstack
        assert original.draft == "Unsent original draft"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "active", "method", "extra"),
    [
        (
            "cancel",
            True,
            "chat.cancel_tool_call",
            {"run_id": "run-one", "tool_call_id": "call-one"},
        ),
        (
            "background",
            True,
            "chat.control_run",
            {"run_id": "run-one", "tool_call_id": "call-one", "action": "background_tool"},
        ),
        ("compact", True, "chat.control_run", {"run_id": "run-one", "action": "compact"}),
        ("compact", False, "chat.stream", {"content": "/compact", "reply_surface": "tui"}),
    ],
)
async def test_run_actions_follow_the_current_server_controls(
    operation: str, active: bool, method: str, extra: JsonObject
) -> None:
    server = ActionServer()
    server.active = active
    app = ConversationApp(server, agent_id="main")
    async with app.run_test(size=(100, 35)) as pilot:
        await pilot.pause()
        worker = await open_actions(app, pilot)
        action = (
            operation,
            "run-one" if active else None,
            "call-one" if operation != "compact" else None,
        )
        await choose(app, pilot, action)
        # An idle picker does not mean the action's server refresh and RPC have finished.
        await worker.wait()
        expected = {"agent_id": "main", **extra}
        if operation != "cancel":
            expected["session_id"] = "session-one"
        assert (method, expected) in server.calls
        assert sum(call[0] == method for call in server.calls) == 1


@pytest.mark.asyncio
async def test_action_selection_rechecks_run_eligibility_and_ignores_a_moved_view() -> None:
    server = ActionServer()
    server.active = True
    app = ConversationApp(server, agent_id="main")
    async with app.run_test(size=(100, 35)) as pilot:
        await pilot.pause()
        await open_actions(app, pilot)
        assert isinstance(app.screen, Picker)
        assert "edit" not in [value for _, value in app.screen.choices]
        server.controls["background_tool_call_ids"] = []
        await choose(app, pilot, ("background", "run-one", "call-one"))
        assert not any(method == "chat.control_run" for method, _ in server.calls)
        server.controls["compaction"] = "pending"
        await open_actions(app, pilot)
        assert isinstance(app.screen, Picker)
        assert not any(
            isinstance(value, tuple) and value[0] in {"compact", "background"}
            for _, value in app.screen.choices
        )
        await choose(app, pilot, "rename")
        assert app.conversation is not None
        app.conversation.agent = "moved"
        app.screen.query_one(TextArea).load_text("Wrong address")
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert not any(method == "session.rename" for method, _ in server.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("resolution", ["accepted", "restore", "review"])
async def test_uncertain_edit_retains_exact_request_and_requires_explicit_reconciliation(
    resolution: str,
) -> None:
    server = ActionServer()
    server.edit_error = ClientError("Lost response", state="unknown")
    app = ConversationApp(server, agent_id="main")
    async with app.run_test(size=(100, 35)) as pilot:
        await pilot.pause()
        original = app.conversation
        assert original is not None
        app.query_one("#composer", TextArea).load_text("Later unsent draft")
        await edit_dialog(app, pilot)
        app.screen.query_one(TextArea).load_text("Edited request")
        await pilot.press("ctrl+s")
        await pilot.pause()
        [saved] = app.submissions.saved_drafts
        assert saved["text"] == "Edited request"
        assert saved["agent"] == "main" and saved["session"] == "session-one"
        assert saved["project_selected"] is False and saved["draft_id"]
        assert saved["uncertain"] is True and saved["operation"] == "chat.edit"
        assert saved["request"] == {
            "agent_id": "main",
            "session_id": "session-one",
            "message_id": "user",
            "content": "Edited request",
            "reply_surface": "tui",
        }
        assert original.draft == "Later unsent draft"
        later = {
            "text": "Another preserved input",
            "agent": "main",
            "session": "session-one",
            "attachments": [],
            "mentions": [],
            "draft_id": "later",
        }
        app.submissions.saved_drafts.append(later)
        await open_actions(app, pilot)
        await choose(app, pilot, "edit")
        await choose(app, pilot, resolution)
        if resolution == "accepted":
            assert app.submissions.saved_drafts == [later]
        else:
            assert isinstance(app.screen, TextDialog)
            assert "Edited request" in app.screen.query_one(TextArea).text
            assert app.screen.read_only is (resolution == "review")
            await pilot.press("escape")
            await pilot.pause()
            assert saved in app.submissions.saved_drafts and later in app.submissions.saved_drafts
        assert sum(method == "chat.edit" for method, _ in server.calls) == 1
        assert original.draft == "Later unsent draft"


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["navigate", "rename", "move"])
async def test_delayed_edit_retains_the_sessions_address_without_submission(change: str) -> None:
    server = ActionServer()
    app = ConversationApp(server, agent_id="main")
    async with app.run_test(size=(100, 35)) as pilot:
        await pilot.pause()
        await edit_dialog(app, pilot)
        assert app.conversation is not None
        expected_agent = "main"
        if change == "navigate":
            app.conversation = Conversation("another", session="another-session")
        elif change == "rename":
            expected_agent = "renamed"
            await app.navigation.resource_changed(
                {
                    "kind": "agents",
                    "scope": {
                        "old_agent_id": "main",
                        "new_agent_id": expected_agent,
                    },
                }
            )
        else:
            expected_agent = "worker@project"
            await app.navigation.resource_changed(
                {
                    "kind": "sessions",
                    "scope": {
                        "agent_id": "main",
                        "session_id": "session-one",
                        "moved_to_agent_id": expected_agent,
                    },
                }
            )
        app.screen.query_one(TextArea).load_text("Keep this edit")
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert not any(method == "chat.edit" for method, _ in server.calls)
        [saved] = app.submissions.saved_drafts
        assert (saved["agent"], saved["session"], saved["text"], saved["uncertain"]) == (
            expected_agent,
            "session-one",
            "Keep this edit",
            False,
        )
        assert saved["request"]["agent_id"] == "main"


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["accepted", "refused", "cancelled", "moved_unknown"])
async def test_edit_retains_input_during_admission_and_only_clears_known_success(
    outcome: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = ActionServer()
    server.release_edit.clear()
    if outcome == "refused":
        server.edit_error = ClientError("Message is no longer editable")
    elif outcome == "moved_unknown":
        server.edit_error = ClientError("Lost response", state="unknown")
    app = ConversationApp(server, agent_id="main")
    checkpoints = []

    async def persist() -> None:
        checkpoints.append(
            (server.edit_started.is_set(), copy.deepcopy(app.submissions.saved_drafts))
        )

    monkeypatch.setattr(app.submissions, "persist", persist)
    async with app.run_test(size=(100, 35)) as pilot:
        await pilot.pause()
        worker = await edit_dialog(app, pilot)
        app.screen.query_one(TextArea).load_text("Preserved while sending")
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert server.edit_started.is_set()
        assert checkpoints[0][0] is False
        assert checkpoints[0][1][0]["text"] == "Preserved while sending"
        assert checkpoints[0][1][0]["uncertain"] is True
        [record] = app.submissions.saved_drafts
        assert record["uncertain"] is True
        if outcome == "moved_unknown":
            await app.navigation.resource_changed(
                {
                    "kind": "sessions",
                    "scope": {
                        "agent_id": "main",
                        "session_id": "session-one",
                        "moved_to_agent_id": "worker@project",
                    },
                }
            )
        if outcome == "cancelled":
            worker.cancel()
        else:
            server.release_edit.set()
        await pilot.pause()
        assert not app.submissions.pending
        assert sum(method == "chat.edit" for method, _ in server.calls) == 1
        if outcome == "accepted":
            assert app.submissions.saved_drafts == []
        else:
            assert app.submissions.saved_drafts == [record]
            assert record["uncertain"] is (outcome in {"cancelled", "moved_unknown"})
            if outcome == "moved_unknown":
                assert record["agent"] == "worker@project"
                assert record["request"]["agent_id"] == "main"


@pytest.mark.asyncio
async def test_actions_reaches_an_unselected_restored_draft() -> None:
    server = ActionServer()
    app = ConversationApp(server, agent_id="main")
    dormant = Conversation("other", draft="Recovered unfinished first message")
    app.navigation.views[dormant.key] = dormant
    async with app.run_test(size=(100, 35)) as pilot:
        await pilot.pause()
        await open_actions(app, pilot)
        assert isinstance(app.screen, Picker)
        assert "restore_draft" in [value for _, value in app.screen.choices]
        await choose(app, pilot, "restore_draft")
        assert isinstance(app.screen, Picker)
        assert any("Recovered unfinished first message" in label for label, _ in app.screen.choices)
