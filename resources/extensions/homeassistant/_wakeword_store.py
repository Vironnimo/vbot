"""The Home Assistant Extension's operator-configured, fixed wakeword calls."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from core.config_validation import (
    JsonDiagnostic,
    JsonValidationReport,
    add_error,
    load_validated_json_file,
    validate_json_file,
)
from core.extensions.operations import ExtensionOperationError
from core.json_documents import (
    JsonDocumentFormat,
    json_document,
    json_list,
    json_object,
    strip_unknown_fields,
    validate_collection_root,
    warn_unknown_fields,
    write_json_document,
)
from core.tools.call_syntax import same_json_value

type Command = dict[str, Any]

FILENAME = "wakeword-commands.json"
COMMAND_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "phrase": {"type": "string", "minLength": 1},
        "aliases": {"type": "array", "items": {"type": "string", "minLength": 1}},
        "tool": {
            "enum": ["ha_call_service", "ha_get_state", "ha_list_entities", "ha_list_services"]
        },
        "arguments": {"type": "object"},
    },
    "required": ["phrase", "aliases", "tool", "arguments"],
    "additionalProperties": False,
}
_VALIDATOR = Draft202012Validator(COMMAND_SCHEMA)
_ROW = json_object(COMMAND_SCHEMA["properties"])
_SHAPE = json_document({"commands"}, {"commands": json_list(_ROW, key="phrase")})


def wording(text: str) -> str:
    """Ignore case, repeated whitespace and sentence endings that preserve intent."""
    return " ".join(text.casefold().split()).rstrip(".!,… ")


def _wording_error(commands: list[Command]) -> str | None:
    seen: dict[str, int] = {}
    for index, command in enumerate(commands, start=1):
        for phrase in (command["phrase"], *command["aliases"]):
            key = wording(phrase)
            if not key.rstrip("? "):
                return f"Command {index} has an empty phrase or alias."
            if key in seen:
                return f"Command {index} repeats a phrase or alias from command {seen[key]}."
            seen[key] = index
    return None


def _validate_document(data: Any) -> list[JsonDiagnostic]:
    diagnostics: list[JsonDiagnostic] = []
    entries = validate_collection_root(
        diagnostics, data, version=1, shape=_SHAPE, collection="commands"
    )
    if entries is None:
        return diagnostics
    commands: list[Command] = []
    for index, entry in enumerate(entries):
        path = f"$.commands[{index}]"
        warn_unknown_fields(diagnostics, path, entry, _ROW)
        row = strip_unknown_fields(entry, _ROW)
        issue = next(_VALIDATOR.iter_errors(row), None)
        if issue is not None:
            field = ".".join(map(str, issue.path))
            add_error(diagnostics, f"{path}.{field}" if field else path, "Invalid wakeword command")
        else:
            commands.append(row)
    conflict = _wording_error(commands)
    if conflict:
        add_error(diagnostics, "$.commands", conflict)
    return diagnostics


_FORMAT = JsonDocumentFormat("Home Assistant wakeword commands", 1, _SHAPE, _validate_document)


def validate_commands_file(path: str | Path) -> JsonValidationReport:
    return validate_json_file(path, _validate_document, missing_ok=True)


def _rows(path: Path) -> list[Command]:
    data = load_validated_json_file(path, _validate_document, missing_ok=True)
    return data["commands"] if data is not None else []


def read_commands(path: Path) -> list[Command]:
    return [strip_unknown_fields(row, _ROW) for row in _rows(path)]


def edit_command(path: Path, previous: Command | None, command: Command | None) -> list[Command]:
    """A single-worker read/compare/write. Keep unknown document and row fields on edit."""
    rows = _rows(path)
    if previous is None:
        if command is None:
            raise ValueError("Supply a command to add or a previous command to remove.")
        rows.append(command)
    else:
        index = next(
            (
                i
                for i, row in enumerate(rows)
                if same_json_value(strip_unknown_fields(row, _ROW), previous)
            ),
            None,
        )
        if index is None:
            raise ExtensionOperationError(
                "wakeword_command_conflict", "This command changed. Reload it and try again."
            )
        if command is None:
            del rows[index]
        else:
            rows[index] = {**rows[index], **command}
    conflict = _wording_error(rows)
    if conflict:
        raise ExtensionOperationError("wakeword_command_conflict", conflict)
    if not same_json_value(previous, command):
        write_json_document(path, {"commands": rows}, _FORMAT, owner_only=True)
    return [strip_unknown_fields(row, _ROW) for row in rows]
