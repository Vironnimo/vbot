"""Composer navigation, file handling and local conversation text actions."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from textual.widgets import TextArea

from cli.tui.dialogs import Picker, TextDialog
from cli.tui.rendering import file_links, flatten, item_text

if TYPE_CHECKING:
    from cli.tui.app import ConversationApp


async def complete(app: ConversationApp, *, commands: bool = False) -> None:
    view = app.conversation
    if view is None:
        return
    editor = app.query_one("#composer", TextArea)
    draft = editor.text
    row, column = editor.cursor_location
    prefix = editor.document.lines[row][:column]
    token = prefix.rsplit(maxsplit=1)[-1] if prefix.strip() else ""
    if token.startswith("@") and not commands:
        await server_file(app, token[1:], replace=(row, column - len(token), column))
        return
    response = await app.client.rpc(
        "chat.commands", {**view.params(draft=True), "reply_surface": "tui"}
    )
    if app.conversation is not view or editor.text != draft:
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
    if value and app.conversation is view and editor.text == draft:
        if token.startswith("/"):
            editor.replace(f"/{value} ", (row, column - len(token)), (row, column))
        else:
            editor.insert(f"/{value} ")
        editor.focus()


async def files(app: ConversationApp) -> None:
    view = app.conversation
    if view is None:
        return
    choices = [
        ("Attach a file from this terminal's computer", "upload"),
        ("Mention a file on the server", "mention"),
        ("Download a conversation file to this computer", "download"),
    ]
    if view.attachments or view.mentions:
        choices.append(("Remove an attachment or file mention from the draft", "remove"))
    action = await app.push_screen_wait(
        Picker("Files · local uploads and server files are separate", choices)
    )
    if action == "upload":
        path = await app.push_screen_wait(TextDialog("Local file path on this computer"))
        if path and path.strip():
            attachment = await app.client.upload(Path(path.strip().strip('"')).expanduser())
            view.attachments.append(attachment)
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
                TextDialog(
                    "New local destination path · existing files are preserved",
                    str(Path.cwd() / Path(link[0]).name),
                )
            )
            if destination and destination.strip():
                await app.client.download(link[1], Path(destination.strip()).expanduser())
                app._notice(f"Saved to {destination.strip()}")
    elif action == "remove":
        entries: list[tuple[str, Any]] = [
            (f"Local upload · {value['filename']}", ("attachment", index))
            for index, value in enumerate(view.attachments)
        ]
        entries.extend(
            (f"Server file · {value}", ("mention", index))
            for index, value in enumerate(view.mentions)
        )
        choice = await app.push_screen_wait(Picker("Remove from draft", entries))
        if choice:
            values = view.attachments if choice[0] == "attachment" else view.mentions
            values.pop(choice[1])
    app._header()


async def server_file(app, query: str = "", *, replace: tuple[int, int, int] | None = None) -> None:
    view = app.conversation
    if view is None:
        return
    editor = app.query_one("#composer", TextArea)
    draft = editor.text
    response = await app.client.rpc("files.list", view.params(draft=True))
    if app.conversation is not view or editor.text != draft:
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
        if app.conversation is not view or editor.text != draft:
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
    if isinstance(choice, str) and app.conversation is view and editor.text == draft:
        if choice not in view.mentions:
            view.mentions.append(choice)
        text = f'@"{choice}" ' if " " in choice else f"@{choice} "
        if replace:
            row, start, end = replace
            editor.replace(text, (row, start), (row, end))
        else:
            editor.insert(text)
        editor.focus()
        app._header()


async def find(app: ConversationApp) -> None:
    view = app.conversation
    if view:
        choices = [
            (item_text(item).replace("\n", " "), item)
            for item in flatten(view.items)
            if item_text(item)
        ]
        item = await app.push_screen_wait(
            Picker("Find in loaded history · Ctrl+O loads earlier pages", choices)
        )
        if item:
            await app.push_screen_wait(
                TextDialog("Conversation excerpt", item_text(item), read_only=True)
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
            path = await app.push_screen_wait(TextDialog("New local file path"))
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
    choices: list[tuple[str, Any]] = [
        ("Unsent draft · " + value["text"].replace("\n", " "), value)
        for value in reversed(app.saved_drafts)
    ]
    choices.extend((text.replace("\n", " "), text) for text in reversed(app.history))
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
                app._header()
        else:
            app.query_one("#composer", TextArea).insert(selected)


async def help(app: ConversationApp) -> None:
    await app.push_screen_wait(
        TextDialog(
            "vBot TUI · keyboard controls",
            "Ctrl+S / Alt+Enter: send; Enter: new line\n"
            "Ctrl+P / F2: Sessions; Ctrl+N / F3: new conversation\n"
            "F4: Model, thinking effort, context and Usage\n"
            "F5: Sub-Agents and background commands\n"
            "F6: Queue, edit, remove and Steer\n"
            "F7: Extension requests; F8: local uploads, server files and downloads\n"
            "F9: Commands and Skills; Ctrl+Space: completion\n"
            "Ctrl+X: stop current Run; F10: stop all background work\n"
            "Ctrl+B: previous / Parent conversation\n"
            "Ctrl+O: load older history; Ctrl+F: find in loaded history\n"
            "Ctrl+G: copy or save loaded conversation; Alt+Up: input history\n"
            "Ctrl+R: refresh/reconnect; Ctrl+Q: detach\n"
            "\n"
            "Tab moves focus. Tool and Reasoning rows expand with Enter/Space.\n"
            "Closing this TUI leaves the server's Runs working.\n"
            "@file paths are server paths. Local files are uploaded through F8.\n"
            "After a lost send response, the draft remains marked uncertain; "
            "no resend is automatic.",
            read_only=True,
        )
    )
