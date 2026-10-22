"""Inline suggestions for `/` commands, `/model` arguments and `@` server files.

The composer suggests while the operator types, as the WebUI does: the popup
offers the values the token under the cursor stands for, filtered as the token
grows, and taking one replaces that token. Recognition and filtering are pure
functions here so the popup's behavior is testable without a terminal.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, override

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

from cli.tui.rendering import one_line, terminal_text

type JsonObject = dict[str, Any]

# Rows the popup shows at once; matches beyond that stay reachable by scrolling.
MAX_SUGGESTIONS = 8
_COMMAND_MARKER = "/"
_FILE_MARKER = "@"
_MODEL_COMMAND = "/model "


@dataclass(frozen=True)
class Suggestion:
    """One popup row and what taking it does."""

    label: str
    value: str
    kind: str
    # A no-argument command and a chosen Model are submitted at once, as the
    # WebUI does; everything else only completes the token.
    send: bool = False


@dataclass(frozen=True)
class Trigger:
    """The token the popup offers values for, within the draft's line."""

    kind: str
    query: str
    start: int
    end: int


@dataclass
class CompletionState:
    """What the popup shows and which token it replaces."""

    kind: str = ""
    title: str = ""
    suggestions: list[Suggestion] = field(default_factory=list)
    index: int = 0
    trigger: Trigger | None = None
    row: int = 0
    loading: bool = False
    # The draft text for which Escape closed the popup; it reopens on any edit.
    dismissed: str = ""
    # The cursor is in a token whose catalog is still being read: the popup has
    # nothing to show yet, and Enter waits for it instead of sending the token.
    pending: bool = False

    @property
    def visible(self) -> bool:
        return bool(self.suggestions)

    def selected(self) -> Suggestion | None:
        if not self.suggestions:
            return None
        return self.suggestions[min(self.index, len(self.suggestions) - 1)]


def trigger_for(text: str, row: int, column: int) -> Trigger | None:
    """Return the suggestion context at the cursor, or None.

    Mirrors the WebUI composer: `/` commands and the `/model ` argument only at
    the start of the draft, `@` file tokens anywhere.
    """
    lines = text.split("\n")
    line = lines[row] if 0 <= row < len(lines) else ""
    prefix = line[:column]
    if row == 0 and prefix.startswith(_MODEL_COMMAND):
        return Trigger("models", prefix[len(_MODEL_COMMAND) :], len(_MODEL_COMMAND), column)
    if row == 0 and prefix.startswith(_COMMAND_MARKER) and " " not in prefix:
        return Trigger("commands", prefix[1:], 0, column)
    start = column
    while start > 0 and not line[start - 1].isspace():
        start -= 1
    token = line[start:column]
    if token.startswith(_FILE_MARKER):
        return Trigger("files", token[1:], start, column)
    return None


def matching(suggestions: list[Suggestion], query: str) -> list[Suggestion]:
    """Keep the suggestions whose row carries every word of the query."""
    words = query.casefold().split()
    if not words:
        return list(suggestions)
    return [item for item in suggestions if all(word in item.label.casefold() for word in words)]


def command_suggestions(items: list[JsonObject]) -> list[Suggestion]:
    """Offer Commands and Skills as `/<name>` with their description."""
    result: list[Suggestion] = []
    for item in items:
        if not isinstance(item, dict) or not item.get("name"):
            continue
        name = str(item["name"]).lstrip(_COMMAND_MARKER)
        kind = str(item.get("type") or "command")
        description = one_line(str(item.get("description") or ""), 58)
        label = f"/{name} · {description}" if description else f"/{name}"
        result.append(Suggestion(label, name, kind, send=item.get("argument") == "none"))
    return result


def model_suggestions(models: list[JsonObject]) -> list[Suggestion]:
    """Offer the chat Models with the reference a `/model` message needs."""
    result: list[Suggestion] = []
    for row in models:
        if not isinstance(row, dict):
            continue
        provider = row.get("provider_id")
        model_id = row.get("model_id")
        reference = row.get("id")
        if (
            not isinstance(reference, str)
            and isinstance(provider, str)
            and isinstance(model_id, str)
        ):
            reference = f"{provider}/{model_id}"
        if not isinstance(reference, str) or not reference:
            continue
        name = str(row.get("name") or model_id or reference)
        result.append(Suggestion(f"{name} · {reference}", reference, "model", send=True))
    return result


def file_suggestions(paths: list[str]) -> list[Suggestion]:
    """Offer server files by path; taking one mentions it in the draft."""
    return [
        Suggestion(str(path), str(path), "file") for path in paths if isinstance(path, str) and path
    ]


class CompletionBar(Vertical):
    """The suggestion popup, shown directly above the composer."""

    DEFAULT_CSS = """
    CompletionBar { display: none; height: auto; max-height: 10; margin: 0 1;
        border: round $rule-strong; background: $surface; padding: 0 1; }
    CompletionBar.visible { display: block; }
    CompletionBar #completion-title { height: 1; color: $text-muted; }
    CompletionBar OptionList { height: auto; max-height: 8; background: transparent;
        border: none; padding: 0; }
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._labels: list[str] = []

    @property
    def options(self) -> OptionList:
        return self.query_one("#completion-options", OptionList)

    @override
    def compose(self) -> ComposeResult:
        yield Static("", id="completion-title", markup=False)
        yield OptionList(id="completion-options")

    def show(self, title: str, suggestions: list[Suggestion], index: int) -> None:
        """Render the popup, reusing the option rows when they did not change."""
        self.query_one("#completion-title", Static).update(terminal_text(title), layout=False)
        labels = [item.label for item in suggestions]
        options = self.options
        if labels != self._labels:
            self._labels = labels
            options.clear_options()
            options.add_options(
                Option(label, id=str(position)) for position, label in enumerate(labels)
            )
        options.highlighted = index
        self.add_class("visible")

    def hide(self) -> None:
        self.remove_class("visible")
