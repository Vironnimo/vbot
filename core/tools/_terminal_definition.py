"""Interactive Terminal Tool definition and action contracts."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from core.storage.temp_files import TEMPORARY_FILE_RETENTION
from core.tools._terminal_arguments import (
    TERMINAL_KEY_SUMMARY,
)
from core.tools.model_names import BASH_TOOL_NAME, model_tool_name
from core.tools.terminal_manager import (
    TERMINAL_DEFAULT_COLUMNS,
    TERMINAL_DEFAULT_ROWS,
    TERMINAL_FINISHED_TTL,
    TERMINAL_INPUT_KEY_SEQUENCES,
    TERMINAL_INPUT_MAX_CHARS,
    TERMINAL_START_WAIT_SECONDS,
    TERMINAL_STATUS_DEFAULT_LINES,
    TERMINAL_STATUS_MAX_LINES,
    TERMINAL_TEMPORARY_CATEGORY,
)
from core.tools.tools import (
    JsonObject,
)

if TYPE_CHECKING:
    pass


TERMINAL_TOOL_NAME = "terminal"
TERMINAL_ACTIONS = (
    "start",
    "list",
    "status",
    "wait",
    "input",
    "resize",
    "kill",
    "attach",
    "detach",
)
TERMINAL_WAIT_DEFAULT_SECONDS = 60
TERMINAL_WAIT_MAX_SECONDS = 600
# input waits this long for the reply to settle, as start does for the first screen.
TERMINAL_REPLY_SECONDS = TERMINAL_START_WAIT_SECONDS
# Case-insensitive, with ^ and $ at every line of the output.
_PATTERN_FLAGS = re.IGNORECASE | re.MULTILINE
TERMINAL_KEYS = tuple(TERMINAL_INPUT_KEY_SEQUENCES)
# Terminals named in errors, and the label length for each.
_LISTED_TERMINALS = 3
# A matched line longer than this is cut.
_MATCHED_LINE_CHARS = 500
# An argument an error names is cut to this many characters.
_ARGUMENT_CHARS = 80
_TERMINAL_LABEL_CHARS = 40
# A list row shows this much of the program's first line.
_PROGRAM_CHARS = 80
_FINISHED_MINUTES = int(TERMINAL_FINISHED_TTL.total_seconds() // 60)
_LOG_RETENTION_HOURS = int(
    TEMPORARY_FILE_RETENTION[TERMINAL_TEMPORARY_CATEGORY].total_seconds() // 3600
)
_SHELL_NAME = model_tool_name(BASH_TOOL_NAME)


def _terminal_description(*, shell: str | None) -> str:
    """The description for one request: the shell Tool's name when offered."""
    sentences = [
        "Start and operate interactive programs you drive by typing, such as REPLs, TUIs and "
        "coding-agent CLIs."
    ]
    if shell:
        sentences.append(f"Run all other commands, servers included, with {shell}.")
    sentences.append("A program keeps running after your turn ends, until it exits or is stopped.")
    sentences.append(
        "In screens, ⟦ and ⟧ mark reverse-video text, such as a selection or a drawn cursor, "
        "and ⟪ and ⟫ mark faint suggestion text after the cursor, which Enter does not submit."
    )
    return " ".join(sentences)


TERMINAL_TOOL_DESCRIPTION = _terminal_description(shell=_SHELL_NAME)

TERMINAL_TOOL_PARAMETERS: JsonObject = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": list(TERMINAL_ACTIONS),
            "description": (
                "start launches a program attached to this Session and returns its first "
                "screen; status shows a terminal's screen; wait waits until the program exits, "
                "prints output matching pattern, or the timeout passes; without pattern, it "
                "also ends when the program goes idle showing something new; input types text "
                "and keys; resize sets columns and rows, returning the screen; kill stops "
                "the program and everything it started; attach takes over an unattached "
                "terminal; detach releases it for another Session."
            ),
        },
        "terminal_id": {
            "type": "string",
            "description": ("From start or list; required except for start and list."),
        },
        "command": {
            "type": "string",
            "description": (
                "Program to start, without shell expansion. Omit to start the user's default shell."
            ),
        },
        "args": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Arguments for command. Omit for none.",
        },
        "workdir": {
            "type": "string",
            "description": (
                "Directory to start in, absolute or relative to the working directory. Omit to "
                "use the working directory."
            ),
        },
        "text": {
            "type": "string",
            "maxLength": TERMINAL_INPUT_MAX_CHARS,
            "description": (
                'For input, text to type; add key "enter" to submit it. Each line break '
                "presses Enter; when the program accepts bracketed paste, multiline text is "
                "pasted as one block and only a final line break presses Enter."
            ),
        },
        "key": {
            "type": "string",
            "description": (f"Named key for input, pressed after text: {TERMINAL_KEY_SUMMARY}."),
        },
        "data": {
            "type": "string",
            "maxLength": TERMINAL_INPUT_MAX_CHARS,
            "description": (
                "Exact characters for input, including control sequences, only for what text "
                "and key cannot express."
            ),
        },
        # The bounds are checked, not advertised: refusals name them.
        "columns": {
            "type": "integer",
            "description": f"Omit on start for {TERMINAL_DEFAULT_COLUMNS}.",
        },
        "rows": {"type": "integer", "description": f"Omit on start for {TERMINAL_DEFAULT_ROWS}."},
        "lines": {
            "type": "integer",
            "minimum": 1,
            "maximum": TERMINAL_STATUS_MAX_LINES,
            "default": TERMINAL_STATUS_DEFAULT_LINES,
            "description": "History lines status shows above the screen.",
        },
        "pattern": {
            "type": "string",
            "description": (
                "For wait: a case-insensitive regular expression (Python syntax), matched line "
                "by line; the wait ends when the output matches it, such as a prompt. Output "
                "printed before the call counts, except output before your last input and the "
                "echo of that input. Omit to wait for the exit, or for the program to go idle."
            ),
        },
        "timeout": {
            "type": "number",
            "minimum": 0,
            "default": TERMINAL_WAIT_DEFAULT_SECONDS,
            "description": (
                f"For wait: the longest wait in seconds, at most {TERMINAL_WAIT_MAX_SECONDS}; "
                f"0 for {TERMINAL_WAIT_MAX_SECONDS}. The program keeps running when the wait "
                "ends."
            ),
        },
    },
    "required": ["action"],
}
# Accepted and validated, never advertised: milliseconds from other harnesses,
# history paging the results address themselves, and the screen guard and wait
# baseline the terminal tracks itself.
TERMINAL_UNADVERTISED_PARAMETERS: JsonObject = {
    "timeout_ms": {"type": "number", "minimum": 0},
    "start_line": {"type": "integer", "minimum": 0},
    "expected_screen_revision": {"type": "integer", "minimum": 0},
    "after_revision": {"type": "integer", "minimum": 0},
    "group": {"type": "string"},
    "name": {"type": "string"},
}
