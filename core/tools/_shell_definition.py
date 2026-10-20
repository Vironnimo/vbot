"""Shell Tool definition and granted-credential prompt contract."""

from __future__ import annotations

import sys
from collections.abc import Sequence
from typing import Any

from core.tools._powershell import (
    PowerShellCulture,
    powershell_culture,
)
from core.tools.availability import bash_allowed_env_keys
from core.tools.model_names import SHELL_MODEL_NAME
from core.tools.tools import (
    JsonObject,
)

# How long a call waits for its command when it names no timeout, and at most.
SHELL_DEFAULT_WAIT_SECONDS = 120.0
SHELL_MAX_WAIT_SECONDS = 600.0


# Definition


if sys.platform == "win32":
    _OPENING = (
        "Run a PowerShell 7 command in a new process and return its output and exit code. "
        "Write PowerShell, not bash or cmd: $env:NAME for environment variables, $null instead "
        "of /dev/null, Select-String instead of grep for command output, and single quotes or a "
        "here-string (@'...'@) instead of \\\" escapes and heredocs."
    )
    # How Agents start a program in the background without the Tool.
    _DETACHED_START = "Start-Process or a trailing &"
    # How Agents cut a command's output themselves.
    _TRIMMING = "Select-Object -First or -Last, or with head and tail, which PowerShell lacks"
else:
    _OPENING = (
        "Run a bash command in a new process and return its output and exit code. Use bash syntax."
    )
    _DETACHED_START = "nohup or a trailing &"
    _TRIMMING = "head or tail"


def _culture_sentence(culture: PowerShellCulture | None) -> str:
    """Name the user's format for numbers and dates where it is not the one Models assume."""
    if culture is None or culture.name in ("", "en-US"):
        return ""
    sentence = (
        f" PowerShell prints and parses numbers and dates in the user's {culture.name} format, "
        f"such as {culture.number} and {culture.date}."
    )
    if culture.number != "3.14":
        # String expansion and casts use the invariant culture, these do not.
        sentence += (
            " Numbers from -f, -join, .ToString(), Export-Csv and file output use it too; for "
            'data that programs read, use "$x" or [cultureinfo]::InvariantCulture, which give '
            "3.14."
        )
    return sentence


# The same for every Tool set: what happens to a long command, and the calls
# that follow it up, the results say when it happens. The culture is read once
# per process, so the definition stays the same for the Provider's cache.
SHELL_TOOL_DESCRIPTION = (
    f"{_OPENING}{_culture_sentence(powershell_culture())} Long output keeps its first and last "
    f"lines, and the result names a file with all of it, so there is no need to trim it with "
    f"{_TRIMMING}. Calls in one message run at the same time; chain dependent commands in one "
    "call. When another tool covers the task, use it instead of a shell command."
)

_COMMAND_PARAMETER: JsonObject = {"type": "string", "minLength": 1}
_DESCRIPTION_PARAMETER: JsonObject = {
    "type": "string",
    "description": (
        "Title of what the command does, in 3–5 words, shown to the user instead of the "
        "command. Send one with every call."
    ),
}
_WORKDIR_PARAMETER: JsonObject = {
    "type": "string",
    "description": (
        "Directory to run in, absolute or relative to the working directory. Omit to use the "
        "working directory."
    ),
}
# No schema maximum: the handler caps the wait, and some Provider adapters
# reject extra keywords.
_TIMEOUT_PARAMETER: JsonObject = {
    "type": "number",
    "minimum": 0,
    "default": int(SHELL_DEFAULT_WAIT_SECONDS),
    "description": (
        "Seconds to wait for the command before it continues in the background, at most "
        f"{SHELL_MAX_WAIT_SECONDS:g}. Omit for {SHELL_DEFAULT_WAIT_SECONDS:g}."
    ),
}
_RUN_IN_BACKGROUND_PARAMETER: JsonObject = {
    "type": "boolean",
    "description": (
        "Use for servers, watchers and other commands whose result your next step does not "
        f"need, instead of {_DETACHED_START}. Omit to wait for the command."
    ),
}
_ENV_KEYS_PARAMETER: JsonObject = {
    "type": "array",
    "description": (
        "Exact names of granted environment credentials to make available to the command. "
        "Omit when no credential is needed."
    ),
    "items": {"type": "string", "minLength": 1},
    "minItems": 1,
    "uniqueItems": True,
}

SHELL_TOOL_PARAMETERS: JsonObject = {
    "type": "object",
    "properties": {
        "command": _COMMAND_PARAMETER,
        "description": _DESCRIPTION_PARAMETER,
        "workdir": _WORKDIR_PARAMETER,
        "timeout": _TIMEOUT_PARAMETER,
        "run_in_background": _RUN_IN_BACKGROUND_PARAMETER,
        "env_keys": _ENV_KEYS_PARAMETER,
    },
    "required": ["command"],
}


# Granted credentials


def format_shell_env_usage(env_keys: Sequence[str], *, intro: str) -> str:
    """Render the shared model-facing contract for granted shell credentials."""
    keys = list(dict.fromkeys(env_keys))
    if not keys:
        return ""
    key_lines = "\n".join(f"- `{key}`" for key in keys)
    return (
        f"{intro}\n\n"
        f"Available environment keys:\n{key_lines}\n\n"
        f"To use one, include its exact name in the `env_keys` array of every "
        f"`{SHELL_MODEL_NAME}` call "
        "that needs it. vBot resolves the value server-side and injects it only into that "
        "process environment; put the name, never the credential value, in the Tool call. "
        "Refer to the variable with the current host shell's environment syntax and do not "
        "print or otherwise expose its value."
    )


def _render_env_prompt_block(context: Any) -> str:
    env_keys = bash_allowed_env_keys(getattr(context.agent, "tools", None))
    if not env_keys:
        return ""
    guidance = format_shell_env_usage(
        env_keys,
        intro="This Agent has permanent permission to use these credentials in shell commands.",
    )
    return f"## Shell Environment Access\n\n{guidance}"
