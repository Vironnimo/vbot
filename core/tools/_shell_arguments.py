"""Shell Tool argument dialects and the env object.

Agents trained on other harnesses call the shell Tool with their own shapes:
Codex argv arrays and ``timeout_ms``, OpenAI ``local_shell`` action objects,
Claude Code ``run_in_background``, Gemini ``directory``, Cursor
``is_background``, Hermes and OpenClaw ``background``/``env``/``pty``. The
normalizer maps every shape whose intent is exact onto the advertised fields
and fails, before anything runs, when the call asks for an effect the Tool
cannot provide.
"""

from __future__ import annotations

import json
import re
import shlex
import sys
from collections.abc import Mapping
from functools import cache
from pathlib import PurePath
from typing import Any

from core.tools._argument_repair import normalize_call_arguments
from core.tools._call_vocabulary import SpellingAliases, spelling
from core.tools.contracts import ToolContract, compile_tool_contract
from core.tools.model_names import BASH_TOOL_NAME, SHELL_MODEL_NAME
from core.tools.tools import JsonObject, ToolDisplayPart

TIMEOUT_MS_PARAMETER: JsonObject = {"type": "number", "minimum": 0}
ENV_PARAMETER: JsonObject = {"type": "object"}
# Accepted and validated, never advertised: other harnesses' spellings.
SHELL_UNADVERTISED_PARAMETERS: JsonObject = {
    "timeout_ms": TIMEOUT_MS_PARAMETER,
    "env": ENV_PARAMETER,
}
# Values at or above this size that are whole thousands are milliseconds.
MILLISECOND_TIMEOUT_THRESHOLD = 10_000


_FIELD_ALIASES = SpellingAliases(
    {
        "command": ("cmd", "cmdline", "command_line", "script", "shell_command", "argv"),
        "workdir": ("cwd", "dir", "directory", "working_directory", "working_dir"),
        "timeout": ("timeout_seconds", "timeout_secs", "timeout_sec", "timeout_s"),
        "timeout_ms": ("timeout_millis", "timeout_milliseconds"),
    }
)
_MODE_VALUES = {
    **dict.fromkeys(
        ("foreground", "fg", "front", "fore", "auto", "sync", "blocking", "wait"), "foreground"
    ),
    **dict.fromkeys(("background", "bg", "back", "async", "detach", "detached"), "background"),
}
_BACKGROUND_FLAGS = frozenset({"background", "runinbackground", "isbackground"})
# Seconds or milliseconds before a running command returns; zero asks for background now.
_YIELD_FIELDS = frozenset({"yieldms", "yieldtimems", "yieldafter", "backgroundafterseconds"})
_DESCRIPTION_FIELDS = frozenset({"explanation", "justification"})
# Requests vBot always satisfies or that only shape output: nothing to do.
_SATISFIED_FIELDS = frozenset(
    {"notifyoncomplete", "login", "maxoutputtokens", "dangerouslydisablesandbox"}
)
_PTY_FIELDS = frozenset({"pty", "tty"})
_ELEVATION_FIELDS = frozenset({"elevated", "withescalatedpermissions"})
_POWERSHELL_WRAPPER_OPTIONS = frozenset(
    {"-noprofile", "-nop", "-nologo", "-noninteractive", "-noni"}
)
_POWERSHELL_COMMAND_OPTIONS = frozenset({"-command", "-c"})
_BASH_WRAPPER_OPTION = re.compile(r"-(?:l?c|cl|l)|--login")
# PowerShell reads numeric-looking bare words as numbers (0x10 becomes 16), so
# only words that start like a name, path, or flag stay unquoted.
_PWSH_BARE_ARGUMENT = re.compile(r"(?:[^\W\d]|[/\\]|\.{1,2}[/\\]|--?[^\W\d])[\w./\\:=+-]*")
_CREDENTIAL_NAME_PARTS = frozenset(
    {
        "KEY",
        "KEYS",
        "APIKEY",
        "TOKEN",
        "TOKENS",
        "SECRET",
        "SECRETS",
        "PASSWORD",
        "PASSWORDS",
        "PASSWD",
        "CREDENTIAL",
        "CREDENTIALS",
    }
)


@cache
def _repair_contract() -> ToolContract:
    # Imported lazily: bash.py imports this module for its registration.
    from core.tools.bash import BASH_TOOL_PARAMETERS

    schema = {
        **BASH_TOOL_PARAMETERS,
        "properties": {**BASH_TOOL_PARAMETERS["properties"], **SHELL_UNADVERTISED_PARAMETERS},
    }
    return compile_tool_contract(
        name=BASH_TOOL_NAME, input_schema=schema, require_closed_input=False
    )


def normalize_shell_arguments(arguments: Any) -> Any:
    """Map one shell call onto command, workdir, timeout, mode, env_keys, and env."""
    if isinstance(arguments, dict):
        arguments = _lift_command_list(arguments)
    normalized = normalize_call_arguments(
        _repair_contract(),
        arguments,
        field_aliases=_FIELD_ALIASES,
        field_normalizers={"command": _command_text, "mode": _mode_value},
        empty_as_omitted=("command", "workdir", "description", "mode"),
    )
    if not isinstance(normalized, dict):
        return normalized
    wrapped = normalized.pop("action", None)
    if isinstance(wrapped, dict):
        # OpenAI local_shell: {"action": {"type": "exec", "command": [...], ...}}.
        inner = {key: value for key, value in wrapped.items() if spelling(key) != "type"}
        for key, value in normalize_shell_arguments(inner).items():
            if key in normalized and normalized[key] != value:
                raise ValueError(_not_run(f"{key} is given twice with different values."))
            normalized[key] = value
    elif wrapped is not None:
        normalized["action"] = wrapped
    if normalized.get("env_keys") == []:
        del normalized["env_keys"]
    elif isinstance(normalized.get("env_keys"), str):
        normalized["env_keys"] = [normalized["env_keys"]]
    _translate_foreign_fields(normalized)
    return normalized


def _lift_command_list(arguments: dict[str, Any]) -> dict[str, Any]:
    """A ``commands`` list is a sequence of shell commands, unlike an argv array."""
    lifted = dict(arguments)
    for key in list(lifted):
        if spelling(key) != "commands":
            continue
        value = lifted.pop(key)
        if isinstance(value, list) and all(isinstance(item, str) for item in value):
            value = "\n".join(item for item in value if item.strip())
        existing = lifted.get("command")
        if existing not in (None, "", value):
            raise ValueError(_not_run("command and commands are both given; send one."))
        lifted["command"] = value
    return lifted


def _command_text(value: Any) -> Any:
    """Return an argv array as one command line for the host shell."""
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return value
    if not value:
        return None
    if len(value) == 1:
        return value[0]
    script = _host_shell_script(value)
    if script is not None:
        return script
    if sys.platform == "win32":
        return _powershell_command_line(value)
    return shlex.join(value)


def _host_shell_script(argv: list[str]) -> str | None:
    """The script of an argv that only starts the host shell on it, such as bash -lc."""
    program = PurePath(argv[0].replace("\\", "/")).name.casefold()
    options = [option.casefold() for option in argv[1:-1]]
    if sys.platform == "win32":
        if program not in {"pwsh", "pwsh.exe"} or not options:
            return None
        if options[-1] not in _POWERSHELL_COMMAND_OPTIONS:
            return None
        if not all(option in _POWERSHELL_WRAPPER_OPTIONS for option in options[:-1]):
            return None
        return argv[-1]
    if program != "bash" or not options or "c" not in options[-1]:
        return None
    if not all(_BASH_WRAPPER_OPTION.fullmatch(option) for option in options):
        return None
    return argv[-1]


def _powershell_command_line(argv: list[str]) -> str:
    """Quote argv for PowerShell 7, which passes each quoted token as one argument."""
    tokens = [
        item if _PWSH_BARE_ARGUMENT.fullmatch(item) else "'" + item.replace("'", "''") + "'"
        for item in argv
    ]
    if tokens[0].startswith("'"):
        tokens.insert(0, "&")
    return " ".join(tokens)


def _mode_value(value: Any) -> Any:
    if isinstance(value, str):
        return _MODE_VALUES.get(spelling(value), value)
    return value


def _flag(key: str, value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().casefold() in {"true", "false"}:
        return value.strip().casefold() == "true"
    if value in (0, 1) and not isinstance(value, float):
        return bool(value)
    raise ValueError(_not_run(f"{key} must be true or false."))


def _translate_foreign_fields(arguments: dict[str, Any]) -> None:
    for key in list(arguments):
        word = spelling(key)
        if word in _BACKGROUND_FLAGS:
            wanted = "background" if _flag(key, arguments.pop(key)) else "foreground"
            _set_mode(arguments, wanted, key)
        elif word in _YIELD_FIELDS:
            if arguments.pop(key) == 0:
                _set_mode(arguments, "background", key)
        elif word in _DESCRIPTION_FIELDS:
            text = arguments.pop(key)
            if isinstance(text, str) and text.strip() and not arguments.get("description"):
                arguments["description"] = text
        elif word in _SATISFIED_FIELDS:
            del arguments[key]
        elif word in _PTY_FIELDS:
            if _flag(key, arguments.pop(key)):
                raise ValueError(
                    _not_run(
                        f"it has no terminal, so {key}: true cannot be honored. Run "
                        "interactive programs with the terminal Tool if you have it; "
                        f"otherwise call again without {key}."
                    )
                )
        elif word in _ELEVATION_FIELDS or word == "sandboxpermissions":
            value = arguments.pop(key)
            if word == "sandboxpermissions":
                elevated = value not in (None, "", "use_default")
            else:
                elevated = _flag(key, value)
            if elevated:
                raise ValueError(
                    _not_run(
                        "it cannot raise permissions. Call again without "
                        f"{key}; if the command needs elevated rights, ask the user to run it."
                    )
                )
        elif word == "requireuserapproval":
            if _flag(key, arguments.pop(key)):
                raise ValueError(
                    _not_run(
                        "it cannot ask the user for approval. Ask the user in your reply "
                        "first, then call again without require_user_approval."
                    )
                )
        elif word == "user":
            if arguments.pop(key) not in (None, ""):
                raise ValueError(
                    _not_run(f"commands run as the vBot user; call again without {key}.")
                )


def _set_mode(arguments: dict[str, Any], wanted: str, source: str) -> None:
    current = arguments.get("mode")
    if current not in (None, wanted):
        raise ValueError(
            _not_run(f'mode is "{current}" but {source} asks for {wanted}; send one of them.')
        )
    arguments["mode"] = wanted


def resolve_timeout(
    timeout: float | None, timeout_ms: float | None, *, tool_name: str = SHELL_MODEL_NAME
) -> tuple[float | None, str | None]:
    """Return the timeout in seconds and a note when a value was read as milliseconds."""
    if timeout_ms is not None:
        seconds = timeout_ms / 1000
        if timeout is not None and timeout not in (seconds, timeout_ms):
            raise ValueError(
                f"{tool_name} was not run: timeout ({timeout:g} s) and timeout_ms "
                f"({timeout_ms:g} ms) disagree; send one of them."
            )
        return seconds, None
    if timeout is not None and timeout >= MILLISECOND_TIMEOUT_THRESHOLD and timeout % 1000 == 0:
        seconds = timeout / 1000
        return seconds, (
            f"timeout {timeout:g} was read as milliseconds ({seconds:g} s); timeout takes seconds."
        )
    return timeout, None


def split_env_object(
    env: Mapping[str, Any] | None, granted: frozenset[str]
) -> tuple[dict[str, str], list[str]]:
    """Split an env object into plain variables and granted credential names.

    A granted credential named with an empty value or a reference to itself
    becomes a credential request; any credential value written in the call is
    refused, so a secret never travels through Tool arguments into a process.
    """
    variables: dict[str, str] = {}
    credentials: list[str] = []
    for name, raw in (env or {}).items():
        if not isinstance(name, str) or not name or "=" in name or "\x00" in name:
            raise ValueError(_not_run(f"env has an invalid variable name: {name!r}."))
        if name.upper().startswith("VBOT_"):
            raise ValueError(_not_run(f"env cannot set {name}; vBot sets VBOT_ variables itself."))
        if isinstance(raw, bool):
            value = json.dumps(raw)
        elif isinstance(raw, (str, int, float)):
            value = str(raw)
        else:
            raise ValueError(_not_run(f"env value for {name} must be a string."))
        if _references(value, name):
            if name in granted:
                credentials.append(name)
            continue
        if value == "" and name in granted:
            credentials.append(name)
            continue
        if name in granted or _credential_name(name):
            raise ValueError(_literal_credential_message(name, granted))
        variables[name] = value
    return variables, credentials


def _references(value: str, name: str) -> bool:
    forms = {f"${name}", f"${{{name}}}", f"$env:{name}", f"${{env:{name}}}", f"%{name}%"}
    return value.strip().casefold() in {form.casefold() for form in forms}


def _credential_name(name: str) -> bool:
    return any(part in _CREDENTIAL_NAME_PARTS for part in re.split(r"[_\-.]+", name.upper()))


def _literal_credential_message(name: str, granted: frozenset[str]) -> str:
    message = (
        f"env sets {name} to a value written in the call, and vBot does not pass credential "
        "values written in Tool calls."
    )
    if name in granted:
        return _not_run(
            f'{message} {name} is a granted credential: pass env_keys: ["{name}"] and leave '
            "it out of env."
        )
    available = (
        f"Granted credentials for env_keys: {', '.join(sorted(granted))}."
        if granted
        else "No credentials are granted to this Agent; ask the user if the command needs one."
    )
    return _not_run(
        f"{message} {available} If {name} is not a secret, set it inside the command instead."
    )


def unknown_env_keys_message(names: list[str], granted: frozenset[str]) -> str:
    """Name the env_keys that are neither granted nor set in the command environment."""
    listed = ", ".join(names)
    subject = f"env_keys names {listed}, which {'is' if len(names) == 1 else 'are'}"
    if granted:
        return _not_run(
            f"{subject} not granted to this Agent. Granted credentials: "
            f"{', '.join(sorted(granted))}. Call again with only those names, or omit env_keys."
        )
    return _not_run(
        f"{subject} not granted to this Agent, and no credentials are granted. Omit env_keys; "
        "if the command needs a credential, ask the user to grant it."
    )


def inherited_env_keys_note(names: list[str]) -> str:
    listed = ", ".join(names)
    verb = "is" if len(names) == 1 else "are"
    return (
        f"{listed} {verb} not a granted credential, so the command sees the value it "
        "inherits; env_keys is only for granted credentials."
    )


def shell_display_parts(arguments: JsonObject) -> tuple[ToolDisplayPart, ...]:
    """Show the description, else the command, whatever dialect the call used."""
    try:
        normalized = normalize_shell_arguments(arguments)
    except ValueError:
        normalized = arguments
    if not isinstance(normalized, dict):
        return ()
    description = normalized.get("description")
    if isinstance(description, str) and description.strip():
        return (ToolDisplayPart(description.strip(), kind="description", quote=True),)
    command = normalized.get("command")
    if isinstance(command, str) and command.strip():
        return (ToolDisplayPart(command, kind="command"),)
    return ()


def _not_run(problem: str) -> str:
    return f"{SHELL_MODEL_NAME} was not run: {problem}"
