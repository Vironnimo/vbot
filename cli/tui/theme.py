"""Terminal colour roles for the conversation accessor.

The accessor wears the WebUI's palette (`webui/src/styles/app/foundation.css`):
warm graphite surfaces in clear tonal steps, warm text levels and one amber
accent, so a terminal Session looks like the same product. The light theme
carries the same roles on warm paper; the WebUI has no light theme to copy.
Both themes set the framework's derived colours too (selection, scrollbars,
cursors, borders), which would otherwise follow its blue defaults.
"""

from __future__ import annotations

from textual.theme import Theme


def _theme(
    name: str,
    *,
    dark: bool,
    accent: str,
    on_accent: str,
    foreground: str,
    muted: str,
    background: str,
    surface: str,
    panel: str,
    raised: str,
    rule: str,
    rule_strong: str,
    success: str,
    warning: str,
    error: str,
) -> Theme:
    return Theme(
        name=name,
        dark=dark,
        primary=accent,
        secondary=accent,
        accent=accent,
        foreground=foreground,
        background=background,
        surface=surface,
        panel=panel,
        boost=f"{foreground}0d",
        success=success,
        warning=warning,
        error=error,
        variables={
            "text-muted": muted,
            "raised": raised,
            "rule": rule,
            "rule-strong": rule_strong,
            "on-accent": on_accent,
            "border": accent,
            "border-blurred": rule_strong,
            "screen-selection-background": f"{accent}38",
            "input-selection-background": f"{accent}38",
            "block-cursor-foreground": on_accent,
            "block-cursor-background": accent,
            "block-cursor-text-style": "bold",
            # A list keeps its choice visible while the search field above it
            # holds the focus, as in every Picker and the completion popup.
            "block-cursor-blurred-foreground": foreground,
            "block-cursor-blurred-background": f"{accent}30",
            "block-cursor-blurred-text-style": "none",
            "block-hover-background": f"{foreground}0d",
            "scrollbar": rule_strong,
            "scrollbar-hover": muted,
            "scrollbar-active": accent,
            "scrollbar-background": background,
            "scrollbar-background-hover": background,
            "scrollbar-background-active": background,
            "scrollbar-corner-color": background,
            "button-color-foreground": on_accent,
            "link-color": accent,
            "link-color-hover": accent,
            "link-background-hover": f"{accent}24",
        },
    )


VBOT_DARK = _theme(
    "vbot-dark",
    dark=True,
    accent="#e8870a",
    on_accent="#17110a",
    foreground="#e3ded6",
    muted="#9d9387",
    background="#15130f",
    surface="#1e1b17",
    panel="#26231e",
    raised="#302c26",
    rule="#2e2a25",
    rule_strong="#423c35",
    success="#5fcf8f",
    warning="#e5b53e",
    error="#f28b82",
)

VBOT_LIGHT = _theme(
    "vbot-light",
    dark=False,
    accent="#a35500",
    on_accent="#fffdf9",
    foreground="#2b2620",
    muted="#6e655b",
    background="#f6f3ee",
    surface="#fffdf9",
    panel="#ebe6dd",
    raised="#e0d9cd",
    rule="#ddd6ca",
    rule_strong="#c8bfb2",
    success="#2e7d4f",
    warning="#8a5d00",
    error="#b3261e",
)

THEMES = (VBOT_DARK, VBOT_LIGHT)
DEFAULT_THEME = VBOT_DARK.name
