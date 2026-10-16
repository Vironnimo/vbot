"""Searchable commands for actions that need no visible key.

The footer keeps only the keys an operator uses constantly; every other action
stays reachable by name through the palette, so hiding a key never hides the
action. Each entry names an ``action_*`` method of the application, which the
palette runs after it dismissed itself.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, override

from textual.command import DiscoveryHit, Hit, Hits, Provider
from textual.style import Style

if TYPE_CHECKING:
    from textual.screen import Screen

# (palette label, application action, key hint)
COMMANDS: tuple[tuple[str, str, str], ...] = (
    ("Send the draft message", "send", "Ctrl+S"),
    ("Choose a Session", "sessions", "F2"),
    ("Start a new conversation", "new", "F3"),
    ("Choose Model and thinking effort", "model", "F4"),
    ("Show Sub-Agents and background work", "activity", "F5"),
    ("Open the Queue", "queue", "F6"),
    ("Answer Extension requests", "requests", "F7"),
    ("Attach, mention or download files", "files", "F8"),
    ("Complete a Command or Skill", "commands", "F9"),
    ("Complete from the draft at the cursor", "complete", "Ctrl+Space"),
    ("Stop the current Run", "stop", "Ctrl+X"),
    ("Stop all background work", "stop_all", "F10"),
    ("Session actions, Tools and drafts", "conversation_actions", "F11"),
    ("Jump to the latest output", "latest", "F12"),
    ("Load older history", "older", "Ctrl+O"),
    ("Go to the parent conversation", "previous_conversation", "Ctrl+B"),
    ("Find in the loaded history", "find", "Ctrl+F"),
    ("Copy or save the loaded conversation", "copy", "Ctrl+G"),
    ("Insert earlier input", "input_history", "Alt+Up"),
    ("Reconnect and reload the Session", "refresh", "Ctrl+R"),
    ("Use the dark theme", "theme_dark", "terminal colours"),
    ("Use the light theme", "theme_light", "terminal colours"),
    ("Keyboard controls", "help", "F1"),
    ("Search every command", "command_palette", "Ctrl+K"),
    ("Detach from the server", "detach", "Ctrl+Q"),
)


class ConversationCommands(Provider):
    """Offer the accessor's actions as palette entries.

    A plain `Provider` rather than Textual's `SimpleProvider`: that helper sets
    its match style only when called, so a provider the palette instantiates
    directly cannot match a query.
    """

    def __init__(self, screen: Screen[Any], match_style: Style | None = None) -> None:
        super().__init__(screen, match_style)
        app = screen.app
        self.entries: list[tuple[str, Any, str]] = [
            (label, getattr(app, f"action_{action}"), hint) for label, action, hint in COMMANDS
        ]

    @override
    async def search(self, query: str) -> Hits:
        matcher = self.matcher(query)
        for label, callback, hint in self.entries:
            match = matcher.match(label)
            if match > 0:
                yield Hit(match, matcher.highlight(label), callback, help=hint)

    @override
    async def discover(self) -> Hits:
        for label, callback, hint in self.entries:
            yield DiscoveryHit(label, callback, help=hint)
