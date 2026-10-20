"""Session and live Tool actions offered from current server facts."""

from __future__ import annotations

import copy
import uuid
from typing import TYPE_CHECKING

from cli.tui.dialogs import Picker, TextDialog
from cli.tui.drafts import draft_record, holds_secret
from cli.tui.rendering import flatten, pretty
from cli.tui.state import Conversation, JsonObject
from cli.tui.transport import ClientError

if TYPE_CHECKING:
    from cli.tui.app import ConversationApp


def _current(app: ConversationApp, view: Conversation, key: tuple | None) -> bool:
    return app.conversation is view and view.key == key and view.available


def _editable(view: Conversation) -> list[JsonObject]:
    if view.facts.get("active_run"):
        return []
    return [
        message
        for item in flatten(view.items)
        if (message := item.get("message"))
        and message.get("role") == "user"
        and message.get("editable") is True
        and isinstance(message.get("content"), str)
    ]


def _tools(view: Conversation) -> list[JsonObject]:
    active = view.facts.get("active_run") or {}
    return [
        tool
        for run in view.items
        if run.get("type") == "assistant_run"
        and run.get("runId", run.get("run_id")) == active.get("run_id")
        for tool in run.get("items", [])
        if tool.get("type") == "tool_call"
        and tool.get("status") == "running"
        and tool.get("toolCallId")
    ]


def _saved_edits(app: ConversationApp, view: Conversation | None = None) -> list[JsonObject]:
    return [
        record
        for record in app.submissions.saved_drafts
        if record.get("operation") == "chat.edit"
        and (view is None or (record["agent"], record.get("session")) == (view.agent, view.session))
    ]


async def menu(app: ConversationApp) -> None:
    """Open the Actions menu; its caller owns the Textual worker and error display."""
    view = app.conversation
    key = view.key if view else None
    if view and view.session and view.available:
        if id(view) in app.submissions.pending:
            app._notice("Wait for the current submission before opening Actions.")
            return
        await app.navigation.refresh(view)
        if not _current(app, view, key):
            return
    choices: list[tuple[str, object]] = []
    if view and view.session and view.available:
        choices += [("Rename Session", "rename"), ("Fork Session", "fork")]
        if _saved_edits(app, view):
            choices.append(("Resolve the previous message edit", "edit"))
        elif _editable(view) and not view.uncertain:
            choices.append(("Edit an earlier user message", "edit"))
        active = view.facts.get("active_run") or {}
        controls = active.get("controls") or {}
        background_ids = controls.get("background_tool_call_ids", [])
        for tool in _tools(view):
            label = f"{tool.get('name', 'Tool')} · {tool['toolCallId']}"
            choices.append(
                (f"Cancel Tool · {label}", ("cancel", active["run_id"], tool["toolCallId"]))
            )
            if tool["toolCallId"] in background_ids:
                choices.append(
                    (
                        f"Background Tool · {label}",
                        ("background", active["run_id"], tool["toolCallId"]),
                    )
                )
        if not active or controls.get("compaction") == "idle":
            choices.append(
                ("Compact conversation context", ("compact", active.get("run_id"), None))
            )
    if _saved_edits(app):
        choices.append(("Recover an earlier edit…", "recover_edit"))
    if (
        any(record.get("operation") != "chat.edit" for record in app.submissions.saved_drafts)
        or app.submissions.recovered_drafts
        or any(draft_record(candidate) is not None for candidate in app.navigation.views.values())
    ):
        choices.append(("Restore a saved draft…", "restore_draft"))
    if not choices:
        app._notice("Send a first message or open a Session to use Actions.")
        return
    action = await app.push_screen_wait(Picker("Actions", choices))
    if not action or app.conversation is not view or (view and view.key != key):
        return
    if action == "restore_draft":
        await app.submissions.restore()
        return
    if action == "recover_edit":
        record = await app.push_screen_wait(
            Picker(
                "Earlier message edits",
                [
                    (f"{record['agent']} · {record.get('session')} · {record['text'][:80]}", record)
                    for record in _saved_edits(app)
                ],
            )
        )
        if record and app.conversation is view and (view is None or view.key == key):
            await _recover_edit(app, record)
        return
    if view is None or not _current(app, view, key):
        return
    if action == "edit":
        saved = _saved_edits(app, view)
        if saved:
            await _recover_edit(app, saved[-1])
        else:
            message = await app.push_screen_wait(
                Picker(
                    "Choose a message to edit",
                    [(message["content"][:160], message) for message in _editable(view)],
                )
            )
            if message and _current(app, view, key):
                await _edit(app, view, message["id"], message["content"])
        return
    if action == "rename":
        title = await app.push_screen_wait(
            TextDialog("Session title · empty clears the title", view.title)
        )
        if title is None or not _current(app, view, key):
            return
        await app.client.rpc("session.rename", {**view.params(), "title": title})
        if _current(app, view, key):
            await app.navigation.refresh(view)
        return
    if action == "fork":
        result = await app.client.rpc("session.fork", view.params())
        if not _current(app, view, key):
            return
        fork = result["session"]
        await app.navigation.open(Conversation(view.agent, session=fork["id"]))
        return
    if isinstance(action, tuple):
        await _run_action(app, view, action)


async def _run_action(app: ConversationApp, view: Conversation, action: tuple) -> None:
    key = view.key
    operation, run_id, tool_id = action
    await app.navigation.refresh(view)
    if not _current(app, view, key) or id(view) in app.submissions.pending:
        return
    active = view.facts.get("active_run") or {}
    if active.get("run_id") != run_id:
        app._notice("The Run changed. Open Actions again for current controls.")
        return
    controls = active.get("controls") or {}
    if operation == "compact" and run_id is None:
        result = await app.client.rpc(
            "chat.stream", {**view.params(), "content": "/compact", "reply_surface": "tui"}
        )
        if _current(app, view, key) and result.get("command_handled"):
            await app.submissions.command_result(view, result)
    elif operation == "compact" and controls.get("compaction") == "idle":
        await app.client.rpc(
            "chat.control_run", {**view.params(), "run_id": run_id, "action": "compact"}
        )
    elif operation == "background" and tool_id in controls.get("background_tool_call_ids", []):
        await app.client.rpc(
            "chat.control_run",
            {
                **view.params(),
                "run_id": run_id,
                "action": "background_tool",
                "tool_call_id": tool_id,
            },
        )
    elif operation == "cancel" and any(tool["toolCallId"] == tool_id for tool in _tools(view)):
        await app.client.rpc(
            "chat.cancel_tool_call",
            {"agent_id": view.agent, "run_id": run_id, "tool_call_id": tool_id},
        )
    else:
        app._notice("This action is no longer available. Open Actions again for current controls.")
        return
    if _current(app, view, key):
        await app.navigation.refresh(view)


def _retain_edit(
    app: ConversationApp,
    view: Conversation,
    request: JsonObject,
    *,
    uncertain: bool,
    previous: JsonObject | None = None,
) -> JsonObject:
    record = {
        "agent": previous["agent"] if previous else view.agent,
        "session": previous["session"] if previous else view.session,
        "project": view.project,
        "project_selected": view.project_selected,
        "text": request["content"],
        "attachments": [],
        "mentions": [],
        "draft_id": previous["draft_id"] if previous else str(uuid.uuid4()),
        "operation": "chat.edit",
        "message_id": request["message_id"],
        "uncertain": uncertain,
        "request": copy.deepcopy(request),
    }
    if previous is not None:
        previous.clear()
        previous.update(record)
    else:
        app.submissions.saved_drafts.append(record)
    app.submissions.changed(view)
    return previous if previous is not None else record


async def _edit(
    app: ConversationApp,
    view: Conversation,
    message_id: str,
    text: str,
    recovery: JsonObject | None = None,
) -> None:
    key = view.key
    origin = copy.copy(view)
    edited = await app.push_screen_wait(
        TextDialog(
            "Edit message · Save replaces its later conversation and starts a Run",
            text,
        )
    )
    if edited is None:
        return
    if holds_secret(edited):
        # A saved edit would keep the value; the composer sends the command alone.
        app._notice("A /secret command cannot replace a message. Send it from the composer.")
        return
    request = {
        **origin.params(),
        "message_id": message_id,
        "content": edited,
        "reply_surface": "tui",
    }
    if not _current(app, view, key):
        # The same Session can move while its editor is open. Keep its current
        # routing separately from the original envelope shown for inspection.
        _retain_edit(app, view, request, uncertain=False, previous=recovery)
        app._notice("The conversation changed. The edited text is saved in Actions.")
        return
    if not edited.strip():
        _retain_edit(app, view, request, uncertain=False, previous=recovery)
        app._notice("An edited message cannot be empty. The text is saved in Actions.")
        return
    try:
        await app.navigation.refresh(view)
    except ClientError:
        _retain_edit(app, view, request, uncertain=False, previous=recovery)
        raise
    if (
        not _current(app, view, key)
        or id(view) in app.submissions.pending
        or not any(message["id"] == message_id for message in _editable(view))
    ):
        _retain_edit(app, view, request, uncertain=False, previous=recovery)
        app._notice("This message is no longer editable. The edited text is saved in Actions.")
        return
    app.submissions.pending.add(id(view))
    recovery = _retain_edit(app, view, request, uncertain=True, previous=recovery)
    mutation_started = False
    try:
        await app.submissions.persist()
        if not _current(app, view, key) or not any(
            message["id"] == message_id for message in _editable(view)
        ):
            app._notice("The conversation changed. The edited text is saved in Actions.")
            return
        mutation_started = True
        await app.client.rpc("chat.edit", request)
    except ClientError as exc:
        _retain_edit(app, view, request, uncertain=exc.state == "unknown", previous=recovery)
        app._notice(
            str(exc) + " The edited text is saved in Actions; it was not retried.", error=True
        )
        return
    finally:
        if not mutation_started:
            recovery["uncertain"] = False
            app.submissions.changed(view)
        app.submissions.pending.discard(id(view))
    app.submissions.saved_drafts = [
        item for item in app.submissions.saved_drafts if item is not recovery
    ]
    app.submissions.changed(view)
    if _current(app, view, key):
        await app.navigation.refresh(view)
        app._notice("Edited message accepted.")


async def _recover_edit(app: ConversationApp, record: JsonObject) -> None:
    view = app.conversation
    key = view.key if view else None
    if record.get("uncertain"):
        choice = await app.push_screen_wait(
            Picker(
                "Previous edit has an unknown outcome. Inspect the conversation before retrying.",
                [
                    ("Review the exact previous edit", "review"),
                    ("Keep blocked and inspect the conversation", "inspect"),
                    ("The edit was accepted · clear recovery", "accepted"),
                    ("It was not accepted · restore the editing dialog", "restore"),
                ],
            )
        )
        if app.conversation is not view or (view and view.key != key):
            return
        if choice == "review":
            await app.push_screen_wait(
                TextDialog("Previous edit · not retried", pretty(record["request"]), read_only=True)
            )
            return
        if choice == "accepted":
            app.submissions.saved_drafts = [
                item for item in app.submissions.saved_drafts if item is not record
            ]
            if view:
                app.submissions.changed(view)
            app._notice("Edit recovery cleared. Later drafts are preserved.")
            return
        if choice != "restore":
            return
        record["uncertain"] = False
        if view:
            app.submissions.changed(view)
    if app.conversation is not view:
        return
    if view is None or (view.agent, view.session) != (record["agent"], record.get("session")):
        await app.navigation.open(
            Conversation(
                record["agent"],
                session=record["session"],
                project=record.get("project"),
                project_selected=record.get("project_selected", False),
            )
        )
        view = app.conversation
    if (
        view
        and view.available
        and (view.agent, view.session) == (record["agent"], record.get("session"))
    ):
        await _edit(app, view, record["message_id"], record["text"], recovery=record)
