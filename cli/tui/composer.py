"""Composer navigation, file handling and local conversation text actions."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from textual.widgets import TextArea

from cli.tui.dialogs import PathDialog, Picker, TextDialog
from cli.tui.mentions import format_mention, open_quoted_mention
from cli.tui.rendering import file_links, flatten, item_text, one_line, tool_detail

if TYPE_CHECKING:
    from cli.tui.app import ConversationApp


async def complete(app: ConversationApp, *, commands: bool = False) -> None:
    view = app._require_conversation("Completion")
    if view is None:
        return
    key = view.key
    editor = app.query_one("#composer", TextArea)
    draft = editor.text
    row, column = editor.cursor_location
    prefix = editor.document.lines[row][:column]
    quoted = open_quoted_mention(prefix)
    if quoted is not None and not commands:
        start, query = quoted
        await server_file(app, query, replace=(row, start, column))
        return
    token = prefix.rsplit(maxsplit=1)[-1] if prefix.strip() else ""
    if token.startswith("@") and not commands:
        await server_file(app, token[1:], replace=(row, column - len(token), column))
        return
    response = await app.client.rpc(
        "chat.commands", {**view.params(draft=True), "reply_surface": "tui"}
    )
    if app.conversation is not view or view.key != key or editor.text != draft:
        return
    choices = [
        (f"/{item['name']} · {item['type']} · {item.get('description', '')}", item["name"])
        for item in response.get("items", [])
    ]
    value = await app.push_screen_wait(
        Picker(
            "Commands and Skills",
            choices,
            query=token.lstrip("/") if token.startswith("/") else "",
        )
    )
    if value and app.conversation is view and view.key == key and editor.text == draft:
        if token.startswith("/"):
            editor.replace(f"/{value} ", (row, column - len(token)), (row, column))
        else:
            editor.insert(f"/{value} ")
        editor.focus()


async def files(app: ConversationApp) -> None:
    view = app._require_conversation("Files")
    if view is None:
        return
    choices = [
        ("Attach a file from this terminal's computer", "upload"),
        ("Mention a file on the server", "mention"),
        ("Download a conversation file to this computer", "download"),
    ]
    if view.attachments:
        choices.append(("Remove a local attachment from the draft", "remove"))
    action = await app.push_screen_wait(
        Picker("Files · local uploads and server files are separate", choices)
    )
    if app.conversation is not view:
        return
    if action == "upload":
        path = await app.push_screen_wait(
            PathDialog("Local file on this computer", must_exist=True)
        )
        if path and app.conversation is view:
            attachment = await app.client.upload(Path(path))
            view.attachments.append(attachment)
            app._view_changed(view)
            app._notice("Local file uploaded and attached to the draft.")
    elif action == "mention":
        await server_file(
            app,
        )
    elif action == "download":
        links = file_links(view.items)
        if not links:
            app._notice("No downloadable files in the loaded conversation history.")
            return
        link = await app.push_screen_wait(
            Picker("Choose a file to download", [(label, (label, url)) for label, url in links])
        )
        if link:
            destination = await app.push_screen_wait(
                PathDialog(
                    "New local destination path · existing files are preserved",
                    str(Path.cwd() / Path(link[0]).name),
                    must_exist=False,
                )
            )
            if destination and destination.strip():
                await app.client.download(link[1], Path(destination.strip()).expanduser())
                app._notice(f"Saved to {destination.strip()}")
    elif action == "remove":
        entries: list[tuple[str, Any]] = [
            (f"Local upload · {value['filename']}", value["attachment_id"])
            for value in view.attachments
        ]
        choice = await app.push_screen_wait(Picker("Remove from draft", entries))
        if choice and app.conversation is view:
            view.attachments = [
                value for value in view.attachments if value["attachment_id"] != choice
            ]
            app._view_changed(view)
    app._header()


async def server_file(app, query: str = "", *, replace: tuple[int, int, int] | None = None) -> None:
    view = app.conversation
    if view is None:
        return
    key = view.key
    editor = app.query_one("#composer", TextArea)
    draft = editor.text
    response = await app.client.rpc("files.list", view.params(draft=True))
    if app.conversation is not view or view.key != key or editor.text != draft:
        return
    choices: list[tuple[str, Any]] = [(name, name) for name in response.get("files", [])]
    choices.append(("Browse directories (including ignored files)…", ("browse", "")))
    choice = await app.push_screen_wait(
        Picker(f"Server files · {response.get('root', '')}", choices, query=query)
    )
    while isinstance(choice, tuple):
        directory = choice[1]
        listing = await app.client.rpc(
            "files.list", {**view.params(draft=True), "directory": directory}
        )
        if app.conversation is not view or view.key != key or editor.text != draft:
            return
        choices = []
        if directory:
            choices.append(("../", ("browse", directory.rpartition("/")[0])))
        for item in listing.get("entries", []):
            path = f"{directory}/{item['name']}".lstrip("/")
            is_directory = item["kind"] == "directory"
            choices.append(
                (
                    item["name"]
                    + ("/" if is_directory else "")
                    + (" (ignored)" if item.get("ignored") else ""),
                    ("browse", path) if is_directory else path,
                )
            )
        choice = await app.push_screen_wait(
            Picker(f"Server · {listing.get('root', '')}/{directory}", choices)
        )
    if (
        isinstance(choice, str)
        and app.conversation is view
        and view.key == key
        and editor.text == draft
    ):
        if choice not in view.mentions:
            view.mentions.append(choice)
        text = format_mention(choice) + " "
        if replace:
            row, start, end = replace
            editor.replace(text, (row, start), (row, end))
        else:
            editor.insert(text)
        editor.focus()
        app._view_changed(view)
        app._header()


async def find(app: ConversationApp) -> None:
    view = app.conversation
    if view:

        def searchable(item: dict[str, Any]) -> str:
            return tool_detail(item) if item.get("type") == "tool_call" else item_text(item)

        choices = [
            (one_line(searchable(item)), item) for item in flatten(view.items) if searchable(item)
        ]
        item = await app.push_screen_wait(
            Picker(
                "Find in loaded history · Ctrl+O loads earlier pages",
                choices,
                search_text=searchable,
            )
        )
        if item:
            app.transcript.reveal(str(item["id"]))
            await app.push_screen_wait(
                TextDialog("Conversation excerpt", searchable(item), read_only=True)
            )


async def copy(app: ConversationApp) -> None:
    view = app.conversation
    if view:
        text = "\n\n".join(
            item_text(item)
            for item in flatten(view.items)
            if item.get("type") != "assistant_run" and item_text(item)
        )
        choice = await app.push_screen_wait(
            Picker(
                "Conversation text",
                [
                    (
                        "Copy loaded conversation to clipboard (terminal support required)",
                        "copy",
                    ),
                    ("Save loaded conversation as a local text file", "save"),
                ],
            )
        )
        if choice == "copy":
            app.copy_to_clipboard(text)
            app._notice("Clipboard copy requested from your terminal.")
        elif choice == "save":
            path = await app.push_screen_wait(PathDialog("New local file path", must_exist=False))
            if path and path.strip():
                try:
                    with (
                        Path(path.strip())
                        .expanduser()
                        .open("x", encoding="utf-8", newline="\n") as target
                    ):
                        target.write(text)
                    app._notice(f"Saved to {path.strip()}")
                except OSError as exc:
                    app._notice(str(exc), error=True)


async def input_history(app: ConversationApp) -> None:
    def where(value: dict[str, Any]) -> str:
        """Name the conversation a draft belongs to; it need not be the current one."""
        session = value.get("session") or "new conversation"
        return f"{value.get('agent', '')} · {session}"

    choices: list[tuple[str, Any]] = [
        (f"Unsent draft · {where(value)} · {one_line(value['text'], 60)}", value)
        for value in reversed(app.saved_drafts)
        if value.get("operation") != "chat.edit"
    ]
    choices.extend((one_line(text), text) for text in reversed(app.history))
    selected = await app.push_screen_wait(
        Picker(
            "Input history",
            choices,
        )
    )
    if selected:
        if isinstance(selected, dict):
            app.query_one("#composer", TextArea).insert(selected["text"])
            if app.conversation:
                existing = {item["attachment_id"] for item in app.conversation.attachments}
                app.conversation.attachments.extend(
                    item
                    for item in selected["attachments"]
                    if item["attachment_id"] not in existing
                )
                app.conversation.mentions.extend(
                    path for path in selected["mentions"] if path not in app.conversation.mentions
                )
                app._view_changed(app.conversation)
                app._header()
        else:
            app.query_one("#composer", TextArea).insert(selected)


async def help(app: ConversationApp) -> None:
    await app.push_screen_wait(
        TextDialog(
            "vBot TUI · keyboard controls",
            "Enter: send · Shift+Enter or Ctrl+Enter: new line · Ctrl+S: send\n"
            "Typing / offers Commands and Skills, /model the chat Models and @ the\n"
            "server files: ↑↓ choose, Enter takes it, Esc closes the list.\n"
            "F2: Sessions · F3: new conversation · F4: Model and effort\n"
            "F8: local uploads, server files and downloads\n"
            "Ctrl+X: stop the current Run · F10: stop all background work\n"
            "Ctrl+K: command palette, every action by name\n"
            "F1: this list\n"
            "\n"
            "F5: activity, Sub-Agents and background commands\n"
            "F6: Queue, edit, remove and Steer · F7: Extension requests\n"
            "F9: Commands and Skills · Ctrl+Space: completion\n"
            "F11: Session actions, Tools and saved drafts · F12: latest output\n"
            "Ctrl+B: parent conversation · Ctrl+O: older history\n"
            "Ctrl+F: find in loaded history · Ctrl+G: copy or save the conversation\n"
            "Alt+Up: input history · Ctrl+R: reconnect · Ctrl+Q: detach\n"
            "\n"
            "The row under the composer names the Project this conversation works in\n"
            "and its directory on the server, then the Model, its thinking effort and\n"
            "the context load. A row above the composer appears while a Run works or\n"
            "something needs attention.\n"
            "Tab moves into the conversation: ↑↓ step through Tools and Reasoning,\n"
            "Enter or Space opens and closes one, Esc returns to the composer.\n"
            "A click opens and closes them too. Dragging, a double click (word) or\n"
            "a triple click (paragraph) selects text and copies it.\n"
            "@file paths are server paths. Local files are uploaded through F8.\n"
            "Drafts are saved locally per server. After a lost send response the draft\n"
            "stays marked uncertain, and no resend is automatic.",
            read_only=True,
        )
    )
