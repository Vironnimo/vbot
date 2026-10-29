"""Inline composer suggestions and their conversation-scoped read catalogs."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from textual.app import ScreenStackError
from textual.screen import ModalScreen
from textual.widgets import TextArea

from cli.tui.completion import (
    CompletionState,
    Suggestion,
    Trigger,
    command_suggestions,
    file_suggestions,
    matching,
    model_suggestions,
    trigger_for,
)
from cli.tui.mentions import format_mention
from cli.tui.state import Conversation
from cli.tui.transport import ClientError

if TYPE_CHECKING:
    from cli.tui.app import ConversationApp


class ComposerSuggestions:
    """Keep completion reads independent of the captured submission authority."""

    def __init__(self, app: ConversationApp) -> None:
        self.app = app
        self.state = CompletionState()
        self.catalogs: dict[tuple[Any, ...], list[Any]] = {}
        self.loading: set[tuple[Any, ...]] = set()
        self.catalog_revision = 0

    def invalidate(self) -> None:
        """Retire cached and pending catalogs after server changes or reconnect."""
        self.catalog_revision += 1
        self.catalogs.clear()
        self.loading.clear()
        self.refresh()

    def modal_owns_the_screen(self) -> bool:
        """Whether a modal screen holds the terminal, which keeps the popup out of it.

        A composer may change before the app's first screen is on the stack.
        """
        try:
            return isinstance(self.app.screen, ModalScreen)
        except ScreenStackError:
            return False

    def refresh(self, editor: TextArea | None = None) -> None:
        """Offer the values the draft's token stands for, as the WebUI composer does."""
        view = self.app.conversation
        composer = editor if editor is not None else self.app._composer()
        if (
            view is None
            or not view.available
            or composer is None
            or not composer.is_mounted
            or self.modal_owns_the_screen()
        ):
            self.close()
            return
        text = composer.text
        row, column = composer.cursor_location
        trigger = trigger_for(text, row, column)
        if trigger is None or text == self.state.dismissed:
            self.close()
            return
        self.state.trigger = trigger
        self.state.row = row
        candidates = self.candidates(view, trigger)
        self.state.pending = candidates is None
        suggestions = matching(candidates or [], trigger.query)
        if not suggestions:
            self.app.bar.hide()
            self.state.kind = ""
            self.state.suggestions = []
            return
        kinds = {"commands": "Commands and Skills", "models": "Models", "files": "Server files"}
        title = f"{kinds[trigger.kind]} · ↑↓ choose · Enter use · Esc close"
        self.state.kind = trigger.kind
        self.state.suggestions = suggestions
        self.state.index = 0
        self.state.title = title
        self.app.bar.show(title, suggestions, 0)

    def candidates(self, view: Conversation, trigger: Trigger) -> list[Suggestion] | None:
        """Return the suggestions for the token, or None while their catalog loads."""
        key = (view.key, trigger.kind)
        catalog = self.catalogs.get(key)
        if catalog is None:
            self.load_catalog(view, trigger.kind)
            return None
        if trigger.kind == "commands":
            return command_suggestions(catalog)
        if trigger.kind == "models":
            return model_suggestions(catalog)
        return file_suggestions(catalog)

    def load_catalog(self, view: Conversation, kind: str) -> None:
        """Read the catalog the popup needs, once per conversation and kind."""
        key = (view.key, kind)
        if key in self.loading:
            return
        self.loading.add(key)
        revision = self.catalog_revision
        if len(self.catalogs) > 40:
            self.catalogs.clear()

        async def load() -> None:
            try:
                if kind == "commands":
                    response = await self.app.client.rpc(
                        "chat.commands", {**view.params(draft=True), "reply_surface": "tui"}
                    )
                    items = response.get("items", [])
                elif kind == "models":
                    items = (await self.app.client.rpc("model.list", {"task": "chat"})).get(
                        "models", []
                    )
                else:
                    items = (await self.app.client.rpc("files.list", view.params(draft=True))).get(
                        "files", []
                    )
            except ClientError, OSError:
                items = []
            finally:
                if revision == self.catalog_revision:
                    self.loading.discard(key)
            if revision != self.catalog_revision:
                return
            self.catalogs[key] = list(items)
            if self.app.conversation is view and view.available:
                self.refresh()

        self.app._start(load)

    def close(self) -> None:
        self.state.kind = ""
        self.state.suggestions = []
        self.state.trigger = None
        self.state.pending = False
        if bar := getattr(self.app, "bar", None):
            bar.hide()

    def move(self, step: int) -> None:
        total = len(self.state.suggestions)
        if not total:
            return
        self.state.index = (self.state.index + step) % total
        self.app.bar.options.highlighted = self.state.index

    async def accept(self) -> None:
        """Take the highlighted suggestion, replacing the token it stands for."""
        view = self.app.conversation
        trigger = self.state.trigger
        suggestion = self.state.selected()
        if view is None or trigger is None or suggestion is None:
            return
        editor = self.app.query_one("#composer", TextArea)
        row = self.state.row
        if suggestion.kind in {"command", "skill"}:
            # A command without an argument runs as soon as it is chosen; one with
            # an argument waits for the operator's text.
            insertion = f"/{suggestion.value}" if suggestion.send else f"/{suggestion.value} "
        elif suggestion.kind == "model":
            insertion = suggestion.value
        else:
            insertion = format_mention(suggestion.value) + " "
            if suggestion.value not in view.mentions:
                view.mentions.append(suggestion.value)
        editor.replace(insertion, (row, trigger.start), (row, trigger.end))
        editor.focus()
        self.close()
        self.app._grow_composer(editor)
        self.app.submissions.changed(view)
        self.app._header()
        if suggestion.send:
            self.app.action_send()
