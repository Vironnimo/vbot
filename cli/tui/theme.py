"""Terminal colour roles for the conversation accessor.

The accessor owns its palette instead of borrowing the framework default, so one
row role reads the same everywhere: the operator's own messages, model output,
Tool activity and reasoning each get one colour, and the chrome bands stay
quieter than the conversation. Both themes carry the same variable names, so
every rule is written once.
"""

from __future__ import annotations

from textual.theme import Theme

VBOT_DARK = Theme(
    name="vbot-dark",
    dark=True,
    primary="#7aa2f7",
    secondary="#bb9af7",
    accent="#7dcfff",
    foreground="#c0caf5",
    background="#16171d",
    surface="#1b1d27",
    panel="#23252f",
    success="#9ece6a",
    warning="#e0af68",
    error="#f7768e",
)

VBOT_LIGHT = Theme(
    name="vbot-light",
    dark=False,
    primary="#2f6fdb",
    secondary="#7a4fbf",
    accent="#1f7f9e",
    foreground="#2b2f38",
    background="#f7f8fa",
    surface="#ffffff",
    panel="#e9ecf1",
    success="#2f8f4e",
    warning="#9a6400",
    error="#c0392b",
)

THEMES = (VBOT_DARK, VBOT_LIGHT)
DEFAULT_THEME = VBOT_DARK.name
