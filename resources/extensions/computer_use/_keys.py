"""Key chord parsing into the canonical key names a desktop target presses.

Agents write keys the way their training taught them: Anthropic's computer tool
and xdotool (``Return``, ``Page_Down``, ``KP_Enter``, ``super``), Claude Code
(``ctrl+shift+tab``), browser automation (``ArrowUp``, ``Control``) and plain
words (``page down``). Every spelling whose key is unambiguous maps to one
canonical name; anything else is refused before input with the accepted forms.
"""

from __future__ import annotations

import re

MODIFIERS = ("ctrl", "shift", "alt", "win")

NAMED_KEYS = frozenset(
    {
        "enter",
        "escape",
        "tab",
        "backspace",
        "delete",
        "insert",
        "home",
        "end",
        "pageup",
        "pagedown",
        "up",
        "down",
        "left",
        "right",
        "space",
        "capslock",
        "numlock",
        "scrolllock",
        "printscreen",
        "pause",
        "menu",
        *(f"f{number}" for number in range(1, 25)),
        "volumeup",
        "volumedown",
        "volumemute",
        "medianext",
        "mediaprev",
        "mediaplaypause",
        "browserback",
        "browserforward",
    }
)

# Spellings compared without case, spaces, ``_`` and ``-``.
_ALIASES = {
    **dict.fromkeys(("ctrl", "control", "ctl", "lctrl", "rctrl", "ctrll", "ctrlr"), "ctrl"),
    **dict.fromkeys(("controll", "controlr", "leftctrl", "rightctrl", "strg"), "ctrl"),
    **dict.fromkeys(("shift", "lshift", "rshift", "shiftl", "shiftr"), "shift"),
    **dict.fromkeys(("leftshift", "rightshift"), "shift"),
    **dict.fromkeys(("alt", "lalt", "ralt", "altl", "altr", "leftalt", "rightalt"), "alt"),
    **dict.fromkeys(("win", "windows", "lwin", "rwin", "super", "superl", "superr"), "win"),
    **dict.fromkeys(("winl", "winr", "leftwin", "rightwin", "windowskey", "winkey"), "win"),
    **dict.fromkeys(("enter", "return", "kpenter", "ret", "numpadenter"), "enter"),
    **dict.fromkeys(("escape", "esc"), "escape"),
    **dict.fromkeys(("tab", "isolefttab"), "tab"),
    **dict.fromkeys(("backspace", "bksp", "bs"), "backspace"),
    **dict.fromkeys(("delete", "del", "kpdelete", "forwarddelete"), "delete"),
    **dict.fromkeys(("insert", "ins", "kpinsert"), "insert"),
    **dict.fromkeys(("home", "kphome"), "home"),
    **dict.fromkeys(("end", "kpend"), "end"),
    **dict.fromkeys(("pageup", "pgup", "prior", "kpprior", "kppageup"), "pageup"),
    **dict.fromkeys(("pagedown", "pgdn", "pgdown", "next", "kpnext", "kppagedown"), "pagedown"),
    **dict.fromkeys(("up", "arrowup", "uparrow", "kpup"), "up"),
    **dict.fromkeys(("down", "arrowdown", "downarrow", "kpdown"), "down"),
    **dict.fromkeys(("left", "arrowleft", "leftarrow", "kpleft"), "left"),
    **dict.fromkeys(("right", "arrowright", "rightarrow", "kpright"), "right"),
    **dict.fromkeys(("space", "spacebar", "kpspace"), "space"),
    **dict.fromkeys(("capslock", "caps"), "capslock"),
    "numlock": "numlock",
    "scrolllock": "scrolllock",
    **dict.fromkeys(
        ("printscreen", "print", "prtsc", "prtscn", "prtscr", "snapshot"), "printscreen"
    ),
    **dict.fromkeys(("pause", "break"), "pause"),
    **dict.fromkeys(("menu", "apps", "contextmenu", "application"), "menu"),
    **dict.fromkeys(("volumeup", "audioraisevolume", "xf86audioraisevolume"), "volumeup"),
    **dict.fromkeys(("audiovolumeup",), "volumeup"),
    **dict.fromkeys(("volumedown", "audiolowervolume", "xf86audiolowervolume"), "volumedown"),
    **dict.fromkeys(("audiovolumedown",), "volumedown"),
    **dict.fromkeys(("volumemute", "audiomute", "xf86audiomute", "audiovolumemute"), "volumemute"),
    **dict.fromkeys(("medianext", "medianexttrack", "audionext", "xf86audionext"), "medianext"),
    **dict.fromkeys(("mediaprev", "mediaprevious", "mediaprevioustrack"), "mediaprev"),
    **dict.fromkeys(("audioprev", "xf86audioprev"), "mediaprev"),
    **dict.fromkeys(
        ("mediaplaypause", "playpause", "audioplay", "xf86audioplay"), "mediaplaypause"
    ),
    **dict.fromkeys(("browserback", "xf86back"), "browserback"),
    **dict.fromkeys(("browserforward", "xf86forward"), "browserforward"),
    # xdotool keysym names of printable characters, and keypad characters.
    **{f"kp{digit}": str(digit) for digit in range(10)},
    **dict.fromkeys(("plus", "kpadd"), "+"),
    **dict.fromkeys(("minus", "kpsubtract"), "-"),
    **dict.fromkeys(("asterisk", "kpmultiply"), "*"),
    **dict.fromkeys(("slash", "kpdivide"), "/"),
    **dict.fromkeys(("period", "kpdecimal"), "."),
    "equal": "=",
    "comma": ",",
    "backslash": "\\",
    "semicolon": ";",
    "apostrophe": "'",
    "grave": "`",
    "bracketleft": "[",
    "bracketright": "]",
    "braceleft": "{",
    "braceright": "}",
    "parenleft": "(",
    "parenright": ")",
    "numbersign": "#",
    "dollar": "$",
    "percent": "%",
    "ampersand": "&",
    "underscore": "_",
    "colon": ":",
    "less": "<",
    "greater": ">",
    "question": "?",
    "exclam": "!",
    "asciitilde": "~",
    "asciicircum": "^",
    "bar": "|",
    "quotedbl": '"',
}

_COMMAND_KEYS = frozenset({"cmd", "command", "meta", "lcmd", "rcmd", "cmdl", "cmdr", "metal"})

_ACCEPTED = (
    "Name keys like enter, escape, tab, backspace, delete, home, end, pageup, pagedown, up, "
    "down, left, right, space, f1-f24, or one character; join the keys of one chord with + "
    '(ctrl+shift+t) and separate chords with spaces ("ctrl+a delete").'
)


class KeyChordError(ValueError):
    """A chord the target cannot press; the message names the accepted forms."""


def _spelled(token: str) -> str:
    return re.sub(r"[\s_-]+", "", token.casefold())


def canonical_key(token: str) -> str:
    """Return the canonical name of one key, or raise ``KeyChordError``."""
    if len(token) == 1 and token.isprintable() and not token.isspace():
        return token.lower()
    name = _spelled(token)
    if name in NAMED_KEYS:
        return name
    if name in _ALIASES:
        return _ALIASES[name]
    if name in _COMMAND_KEYS:
        raise KeyChordError(
            f'"{token}" is a Mac key, but this computer runs Windows. Use ctrl for shortcuts '
            "(ctrl+c, ctrl+s) or win for the Windows key."
        )
    raise KeyChordError(f'Unknown key "{token}". {_ACCEPTED}')


def _chord_tokens(chord: str) -> list[str]:
    """Split one chord written with ``+`` (or ``-`` between modifiers) into key tokens."""
    if chord == "+":
        return ["+"]
    if chord.endswith("++"):
        return [*chord[:-2].split("+"), "+"]
    if "+" in chord:
        return chord.split("+")
    parts = chord.split("-")
    # "ctrl-s" and "ctrl-shift-t": every part before the last is a modifier.
    if len(parts) > 1 and all(parts) and all(_is_modifier(part) for part in parts[:-1]):
        return parts
    return [chord]


def _is_modifier(token: str) -> bool:
    try:
        return canonical_key(token) in MODIFIERS
    except KeyChordError:
        return False


def parse_chord(chord: str) -> list[str]:
    """Return one chord's canonical keys, modifiers first."""
    tokens = _chord_tokens(chord.strip())
    if any(not token.strip() for token in tokens):
        raise KeyChordError(f'"{chord}" has an empty key. {_ACCEPTED}')
    keys: list[str] = []
    for token in tokens:
        key = canonical_key(token.strip())
        if key not in keys:
            keys.append(key)
    return sorted(keys, key=lambda key: key not in MODIFIERS)


def parse_chords(text: str) -> list[list[str]]:
    """Return the chords of *text* in order: ``"ctrl+a Delete"`` is two chords.

    A key name written as two words (``page down``) is read as one key.
    """
    compact = re.sub(r"\s*\+\s*", "+", text.strip())
    words = compact.split()
    if not words:
        raise KeyChordError(f"No key was given. {_ACCEPTED}")
    chords: list[list[str]] = []
    index = 0
    while index < len(words):
        try:
            chords.append(parse_chord(words[index]))
            index += 1
            continue
        except KeyChordError as error:
            if index + 1 >= len(words):
                raise
            try:
                chords.append(parse_chord(words[index] + words[index + 1]))
            except KeyChordError:
                raise error from None
            index += 2
    return chords


def parse_modifiers(text: str) -> list[str]:
    """Return the modifier keys of *text* (``"shift"``, ``"ctrl+shift"``) to hold."""
    if not text.strip():
        return []
    chords = parse_chords(text)
    keys = [key for chord in chords for key in chord]
    others = [key for key in keys if key not in MODIFIERS]
    if others:
        raise KeyChordError(
            f'"{text}" is not a modifier. On clicks, scrolls and drags, text names modifier '
            "keys to hold: ctrl, shift, alt or win, joined with + (ctrl+shift). To type text "
            'or press other keys, use the "type" or "key" action instead.'
        )
    return list(dict.fromkeys(keys))


def chord_text(chord: list[str]) -> str:
    return "+".join(chord)


__all__ = [
    "MODIFIERS",
    "NAMED_KEYS",
    "KeyChordError",
    "canonical_key",
    "chord_text",
    "parse_chord",
    "parse_chords",
    "parse_modifiers",
]
