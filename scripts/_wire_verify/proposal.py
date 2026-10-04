"""Turn a verification run into a wire profile Model entry.

Learned facts become explicit profile values (so the entry no longer depends
on any learned-facts cache) and a clean run adds a ``verified`` record for the
checked Connection. ``write_entry`` merges the entry into the bundled
``resources/wire/<provider>.json`` file; a Custom Provider's entry goes into its
Settings wire block, through the running server when there is one
(``save_custom_provider_entry``) or directly (``write_custom_provider_entry``).
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.providers._wire_profile_files import WIRE_PROFILE_FORMAT_VERSION
from core.providers.wire_observations import ObservedFacts
from core.providers.wire_profile import WireProfile
from core.settings.normalizers import CUSTOM_PROVIDER_FIELDS
from core.storage.storage import StorageManager
from core.utils.errors import StorageError
from scripts._wire_verify.checks import CheckResult


def propose_entry(
    profile: WireProfile,
    facts: ObservedFacts,
    results: Sequence[CheckResult],
    *,
    connection_id: str,
    today: str | None = None,
) -> dict[str, Any]:
    """Return the Model entry the run supports (``set`` and, if clean, ``verified``)."""

    values: dict[str, Any] = {}
    if facts.reasoning_field:
        rest = [
            field for field in profile.response.reasoning_fields if field != facts.reasoning_field
        ]
        values.setdefault("response", {})["reasoning_fields"] = [facts.reasoning_field, *rest]
        if profile.source_of("replay.history_field") == "observed":
            values.setdefault("replay", {})["history_field"] = profile.replay.history_field
    if facts.rejected_efforts:
        values.setdefault("reasoning", {})["levels"] = list(profile.reasoning.ladder)
    if facts.rejected_parameters:
        values.setdefault("request", {})["parameters"] = {
            name: {"mode": "drop"} for name in facts.rejected_parameters
        }
    if facts.exclusive_parameters:
        values.setdefault("request", {})["exclusive_parameters"] = [
            list(group) for group in profile.request.exclusive_parameters
        ]
    if facts.reasoning_returned and profile.source_of("reasoning.supported") == "observed":
        values.setdefault("reasoning", {})["supported"] = True

    entry: dict[str, Any] = {"set": values} if values else {}
    if results and all(result.status in ("ok", "skipped") for result in results):
        passed = [result.name for result in results if result.status == "ok"]
        entry["verified"] = {
            "date": today or datetime.now(UTC).date().isoformat(),
            "connections": [connection_id],
            "evidence": "scripts/verify_wire_profile.py: " + ", ".join(passed),
        }
    return entry


def write_entry(wire_dir: Path, provider_id: str, model_id: str, entry: Mapping[str, Any]) -> Path:
    """Merge ``entry`` into ``models[model_id]`` of a bundled Provider's wire file.

    Only that Model entry's text changes: the hand-formatted rest of the file
    keeps its layout, so the diff shows the verified evidence alone.
    """

    path = wire_dir / f"{provider_id}.json"
    if not path.is_file():
        document: dict[str, Any] = {"format_version": WIRE_PROFILE_FORMAT_VERSION}
        _merge_entry(document, model_id, entry)
        path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8", newline="\n")
        return path
    text = path.read_text(encoding="utf-8")
    document = json.loads(text)
    _merge_entry(document, model_id, entry)
    key = model_id.split("::", 1)[0]
    updated = _splice_model_entry(text, key, document["models"][key])
    if json.loads(updated) != document:
        raise StorageError(f"could not place the entry for {key!r} in {path}")
    path.write_text(updated, encoding="utf-8", newline="\n")
    return path


def _splice_model_entry(text: str, key: str, value: Mapping[str, Any]) -> str:
    """Return ``text`` with ``models[key]`` set to ``value``, every other byte kept."""

    root_members, root_end = _object_members(text, text.index("{"))
    models = next((member for member in root_members if member[0] == "models"), None)
    if models is None:
        return _insert_member(text, root_members, root_end, "models", {key: value})
    model_members, models_end = _object_members(text, models[2])
    current = next((member for member in model_members if member[0] == key), None)
    if current is None:
        return _insert_member(text, model_members, models_end, key, value)
    _, key_start, value_start, value_end = current
    indent = _line_indent(text, key_start)
    return text[:value_start] + _render(value, indent) + text[value_end:]


_Member = tuple[str, int, int, int]
"""A JSON object member: key, key start, value start and value end offsets."""


def _object_members(text: str, open_index: int) -> tuple[list[_Member], int]:
    """Return the members of the object opening at ``open_index`` and its ``}`` offset."""

    decoder = json.JSONDecoder()
    members: list[_Member] = []
    index = _skip_space(text, open_index + 1)
    while text[index] != "}":
        key, after_key = decoder.raw_decode(text, index)
        value_start = _skip_space(text, _skip_space(text, after_key) + 1)
        _, value_end = decoder.raw_decode(text, value_start)
        members.append((key, index, value_start, value_end))
        index = _skip_space(text, value_end)
        if text[index] == ",":
            index = _skip_space(text, index + 1)
    return members, index


def _insert_member(
    text: str, members: list[_Member], close_index: int, key: str, value: Any
) -> str:
    if members:
        last_end = members[-1][3]
        indent = _line_indent(text, members[-1][1])
        member = f"{json.dumps(key)}: {_render(value, indent)}"
        return text[:last_end] + f",\n{indent}{member}" + text[last_end:]
    outer = _line_indent(text, close_index)
    indent = outer + "  "
    member = f"{json.dumps(key)}: {_render(value, indent)}"
    open_index = text.rindex("{", 0, close_index)
    return text[: open_index + 1] + f"\n{indent}{member}\n{outer}" + text[close_index:]


def _render(value: Any, indent: str) -> str:
    """Render ``value`` in the wire files' style: one line while it fits, else expanded."""

    flat = json.dumps(value)
    if len(indent) + len(flat) <= _LINE_WIDTH or not isinstance(value, dict | list) or not value:
        return flat
    inner = indent + "  "
    if isinstance(value, dict):
        items = [f"{inner}{json.dumps(key)}: {_render(item, inner)}" for key, item in value.items()]
        return "{\n" + ",\n".join(items) + f"\n{indent}}}"
    items = [f"{inner}{_render(item, inner)}" for item in value]
    return "[\n" + ",\n".join(items) + f"\n{indent}]"


_LINE_WIDTH = 100


def _line_indent(text: str, index: int) -> str:
    line_start = text.rfind("\n", 0, index) + 1
    line = text[line_start:index]
    return line[: len(line) - len(line.lstrip())]


def _skip_space(text: str, index: int) -> int:
    while text[index] in " \t\r\n":
        index += 1
    return index


def write_custom_provider_entry(
    storage: StorageManager, provider_id: str, model_id: str, entry: Mapping[str, Any]
) -> str:
    """Merge ``entry`` into ``wire.models[model_id]`` of a Custom Provider's Settings.

    Writes the Settings file directly, for a data directory without a running
    server. Returns the Settings path written.
    """

    storage.update_custom_provider_settings(
        provider_id, lambda record: _with_entry(record, provider_id, model_id, entry)
    )
    return _entry_path(provider_id, model_id)


RpcCall = Callable[[str, dict[str, Any]], Mapping[str, Any]]
"""Call one server RPC method; raises on an error answer."""


def save_custom_provider_entry(
    call: RpcCall, provider_id: str, model_id: str, entry: Mapping[str, Any]
) -> str:
    """Merge ``entry`` into a Custom Provider's wire block through a running server.

    ``provider.custom_save`` validates the block, applies it live, and refuses
    the save when the record changed after it was listed. Returns the Settings
    path written.
    """

    listing = call("provider.custom_list", {})
    stored = next(
        (
            item
            for item in listing.get("providers") or ()
            if isinstance(item, Mapping) and item.get("id") == provider_id
        ),
        None,
    )
    if stored is None:
        raise StorageError(f"Custom Provider '{provider_id}' does not exist")
    record = {key: value for key, value in stored.items() if key in CUSTOM_PROVIDER_FIELDS}
    params: dict[str, Any] = {
        "provider": {"id": provider_id, **_with_entry(record, provider_id, model_id, entry)}
    }
    if isinstance(stored.get("revision"), str):
        params["expected_revision"] = stored["revision"]
    call("provider.custom_save", params)
    return _entry_path(provider_id, model_id)


def _with_entry(
    record: Mapping[str, Any], provider_id: str, model_id: str, entry: Mapping[str, Any]
) -> dict[str, Any]:
    """Return ``record`` with ``entry`` merged into its wire block.

    Refuses a ``wire`` value that is not an object instead of replacing it.
    """

    wire = record.get("wire")
    if wire is not None and not isinstance(wire, Mapping):
        raise StorageError(
            f"providers.custom.{provider_id}.wire is not an object; "
            "fix it before writing a verified entry"
        )
    document = copy.deepcopy(dict(wire or {}))
    _merge_entry(document, model_id, entry)
    return {**record, "wire": document}


def _entry_path(provider_id: str, model_id: str) -> str:
    return f"providers.custom.{provider_id}.wire.models.{model_id.split('::', 1)[0]}"


def _merge_entry(document: dict[str, Any], model_id: str, entry: Mapping[str, Any]) -> None:
    """Merge ``entry`` into ``document["models"][model_id]`` in place."""

    models = document.setdefault("models", {})
    current = models.setdefault(model_id.split("::", 1)[0], {})
    if "set" in entry:
        current["set"] = _merged(current.get("set", {}), entry["set"])
    if "verified" in entry:
        verified = dict(entry["verified"])
        earlier = current.get("verified")
        if isinstance(earlier, Mapping) and isinstance(earlier.get("connections"), list):
            # Verifying another Connection adds to the evidence, never replaces it.
            verified["connections"] = list(
                dict.fromkeys([*earlier["connections"], *verified.get("connections", [])])
            )
        current["verified"] = verified


def _merged(base: Mapping[str, Any], update: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in update.items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), Mapping):
            merged[key] = _merged(merged[key], value)
        else:
            merged[key] = value
    return merged
