"""Composer navigation, file handling and local conversation text actions."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from textual.widgets import TextArea

from cli.tui.dialogs import KeysDialog, PathDialog, Picker, TextDialog
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
    # A Skill a command shadows runs only as `$name`, never as the `/name` inserted here.
    choices = [
        (f"/{item['name']} · {item['type']} · {item.get('description', '')}", item["name"])
        for item in response.get("items", [])
        if item.get("slash") is not False
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
            app.submissions.changed(view)
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
            app.submissions.changed(view)
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
        app.submissions.changed(view)
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
        for value in reversed(app.submissions.saved_drafts)
        if value.get("operation") != "chat.edit"
    ]
    choices.extend((one_line(text), text) for text in reversed(app.submissions.history))
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
                app.submissions.changed(app.conversation)
                app._header()
        else:
            app.query_one("#composer", TextArea).insert(selected)


async def help(app: ConversationApp) -> None:
    await app.push_screen_wait(
        KeysDialog(
            "Keyboard and mouse",
            [
                (
                    "Write",
                    [
                        ("Enter", "send the draft"),
                        ("Shift+Enter", "new line (Ctrl+Enter works too)"),
                        ("/", "Commands and Skills; /model offers the chat Models"),
                        ("@", "mention a server file"),
                        ("↑↓ Enter Esc", "choose, take or close a suggestion"),
                        ("Ctrl+Space", "complete at the cursor"),
                        ("Alt+Up", "insert earlier input or a saved draft"),
                        ("F8", "attach local files, mention or download server files"),
                    ],
                ),
                (
                    "Run",
                    [
                        ("F4", "Model and thinking effort"),
                        ("F5", "activity, Sub-Agents and background commands"),
                        ("F6", "Queue: edit, remove and Steer"),
                        ("F7", "Secret and Extension requests"),
                        ("Ctrl+X", "stop the current Run"),
                        ("F10", "stop all background work"),
                    ],
                ),
                (
                    "Move around",
                    [
                        ("Ctrl+K", "command palette, every action by name"),
                        ("F2", "Sessions"),
                        ("F3", "new conversation"),
                        ("Ctrl+B", "parent conversation"),
                        ("F11", "Session actions, Tools and saved drafts"),
                        ("F12", "latest output"),
                        ("Ctrl+O", "older history"),
                        ("Ctrl+F", "find in the loaded history"),
                    ],
                ),
                (
                    "Read the conversation",
                    [
                        ("Tab", "step into it; ↑↓ move through Tools and Reasoning"),
                        ("Enter, Space", "open or close a step; Esc returns to the composer"),
                        ("Click", "open or close a step"),
                        ("Drag", "select text and copy it"),
                        ("Double click", "select and copy a word"),
                        ("Triple click", "select and copy a paragraph"),
                        ("Ctrl+G", "copy or save the whole conversation"),
                    ],
                ),
                (
                    "Connection",
                    [
                        ("Ctrl+R", "reconnect and reload"),
                        ("Ctrl+Q", "detach"),
                        ("F1", "this list"),
                    ],
                ),
            ],
            [
                "The row under the composer names the Project this conversation works in "
                "and its directory on the server, then the Model, its thinking effort and "
                "the context load. A row above the composer appears while a Run works or "
                "something needs attention.",
                "@file paths are server paths; local files are uploaded through F8. Drafts "
                "are saved locally per server. After a lost send response the draft stays "
                "marked uncertain, and nothing is resent on its own.",
                "/secret alone opens a masked form that gives the Agent a password or key. "
                "A /secret command with its value is sent as it is and never kept: no saved "
                "draft, no input history.",
            ],
        )
    )
