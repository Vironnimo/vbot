"""Canonical Tool schema compilation and runtime validation."""

from __future__ import annotations

import copy
import difflib
import hashlib
import json
import re
import threading
from collections import OrderedDict
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError

from core.tools._argument_repair import decode_json_argument, normalize_schema_value
from core.tools._contract_error import ToolContractError
from core.tools.model_names import model_tool_name

JsonObject = dict[str, Any]
# The data field of a successful result that says how the call was read.
CALL_NOTE_FIELD = "note"
# A label some Models attach to a call; it requests nothing.
_REMARK = "commentary"
# The fields of a call sent as one name/value pair: {"name": "command", "value": "ls"}.
_PAIR_NAME = "name"
_PAIR_VALUE = "value"
# Words in the name of an unknown field that make ignoring it unsafe: the field
# reads as a mode, condition or safety switch of the call, or as a target or
# content of its own. The call without it could have an effect the Agent did not
# ask for: dryRun on edit would write for real, mode on write would overwrite
# instead of append, thread_id on channel_send would post to the channel.
_DECISIVE_WORDS = frozenset(
    {
        # The operation itself, modes, states, formats, conditions and safety switches.
        "action",
        "all",
        "allow",
        "append",
        "atomic",
        "backup",
        "base64",
        "binary",
        "check",
        "chmod",
        "condition",
        "confirm",
        "create",
        "delete",
        "draft",
        "dry",
        "dryrun",
        "encoded",
        "encoding",
        "escaped",
        "etag",
        "executable",
        "exist",
        "exists",
        "expect",
        "expected",
        "force",
        "if",
        "insert",
        "isolated",
        "isolation",
        "keep",
        "lock",
        "method",
        "mode",
        "no",
        "notification",
        "notify",
        "only",
        "op",
        "operation",
        "overwrite",
        "permission",
        "prepend",
        "preview",
        "private",
        "public",
        "raw",
        "recursive",
        "remove",
        "replace",
        "require",
        "revision",
        "safe",
        "sandbox",
        "sandboxed",
        "silent",
        "simulate",
        "skip",
        "state",
        "status",
        "strict",
        "unless",
        "validate",
        "verify",
        "version",
        "visibility",
        "whatif",
        "when",
        # Targets.
        "account",
        "address",
        "agent",
        "branch",
        "channel",
        "chat",
        "dest",
        "destination",
        "dir",
        "directory",
        "discussion",
        "email",
        "file",
        "folder",
        "id",
        "into",
        "key",
        "name",
        "page",
        "parent",
        "path",
        "post",
        "project",
        "recipient",
        "ref",
        "reply",
        "session",
        "target",
        "thread",
        "to",
        "uri",
        "url",
        "user",
        # Contents.
        "attachment",
        "body",
        "code",
        "command",
        "content",
        "data",
        "diff",
        "image",
        "input",
        "instruction",
        "message",
        "new",
        "old",
        "patch",
        "payload",
        "prompt",
        "replacement",
        "script",
        "text",
        "value",
    }
)
_NAME_WORDS = re.compile(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+")
# The longest text an unknown field can hold and still read as a label of the
# call ("support wiring", "Check working directory") rather than content.
_LABEL_LENGTH = 80
_TOOL_NAME_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
# Compiled contracts keyed by the digest of their exact JSON inputs. Chat compiles
# every Provider Tool definition per request; the definitions rarely change.
_CONTRACT_CACHE_LIMIT = 512
_CONTRACT_CACHE: OrderedDict[str, ToolContract] = OrderedDict()
_CONTRACT_CACHE_LOCK = threading.Lock()


@dataclass(frozen=True)
class ToolContract:
    """One compiled, Provider-neutral Tool contract."""

    name: str
    input_schema: JsonObject
    input_validator: Draft202012Validator
    result_schema: JsonObject | None
    result_validator: Draft202012Validator | None
    parallel_safe: bool
    schema_fingerprint: str

    def normalize_arguments(self, arguments: Any) -> Any:
        """Repair common unambiguous model encodings in a copied argument value.

        An empty value under a name that is not a parameter requests nothing,
        and neither does ``commentary``, which some Models attach to calls as a
        label of their output channel; both are dropped instead of rejected.
        """
        copied_arguments = copy.deepcopy(arguments)
        normalized = normalize_schema_value(
            copied_arguments,
            self.input_schema,
            root_schema=self.input_schema,
            root_validator=self.input_validator,
        )
        parameters = _closed_root_parameters(self.input_schema)
        if parameters is None or not isinstance(normalized, dict):
            return normalized
        return {
            key: value
            for key, value in normalized.items()
            if key in parameters or (value not in (None, "", [], {}) and key != _REMARK)
        }

    def read_name_value_pair(self, arguments: Any, *, unadvertised: Collection[str] = ()) -> Any:
        """Read a call sent as one name/value pair as that parameter.

        ``{"name": "command", "value": "ls"}`` is ``{"command": "ls"}`` when
        neither ``name`` nor ``value`` is a parameter (advertised or in
        ``unadvertised``), ``name`` is exactly one of the advertised parameters,
        and every other field is another parameter, or the same one with the
        same value. Anything else stays as sent, for the owner's repair and
        validation.
        """
        parameters = _closed_root_parameters(self.input_schema)
        if parameters is None or not isinstance(arguments, dict) or _PAIR_VALUE not in arguments:
            return arguments
        known = {*parameters, *unadvertised}
        field = arguments.get(_PAIR_NAME)
        if (
            not isinstance(field, str)
            or field not in parameters
            or {_PAIR_NAME, _PAIR_VALUE} & known
        ):
            return arguments
        value = arguments[_PAIR_VALUE]
        rest = {
            key: item for key, item in arguments.items() if key not in (_PAIR_NAME, _PAIR_VALUE)
        }
        if not rest.keys() - {_REMARK} <= known or rest.get(field, value) != value:
            return arguments
        return {**rest, field: value}

    def drop_inert_fields(
        self,
        arguments: Any,
        *,
        unadvertised: Mapping[str, JsonObject] | None = None,
        withheld: Collection[str] = (),
    ) -> tuple[Any, tuple[str, ...]]:
        """Return the arguments without unknown fields the call can run without.

        Also returns the dropped names, which the call's result names as without
        effect. An unknown field stays, and ``validate_arguments`` refuses the
        call, when ignoring it could change what the call does: its name is
        close to a parameter, so it is likely a misspelled one; it is one of
        ``withheld``, parameters of the Tool this Model request left out; it
        reads as a mode, condition or safety switch (``dryRun``, ``mode``,
        ``force``), or as a target or content of its own (``thread_id``,
        ``message``); or its value is content or fields of the call rather
        than a label (an object, or text with a line break or longer than a
        label).
        """
        parameters = _closed_root_parameters(self.input_schema)
        if parameters is None or not isinstance(arguments, dict):
            return arguments, ()
        known = {*parameters, *(unadvertised or {})}
        inert = tuple(
            key
            for key, value in arguments.items()
            if key not in known
            and key not in withheld
            and not _similar_names(key, list(known))
            and not _decisive(key)
            and _label_value(value)
        )
        if not inert:
            return arguments, ()
        kept = {key: value for key, value in arguments.items() if key not in inert}
        return kept, inert

    def validate_arguments(
        self, arguments: Any, *, unadvertised: Mapping[str, JsonObject] | None = None
    ) -> None:
        """Raise one readable error naming every argument problem, if any.

        ``unadvertised`` maps root parameters the Tool accepts without offering
        them to the Model to their schemas; they are validated but never listed.
        """
        problems, names_matter = _argument_problems(
            self.input_validator, arguments, unadvertised or {}
        )
        if problems:
            raise ToolContractError(
                _render_argument_problems(self.name, self.input_schema, problems, names_matter)
            )

    def no_arguments_message(self) -> str:
        """Return the refusal of a call that arrived with no arguments at all.

        Such a call lost its arguments on the way, most often in Tool-call
        markup the Provider could not read, so naming each missing parameter
        would miss the point: the whole call has to be sent again.
        """
        tool_name = model_tool_name(self.name)
        lines = [
            f"{tool_name} was not run: the call arrived with no arguments at all. "
            "Send the complete call again."
        ]
        parameter_line = _parameter_line(tool_name, self.input_schema)
        if parameter_line:
            lines.append(parameter_line)
        return "\n".join(lines)

    def validate_success_data(self, data: Any) -> None:
        """Raise an actionable error when successful Tool data violates its schema.

        Every successful result can carry ``note``, a sentence about how the call
        was read (``with_call_note``); a result schema that does not declare it
        is checked without it.
        """
        if self.result_validator is None:
            if not isinstance(data, dict):
                raise ToolContractError("data must be an object")
            return
        properties = (self.result_schema or {}).get("properties")
        if (
            isinstance(data, dict)
            and CALL_NOTE_FIELD in data
            and not (isinstance(properties, dict) and CALL_NOTE_FIELD in properties)
        ):
            data = {key: value for key, value in data.items() if key != CALL_NOTE_FIELD}
        _validate_instance(self.result_validator, data, label="data")


def compile_tool_contract(
    *,
    name: str,
    input_schema: JsonObject,
    result_schema: JsonObject | None = None,
    parallel_safe: bool = True,
    require_closed_input: bool = True,
) -> ToolContract:
    """Validate, copy, compile, and fingerprint one canonical Tool contract.

    Identical JSON inputs return the same immutable contract without validating
    the schemas again; any content change compiles afresh.
    """
    key = _contract_cache_key(
        name, input_schema, result_schema, parallel_safe, require_closed_input
    )
    if key is not None:
        with _CONTRACT_CACHE_LOCK:
            cached = _CONTRACT_CACHE.get(key)
            if cached is not None:
                _CONTRACT_CACHE.move_to_end(key)
                return cached
    contract = _compile_tool_contract(
        name=name,
        input_schema=input_schema,
        result_schema=result_schema,
        parallel_safe=parallel_safe,
        require_closed_input=require_closed_input,
    )
    if key is not None:
        with _CONTRACT_CACHE_LOCK:
            _CONTRACT_CACHE[key] = contract
            _CONTRACT_CACHE.move_to_end(key)
            while len(_CONTRACT_CACHE) > _CONTRACT_CACHE_LIMIT:
                _CONTRACT_CACHE.popitem(last=False)
    return contract


def _contract_cache_key(
    name: Any,
    input_schema: Any,
    result_schema: Any,
    parallel_safe: Any,
    require_closed_input: Any,
) -> str | None:
    """Digest plain JSON inputs; ``None`` leaves anything else to validation.

    Only exact JSON types qualify, so a value ``json.dumps`` would coerce (such
    as a tuple) can never share a contract with the JSON value it resembles.
    """
    if not (_is_plain_json(input_schema) and _is_plain_json(result_schema)):
        return None
    try:
        encoded = json.dumps(
            [name, input_schema, result_schema, parallel_safe, require_closed_input],
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except TypeError, ValueError:
        return None
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _is_plain_json(value: Any) -> bool:
    if isinstance(value, dict):
        return all(isinstance(key, str) and _is_plain_json(item) for key, item in value.items())
    if isinstance(value, list):
        return all(_is_plain_json(item) for item in value)
    return value is None or isinstance(value, (str, int, float))


def _compile_tool_contract(
    *,
    name: str,
    input_schema: JsonObject,
    result_schema: JsonObject | None,
    parallel_safe: bool,
    require_closed_input: bool,
) -> ToolContract:
    if not isinstance(name, str) or not name:
        raise ToolContractError("Tool name is required")
    if _TOOL_NAME_PATTERN.fullmatch(name) is None:
        raise ToolContractError(
            "Tool name must start with a letter and contain at most 64 letters, digits, "
            "or underscores"
        )
    if not isinstance(parallel_safe, bool):
        raise ToolContractError("Tool parallel_safe must be a boolean")

    canonical_input = _compile_schema(
        input_schema,
        label="input",
        require_closed_objects=require_closed_input,
    )
    canonical_result = (
        _compile_schema(result_schema, label="result", require_closed_objects=True)
        if result_schema is not None
        else None
    )
    fingerprint_payload = {
        "name": name,
        "input_schema": canonical_input,
        "result_schema": canonical_result,
        "parallel_safe": parallel_safe,
    }
    encoded = json.dumps(
        fingerprint_payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return ToolContract(
        name=name,
        input_schema=canonical_input,
        input_validator=Draft202012Validator(canonical_input),
        result_schema=canonical_result,
        result_validator=(
            Draft202012Validator(canonical_result) if canonical_result is not None else None
        ),
        parallel_safe=parallel_safe,
        schema_fingerprint=hashlib.sha256(encoded).hexdigest(),
    )


def _compile_schema(
    schema: Any,
    *,
    label: str,
    require_closed_objects: bool,
) -> JsonObject:
    if not isinstance(schema, dict):
        raise ToolContractError(f"Tool {label} schema must be a JSON Schema object")
    canonical = copy.deepcopy(schema)
    try:
        Draft202012Validator.check_schema(canonical)
    except SchemaError as error:
        path = _format_path(error.absolute_schema_path)
        raise ToolContractError(f"Tool {label} schema{path}: {error.message}") from None

    if canonical.get("type") != "object":
        raise ToolContractError(f"Tool {label} schema root must have type object")
    _validate_schema_invariants(
        canonical,
        label=label,
        path=(),
        require_closed_objects=require_closed_objects,
    )
    return canonical


def _validate_schema_invariants(
    node: Any,
    *,
    label: str,
    path: tuple[str | int, ...],
    require_closed_objects: bool,
) -> None:
    if isinstance(node, dict):
        reference = node.get("$ref")
        if isinstance(reference, str) and not reference.startswith("#/"):
            raise ToolContractError(
                f"Tool {label} schema{_format_path(path)} uses an external $ref"
            )
        if node.get("type") == "object" and "properties" in node:
            if require_closed_objects and node.get("additionalProperties") is not False:
                raise ToolContractError(
                    f"Tool {label} schema{_format_path(path)} must set "
                    "additionalProperties to false"
                )
            properties = node.get("properties")
            if not isinstance(properties, dict):
                raise ToolContractError(
                    f"Tool {label} schema{_format_path(path)} properties must be an object"
                )
            required = node.get("required", [])
            if not isinstance(required, list):
                raise ToolContractError(
                    f"Tool {label} schema{_format_path(path)} required must be an array"
                )
            unknown_required = sorted(set(required).difference(properties))
            if unknown_required:
                names = ", ".join(unknown_required)
                raise ToolContractError(
                    f"Tool {label} schema{_format_path(path)} requires unknown properties: {names}"
                )
        for key, value in node.items():
            _validate_schema_invariants(
                value,
                label=label,
                path=(*path, key),
                require_closed_objects=require_closed_objects,
            )
    elif isinstance(node, list):
        for index, value in enumerate(node):
            _validate_schema_invariants(
                value,
                label=label,
                path=(*path, index),
                require_closed_objects=require_closed_objects,
            )


def _closed_root_parameters(schema: Any) -> tuple[str, ...] | None:
    """Return a Tool input schema's complete top-level parameter names.

    Model-facing schemas omit ``additionalProperties``, so a Tool's declared
    root properties are its complete parameter list: an omitted keyword closes
    the root like ``false``. An explicit schema or ``true``, or
    ``patternProperties``, keeps it open (``None``).
    """
    if not isinstance(schema, Mapping):
        return None
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return None
    if schema.get("additionalProperties", False) is not False or "patternProperties" in schema:
        return None
    return tuple(properties)


_MAX_REPORTED_PROBLEMS = 6
_MAX_HINT_LENGTH = 160
_MAX_RECEIVED_LENGTH = 60


def _argument_problems(
    validator: Draft202012Validator, arguments: Any, unadvertised: Mapping[str, JsonObject]
) -> tuple[list[str], bool]:
    """Return every argument problem as a sentence, and whether names were wrong.

    Names are wrong when a parameter is missing or unknown; the rendered error
    then lists the Tool's parameters.
    """
    schema = validator.schema
    if not isinstance(arguments, dict):
        received = _received(arguments)
        return [f"Arguments must be a JSON object of named parameters; received {received}."], True
    problems: list[str] = []
    names_matter = False
    parameters = _closed_root_parameters(schema)
    if parameters is not None:
        for key in arguments:
            if key not in parameters and key not in unadvertised:
                problems.append(_unknown_parameter(key, parameters, arguments))
                names_matter = True
    advertised = {key: value for key, value in arguments.items() if key not in unadvertised}
    errors = list(validator.iter_errors(advertised))
    supplied = {key: arguments[key] for key in unadvertised if key in arguments}
    if supplied:
        extra = Draft202012Validator({"type": "object", "properties": dict(unadvertised)})
        errors.extend(extra.iter_errors(supplied))
    for error in errors:
        best = _best_validation_error(error)
        root_unknown = best.validator == "additionalProperties" and not best.absolute_path
        if root_unknown and parameters is not None:
            continue
        if best.validator == "required":
            names_matter = True
        for problem in _describe_problem(schema, best):
            if problem not in problems:
                problems.append(problem)
    return problems, names_matter


def _render_argument_problems(
    tool_name: str, schema: JsonObject, problems: list[str], names_matter: bool
) -> str:
    tool_name = model_tool_name(tool_name)
    parameter_line = _parameter_line(tool_name, schema) if names_matter else ""
    if len(problems) == 1 and not parameter_line:
        return f"{tool_name} was not run: {problems[0]}"
    shown = problems[:_MAX_REPORTED_PROBLEMS]
    lines = [f"{tool_name} was not run:", *(f"- {problem}" for problem in shown)]
    if len(problems) > len(shown):
        lines.append(f"- ...and {len(problems) - len(shown)} more.")
    if parameter_line:
        lines.append(parameter_line)
    return "\n".join(lines)


def _parameter_line(tool_name: str, schema: JsonObject) -> str:
    """Return the line listing the Tool's parameters, ``""`` when it has none."""
    parameters = schema.get("properties")
    if not isinstance(parameters, dict) or not parameters:
        return ""
    required = schema.get("required", [])
    names = ", ".join(f"{name} (required)" if name in required else name for name in parameters)
    return f"{tool_name} parameters: {names}."


def _unknown_parameter(key: str, parameters: tuple[str, ...], arguments: JsonObject) -> str:
    candidates = [name for name in parameters if name not in arguments]
    suggestion = _similar_name(key, candidates)
    hint = f' Did you mean "{suggestion}"?' if suggestion else ""
    return f'"{key}" is not a parameter.{hint}'


def _similar_name(key: str, candidates: list[str]) -> str | None:
    """Suggest a parameter for an unknown name; the suggestion is never applied."""
    containing = _contained_names(key, candidates)
    if len(containing) == 1:
        return containing[0]
    close = _typo_names(key, candidates)
    return close[0] if close else None


def _similar_names(key: str, candidates: list[str]) -> list[str]:
    """Return the parameters an unknown name contains or misspells."""
    return [*_contained_names(key, candidates), *_typo_names(key, candidates)]


def _contained_names(key: str, candidates: list[str]) -> list[str]:
    """Return the parameters that are a word of ``key`` (``path`` in ``file_path``)."""
    tokens = {token for token in re.split(r"[^a-z0-9]+", key.casefold()) if token}
    return [name for name in candidates if name.casefold() in tokens]


def _typo_names(key: str, candidates: list[str]) -> list[str]:
    """Return the parameters a typo away from ``key``, the closest first."""
    # A typo keeps the length; "country" is another word, not a misspelled "count".
    return [
        name
        for name in difflib.get_close_matches(key, candidates, n=3, cutoff=0.75)
        if abs(len(name) - len(key)) <= max(1, len(name) // 4)
    ]


def _decisive(key: str) -> bool:
    """Return whether an unknown field's name reads as a switch, target or content."""
    words = [word.casefold() for word in _NAME_WORDS.findall(key)]
    compact = "".join(words)
    return compact in _DECISIVE_WORDS or any(
        word in _DECISIVE_WORDS or word.removesuffix("s") in _DECISIVE_WORDS for word in words
    )


def _label_value(value: Any) -> bool:
    """Return whether an unknown field's value reads as a label, not as content."""
    if isinstance(value, str):
        return "\n" not in value and len(value) <= _LABEL_LENGTH
    if isinstance(value, list):
        return all(not isinstance(item, (dict, list)) and _label_value(item) for item in value)
    return not isinstance(value, dict)


def ignored_fields_note(tool_name: str, names: Collection[str]) -> str:
    """Return the note that unknown fields of a call had no effect."""
    quoted = [json.dumps(name, ensure_ascii=False) for name in names]
    listed = quoted[0] if len(quoted) == 1 else f"{', '.join(quoted[:-1])} and {quoted[-1]}"
    tool = model_tool_name(tool_name)
    if len(quoted) == 1:
        return f"{listed} had no effect: {tool} has no such parameter."
    return f"{listed} had no effect: {tool} has no such parameters."


def _describe_problem(root_schema: Any, error: ValidationError) -> list[str]:
    field = _field_name(error.absolute_path)
    subject = f'"{field}"' if field else "The arguments"
    keyword = error.validator
    expected = error.validator_value
    instance = error.instance
    if keyword == "required" and isinstance(instance, dict) and isinstance(expected, list):
        properties = error.schema.get("properties", {}) if isinstance(error.schema, dict) else {}
        return [
            _missing_parameter(_field_name([*error.absolute_path, name]), properties.get(name))
            for name in expected
            if name not in instance
        ]
    if keyword == "additionalProperties" and isinstance(instance, dict):
        properties = error.schema.get("properties", {}) if isinstance(error.schema, dict) else {}
        return [
            f'"{_field_name([*error.absolute_path, key])}" is not a known field of {subject}.'
            for key in instance
            if key not in properties
        ]
    if keyword == "type":
        default_hint = _optional_property_default_hint(root_schema, error)
        return [
            f"{subject} must be {_expected_types(expected)}; received {_received(instance)}"
            f"{default_hint}."
        ]
    if keyword == "enum" and isinstance(expected, list):
        options = ", ".join(json.dumps(option, ensure_ascii=False) for option in expected)
        return [f"{subject} must be one of {options}; received {_received(instance)}."]
    if keyword == "const":
        return [f"{subject} must be {json.dumps(expected, ensure_ascii=False)}."]
    bounds = {
        "minimum": "at least",
        "exclusiveMinimum": "greater than",
        "maximum": "at most",
        "exclusiveMaximum": "less than",
    }
    if keyword in bounds:
        return [f"{subject} must be {bounds[keyword]} {expected}; received {_received(instance)}."]
    if keyword in ("minLength", "minItems") and expected == 1:
        return [f"{subject} must not be empty."]
    if keyword == "minLength":
        return [f"{subject} must be at least {expected} characters long."]
    if keyword == "maxLength" and isinstance(instance, str):
        return [f"{subject} must be at most {expected} characters long; received {len(instance)}."]
    if keyword == "minItems" and isinstance(instance, list):
        return [f"{subject} needs at least {expected} items; received {len(instance)}."]
    if keyword == "maxItems" and isinstance(instance, list):
        return [f"{subject} allows at most {expected} items; received {len(instance)}."]
    if keyword == "uniqueItems":
        return [f"{subject} must not repeat items."]
    if keyword == "pattern":
        return [f"{subject} must match the pattern {expected}; received {_received(instance)}."]
    message = error.message.rstrip(".")
    return [f"{subject}: {message}." if field else f"{message}."]


def _missing_parameter(field: str, property_schema: Any) -> str:
    options = property_schema.get("enum") if isinstance(property_schema, dict) else None
    if isinstance(options, list) and options:
        # The valid values say what to send; a description cut to its first
        # sentence can stop mid-list (an action described value by value).
        named = ", ".join(json.dumps(option, ensure_ascii=False) for option in options)
        return f'"{field}" is required: one of {named}.'
    description = property_schema.get("description") if isinstance(property_schema, dict) else None
    if not isinstance(description, str) or not description.strip():
        return f'"{field}" is required.'
    hint = description.strip()
    sentence_end = hint.find(". ")
    if sentence_end >= 0:
        hint = hint[: sentence_end + 1]
    if len(hint) > _MAX_HINT_LENGTH:
        hint = hint[: _MAX_HINT_LENGTH - 3].rstrip() + "..."
    if not hint.endswith((".", "?", "!")):
        hint += "."
    return f'"{field}" is required: {hint}'


def _field_name(path: Any) -> str:
    name = ""
    for segment in path:
        if isinstance(segment, int):
            name += f"[{segment}]"
        else:
            name += f".{segment}" if name else str(segment)
    return name


def _expected_types(expected: Any) -> str:
    names = [expected] if isinstance(expected, str) else list(expected or [])
    articles = {
        "string": "a string",
        "integer": "an integer",
        "number": "a number",
        "boolean": "a boolean",
        "array": "an array",
        "object": "an object",
        "null": "null",
    }
    return " or ".join(articles.get(str(name), str(name)) for name in names)


def _received(value: Any) -> str:
    if isinstance(value, dict):
        return "an object"
    if isinstance(value, list):
        return "an array"
    text = json.dumps(value, ensure_ascii=False)
    if isinstance(value, str) and len(text) > _MAX_RECEIVED_LENGTH:
        return text[: _MAX_RECEIVED_LENGTH - 4] + '..."'
    return text


def _validate_instance(
    validator: Draft202012Validator,
    instance: Any,
    *,
    label: str,
) -> None:
    error = next(iter(validator.iter_errors(instance)), None)
    if error is None:
        return
    best = _best_validation_error(error)
    path = _format_path(best.absolute_path)
    keyword = best.validator if isinstance(best.validator, str) else "schema"
    message = (
        _format_type_validation_error(validator.schema, best)
        if best.validator == "type"
        else best.message
    )
    raise ToolContractError(f"{label}{path}: {message} [{keyword}]")


def _format_type_validation_error(root_schema: Any, error: ValidationError) -> str:
    expected = error.validator_value
    if isinstance(expected, str):
        expected_types = [expected]
    elif isinstance(expected, list):
        expected_types = [str(item) for item in expected]
    else:
        expected_types = [str(expected)]
    expected_text = " or ".join(f"JSON {item}" for item in expected_types)
    received_text = _describe_json_value(error.instance)
    default_hint = _optional_property_default_hint(root_schema, error)
    return f"expected {expected_text}, received {received_text}{default_hint}"


def _describe_json_value(value: Any) -> str:
    if value is None:
        return "JSON null"
    if isinstance(value, bool):
        return f"JSON boolean {json.dumps(value)}"
    if isinstance(value, str):
        return f"JSON string {json.dumps(value, ensure_ascii=False)}"
    if isinstance(value, int):
        return f"JSON integer {value}"
    if isinstance(value, float):
        return f"JSON number {value}"
    if isinstance(value, list):
        return "JSON array"
    if isinstance(value, dict):
        return "JSON object"
    return type(value).__name__


def _optional_property_default_hint(root_schema: Any, error: ValidationError) -> str:
    if not isinstance(error.schema, dict) or "default" not in error.schema:
        return ""

    schema_path = tuple(error.absolute_schema_path)
    for index in range(len(schema_path) - 2, -1, -1):
        if schema_path[index] != "properties":
            continue
        property_name = schema_path[index + 1]
        parent_schema = _schema_node_at_path(root_schema, schema_path[:index])
        if not isinstance(parent_schema, dict):
            continue
        required = parent_schema.get("required", [])
        if isinstance(required, list) and property_name not in required:
            default = json.dumps(error.schema["default"], ensure_ascii=False)
            return f"; omit this optional field to use its default {default}"
    return ""


def _schema_node_at_path(root_schema: Any, path: tuple[Any, ...]) -> Any:
    node = root_schema
    for segment in path:
        if isinstance(node, dict):
            if segment not in node:
                return None
            node = node[segment]
            continue
        if isinstance(node, list) and isinstance(segment, int) and 0 <= segment < len(node):
            node = node[segment]
            continue
        return None
    return node


def _best_validation_error(error: ValidationError) -> ValidationError:
    if not error.context:
        return error
    leaves: list[ValidationError] = []
    pending = list(error.context)
    while pending:
        candidate = pending.pop(0)
        if candidate.context:
            pending.extend(candidate.context)
        else:
            leaves.append(candidate)
    if not leaves:
        return error
    # ``anyOf``/``oneOf`` reports every branch. Discard complete alternatives
    # whose discriminator enum does not match the supplied value; otherwise an
    # error from an unrelated operation can hide the actionable error in the
    # selected branch.
    mismatched_alternatives = {
        prefix
        for leaf in leaves
        if leaf.validator == "enum"
        and isinstance(leaf.validator_value, list)
        and leaf.instance not in leaf.validator_value
        for prefix in _alternative_prefixes(tuple(leaf.absolute_schema_path))
    }
    matching_leaves = [
        leaf
        for leaf in leaves
        if not any(
            tuple(leaf.absolute_schema_path)[: len(prefix)] == prefix
            for prefix in mismatched_alternatives
        )
    ]
    if matching_leaves:
        leaves = matching_leaves
    elif mismatched_alternatives:
        return error
    return sorted(
        leaves,
        key=lambda item: (
            -len(item.absolute_path),
            _format_path(item.absolute_path),
            str(item.validator),
            item.message,
        ),
    )[0]


def _alternative_prefixes(path: tuple[Any, ...]) -> list[tuple[Any, ...]]:
    prefixes: list[tuple[Any, ...]] = []
    for index, segment in enumerate(path[:-1]):
        if segment in {"anyOf", "oneOf"} and isinstance(path[index + 1], int):
            prefixes.append(path[: index + 2])
    return prefixes


def _format_path(path: Any) -> str:
    parts: list[str] = []
    for segment in path:
        if isinstance(segment, int):
            parts.append(f"[{segment}]")
        else:
            escaped = str(segment).replace("~", "~0").replace("/", "~1")
            parts.append(f"/{escaped}")
    return "".join(parts)


__all__ = [
    "CALL_NOTE_FIELD",
    "ToolContract",
    "ToolContractError",
    "compile_tool_contract",
    "decode_json_argument",
    "ignored_fields_note",
]
