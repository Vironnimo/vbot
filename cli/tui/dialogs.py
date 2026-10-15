"""Keyboard-first search, text and Extension request dialogs."""

from __future__ import annotations

import json
from typing import Any, override

from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Checkbox,
    Input,
    Label,
    Markdown,
    OptionList,
    Select,
    SelectionList,
    Static,
    TextArea,
)
from textual.widgets.option_list import Option

from cli.tui.rendering import terminal_text

type JsonObject = dict[str, Any]


class Picker(ModalScreen[Any]):
    """Filter user-visible labels, carrying exact server identities as values."""

    BINDINGS = [("escape", "dismiss(None)", "Close")]
    DEFAULT_CSS = """
    Picker { align: center middle; }
    Picker > Vertical {
        width: 86%; max-width: 120; height: 80%; border: round $accent;
        background: $surface; padding: 1 2;
    }
    Picker Label { height: auto; margin-bottom: 1; }
    Picker Input { margin-bottom: 1; }
    Picker OptionList { height: 1fr; }
    """

    def __init__(self, title: str, choices: list[tuple[str, Any]], *, query: str = "") -> None:
        super().__init__()
        self.heading = terminal_text(title)
        self.choices = [(terminal_text(label), value) for label, value in choices]
        self.filtered = self.choices
        self.initial_query = query

    @override
    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(self.heading, markup=False)
            yield Input(value=self.initial_query, placeholder="Search…", id="search")
            yield OptionList(id="choices", markup=False)
            yield Static("↑ ↓ select · Enter open · Esc return", markup=False)

    def on_mount(self) -> None:
        self.filter(self.initial_query)
        self.query_one(Input).focus()

    def filter(self, query: str) -> None:
        words = query.casefold().split()
        self.filtered = [
            (label, value)
            for label, value in self.choices
            if all(word in label.casefold() for word in words)
        ]
        options = self.query_one(OptionList)
        options.clear_options()
        options.add_options(
            [Option(label, id=str(index)) for index, (label, _) in enumerate(self.filtered)]
        )
        if self.filtered:
            options.highlighted = 0

    @on(Input.Changed)
    def search_changed(self, event: Input.Changed) -> None:
        self.filter(event.value)

    @on(Input.Submitted)
    def search_submitted(self) -> None:
        self.choose()

    @on(OptionList.OptionSelected)
    def option_selected(self) -> None:
        self.choose()

    def choose(self) -> None:
        index = self.query_one(OptionList).highlighted
        if index is not None and index < len(self.filtered):
            self.dismiss(self.filtered[index][1])

    async def on_key(self, event: Any) -> None:
        if event.key in {"up", "down"} and isinstance(self.focused, Input):
            options = self.query_one(OptionList)
            if event.key == "up":
                options.action_cursor_up()
            else:
                options.action_cursor_down()
            event.stop()
            event.prevent_default()


class TextDialog(ModalScreen[str | None]):
    BINDINGS = [("escape", "dismiss(None)", "Close"), ("ctrl+s", "save", "Save")]
    DEFAULT_CSS = """
    TextDialog { align: center middle; }
    TextDialog > Vertical {
        width: 86%; max-width: 120; height: 70%; border: round $accent;
        background: $surface; padding: 1 2;
    }
    TextDialog Label { height: auto; margin-bottom: 1; }
    TextDialog TextArea { height: 1fr; }
    TextDialog Horizontal { height: 3; margin-top: 1; }
    """

    def __init__(self, title: str, text: str = "", *, read_only: bool = False) -> None:
        super().__init__()
        self.heading = terminal_text(title)
        self.initial = terminal_text(text) if read_only else text
        self.read_only = read_only

    @override
    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(self.heading, markup=False)
            yield TextArea(self.initial, read_only=self.read_only, id="text")
            with Horizontal():
                if not self.read_only:
                    yield Button("Save", id="save", variant="primary")
                yield Button("Close", id="close")

    def on_mount(self) -> None:
        self.query_one(TextArea).focus()

    @on(Button.Pressed)
    def button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "save":
            self.action_save()
        else:
            self.dismiss(None)

    def action_save(self) -> None:
        if not self.read_only:
            self.dismiss(self.query_one(TextArea).text)


class ExtensionDialog(ModalScreen[JsonObject | None]):
    """Render the Extension-owned form schema, preserving typed values."""

    BINDINGS = [("escape", "dismiss(None)", "Return without answering")]
    DEFAULT_CSS = """
    ExtensionDialog { align: center middle; }
    ExtensionDialog > Vertical {
        width: 88%; max-width: 120; height: 88%; border: round $warning;
        background: $surface; padding: 1 2;
    }
    ExtensionDialog VerticalScroll { height: 1fr; }
    ExtensionDialog Label { height: auto; margin-top: 1; }
    ExtensionDialog Horizontal { height: 3; }
    ExtensionDialog #problem { color: $error; height: auto; }
    ExtensionDialog TextArea { min-height: 5; max-height: 12; }
    """

    def __init__(self, request: JsonObject) -> None:
        super().__init__()
        self.request = request
        self.schema = request.get("payload", {}).get("requestedSchema", {})
        self.fields = list(self.schema.get("properties", {}).items())
        self.enums: dict[str, list[Any]] = {}

    @override
    def compose(self) -> ComposeResult:
        payload = self.request.get("payload") or {}
        with Vertical():
            yield Label(
                f"{self.request.get('extension', '')} · {self.request.get('kind', 'Request')}",
                markup=False,
            )
            with VerticalScroll():
                yield Markdown(
                    terminal_text(str(payload.get("message", payload.get("title", "")))),
                    open_links=False,
                )
                url = payload.get("url", payload.get("authorization_url"))
                if url:
                    yield TextArea(str(url), read_only=True, classes="request-url")
                    yield Static(
                        "Open this address in a browser on your own computer.", markup=False
                    )
                if self.request.get("kind") == "oauth":
                    yield Label(
                        "Paste the final redirect URL "
                        "(leave empty if the browser completed sign-in)",
                        markup=False,
                    )
                    yield Input(id="redirect")
                elif payload.get("mode") != "url":
                    required = self.schema.get("required", [])
                    for index, (name, schema) in enumerate(self.fields):
                        key = f"field-{index}"
                        label = str(schema.get("title", name)) + (" *" if name in required else "")
                        yield Label(label, markup=False)
                        if schema.get("description"):
                            yield Static(schema["description"], markup=False)
                        enums = schema.get("enum")
                        constants = schema.get("oneOf", schema.get("anyOf"))
                        if (
                            enums is None
                            and isinstance(constants, list)
                            and all("const" in value for value in constants)
                        ):
                            enums = [value["const"] for value in constants]
                        if isinstance(enums, list):
                            self.enums[key] = enums
                            default = schema.get("default")
                            yield Select(
                                [(str(value), str(i)) for i, value in enumerate(enums)],
                                value=str(enums.index(default))
                                if default in enums
                                else Select.NULL,
                                id=key,
                            )
                        elif schema.get("type") == "boolean":
                            yield Checkbox(label, value=schema.get("default") is True, id=key)
                        elif schema.get("type") == "array" and isinstance(
                            (schema.get("items") or {}).get("enum"), list
                        ):
                            choices = schema["items"]["enum"]
                            self.enums[key] = choices
                            defaults = schema.get("default", [])
                            yield SelectionList(
                                *[
                                    (str(value), i, value in defaults)
                                    for i, value in enumerate(choices)
                                ],
                                id=key,
                            )
                        elif (
                            schema.get("type") in {"array", "object"} or schema.get("type") is None
                        ):
                            yield TextArea(
                                json.dumps(schema["default"], ensure_ascii=False)
                                if "default" in schema
                                else "",
                                id=key,
                            )
                        else:
                            yield Input(
                                value=str(schema.get("default", "")),
                                placeholder="RFC 3339 with UTC offset"
                                if schema.get("format") == "date-time"
                                else "",
                                id=key,
                            )
            yield Static("", id="problem", markup=False)
            with Horizontal():
                yield Button("Accept", id="accept", variant="primary")
                yield Button("Decline", id="decline")
                yield Button("Cancel request", id="cancel", variant="error")

    @on(Button.Pressed)
    def respond(self, event: Button.Pressed) -> None:
        action = event.button.id
        if action not in {"accept", "decline", "cancel"}:
            return
        response: JsonObject = {"action": action}
        if self.request.get("kind") == "oauth":
            response["redirect_url"] = self.query_one("#redirect", Input).value
        elif action == "accept" and self.request.get("payload", {}).get("mode") != "url":
            content: JsonObject = {}
            try:
                for index, (name, schema) in enumerate(self.fields):
                    key = f"field-{index}"
                    widget = self.query_one(f"#{key}")
                    if isinstance(widget, Checkbox):
                        value: Any = widget.value
                    elif isinstance(widget, SelectionList):
                        value = [self.enums[key][index] for index in widget.selected]
                    elif isinstance(widget, Select):
                        if widget.value is Select.NULL:
                            value = ""
                        else:
                            value = self.enums[key][int(str(widget.value))]
                    elif isinstance(widget, TextArea):
                        value = json.loads(widget.text) if widget.text.strip() else ""
                    else:
                        assert isinstance(widget, Input)
                        value = widget.value
                        if value and schema.get("type") in {"number", "integer"}:
                            value = int(value) if schema["type"] == "integer" else float(value)
                    if value == "":
                        if name in self.schema.get("required", []):
                            raise ValueError(f"{schema.get('title', name)} is required.")
                        continue
                    content[name] = value
            except (ValueError, TypeError) as exc:
                self.query_one("#problem", Static).update(str(exc))
                return
            response["content"] = content
        self.dismiss(response)
