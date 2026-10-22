"""Keyboard-first search, text, Extension request and Secret dialogs.

Every dialog is one quiet box: its title sits in the frame, the keys that act
on it in the frame's lower edge, and the fields inside carry no frames of
their own, so the eye lands on the content rather than on nested borders.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, override

from textual import events, on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.content import Content
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Checkbox,
    DirectoryTree,
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
from core.utils.file_status import exists_strict, is_dir_strict, is_file_strict

type JsonObject = dict[str, Any]

# The frame every dialog shares; a dialog adds only its size and contents.
_BOX = """
    border: round $border-blurred; background: $surface; padding: 1 2 0 2;
    border-title-color: $accent; border-title-style: bold;
    border-subtitle-color: $text-muted;
"""
_FIELDS = """
    Input { background: $panel; padding: 0 1; margin-bottom: 1; }
    Horizontal { height: 1; margin: 1 0; }
    Button { margin-right: 2; min-width: 12; }
"""


class Picker(ModalScreen[Any]):
    """Filter user-visible labels, carrying exact server identities as values."""

    # The box fits its content: three choices do not need a full screen.
    BINDINGS = [("escape", "dismiss(None)", "Close")]
    DEFAULT_CSS = f"""
    Picker {{ align: center middle; }}
    Picker > Vertical {{ width: 86%; max-width: 110; height: auto; max-height: 90%; {_BOX} }}
    Picker {{ {_FIELDS} }}
    Picker OptionList {{
        height: auto; max-height: 16; border: none; padding: 0; margin-bottom: 1;
        background: $surface;
    }}
    """

    def __init__(
        self,
        title: str,
        choices: list[tuple[str, Any]],
        *,
        query: str = "",
        search_text: Callable[[Any], str] | None = None,
    ) -> None:
        super().__init__()
        self.heading = terminal_text(title)
        self.choices = [(terminal_text(label), value) for label, value in choices]
        self.filtered = self.choices
        self.initial_query = query
        self.search_text = search_text

    @override
    def compose(self) -> ComposeResult:
        with Vertical() as box:
            box.border_title = self.heading
            box.border_subtitle = "↑↓ choose · Enter open · Esc close"
            yield Input(
                value=self.initial_query, placeholder="Type to filter", id="search", compact=True
            )
            yield OptionList(id="choices", markup=False)

    def on_mount(self) -> None:
        self.filter(self.initial_query)
        self.query_one(Input).focus()

    def filter(self, query: str) -> None:
        words = query.casefold().split()
        self.filtered = [
            (label, value)
            for label, value in self.choices
            if all(
                word in (self.search_text(value) if self.search_text else label).casefold()
                for word in words
            )
        ]
        options = self.query_one(OptionList)
        options.clear_options()
        options.add_options(
            [Option(label, id=str(index)) for index, (label, _) in enumerate(self.filtered)]
            or [Option("Nothing matches", disabled=True)]
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
    DEFAULT_CSS = f"""
    TextDialog {{ align: center middle; }}
    TextDialog > Vertical {{ width: 86%; max-width: 120; height: 75%; {_BOX} }}
    TextDialog {{ {_FIELDS} }}
    TextDialog TextArea {{ height: 1fr; border: none; padding: 0 1; background: $panel; }}
    TextDialog TextArea.-read-only {{ background: $surface; padding: 0; margin-bottom: 1; }}
    """

    def __init__(self, title: str, text: str = "", *, read_only: bool = False) -> None:
        super().__init__()
        self.heading = terminal_text(title)
        self.initial = terminal_text(text) if read_only else text
        self.read_only = read_only

    @override
    def compose(self) -> ComposeResult:
        with Vertical(id="text-dialog-body") as box:
            box.border_title = self.heading
            box.border_subtitle = "Esc close" if self.read_only else "Ctrl+S save · Esc close"
            yield TextArea(self.initial, read_only=self.read_only, id="text")
            if not self.read_only:
                with Horizontal():
                    yield Button("Save", id="save", variant="primary", compact=True)
                    yield Button("Close", id="close", compact=True)

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


class KeysDialog(ModalScreen[None]):
    """List keys beside what they do, grouped by task, with notes underneath."""

    BINDINGS = [("escape", "dismiss(None)", "Close"), ("f1", "dismiss(None)", "Close")]
    DEFAULT_CSS = f"""
    KeysDialog {{ align: center middle; }}
    KeysDialog > VerticalScroll {{
        width: 86%; max-width: 96; height: auto; max-height: 90%; {_BOX}
    }}
    KeysDialog .section {{ color: $text-muted; text-style: bold; }}
    KeysDialog .keys {{ margin-bottom: 1; }}
    KeysDialog .note {{ color: $text-muted; margin-bottom: 1; }}
    """
    KEY_WIDTH = 14

    def __init__(
        self, title: str, sections: list[tuple[str, list[tuple[str, str]]]], notes: list[str]
    ) -> None:
        super().__init__()
        self.heading = title
        self.sections = sections
        self.notes = notes

    @override
    def compose(self) -> ComposeResult:
        with VerticalScroll() as box:
            box.border_title = self.heading
            box.border_subtitle = "↑↓ scroll · Esc close"
            for name, keys in self.sections:
                yield Static(name.upper(), classes="section", markup=False)
                yield Static(
                    Content("\n").join(
                        Content.assemble((key.ljust(self.KEY_WIDTH), "bold $accent"), action)
                        for key, action in keys
                    ),
                    classes="keys",
                )
            for note in self.notes:
                yield Static(note, classes="note", markup=False)

    def on_mount(self) -> None:
        self.query_one(VerticalScroll).focus()


class PathDialog(ModalScreen[str | None]):
    """Browse local paths or type one, with distinct open/save validation."""

    BINDINGS = [("escape", "dismiss(None)", "Close"), ("ctrl+s", "choose", "Choose")]
    DEFAULT_CSS = f"""
    PathDialog {{ align: center middle; }}
    PathDialog > Vertical {{ width: 94%; max-width: 140; height: 80%; {_BOX} }}
    PathDialog {{ {_FIELDS} }}
    PathDialog DirectoryTree {{ height: 1fr; background: $surface; padding: 0; }}
    PathDialog #hint {{ color: $text-muted; height: auto; margin-top: 1; }}
    PathDialog #problem {{ color: $error; height: auto; }}
    """

    def __init__(self, title: str, path: str = "", *, must_exist: bool) -> None:
        super().__init__()
        self.heading = terminal_text(title)
        self.initial = path
        self.must_exist = must_exist
        self.problem = ""
        try:
            initial_path = Path(path).expanduser() if path else Path.cwd()
            if is_dir_strict(initial_path):
                root = initial_path
            elif is_dir_strict(initial_path.parent):
                root = initial_path.parent
            else:
                root = Path.cwd()
        except (OSError, RuntimeError) as exc:
            # A path that cannot be inspected is different from a missing one.
            # Keep the chooser usable from the current directory and preserve
            # that distinction for the operator.
            root = Path.cwd()
            target = path or "the initial directory"
            self.problem = f"Cannot inspect {target}: {getattr(exc, 'strerror', None) or exc}"
        self.root = root

    @override
    def compose(self) -> ComposeResult:
        with Vertical(id="path-dialog-body") as box:
            box.border_title = self.heading
            box.border_subtitle = "Tab browse · Enter or Ctrl+S use path · Esc close"
            yield Input(value=self.initial, placeholder="Local path", id="path", compact=True)
            yield DirectoryTree(self.root, id="path-tree")
            yield Static(
                "Use arrows to browse; select or type an existing file."
                if self.must_exist
                else (
                    "Use arrows to browse; select or type a new path. "
                    "Existing files are never replaced."
                ),
                id="hint",
                markup=False,
            )
            yield Static(self.problem, id="problem", markup=False)
            with Horizontal():
                yield Button("Use path", id="choose", variant="primary", compact=True)
                yield Button("Close", id="close", compact=True)

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    @on(Input.Submitted)
    def path_submitted(self) -> None:
        # The hint tells the operator to type a path, so Enter accepts it.
        self.action_choose()

    @on(DirectoryTree.FileSelected)
    def file_selected(self, event: DirectoryTree.FileSelected) -> None:
        # A destination that must not exist yet can never be an existing file.
        if not self.must_exist:
            return
        self.query_one("#path", Input).value = str(event.path)

    @on(DirectoryTree.DirectorySelected)
    def directory_selected(self, event: DirectoryTree.DirectorySelected) -> None:
        # Opening a file: a folder is a step of browsing, not the answer. Saving a
        # new file: the folder is the base, kept with its separator so the name the
        # operator types next lands inside it.
        if self.must_exist:
            return
        self.query_one("#path", Input).value = str(event.path) + os.sep

    @on(Button.Pressed)
    def button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "choose":
            self.action_choose()
        else:
            self.dismiss(None)

    def action_choose(self) -> None:
        raw = self.query_one("#path", Input).value.strip()
        try:
            if not raw:
                raise ValueError("Enter a local path.")
            if "\x00" in raw:
                raise ValueError("The path contains an invalid null character.")
            path = Path(raw).expanduser()
            if self.must_exist:
                if not is_file_strict(path):
                    raise ValueError("Choose an existing local file.")
            else:
                if is_dir_strict(path):
                    raise ValueError(
                        "This is a folder; add a file name, for example "
                        f"{str(path).rstrip(os.sep)}{os.sep}name.txt"
                    )
                if exists_strict(path, follow_symlinks=False):
                    raise ValueError("That path already exists; choose a new destination.")
                if not is_dir_strict(path.parent):
                    raise ValueError("The destination directory must already exist.")
        except (OSError, RuntimeError, ValueError) as exc:
            self.problem = (
                f"Cannot check this path: {exc.strerror or exc}"
                if isinstance(exc, OSError)
                else str(exc)
            )
            self.query_one("#problem", Static).update(self.problem)
            return
        self.dismiss(str(path))


class ExtensionDialog(ModalScreen[JsonObject | None]):
    """Render the Extension-owned form schema, preserving typed values."""

    BINDINGS = [("escape", "dismiss(None)", "Return without answering")]
    DEFAULT_CSS = f"""
    ExtensionDialog {{ align: center middle; }}
    ExtensionDialog > Vertical {{
        width: 88%; max-width: 120; height: 85%; {_BOX}
        border: round $warning; border-title-color: $warning;
    }}
    ExtensionDialog {{ {_FIELDS} }}
    ExtensionDialog VerticalScroll {{ height: 1fr; }}
    ExtensionDialog Label {{ height: auto; margin-top: 1; text-style: bold; }}
    ExtensionDialog #problem {{ color: $error; height: auto; }}
    ExtensionDialog TextArea {{ min-height: 5; max-height: 12; }}
    """

    def __init__(
        self,
        request: JsonObject,
        *,
        values: JsonObject | None = None,
        problem: str = "",
        outcome_unknown: bool = False,
    ) -> None:
        super().__init__()
        self.request = request
        self.schema = request.get("payload", {}).get("requestedSchema", {})
        self.fields = list(self.schema.get("properties", {}).items())
        self.enums: dict[str, list[Any]] = {}
        self.form_values = dict(values or {})
        self.problem = problem
        self.outcome_unknown = outcome_unknown

    @override
    def compose(self) -> ComposeResult:
        payload = self.request.get("payload") or {}
        with Vertical(id="extension-dialog-body"):
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
                    yield Input(value=str(self.form_values.get("redirect_url", "")), id="redirect")
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
                            default = self.form_values.get(name, schema.get("default"))
                            yield Select(
                                [(str(value), str(i)) for i, value in enumerate(enums)],
                                value=str(enums.index(default))
                                if default in enums
                                else Select.NULL,
                                id=key,
                            )
                        elif schema.get("type") == "boolean":
                            yield Checkbox(
                                label,
                                value=self.form_values.get(name, schema.get("default") is True)
                                is True,
                                id=key,
                            )
                        elif schema.get("type") == "array" and isinstance(
                            (schema.get("items") or {}).get("enum"), list
                        ):
                            choices = schema["items"]["enum"]
                            self.enums[key] = choices
                            defaults = self.form_values.get(name, schema.get("default", []))
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
                            initial = self.form_values.get(name, schema.get("default", ""))
                            yield TextArea(
                                json.dumps(initial, ensure_ascii=False) if initial != "" else "",
                                id=key,
                            )
                        else:
                            initial = self.form_values.get(name, schema.get("default", ""))
                            yield Input(
                                value=str(initial),
                                placeholder="RFC 3339 with UTC offset"
                                if schema.get("format") == "date-time"
                                else "",
                                id=key,
                            )
            yield Static(self.problem, id="problem", markup=False)
            with Horizontal():
                yield Button(
                    "Respond again" if self.outcome_unknown else "Accept",
                    id="accept",
                    variant="primary",
                    compact=True,
                )
                yield Button("Decline", id="decline", compact=True)
                yield Button("Cancel request", id="cancel", variant="error", compact=True)

    def on_mount(self) -> None:
        box = self.query_one("#extension-dialog-body", Vertical)
        box.border_title = (
            f"{self.request.get('extension', '')} · {self.request.get('kind', 'Request')}"
        )
        box.border_subtitle = "Esc return without answering"

    @on(Button.Pressed)
    def respond(self, event: Button.Pressed) -> None:
        action = event.button.id
        if action not in {"accept", "decline", "cancel"}:
            return
        response: JsonObject = {"action": action}
        if self.request.get("kind") == "oauth":
            redirect = self.query_one("#redirect", Input).value
            self.form_values["redirect_url"] = redirect
            response["redirect_url"] = redirect
        elif action == "accept" and self.request.get("payload", {}).get("mode") != "url":
            content: JsonObject = {}
            form_values: JsonObject = {}
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
                    form_values[name] = value
                    if value == "":
                        if name in self.schema.get("required", []):
                            raise ValueError(f"{schema.get('title', name)} is required.")
                        continue
                    content[name] = value
            except (ValueError, TypeError) as exc:
                self.form_values.update(form_values)
                self.query_one("#problem", Static).update(str(exc))
                return
            self.form_values = form_values
            response["content"] = content
        self.dismiss(response)


#: The fields each Secret kind starts with, as (name, concealed, optional). They
#: mirror the server's kinds (`core/secrets/_agent_secret_model.py`); an Agent's
#: request names its own fields.
SECRET_KIND_FIELDS: dict[str, tuple[tuple[str, bool, bool], ...]] = {
    "token": (("value", True, False),),
    "login": (
        ("username", False, True),
        ("password", True, False),
        ("url", False, True),
        ("totp", True, True),
    ),
    "text": (("value", True, False),),
}
_SECRET_KINDS = (
    ("Token or API key", "token"),
    ("Login: username and password", "login"),
    ("Text of several lines", "text"),
)
_SECRET_LIFETIMES = (
    ("For this Session", "session"),
    ("Permanently", "permanent"),
    ("For a duration", "until"),
    ("For one use", "once"),
)
_SECRET_FIELD_LABELS = {
    "value": "Value",
    "username": "Username",
    "password": "Password",
    "url": "Address (URL)",
    "totp": "TOTP seed for one-time codes",
}
# The durations `/secret --for` takes.
_DURATION = re.compile(r"(\d+)\s*([mhdw])", re.IGNORECASE)
_DURATION_UNITS = {"m": "minutes", "h": "hours", "d": "days", "w": "weeks"}


class SecretInput(Input):
    """A masked field; a multi-line one keeps every line of a paste."""

    def __init__(self, *, multiline: bool = False, **options: Any) -> None:
        super().__init__(password=True, **options)
        self.multiline = multiline

    def on_paste(self, event: events.Paste) -> None:
        # Input keeps only the first line of a paste; a key or certificate needs all.
        if not self.multiline or not event.text:
            return
        text = event.text.replace("\r\n", "\n").replace("\r", "\n")
        if self.selection.is_empty:
            self.insert_text_at_cursor(text)
        else:
            self.replace(text, *self.selection)
        event.prevent_default()
        event.stop()


class _FormError(ValueError):
    def __init__(self, message: str, widget: Any = None) -> None:
        super().__init__(message)
        self.widget = widget


class SecretDialog(ModalScreen[JsonObject | None]):
    """Ask for a Secret's values in masked fields; they leave only as the result.

    With ``request`` it answers an Agent's request, which names the Secret and
    its fields, and Decline is offered. Without one the operator names the
    Secret and picks its kind (a bare `/secret`). ``values`` refills the form
    after a refusal; the dialog keeps them in memory only. Results:
    ``{"action": "store", ...}``, ``{"action": "decline"}``,
    ``{"action": "withdrawn"}`` (the request went away) or None.
    """

    BINDINGS = [("escape", "dismiss(None)", "Close"), ("ctrl+s", "store", "Store")]
    DEFAULT_CSS = f"""
    SecretDialog {{ align: center middle; }}
    SecretDialog > Vertical {{
        width: 86%; max-width: 96; height: auto; max-height: 95%; {_BOX}
    }}
    SecretDialog.request > Vertical {{ border: round $warning; border-title-color: $warning; }}
    SecretDialog {{ {_FIELDS} }}
    SecretDialog VerticalScroll {{ height: auto; max-height: 1fr; }}
    SecretDialog #secret-actions {{ dock: bottom; height: auto; }}
    SecretDialog .kind-fields {{ height: auto; }}
    SecretDialog Label {{ height: auto; text-style: bold; }}
    SecretDialog Select {{ margin-bottom: 1; }}
    SecretDialog .note {{ color: $text-muted; height: auto; margin-bottom: 1; }}
    SecretDialog #problem {{ color: $error; height: auto; }}
    """

    def __init__(
        self,
        *,
        request: JsonObject | None = None,
        values: JsonObject | None = None,
        problem: str = "",
        outcome_unknown: bool = False,
    ) -> None:
        super().__init__(classes="request" if request is not None else "")
        self.request = request
        self.form = dict(values or {})
        self.problem = problem
        self.outcome_unknown = outcome_unknown
        if request is not None:
            kind = str(request.get("kind") or "token")
            fields = tuple(
                (str(item["name"]), bool(item.get("concealed")), bool(item.get("optional")))
                for item in request.get("fields") or []
                if isinstance(item, dict) and item.get("name")
            )
            self.kinds = {kind: fields or SECRET_KIND_FIELDS.get(kind, ())}
        else:
            self.kinds = dict(SECRET_KIND_FIELDS)

    @property
    def request_id(self) -> str | None:
        return None if self.request is None else str(self.request.get("id"))

    @override
    def compose(self) -> ComposeResult:
        request = self.request
        form_values: JsonObject = self.form.get("values") or {}
        kind = str(self.form.get("kind") or next(iter(self.kinds)))
        with Vertical(id="secret-dialog-body") as box:
            if request is not None:
                box.border_title = terminal_text(f"Secret request · {request.get('name', '')}")
                box.border_subtitle = "Enter store · Esc return without answering"
            else:
                box.border_title = "Give the Agent a Secret"
                box.border_subtitle = "Enter store · Esc close"
            with VerticalScroll():
                if request is not None:
                    if request.get("description"):
                        yield Static(
                            terminal_text(str(request["description"])),
                            classes="note",
                            markup=False,
                        )
                    note = (
                        "The Agent waits for your answer. It uses the value by reference "
                        "and never sees it."
                    )
                    if request.get("replaces"):
                        note += " Storing replaces the value it has under this name."
                    yield Static(note, classes="note", markup=False)
                else:
                    yield Label("Name", markup=False)
                    yield Input(
                        value=str(self.form.get("name") or ""),
                        placeholder="GITHUB_TOKEN",
                        id="secret-name",
                        compact=True,
                    )
                    yield Label("Kind", markup=False)
                    yield Select(
                        _SECRET_KINDS, value=kind, allow_blank=False, id="secret-kind", compact=True
                    )
                for group, fields in self.kinds.items():
                    with Vertical(id=f"kind-{group}", classes="kind-fields") as container:
                        container.display = group == kind
                        for name, concealed, optional in fields:
                            label = _SECRET_FIELD_LABELS.get(name, name.replace("_", " ").title())
                            yield Label(label + (" (optional)" if optional else ""), markup=False)
                            value = str(form_values.get(name, "")) if group == kind else ""
                            identifier = f"field-{group}-{name}"
                            if concealed:
                                yield SecretInput(
                                    multiline=group == "text",
                                    value=value,
                                    id=identifier,
                                    compact=True,
                                )
                            else:
                                yield Input(value=value, id=identifier, compact=True)
                        if group == "text":
                            yield Static(
                                "Paste the text: every line is kept, and it stays masked.",
                                classes="note",
                                markup=False,
                            )
                # A value for a Secret the Agent has keeps that Secret's
                # lifetime unless the operator picks another.
                if request is None:
                    keep: tuple[tuple[str, str], ...] = (
                        ("As it is now, or for this Session if new", "keep"),
                    )
                elif request.get("replaces"):
                    keep = (("As it is now", "keep"),)
                else:
                    keep = ()
                choices = (*keep, *_SECRET_LIFETIMES)
                lifetime = str(
                    self.form.get("lifetime")
                    or ("keep" if keep else (request or {}).get("lifetime"))
                    or "session"
                )
                if lifetime not in {value for _, value in choices}:
                    lifetime = "session"
                yield Label("Lifetime", markup=False)
                yield Select(
                    choices,
                    value=lifetime,
                    allow_blank=False,
                    id="secret-lifetime",
                    compact=True,
                )
                duration = Input(
                    value=str(self.form.get("duration") or ""),
                    placeholder="Duration: 30m, 2h, 7d or 2w",
                    id="secret-duration",
                    compact=True,
                )
                duration.display = lifetime == "until"
                yield duration
                if request is None:
                    yield Label("Description (optional)", markup=False)
                    yield Input(
                        value=str(self.form.get("description") or ""),
                        placeholder="What the Agent uses it for",
                        id="secret-description",
                        compact=True,
                    )
            with Vertical(id="secret-actions"):
                yield Static(self.problem, id="problem", markup=False)
                with Horizontal():
                    yield Button(
                        "Store again" if self.outcome_unknown else "Store",
                        id="store",
                        variant="primary",
                        compact=True,
                    )
                    if request is not None:
                        yield Button("Decline", id="decline", compact=True)
                    yield Button("Close", id="close", compact=True)

    def on_mount(self) -> None:
        if self.request is None and not self.form.get("name"):
            self.query_one("#secret-name", Input).focus()
            return
        kind = self._kind()
        fields = self.kinds.get(kind, ())
        if fields:
            self.query_one(f"#field-{kind}-{fields[0][0]}", Input).focus()

    def _kind(self) -> str:
        if self.request is not None:
            return next(iter(self.kinds))
        return str(self.query_one("#secret-kind", Select).value)

    @on(Select.Changed, "#secret-kind")
    def kind_changed(self, event: Select.Changed) -> None:
        for group in self.kinds:
            self.query_one(f"#kind-{group}", Vertical).display = group == event.value

    @on(Select.Changed, "#secret-lifetime")
    def lifetime_changed(self, event: Select.Changed) -> None:
        self.query_one("#secret-duration", Input).display = event.value == "until"

    @on(Input.Submitted)
    def field_submitted(self) -> None:
        self.action_store()

    @on(Button.Pressed)
    def button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "store":
            self.action_store()
        elif event.button.id == "decline":
            self.dismiss({"action": "decline"})
        else:
            self.dismiss(None)

    def withdraw(self) -> None:
        """Close the form of a request that went away; nothing was sent."""
        self.dismiss({"action": "withdrawn"})

    def action_store(self) -> None:
        try:
            result = self._collect()
        except _FormError as problem:
            self.problem = str(problem)
            self.query_one("#problem", Static).update(self.problem)
            if problem.widget is not None:
                problem.widget.focus()
            return
        self.dismiss(result)

    def _collect(self) -> JsonObject:
        kind = self._kind()
        result: JsonObject = {"action": "store", "kind": kind}
        if self.request is None:
            name_input = self.query_one("#secret-name", Input)
            name = name_input.value.strip()
            if not name:
                raise _FormError("Name the Secret, for example GITHUB_TOKEN.", name_input)
            result["name"] = name
            result["description"] = self.query_one("#secret-description", Input).value.strip()
        values: dict[str, str] = {}
        for name, concealed, optional in self.kinds.get(kind, ()):
            widget = self.query_one(f"#field-{kind}-{name}", Input)
            # A concealed value is kept exactly as typed or pasted.
            value = widget.value if concealed else widget.value.strip()
            if not value.strip():
                if optional:
                    continue
                label = _SECRET_FIELD_LABELS.get(name, name)
                raise _FormError(f"{label} is required.", widget)
            values[name] = value
        result["values"] = values
        lifetime = str(self.query_one("#secret-lifetime", Select).value)
        if lifetime == "keep":
            return result
        result["lifetime"] = lifetime
        if lifetime == "until":
            duration_input = self.query_one("#secret-duration", Input)
            duration = duration_input.value.strip()
            match = _DURATION.fullmatch(duration)
            if match is None or int(match.group(1)) <= 0:
                raise _FormError("Write the duration as 30m, 2h, 7d or 2w.", duration_input)
            delta = timedelta(**{_DURATION_UNITS[match.group(2).lower()]: int(match.group(1))})
            result["duration"] = duration
            result["expires_at"] = (datetime.now(UTC) + delta).isoformat()
        return result
