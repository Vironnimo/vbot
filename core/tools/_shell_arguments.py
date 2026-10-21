"""Shell Tool argument dialects and the env object.

Agents trained on other harnesses call the shell Tool with their own shapes:
Codex argv arrays and ``timeout_ms``, OpenAI ``local_shell`` action objects,
vBot's earlier ``mode``, Gemini ``directory``, Cursor ``is_background``, Hermes
and OpenClaw ``background``/``env``/``pty``. The normalizer maps every shape
whose intent is exact onto the advertised fields and fails, before anything
runs, when the call asks for an effect the Tool cannot provide. Requests for a
terminal, for input or for a yield time cannot be met either, since a command
gets no input and has no terminal; the command runs, and the result notes that
the field had no effect (``NO_EFFECT_FIELDS``, ``no_effect_notes``).
"""

from __future__ import annotations

import json
import re
import shlex
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from functools import cache
from pathlib import Path, PurePath
from typing import Any

from core.projects import ProjectStore
from core.secrets.agent_secrets import REFERENCE_PATTERN, has_reference
from core.tools._path_suggestions import similar_entries
from core.tools._powershell import (
    powershell_literal,
)
from core.tools._workdir import ProjectWorkdirError, is_project_workdir, project_workdir
from core.tools.arguments import optional_number, optional_string
from core.tools.availability import bash_allowed_env_keys, normalize_env_keys
from core.tools.call_syntax import SpellingAliases, normalize_call_arguments, spelling
from core.tools.contracts import ToolContract, ToolContractError, compile_tool_contract
from core.tools.model_names import BASH_TOOL_NAME, SHELL_MODEL_NAME
from core.tools.shell_environment import (
    inherited_value,
)
from core.tools.tools import (
    JsonObject,
    ToolContext,
    ToolDisplayPart,
)
from core.utils.file_status import is_dir_strict
from core.utils.paths import model_path

from ._shell_definition import (
    SHELL_DEFAULT_WAIT_SECONDS,
    SHELL_MAX_WAIT_SECONDS,
)

TIMEOUT_MS_PARAMETER: JsonObject = {"type": "number", "minimum": 0}
ENV_PARAMETER: JsonObject = {"type": "object"}
# The fields of the call that cannot have an effect, by the key the call used:
# ``terminal`` (a terminal asked for), ``input`` (input text), ``wait`` (a yield
# time). Set by the normalizer only; the result notes each one.
NO_EFFECT_FIELDS = "no_effect_fields"
_NO_EFFECT_KINDS = ("terminal", "input", "wait")
# Accepted and validated, never advertised: other harnesses' spellings.
SHELL_UNADVERTISED_PARAMETERS: JsonObject = {
    "timeout_ms": TIMEOUT_MS_PARAMETER,
    "env": ENV_PARAMETER,
    "mode": {"type": "string"},
    NO_EFFECT_FIELDS: {
        "type": "object",
        "additionalProperties": {"type": "string", "enum": list(_NO_EFFECT_KINDS)},
    },
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
_BACKGROUND_FLAGS = frozenset({"background", "isbackground"})
# Seconds or milliseconds before a running command returns; zero asks for background now.
_YIELD_FIELDS = frozenset({"yieldms", "yieldtimems", "yieldafter", "backgroundafterseconds"})
# A terminal for the command, and input text for it: commands have neither.
_TERMINAL_FIELDS = frozenset({"pty", "tty", "interactive"})
_INPUT_FIELDS = frozenset({"stdin", "input"})
_DESCRIPTION_FIELDS = frozenset({"explanation", "justification", "title", "label", "summary"})
# Requests vBot always satisfies or that only shape output: nothing to do.
_SATISFIED_FIELDS = frozenset(
    {"notifyoncomplete", "login", "maxoutputtokens", "dangerouslydisablesandbox"}
)
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
    # Imported lazily: shell.py imports this module for its registration.
    from core.tools.shell import SHELL_TOOL_PARAMETERS

    schema = {
        **SHELL_TOOL_PARAMETERS,
        "properties": {**SHELL_TOOL_PARAMETERS["properties"], **SHELL_UNADVERTISED_PARAMETERS},
    }
    return compile_tool_contract(
        name=BASH_TOOL_NAME, input_schema=schema, require_closed_input=False
    )


def normalize_shell_arguments(arguments: Any) -> Any:
    """Map one shell call onto command, workdir, timeout, run_in_background, env_keys, and env.

    Fields that cannot have an effect are collected under ``NO_EFFECT_FIELDS``.
    """
    if isinstance(arguments, dict):
        arguments = _lift_command_list(arguments)
    normalized = normalize_call_arguments(
        _repair_contract(),
        arguments,
        field_aliases=_FIELD_ALIASES,
        field_normalizers={"command": _command_text, "mode": _mode_value},
        empty_as_omitted=("command", "workdir", "description", "mode", "run_in_background"),
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
        item if _PWSH_BARE_ARGUMENT.fullmatch(item) else powershell_literal(item) for item in argv
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
    # Each field that asks for the background (True) or the foreground, as sent.
    requests: list[tuple[str, bool]] = []
    no_effect: dict[str, str] = dict(arguments.pop(NO_EFFECT_FIELDS, None) or {})
    for key in list(arguments):
        word = spelling(key)
        if key == "run_in_background":
            requests.append((key, _flag(key, arguments[key])))
        elif key == "mode":
            requests.append((key, _mode_request(arguments.pop(key))))
        elif word in _BACKGROUND_FLAGS:
            requests.append((key, _flag(key, arguments.pop(key))))
        elif word in _YIELD_FIELDS:
            value = arguments.pop(key)
            if _number(value) == 0:
                requests.append((key, True))
            elif value not in (None, "", False):
                no_effect[key] = "wait"
        elif word in _TERMINAL_FIELDS:
            if _flag(key, arguments.pop(key)):
                no_effect[key] = "terminal"
        elif word in _INPUT_FIELDS:
            if arguments.pop(key) not in (None, "", False, [], {}):
                no_effect[key] = "input"
        elif word in _DESCRIPTION_FIELDS:
            text = arguments.pop(key)
            if isinstance(text, str) and text.strip() and not arguments.get("description"):
                arguments["description"] = text
        elif word in _SATISFIED_FIELDS:
            del arguments[key]
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
        elif word == "user" and arguments.pop(key) not in (None, ""):
            raise ValueError(_not_run(f"commands run as the vBot user; call again without {key}."))

    if requests:
        arguments["run_in_background"] = _background_request(requests)
    if no_effect:
        arguments[NO_EFFECT_FIELDS] = no_effect


def _mode_request(mode: Any) -> bool:
    """Whether ``mode`` asks for the background; any other value is refused."""
    if mode not in ("foreground", "background"):
        raise ValueError(
            _not_run(
                f"mode {mode!r} is not a mode. Pass run_in_background: true to run the command "
                "in the background, or omit both to wait for it."
            )
        )
    return bool(mode == "background")


def _background_request(requests: list[tuple[str, bool]]) -> bool:
    """The one answer of every field that asks for the background or the foreground.

    ``run_in_background`` is the reference when it was sent, else the first
    such field; a field that asks for the other is refused.
    """
    reference = next(
        (request for request in requests if request[0] == "run_in_background"), requests[0]
    )
    for key, background in requests:
        if background == reference[1]:
            continue
        wanted = "background" if background else "foreground"
        if reference[0] == "run_in_background":
            sent = json.dumps(reference[1])
            raise ValueError(
                _not_run(
                    f"run_in_background is {sent} but {key} asks for {wanted}; send one of them."
                )
            )
        first = "background" if reference[1] else "foreground"
        raise ValueError(
            _not_run(
                f"{reference[0]} asks for {first} but {key} asks for {wanted}; send one of them."
            )
        )
    return reference[1]


def _number(value: Any) -> float | None:
    """*value* as a number: a JSON number or a numeric text; None otherwise."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def no_effect_notes(fields: Mapping[str, str], *, terminal: bool) -> list[str]:
    """The notes for the fields of a call that cannot have an effect, in call order.

    *fields* maps each key as the call sent it to its kind (``NO_EFFECT_FIELDS``);
    *terminal* tells whether the Agent can call the terminal Tool.
    """
    notes: list[str] = []
    for key, kind in fields.items():
        if kind == "terminal":
            note = f"{key} has no effect: commands run without a terminal and get no input."
            if terminal:
                note += " Run a program that needs a terminal or input with terminal."
        elif kind == "input":
            example = "'y' | program" if sys.platform == "win32" else "printf 'y\\n' | program"
            note = (
                f"{key} has no effect: commands get no input. Pipe the text into the program "
                f"inside the command instead, such as {example}."
            )
        else:
            note = (
                f"{key} has no effect: set how long the call waits with timeout, or return at "
                "once with run_in_background."
            )
        notes.append(note)
    return notes


def resolve_timeout(
    timeout: float | None, timeout_ms: float | None, *, tool_name: str = SHELL_MODEL_NAME
) -> tuple[float | None, str | None]:
    """Return the timeout in seconds and a note when a value was read as milliseconds.

    Raises ``ToolContractError`` when the two values disagree.
    """
    if timeout_ms is not None:
        seconds = timeout_ms / 1000
        if timeout is not None and timeout not in (seconds, timeout_ms):
            raise ToolContractError(
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
) -> tuple[dict[str, str], list[str], dict[str, str]]:
    """Split an env object into plain variables, credential names and Secret references.

    A granted credential named with an empty value or a reference to itself
    becomes a credential request; a value that is exactly a Secret reference
    (``{{secret:DB.password}}``) becomes that Secret's value; any credential
    value written in the call is refused, so a secret never travels through
    Tool arguments into a process.
    """
    variables: dict[str, str] = {}
    credentials: list[str] = []
    secret_references: dict[str, str] = {}
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
        if "\x00" in value:
            raise ValueError(
                _not_run(
                    f"env value for {name} contains a NUL character, which an environment "
                    "variable cannot hold. Pass the value without NUL characters; for binary "
                    "data, write it to a file in the command and pass the file's path in env "
                    "instead."
                )
            )
        if REFERENCE_PATTERN.fullmatch(value.strip()):
            secret_references[name] = value.strip()
            continue
        if has_reference(value):
            raise ValueError(_secret_in_text_message(f"env value for {name}"))
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
    return variables, credentials, secret_references


def _secret_in_text_message(place: str) -> str:
    return _not_run(
        f"{place} contains a Secret reference, and vBot puts Secret values only into a "
        'command environment. Pass the Secret name in env_keys (env_keys: ["NAME"]) and use '
        "its variable in the command, or set an env variable to the reference alone "
        '(env: {"VARIABLE": "{{secret:NAME.field}}"}).'
    )


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
    """Show the description, else the command, whatever dialect the call used.

    The command stays copyable either way: in its own value card, or as the
    detail of the description that stands in for it.
    """
    try:
        normalized = normalize_shell_arguments(arguments)
    except ValueError:
        normalized = arguments
    if not isinstance(normalized, dict):
        return ()
    description = normalized.get("description")
    raw_command = normalized.get("command")
    command = raw_command if isinstance(raw_command, str) and raw_command.strip() else None
    if isinstance(description, str) and description.strip():
        return (
            ToolDisplayPart(
                description.strip(),
                kind="description",
                quote=True,
                tooltip="always" if command else "truncated",
                copyable=command is not None,
                detail=command,
                detail_kind="command",
            ),
        )
    if command:
        return (ToolDisplayPart(command, kind="command", copyable=True),)
    return ()


def _not_run(problem: str) -> str:
    return f"{SHELL_MODEL_NAME} was not run: {problem}"


@dataclass(frozen=True)
class _ShellCall:
    command: str
    description: str | None
    workdir: Path
    # How long the call waits; *timeout_given* when the Agent chose it.
    wait_seconds: float
    timeout_given: bool
    background: bool
    variables: dict[str, str]
    credential_names: tuple[str, ...]
    notes: tuple[str, ...]
    # Agent Secrets: names from env_keys, and env variables set to a reference.
    secret_keys: tuple[str, ...] = ()
    secret_variables: tuple[tuple[str, str], ...] = ()


def _call_error(problem: str) -> ToolContractError:
    return ToolContractError(f"{SHELL_MODEL_NAME} was not run: {problem}")


def _parse_call(
    context: ToolContext,
    arguments: JsonObject,
    projects: ProjectStore | None,
    *,
    terminal_available: bool,
    secret_names: frozenset[str] = frozenset(),
) -> _ShellCall:
    """*secret_names* are the names ``env_keys`` accepts for the caller's Agent Secrets."""
    notes: list[str] = []
    command = arguments.get("command")
    if not isinstance(command, str) or not command.strip():
        raise _call_error("command is required; pass the command line to run as a string.")
    if has_reference(command):
        # Command lines reach process listings and shell history.
        raise ToolContractError(_secret_in_text_message("command"))
    description = optional_string(arguments.get("description"), field_name="description")
    background = arguments.get("run_in_background") is True
    timeout, note = resolve_timeout(
        optional_number(arguments.get("timeout"), field_name="timeout"),
        optional_number(arguments.get("timeout_ms"), field_name="timeout_ms"),
    )
    if note is not None:
        notes.append(note)
    if timeout is not None and timeout < 0:
        raise _call_error("timeout must be 0 or more seconds.")
    wait_seconds, timeout_given = SHELL_DEFAULT_WAIT_SECONDS, False
    # A call in the background does not wait: its timeout has nothing to bound.
    if timeout is not None and not background:
        if timeout == 0:
            notes.append(
                f"timeout 0 was read as omitted, so the call waits up to "
                f"{SHELL_DEFAULT_WAIT_SECONDS:g} seconds."
            )
        elif timeout > SHELL_MAX_WAIT_SECONDS:
            notes.append(
                f"timeout {timeout:g} is more than {SHELL_MAX_WAIT_SECONDS:g} seconds, the "
                f"longest a call waits, so it was read as {SHELL_MAX_WAIT_SECONDS:g}."
            )
            wait_seconds, timeout_given = SHELL_MAX_WAIT_SECONDS, True
        else:
            wait_seconds, timeout_given = timeout, True
    no_effect = arguments.get(NO_EFFECT_FIELDS)
    if isinstance(no_effect, dict):
        notes.extend(no_effect_notes(no_effect, terminal=terminal_available))

    workdir = _workdir(context, arguments, projects)
    context.record_worked_paths(workdir)

    granted = frozenset(bash_allowed_env_keys(context.tool_settings)) | frozenset(
        context.skill_env_keys
    )
    env = arguments.get("env")
    if env is not None and not isinstance(env, dict):
        raise _call_error("env must be an object of variable names and values.")
    try:
        variables, env_credentials, secret_references = split_env_object(env, granted)
    except ValueError as error:
        raise ToolContractError(str(error)) from error
    raw_keys = arguments.get("env_keys")
    try:
        requested = normalize_env_keys(raw_keys, field_name="env_keys") if raw_keys else []
    except ValueError as error:
        raise _call_error(f"env_keys is invalid: {error}") from error
    # A granted credential keeps its name; Secret names never collide with one.
    secret_keys = [
        name for name in requested if name not in granted and name.upper() in secret_names
    ]
    credential_names = [
        name for name in dict.fromkeys([*requested, *env_credentials]) if name not in secret_keys
    ]
    ungranted = [name for name in credential_names if name not in granted]
    if ungranted:
        unknown = [name for name in ungranted if inherited_value(name) is None]
        if unknown:
            raise ToolContractError(unknown_env_keys_message(unknown, granted))
        notes.append(inherited_env_keys_note(ungranted))
    return _ShellCall(
        command=command,
        description=description.strip() if description and description.strip() else None,
        workdir=workdir,
        wait_seconds=wait_seconds,
        timeout_given=timeout_given,
        background=background,
        variables=variables,
        credential_names=tuple(name for name in credential_names if name in granted),
        notes=tuple(notes),
        secret_keys=tuple(secret_keys),
        secret_variables=tuple(secret_references.items()),
    )


def _workdir(context: ToolContext, arguments: JsonObject, projects: ProjectStore | None) -> Path:
    raw = optional_string(arguments.get("workdir"), field_name="workdir")
    if not raw:
        workdir = context.effective_cwd
    elif is_project_workdir(raw):
        # Not advertised; the Projects block names each Project's directory path.
        try:
            return project_workdir(projects, raw)
        except ProjectWorkdirError as error:
            raise _call_error(str(error)) from error
    else:
        workdir = context.resolve_path(raw)
    try:
        directory = is_dir_strict(workdir)
    except OSError:
        # Launch owns host failures. Keep an unavailable path distinct from a
        # missing one, so the manager reports the actual filesystem failure.
        return workdir
    if not directory:
        raise _call_error(_missing_workdir(workdir, given=bool(raw), cwd=context.effective_cwd))
    return workdir


def _missing_workdir(workdir: Path, *, given: bool, cwd: Path) -> str:
    """The failure for a missing directory; *given* when the call named it as
    workdir, *cwd* the working directory a call without workdir uses.

    Its path lets the Agent pass a path relative to it: Agents guessed an
    absolute path, then ran the command without workdir in the wrong directory.
    """
    if not given:
        return (
            f"the working directory {model_path(workdir)} is not an existing directory. Pass an "
            "existing directory as workdir."
        )
    fallback = f"omit workdir to use the working directory {model_path(cwd)}."
    if workdir.exists():
        return (
            f"workdir {model_path(workdir)} is a file, not a directory. Pass the directory it "
            f"is in, or {fallback}"
        )
    message = f"workdir {model_path(workdir)} is not an existing directory."
    nearby = similar_entries(workdir, kind="dirs")
    if nearby:
        message += f" Similar directories: {', '.join(model_path(path) for path in nearby)}."
    return f"{message} Pass an existing directory, or {fallback}"
