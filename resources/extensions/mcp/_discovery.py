"""How the connection Tool finds, names and describes the items of a connection's catalog.

Every Tool, Resource, template, Prompt and protocol operation of a connection is
an entry with a target ``kind:name:fingerprint``. The fingerprint hashes what an
item does and how it is called, so a target from an earlier definition is told
apart from the current one without remembering earlier targets. Search ranks
entries by query words and renders one bounded page; describe and call resolve
a target, or a bare name, to exactly one entry and never choose between several.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import re
from collections.abc import Callable
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import best_match

from core.tools.call_syntax import normalize_call_arguments
from core.tools.contracts import ToolContractError, compile_tool_contract
from core.tools.tools import tool_failure

from ._definitions import (
    GUIDANCE_PREVIEW_CHARACTERS,
    MCP_MESSAGES,
    MCP_OPERATION_DESCRIPTIONS,
    SEARCH_MAX_LIMIT,
    SEARCH_PAGE_CHARACTERS,
    SEARCH_PAGE_SIZE,
    SEARCH_SUMMARY_CHARACTERS,
    TARGET_FINGERPRINT_LENGTH,
    TOOL_NAME_HASH_LENGTH,
    TOOL_NAME_LABEL_LENGTH,
)
from ._views import argument_problem, compact, schema_summary, without_protocol_meta
from .client import (
    READ_OPERATIONS,
    TASK_OPERATIONS,
    operation_schema,
    unsupported_operations,
)
from .content import pointer_part

_TARGET_KINDS = ("tool", "resource", "template", "prompt", "operation")

# What a search without kind covers: the application's own items.
_APPLICATION_KINDS = ("tool", "resource", "template", "prompt")

# The protocol operation a call of each application kind performs; an operation
# target performs the operation it names.
_KIND_OPERATIONS = {
    "tool": "tools/call",
    "resource": "resources/read",
    "template": "resources/read",
    "prompt": "prompts/get",
}

# The definition fields a target fingerprint covers: what the item does and how it
# is called. Metadata a server can vary between listings (``_meta``, icons, a
# resource's size or modification date) leaves the fingerprint unchanged.
_FINGERPRINT_FIELDS = {
    "tool": (
        "name",
        "title",
        "description",
        "inputSchema",
        "outputSchema",
        "annotations",
        "execution",
    ),
    "resource": ("name", "title", "uri", "description", "mimeType"),
    "template": ("name", "title", "uriTemplate", "description", "mimeType"),
    "prompt": ("name", "title", "description", "arguments"),
}

# Catalog fields that list items; the connection entry shows the others.
_CATALOG_LISTS = (
    ("tool", "tools"),
    ("resource", "resources"),
    ("template", "resource_templates"),
    ("prompt", "prompts"),
)


def remote_tool_name(connection: str, name: str) -> str:
    """The deterministic, bounded registry name of a remote Tool."""
    label = re.sub(r"[^a-zA-Z0-9_]", "_", name)[:TOOL_NAME_LABEL_LENGTH]
    digest = hashlib.sha256(name.encode()).hexdigest()[:TOOL_NAME_HASH_LENGTH]
    return f"mcp_{connection}_{label}_{digest}"


def target_for(kind: str, name: str, definition: Any) -> str:
    """``kind:name:fingerprint``; the fingerprint changes with the definition's meaning."""
    fields = _FINGERPRINT_FIELDS.get(kind)
    meaning = (
        definition
        if fields is None
        else {field: definition[field] for field in fields if field in definition}
    )
    fingerprint = hashlib.sha256(
        json.dumps(meaning, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()[:TARGET_FINGERPRINT_LENGTH]
    return f"{kind}:{name}:{fingerprint}"


def operation_target(operation: str) -> str:
    return target_for("operation", operation, operation_schema(operation))


def entry_operation(entry: dict[str, Any]) -> str:
    """The protocol operation a call of *entry* performs."""
    operation: str = _KIND_OPERATIONS.get(entry["kind"], entry["name"])
    return operation


def catalog_entries(
    connection: str, catalog: dict[str, Any], allowed: tuple[str, ...] | None
) -> list[dict[str, Any]]:
    """The connection's items; with *allowed*, only the remote Tools it contains.

    Protocol operations the server does not offer under its protocol are left out.
    """
    entries = []
    for kind, field in _CATALOG_LISTS:
        for definition in catalog.get(field, []):
            name = definition.get("name") or definition.get("uri") or definition.get("uriTemplate")
            if (
                kind == "tool"
                and allowed is not None
                and remote_tool_name(connection, name) not in allowed
            ):
                continue
            entries.append(
                {
                    "kind": kind,
                    "name": name,
                    "target": target_for(kind, name, definition),
                    "description": definition.get("description") or definition.get("title") or name,
                    "definition": definition,
                }
            )
    unsupported = unsupported_operations(catalog)
    entries.extend(
        {
            "kind": "operation",
            "name": name,
            "target": operation_target(name),
            "description": description,
            "definition": operation_schema(name),
        }
        for name, description in MCP_OPERATION_DESCRIPTIONS.items()
        if name not in unsupported
    )
    listed = {field for _kind, field in _CATALOG_LISTS} | {"pages"}
    entries.append(
        {
            "kind": "connection",
            "name": connection,
            "target": "connection",
            "description": "Connection details: server, capabilities and guidance.",
            "definition": {key: value for key, value in catalog.items() if key not in listed},
        }
    )
    return sorted(entries, key=lambda entry: (entry["kind"], entry["name"]))


def search_entries(
    entries: list[dict[str, Any]], query: str = "", kind: str | None = None
) -> list[dict[str, Any]]:
    """The entries of *kind* (application items without one) ranked by matching query words."""
    words = set(query.casefold().split())
    order = {
        name: index
        for index, name in enumerate(
            ("tool", "resource", "template", "prompt", "connection", "operation")
        )
    }
    kinds = (kind,) if kind else _APPLICATION_KINDS
    scored = []
    for entry in entries:
        if entry["kind"] not in kinds:
            continue
        text = (entry["name"] + " " + entry["description"]).casefold()
        score = sum(word in text for word in words)
        if not words or score:
            scored.append((-score, order[entry["kind"]], entry["name"], entry))
    return [item[3] for item in sorted(scored, key=lambda item: item[:3])]


def summarize(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "target": entry["target"],
        "kind": entry["kind"],
        "name": entry["name"],
        "description": entry["description"][:SEARCH_SUMMARY_CHARACTERS],
        "describe": {"action": "describe", "target": entry["target"]},
    }


def search_page(
    connection: str,
    instructions: str,
    entries: list[dict[str, Any]],
    arguments: dict[str, Any],
    attach: Callable[[dict[str, Any]], str] | None,
) -> dict[str, Any]:
    """The result data of one search of the connection Tool.

    *attach* keeps a payload with the Tool Result and returns its id; without
    one (outside a Session) nothing could read it later, so the complete
    payload is returned instead of a page.
    """
    query = arguments.get("query", "")
    kind = arguments.get("kind")
    matches = search_entries(entries, query, kind)
    offset = arguments.get("offset", 0)
    requested = arguments.get("limit")
    limit = SEARCH_PAGE_SIZE if requested is None else min(requested, SEARCH_MAX_LIMIT)
    # A continuation repeats the search with the limit that was applied.
    continued = arguments if requested is None else {**arguments, "limit": limit}
    prompts = [entry for entry in entries if entry["kind"] == "prompt"]
    available = {
        kind_name: sum(entry["kind"] == kind_name for entry in entries)
        for kind_name in _APPLICATION_KINDS
    }
    payload = {
        "connection": connection,
        "matches": [summarize(entry) for entry in matches],
        "total": len(matches),
        "available": available,
        "server_guidance": {
            "instructions": instructions,
            "prompts": [summarize(entry) for entry in prompts],
        },
    }
    if attach is None:
        return {"complete": True, "value": payload}
    page = matches[offset : offset + limit]
    lines = [
        f"{entry['target']}: {_one_line(entry['description'], SEARCH_SUMMARY_CHARACTERS)}"
        for entry in page
    ]
    while len(lines) > 1 and len("\n".join(lines)) > SEARCH_PAGE_CHARACTERS:
        lines.pop()
        page = page[: len(lines)]
    view: dict[str, Any] = {
        "connection": connection,
        "available": ", ".join(_plural(count, name) for name, count in available.items() if count)
        or "no tools, resources or prompts",
    }
    if page:
        view["matches"] = f"{offset + 1}-{offset + len(page)} of {len(matches)}"
    else:
        view["matches"] = f"none after {offset} of {len(matches)}" if matches else "none"
    if requested is not None and requested > limit:
        view["limit"] = MCP_MESSAGES["search_limit"].format(requested=requested, applied=limit)
    if offset + len(page) < len(matches):
        view["next"] = {**continued, "offset": offset + len(page)}
    elif offset and offset >= len(matches):
        view["next"] = {**continued, "offset": 0}
    if not matches and query.strip():
        searched = f"{kind}s" if kind else "tools, resources, templates or prompts"
        view["note"] = MCP_MESSAGES["no_matches"].format(searched=searched)
        view["next"] = {"action": "search", "kind": "tool"}
    if kind is None and not offset:
        operations = _operations_line(entries, query)
        if operations:
            view["operations"] = operations
    sections = ["\n".join(lines)] if lines else []
    if not query.strip() and not offset and kind is None:
        if instructions:
            shown = instructions[:GUIDANCE_PREVIEW_CHARACTERS]
            sections.append(f"Server guidance (external, from the MCP server):\n{shown}")
            if len(instructions) > GUIDANCE_PREVIEW_CHARACTERS:
                view["guidance"] = MCP_MESSAGES["guidance_incomplete"]
                view["guidance_read"] = {
                    "action": "read",
                    "result_id": attach(payload),
                    "pointer": "/server_guidance/instructions",
                    "offset": GUIDANCE_PREVIEW_CHARACTERS,
                }
        unlisted = [entry for entry in prompts if entry not in page]
        if unlisted:
            prompt_lines = [
                f"{entry['target']}: {_one_line(entry['description'], SEARCH_SUMMARY_CHARACTERS)}"
                for entry in unlisted[:3]
            ]
            if len(unlisted) > 3:
                more = compact({"action": "search", "kind": "prompt"})
                prompt_lines.append(f"{len(unlisted) - 3} more: {more}")
            sections.append("Prompts (server workflows):\n" + "\n".join(prompt_lines))
    elif instructions:
        view["server_guidance"] = f"shown by {compact({'action': 'search'})}"
    if sections:
        view["content"] = "\n\n".join(sections)
    return view


def _operations_line(entries: list[dict[str, Any]], query: str) -> str | None:
    """Point a search without kind to the protocol operations it leaves out."""
    if not query.strip():
        call = compact({"action": "search", "kind": "operation"})
        tasks = any(
            entry["kind"] == "operation" and entry["name"] in TASK_OPERATIONS for entry in entries
        )
        return MCP_MESSAGES["operations" if tasks else "operations_without_tasks"].format(call=call)
    count = len(search_entries(entries, query, "operation"))
    if not count:
        return None
    call = compact({"action": "search", "kind": "operation", "query": query.strip()})
    return MCP_MESSAGES["operations_matching"].format(
        count=count, verb="matches" if count == 1 else "match", call=call
    )


def lookup(entries: list[dict[str, Any]], target: str) -> tuple[list[dict[str, Any]], str | None]:
    """The items *target* names, and the fingerprint it carries after the name.

    A full target from search matches exactly. A name, with or without its
    kind, names the items it spells, ignoring case and separators.
    """
    exact = next((entry for entry in entries if entry["target"] == target), None)
    if exact is not None:
        return [exact], None
    kind, name, fingerprint = _parse_target(target)
    pool = _candidates(entries, kind)
    named = [entry for entry in pool if name in _item_names(entry)]
    if not named:
        folded = _folded(name)
        named = [
            entry for entry in pool if folded in {_folded(item) for item in _item_names(entry)}
        ]
    return named, fingerprint


def resolve(
    entries: list[dict[str, Any]], arguments: dict[str, Any]
) -> tuple[dict[str, Any] | None, str | None, dict[str, Any] | None]:
    """Find the one item a describe or call names; never choose between several.

    Returns the entry, a note on how the target was read, or the failure. A
    fingerprint that does not match the item's current definition may come from
    an earlier definition, so a call that can change something is refused with
    the current target. Describe and reading calls use the current definition
    and say so in the note.
    """
    target = arguments["target"]
    named, fingerprint = lookup(entries, target)
    if len(named) == 1:
        entry = named[0]
        current = entry["target"]
        if fingerprint is None or current == f"{entry['kind']}:{entry['name']}:{fingerprint}":
            return entry, None, None
        if arguments["action"] == "describe" or entry_operation(entry) in READ_OPERATIONS:
            return (
                entry,
                MCP_MESSAGES["target_current"].format(current=current, sent=target),
                None,
            )
        return (
            None,
            None,
            tool_failure(
                "mcp_target_mismatch",
                MCP_MESSAGES["target_mismatch"].format(
                    item=f"{entry['kind']} {entry['name']}",
                    sent=target,
                    target=current,
                    describe=compact({"action": "describe", "target": current}),
                ),
            ),
        )
    kind, name, _ = _parse_target(target)
    shown = name[:80] or target[:80]
    if not named and kind in {None, "operation"} and name.casefold() in MCP_OPERATION_DESCRIPTIONS:
        # A protocol operation this connection's server does not offer.
        return None, None, unsupported_operation(name.casefold())
    if len(named) > 1:
        return (
            None,
            None,
            tool_failure(
                "mcp_unknown_target",
                MCP_MESSAGES["target_ambiguous"].format(
                    name=shown, targets=", ".join(entry["target"] for entry in named)
                ),
            ),
        )
    return (
        None,
        None,
        tool_failure("mcp_unknown_target", _unknown(_candidates(entries, kind), kind, shown)),
    )


def unsupported_operation(operation: str) -> dict[str, Any]:
    """The failure for a protocol operation the connection's server does not offer."""
    return tool_failure(
        "mcp_operation_unsupported",
        MCP_MESSAGES["operation_unsupported"].format(
            operation=operation, search=compact({"action": "search", "kind": "operation"})
        ),
        retryable=False,
    )


def names_denied_tool(
    connection: str, catalog: dict[str, Any], target: str, allowed: tuple[str, ...]
) -> bool:
    """Whether an unresolved target names a remote Tool the Agent may not use.

    The connection Tool's description lists every remote Tool name, so such a
    target is refused for its real cause instead of reading as unknown.
    """
    denied = [
        entry
        for entry in catalog_entries(connection, catalog, None)
        if entry["kind"] == "tool" and remote_tool_name(connection, entry["name"]) not in allowed
    ]
    return len(lookup(denied, target)[0]) == 1


def noted(result: dict[str, Any], note: str) -> dict[str, Any]:
    """Add how the target was read to a call's own result or error."""
    if not result["ok"]:
        error = result["error"]
        return {**result, "error": {**error, "message": f"{error['message']} {note}"}}
    data = result["data"]
    earlier = data.get("note")
    if earlier is not None and not isinstance(earlier, str):
        # A field of the remote result keeps its name and value.
        return {**result, "data": {"target_note": note, **data}}
    combined = f"{note} {earlier}" if earlier else note
    rest = {key: value for key, value in data.items() if key != "note"}
    return {**result, "data": {"note": combined, **rest}}


def arguments_schema(entry: dict[str, Any]) -> dict[str, Any]:
    """The schema of the arguments a call of *entry* takes."""
    if entry["kind"] == "tool":
        return dict(entry["definition"]["inputSchema"])
    if entry["kind"] == "operation":
        return dict(entry["definition"])
    if entry["kind"] == "template":
        return operation_schema("resources/read")
    if entry["kind"] == "prompt":
        arguments = entry["definition"].get("arguments", [])
        return {
            "type": "object",
            "properties": {
                argument["name"]: {
                    "type": "string",
                    "description": argument.get("description", ""),
                }
                for argument in arguments
            },
            "required": [argument["name"] for argument in arguments if argument.get("required")],
        }
    return {"type": "object", "properties": {}, "required": []}


def describe_payload(entry: dict[str, Any], note: str | None) -> dict[str, Any]:
    """What describe shows of *entry*: its definition, arguments schema and call shape."""
    kind = entry["kind"]
    payload: dict[str, Any] = {"target": entry["target"]}
    if note is not None:
        payload["note"] = note
    if kind != "connection" and entry["description"] != entry["name"]:
        payload["description"] = entry["description"]
    hidden = {"name", "description", "inputSchema" if kind == "tool" else ""}
    if kind == "prompt":
        hidden.add("arguments")
    details = (
        without_protocol_meta(
            {key: value for key, value in entry["definition"].items() if key not in hidden}
        )
        if kind != "operation"
        else {}
    )
    if details:
        payload["definition"] = details
    if kind != "connection":
        payload["arguments_schema"] = arguments_schema(entry)
        payload["call"] = (
            f'{{"action":"call","target":"{entry["target"]}","arguments":{{...}}}}'
            " with arguments matching arguments_schema"
        )
    return payload


def target_arguments(
    entry: dict[str, Any], inputs: dict[str, Any]
) -> tuple[dict[str, Any], str | None]:
    """Repair and validate the arguments of a call of *entry* against its exact schema.

    Returns the repaired arguments and ``None``, or the arguments as sent and the
    problem, which names the JSON Pointer without echoing values.
    """
    schema = arguments_schema(entry)
    try:
        contract = compile_tool_contract(
            name="mcp_target", input_schema=schema, require_closed_input=False
        )
        repaired = (
            normalize_call_arguments(contract, inputs, enum_fields=("level",))
            if entry["kind"] == "operation" and entry["name"] == "logging/setLevel"
            else contract.normalize_arguments(inputs)
        )
    except ToolContractError as repair_error:
        return inputs, str(repair_error)
    error = best_match(Draft202012Validator(schema).iter_errors(repaired))
    if error is None:
        return repaired, None
    pointer = "/arguments" + "".join("/" + pointer_part(str(part)) for part in error.absolute_path)
    return inputs, argument_problem(
        str(error.validator), error.validator_value, error.schema, error.instance, pointer
    )


def invalid_target_arguments(entry: dict[str, Any], problem: str) -> dict[str, Any]:
    """The refusal of a call whose arguments do not fit *entry*; *problem* is redacted."""
    return tool_failure(
        "mcp_invalid_arguments",
        MCP_MESSAGES["target_invalid"].format(
            item=f"{entry['kind']} {entry['name']}",
            problem=problem[:500],
            summary=schema_summary(arguments_schema(entry)),
            describe=compact({"action": "describe", "target": entry["target"]}),
        ),
    )


def _parse_target(target: str) -> tuple[str | None, str, str | None]:
    """Split ``kind:name:fingerprint``; kind and fingerprint may be absent."""
    parts = target.split(":")
    kind = parts[0].strip().casefold() if len(parts) > 1 else None
    rest = parts[1:] if kind in _TARGET_KINDS else parts
    if kind not in _TARGET_KINDS:
        kind = None
    fingerprint = None
    if len(rest) > 1 and re.fullmatch(rf"[0-9a-f]{{{TARGET_FINGERPRINT_LENGTH}}}", rest[-1]):
        fingerprint = rest[-1]
        rest = rest[:-1]
    return kind, ":".join(rest).strip(), fingerprint


def _folded(name: str) -> str:
    return re.sub(r"[\s_-]+", "_", name.strip().casefold())


def _candidates(entries: list[dict[str, Any]], kind: str | None) -> list[dict[str, Any]]:
    """The items a target of *kind* can name; the connection entry only by its full target."""
    return [
        entry
        for entry in entries
        if entry["kind"] != "connection" and (kind is None or entry["kind"] == kind)
    ]


def _item_names(entry: dict[str, Any]) -> set[str]:
    """The names a target can use for *entry*: its name and a Resource's URI or template."""
    definition = entry["definition"]
    return {
        value
        for value in (entry["name"], definition.get("uri"), definition.get("uriTemplate"))
        if isinstance(value, str)
    }


def _unknown(pool: list[dict[str, Any]], kind: str | None, name: str) -> str:
    subject = f"No {kind}" if kind else "No tool, resource, prompt or operation"
    message = f"{subject} named {name} is available on this connection, so nothing was run."
    by_name = {entry["name"].casefold(): entry for entry in pool}
    close = difflib.get_close_matches(name.casefold(), list(by_name), n=5, cutoff=0.6)
    words = [word for word in re.split(r"[\s_./:-]+", name.casefold()) if len(word) > 2]
    close += [
        key
        for key, entry in by_name.items()
        if key not in close and words and any(word in key for word in words)
    ][: max(0, 5 - len(close))]
    candidates = [by_name[key]["target"] for key in close]
    if len(candidates) == 1:
        return (
            f"{message} The closest is {candidates[0]}; if you mean it, repeat the call "
            f'with "target":"{candidates[0]}".'
        )
    if candidates:
        return (
            f"{message} Close names: {', '.join(candidates)}. Repeat the call with the "
            "target you mean."
        )
    query = " ".join(words) or name
    return f"{message} Find it with {compact({'action': 'search', 'query': query[:60]})}."


def _one_line(text: str, limit: int) -> str:
    return " ".join(text.split())[:limit]


def _plural(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"
