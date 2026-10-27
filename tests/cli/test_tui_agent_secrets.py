"""Agent Secret forms and `/secret` input: values reach only their RPC, never a copy."""

from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, override

import pytest
from textual import events
from textual.widgets import Input, Select, TextArea

from cli.tui import agent_secrets
from cli.tui.app import ConversationApp
from cli.tui.dialogs import Picker, SecretDialog, SecretInput, TextDialog
from cli.tui.drafts import DraftStore
from cli.tui.transport import ClientError
from tests.cli.test_tui import Server

type JsonObject = dict[str, Any]

_SECRET_CHANGED = {"type": "resource_changed", "payload": {"kind": "agent_secrets"}}


def _request(**overrides: Any) -> JsonObject:
    request: JsonObject = {
        "id": "sreq_one",
        "agent_id": "main",
        "session_id": "session-one",
        "project_id": None,
        "name": "GITHUB",
        "kind": "login",
        "description": "Push the release branch",
        "lifetime": "permanent",
        "replaces": False,
        "fields": [
            {"name": "username", "concealed": False, "optional": True},
            {"name": "password", "concealed": True, "optional": False},
            {"name": "url", "concealed": False, "optional": True},
            {"name": "totp", "concealed": True, "optional": True},
        ],
        "created_at": "2026-10-21T10:00:00+00:00",
        "expires_at": "2026-10-21T10:10:00+00:00",
    }
    request.update(overrides)
    return request


class SecretServer(Server):
    """Answers the Agent Secret RPCs; ``next_refusal`` makes the next answer fail."""

    @override
    def __init__(self) -> None:
        super().__init__()
        self.next_refusal: ClientError | None = None
        self.refusal_ends_request = False
        self.command_reply: JsonObject = {
            "command_handled": True,
            "reply": "Stored GITHUB_TOKEN.",
            "output": "toast",
            "session_id": "session-one",
        }

    @override
    async def rpc(self, method: str, params: JsonObject | None = None) -> JsonObject:
        params = params or {}
        if method in {"agent_secrets.answer", "agent_secrets.decline", "agent_secrets.give"}:
            self.calls.append((method, copy.deepcopy(params)))
            if self.next_refusal is not None:
                refusal, self.next_refusal = self.next_refusal, None
                if self.refusal_ends_request:
                    self.secret_requests = []
                raise refusal
            self.secret_requests = [
                item for item in self.secret_requests if item["id"] != params.get("id")
            ]
            if method == "agent_secrets.decline":
                return {"id": params["id"], "declined": True}
            name = params.get("name", "GITHUB")
            secret = {"name": name, "reference": "{{secret:" + name + "}}"}
            return {"secret": secret, "replaced": False}
        if method == "chat.stream" and str(params.get("content", "")).startswith("/secret"):
            self.calls.append((method, copy.deepcopy(params)))
            self.sent.set()
            if self.fail_send:
                raise ClientError(
                    "Server connection failed. Its outcome is unknown.", state="unknown"
                )
            return dict(self.command_reply)
        return await super().rpc(method, params)

    def secret_calls(self) -> list[tuple[str, JsonObject]]:
        """The calls that send an answer; reads are left out."""
        return [
            call
            for call in self.calls
            if call[0].startswith("agent_secrets.") and call[0] != "agent_secrets.list"
        ]


async def _open_request(app: ConversationApp, server: SecretServer, pilot: Any) -> SecretDialog:
    await server.bus.put(_SECRET_CHANGED)
    await pilot.pause()
    await pilot.press("f7")
    await pilot.pause()
    assert isinstance(app.screen, Picker)
    await pilot.press("enter")
    await pilot.pause()
    assert isinstance(app.screen, SecretDialog)
    return app.screen


@pytest.mark.asyncio
@pytest.mark.parametrize("flow", ["store", "replace", "decline", "leave"])
async def test_waiting_request_outranks_the_run_and_its_masked_form_answers_it(flow: str) -> None:
    server = SecretServer()
    server.active = True
    server.secret_requests = [
        _request(replaces=flow == "replace"),
        _request(id="sreq_other", session_id="session-two"),
    ]
    app = ConversationApp(server, agent_id="main")
    async with app.run_test(size=(120, 45)) as pilot:
        await pilot.pause()
        app.query_one("#composer", TextArea).load_text("Unsent draft")
        await server.bus.put(_SECRET_CHANGED)
        await pilot.pause()
        assert app.conversation is not None
        # The Run waits on the answer, so the request is what the status row says.
        message, level = app._status_message(app.conversation)
        assert "GITHUB" in message and level == "alert"
        dialog = await _open_request(app, server, pilot)
        username = dialog.query_one("#field-login-username", Input)
        password = dialog.query_one("#field-login-password", Input)
        assert not username.password and password.password
        # A replacing answer keeps the stored Secret's lifetime unless one is picked.
        assert dialog.query_one("#secret-lifetime", Select).value == (
            "keep" if flow == "replace" else "permanent"
        )
        if flow in {"store", "replace"}:
            username.value = "ada"
            password.value = "correct horse"
            await pilot.click("#store")
        elif flow == "decline":
            await pilot.click("#decline")
        else:
            await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, SecretDialog)
        expected = {
            "store": [
                (
                    "agent_secrets.answer",
                    {
                        "id": "sreq_one",
                        "values": {"username": "ada", "password": "correct horse"},
                        "lifetime": "permanent",
                    },
                )
            ],
            "replace": [
                (
                    "agent_secrets.answer",
                    {"id": "sreq_one", "values": {"username": "ada", "password": "correct horse"}},
                )
            ],
            "decline": [("agent_secrets.decline", {"id": "sreq_one"})],
            "leave": [],
        }[flow]
        assert server.secret_calls() == expected
        assert app.query_one("#composer", TextArea).text == "Unsent draft"
        assert bool(agent_secrets.requests_for(app, app.conversation)) is (flow == "leave")
        assert not server.sent.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["withdrawn", "refused_and_gone", "refused_and_waiting"])
async def test_request_that_goes_away_closes_its_form_and_a_refusal_keeps_typed_values(
    ending: str,
) -> None:
    server = SecretServer()
    server.secret_requests = [_request()]
    app = ConversationApp(server, agent_id="main")
    async with app.run_test(size=(120, 45)) as pilot:
        await pilot.pause()
        dialog = await _open_request(app, server, pilot)
        if ending == "withdrawn":
            # The Agent stopped waiting: its form closes; nothing was sent.
            server.secret_requests = []
            await server.bus.put(_SECRET_CHANGED)
            await pilot.pause()
            assert not isinstance(app.screen, SecretDialog)
            assert server.secret_calls() == []
            return
        message = (
            "This request is no longer waiting; the Agent stopped asking."
            if ending == "refused_and_gone"
            else "Not a usable value for password."
        )
        server.next_refusal = ClientError(message, code="invalid_request")
        server.refusal_ends_request = ending == "refused_and_gone"
        dialog.query_one("#field-login-password", Input).value = "typed value"
        await pilot.press("enter")
        await pilot.pause()
        assert len(server.secret_calls()) == 1
        if ending == "refused_and_gone":
            assert not isinstance(app.screen, SecretDialog)
            assert app.last_notice == message
        else:
            reopened = app.screen
            assert isinstance(reopened, SecretDialog)
            assert reopened.problem == message
            assert reopened.query_one("#field-login-password", Input).value == "typed value"


@pytest.mark.asyncio
@pytest.mark.parametrize("address", ["main", "worker@proj", "new"])
async def test_bare_secret_command_opens_the_give_form_instead_of_sending(address: str) -> None:
    server = SecretServer()
    if address == "new":
        app = ConversationApp(server, agent_id="main", project_id="proj")
    else:
        app = ConversationApp(server, agent_id=address, session_id="session-one")
    async with app.run_test(size=(120, 50)) as pilot:
        await pilot.pause()
        editor = app.query_one("#composer", TextArea)
        editor.load_text("/secret")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        if address == "new":
            # Giving needs the Session; nothing is sent in its place.
            assert not isinstance(app.screen, SecretDialog)
            assert editor.text == "/secret"
            assert server.secret_calls() == [] and not server.sent.is_set()
            return
        dialog = app.screen
        assert isinstance(dialog, SecretDialog)
        dialog.query_one("#secret-name", Input).value = "deploy key"
        dialog.query_one("#secret-kind", Select).value = "text"
        await pilot.pause()
        await pilot.click("#store")
        await pilot.pause()
        # A missing value is named in the form; nothing was sent.
        assert isinstance(app.screen, SecretDialog) and dialog.problem
        assert server.secret_calls() == []
        value = dialog.query_one("#field-text-value", SecretInput)
        assert value.password
        value.focus()
        value.post_message(events.Paste("-----BEGIN KEY-----\r\nline\r\n-----END KEY-----"))
        dialog.query_one("#secret-lifetime", Select).value = "until"
        await pilot.pause()
        dialog.query_one("#secret-duration", Input).value = "2h"
        dialog.query_one("#secret-description", Input).value = "Deploys the site"
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert not isinstance(app.screen, SecretDialog)
        [(method, params)] = server.secret_calls()
        expires_at = datetime.fromisoformat(params.pop("expires_at"))
        assert abs(expires_at - (datetime.now(UTC) + timedelta(hours=2))) < timedelta(minutes=1)
        agent, _, project = address.partition("@")
        assert (method, params) == (
            "agent_secrets.give",
            {
                "agent_id": agent,
                "session_id": "session-one",
                **({"project_id": project} if project else {}),
                "name": "deploy key",
                "kind": "text",
                "values": {"value": "-----BEGIN KEY-----\nline\n-----END KEY-----"},
                "lifetime": "until",
                "description": "Deploys the site",
            },
        )
        assert editor.text == ""
        assert not server.sent.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["accepted", "unknown"])
@pytest.mark.parametrize(
    "typed",
    [
        "/secret GITHUB_TOKEN --permanent ghp_value123",
        "  /SECRET\nGITHUB_TOKEN --permanent ghp_value123",
    ],
)
async def test_secret_command_is_sent_alone_and_never_kept(
    tmp_path: Path, typed: str, outcome: str
) -> None:
    server = SecretServer()
    server.fail_send = outcome == "unknown"
    store = DraftStore(server.url, directory=tmp_path)
    app = ConversationApp(server, agent_id="main", draft_store=store, restored_drafts={})
    attachment = {"attachment_id": "att-one", "filename": "notes.txt", "kind": "file"}
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        editor = app.query_one("#composer", TextArea)
        assert app.conversation is not None
        app.conversation.attachments.append(attachment)
        editor.load_text(typed)
        await pilot.pause()
        await app.submissions.persist()
        assert "ghp_value123" not in store.path.read_text(encoding="utf-8")
        await pilot.press("enter")
        await server.sent.wait()
        await pilot.pause()
        if outcome == "accepted":
            assert isinstance(app.screen, TextDialog)
            await pilot.press("escape")
            await pilot.pause()
        sends = [params for method, params in server.calls if method == "chat.stream"]
        # One space after the name, as the server recognizes the command; the
        # draft's upload waits for the next message.
        assert sends == [
            {
                "agent_id": "main",
                "session_id": "session-one",
                "reply_surface": "tui",
                "content": "/secret GITHUB_TOKEN --permanent ghp_value123",
            }
        ]
        assert app.conversation.attachments == [attachment]
        assert not app.conversation.uncertain
        assert app.submissions.history == []
        assert editor.text == ("" if outcome == "accepted" else typed)
        await pilot.press("ctrl+q")
        for worker in [worker for worker in app.workers if worker.group == "detach"]:
            await worker.wait()
    assert "ghp_value123" not in store.path.read_text(encoding="utf-8")
    assert [record["draft"] for record in store.load().values()] == [""]
