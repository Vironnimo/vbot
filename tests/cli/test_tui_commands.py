"""Palette contracts for the terminal accessor's actions."""

from __future__ import annotations

import pytest
from textual.binding import Binding
from textual.command import CommandPalette

from cli.tui.app import ConversationApp
from cli.tui.commands import COMMANDS, POPUP_KEYS
from tests.cli.test_tui import Server


def _self_bindings() -> list[Binding]:
    """The app's own bindings, as the objects it declares (not tuple shorthands)."""
    return [entry for entry in ConversationApp.BINDINGS if isinstance(entry, Binding)]


def test_every_key_is_reachable_from_the_palette() -> None:
    # There is no key footer: the palette is where every bound action is found.
    bound = {binding.action for binding in _self_bindings()}
    assert bound - POPUP_KEYS <= {action for _label, action, _hint in COMMANDS}


@pytest.mark.asyncio
async def test_command_palette_runs_an_action_by_name() -> None:
    app = ConversationApp(Server(), agent_id="main")
    async with app.run_test(size=(100, 35)) as pilot:
        await pilot.pause()
        assert app.theme == "vbot-dark"

        await pilot.press("ctrl+k")
        await pilot.pause()
        assert isinstance(app.screen, CommandPalette)

        await pilot.press(*"light")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()

        assert app.theme == "vbot-light"
        assert not isinstance(app.screen, CommandPalette)
