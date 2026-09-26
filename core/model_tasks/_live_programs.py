"""Coding programs a Live call starts in Terminals: Codex and Claude Code.

One table describes each program: the command that starts it, the screen that
shows it is ready for a task, the startup questions that block it, and the key
that interrupts its work. Live writes to a program only while its process runs
in the Terminal (``expected_program`` on ``terminal.input``); these screen rules
decide where the text lands. Live types only while the program's own input line
is on screen and never while a menu, a startup question or the shell is:
typing into the Terminal's shell would run the text as shell commands.

The markers were observed on Windows 11 with codex-cli 0.153.2 and Claude Code
2.1.280 running in a vBot Terminal (PowerShell 7.6) on 2026-09-25:

* Codex draws its header box (``>_ OpenAI Codex (v0.153.2)``) and the input line
  ``› Ask Codex to do anything`` while its model still says ``loading``. A
  pending update question (``Update available!`` ... ``3. Skip until next
  version``) replaces the screen about a second later, so the header must no
  longer say ``loading``; ``Starting MCP servers`` follows while it finishes
  starting. The folder trust question offers ``1. Yes, continue`` (selected)
  and ``2. No, quit``.
* Claude Code draws ``Claude Code v2.1.280`` and the input line ``❯ Try "..."``
  (a no-break space after the arrow) between two rules. Its folder trust
  question selects ``No, exit`` first; Down selects ``Yes, I trust this folder``.
* Both mark the selected answer of a menu with the same arrow as their input
  line, numbered (``❯ 1. Yes``) except in Claude Code's trust question.
* Both show "esc to interrupt" while working and enable bracketed paste. Text
  and Enter written together only fill the input line (Codex treats the fast
  burst as a paste), so Enter goes in a separate write after the text arrived.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

# A line where the shell started a program: PowerShell's ``PS C:\work``, cmd's
# ``C:\work>``, or a POSIX ``user@host`` prompt (bash ``:~/dir$``, zsh ``%``,
# fish ``>``) followed by the command. A long prompt wraps onto following
# lines. A program started from the prompt draws below the last such line.
_PROMPT_START = re.compile(
    r"^(?:PS [A-Za-z]:\\|[A-Za-z]:\\[^\n]*>|[\w.-]+@[\w.-]+[: ][^\n]*[$#%>] )", re.MULTILINE
)
# The shell prompt as the last line: the PowerShell and cmd forms, a POSIX
# shell's ``$`` / ``#``, a ``user@host`` prompt ending in zsh's ``%`` or fish's
# ``>``, or a prompt theme's arrow (starship and oh-my-posh end in ``❯``).
# Program status lines may end in ``%`` or ``>``, so those count only in the
# prompt forms.
_SHELL_PROMPT = re.compile(r"(?:^PS .*>|^[A-Za-z]:\\.*>|[$#]|^[\w.-]+@[\w.-]+[: ].*[%>]|[❯➜λ])\s*$")
_WRAPPED_PROMPT_LINES = 2
# A menu answer: ``1. Yes``.
_NUMBERED = re.compile(r"\d+\.\s")
_OTHER_ANSWER = re.compile(r"^[ \t]+\d+\.\s", re.MULTILINE)
# How much of the typed text must show on the input line before Enter.
_PENDING_PREFIX_CHARS = 12
# Both programs collapse a large paste into a placeholder such as
# ``[Pasted text #1 +12 lines]`` or ``[Pasted Content 1200 chars]``.
_PASTE_PLACEHOLDER = "[Pasted"


@dataclass(frozen=True)
class CliPrompt:
    """A startup question that blocks the program until the user answers it.

    ``markers`` must all appear on screen. ``keys`` are the named keys that
    select ``choice`` and confirm it: accepting a trust question or skipping an
    update. Enter confirms only while ``choice`` is the selected answer.
    """

    kind: str
    markers: tuple[str, ...]
    keys: tuple[str, ...]
    choice: str


@dataclass(frozen=True)
class CodingProgram:
    """One coding program Live can start and type into."""

    key: str
    label: str
    command: str
    # Every marker appears once the program has drawn its start screen.
    ready_markers: tuple[str, ...]
    # The arrow that starts the input line and marks a menu's selected answer.
    marker: str
    # While any of these shows, the program is still starting.
    starting_markers: tuple[re.Pattern[str], ...]
    prompts: tuple[CliPrompt, ...]
    interrupt_key: str


CODING_PROGRAMS: Final[dict[str, CodingProgram]] = {
    "codex": CodingProgram(
        key="codex",
        label="Codex",
        command="codex",
        ready_markers=("OpenAI Codex",),
        marker="›",
        starting_markers=(re.compile(r"model:\s+loading"), re.compile(r"Starting MCP servers")),
        prompts=(
            CliPrompt(
                kind="trust",
                markers=("Yes, continue", "No, quit"),
                keys=("enter",),
                choice="Yes, continue",
            ),
            CliPrompt(
                kind="update",
                markers=("Update available", "Skip until next version"),
                keys=("down", "enter"),
                choice="Skip",
            ),
        ),
        interrupt_key="escape",
    ),
    "claude": CodingProgram(
        key="claude",
        label="Claude Code",
        command="claude",
        ready_markers=("Claude Code v",),
        marker="❯",
        starting_markers=(),
        prompts=(
            CliPrompt(
                kind="trust",
                markers=("Yes, I trust this folder",),
                keys=("down", "enter"),
                choice="Yes, I trust this folder",
            ),
        ),
        interrupt_key="escape",
    ),
}

# Program names Models use for the table's programs, compared by spelling.
CODING_PROGRAM_ALIASES: Final[dict[str, str]] = {
    "codex": "codex",
    "codexcli": "codex",
    "openaicodex": "codex",
    "claude": "claude",
    "claudecode": "claude",
    "claudecli": "claude",
    "anthropicclaude": "claude",
    "anthropicclaudecode": "claude",
}


def program_prompt(program: CodingProgram, screen: str) -> CliPrompt | None:
    """Return the blocking startup question the screen shows, if any."""
    drawn = _program_area(screen)
    for prompt in program.prompts:
        if all(marker in drawn for marker in prompt.markers):
            return prompt
    return None


def selected_answer(program: CodingProgram, screen: str) -> str | None:
    """The selected answer of the menu or question on screen, without its number."""
    selection = _last_marker_line(program, _program_area(screen))
    if selection is None:
        return None
    text = _marker_text(selection).strip()
    numbered = _NUMBERED.match(text)
    return text[numbered.end() :].strip() if numbered else text


def program_ready(program: CodingProgram, screen: str) -> bool:
    """Whether the program finished starting and shows its input line."""
    drawn = _program_area(screen)
    return (
        all(marker in drawn for marker in program.ready_markers)
        and not any(marker.search(drawn) for marker in program.starting_markers)
        and program_input_visible(program, screen)
    )


def program_input_visible(program: CodingProgram, screen: str) -> bool:
    """Whether typed text reaches the program's input line, not a menu or the shell."""
    if program_prompt(program, screen) is not None or shell_prompt_visible(screen):
        return False
    area = _program_area(screen)
    line = _last_marker_line(program, area)
    if line is None or _NUMBERED.match(_marker_text(line).strip()):
        return False
    # A menu lists its other answers below the selected one.
    return _OTHER_ANSWER.search(area, line.end()) is None


def program_text_pending(program: CodingProgram, screen: str, text: str) -> bool:
    """Whether the program's input line holds *text*, so Enter sends exactly it.

    The input line must start with the text's first characters or show the
    placeholder of a large paste; a menu or question that appeared since the
    text was typed never matches.
    """
    if program_prompt(program, screen) is not None or shell_prompt_visible(screen):
        return False
    line = _last_marker_line(program, _program_area(screen))
    if line is None:
        return False
    shown = " ".join(_marker_text(line).split())
    if shown.startswith(_PASTE_PLACEHOLDER):
        return True
    first = next((part for part in text.splitlines() if part.strip()), "")
    expected = " ".join(first.split())[:_PENDING_PREFIX_CHARS]
    return bool(expected) and shown.startswith(expected)


def shell_prompt_visible(screen: str) -> bool:
    """Whether the Terminal's shell prompt is the last line: no program runs in front of it."""
    lines = [line.rstrip() for line in screen.splitlines() if line.strip()]
    if not lines or _SHELL_PROMPT.search(lines[-1]):
        return True
    # A wrapped prompt: its first line, then only its continuation ending in ``>``.
    tail = [line for line in _program_area(screen).splitlines() if line.strip()]
    return (
        _PROMPT_START.search(screen) is not None
        and len(tail) <= _WRAPPED_PROMPT_LINES
        and lines[-1].endswith(">")
    )


def _last_marker_line(program: CodingProgram, area: str) -> re.Match[str] | None:
    """The last line starting with the program's arrow: its input line or a menu's selection."""
    pattern = re.compile(rf"^[ \t]*{re.escape(program.marker)}(?:[ \xa0](.*))?$", re.MULTILINE)
    matches = list(pattern.finditer(area))
    return matches[-1] if matches else None


def _marker_text(line: re.Match[str]) -> str:
    """The text after the arrow."""
    return line.group(1) or ""


def _program_area(screen: str) -> str:
    """The screen below the last shell prompt line, where a program started from it draws.

    Text above it (an earlier program's screen) says nothing about the current one.
    """
    starts = list(_PROMPT_START.finditer(screen))
    if not starts:
        return screen
    line_end = screen.find("\n", starts[-1].start())
    return "" if line_end < 0 else screen[line_end + 1 :]


__all__ = [
    "CODING_PROGRAMS",
    "CODING_PROGRAM_ALIASES",
    "CliPrompt",
    "CodingProgram",
    "program_input_visible",
    "program_prompt",
    "program_ready",
    "program_text_pending",
    "selected_answer",
    "shell_prompt_visible",
]
