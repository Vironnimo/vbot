"""The Generation 1 contract shared by durable JSON documents in the data directory.

Every in-scope document is a JSON object with a top-level ``format_version``. Its
owner describes the object levels it models with a :class:`JsonShape` and keeps
validating the content itself (see :mod:`core.config_validation`). This module
owns the list of in-scope documents (:data:`DURABLE_DOCUMENTS`), the part of it
data snapshots capture (:data:`SNAPSHOT_DOCUMENTS`), and only the contract
mechanics every owner shares:

- :func:`validate_format_version` refuses a missing, malformed, older, or newer
  version. A newer version comes from a newer vBot and is never overwritten.
- :func:`strip_unknown_fields` hands an owner only the fields it models, so its
  normalizers and in-memory records never see fields a newer vBot added.
- :func:`write_json_document` refuses to overwrite a file that fails to load, then
  writes the owner's payload with every unknown field of the file on disk merged
  back unchanged at each modeled level.

Removing, renaming, or changing the meaning of a field is not an additive change:
it needs a ``format_version`` bump and a converter.
"""

from __future__ import annotations

import copy
import fnmatch
import json
import os
import stat
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal

from core.config_validation import (
    JsonConfigValidationError,
    JsonDiagnostic,
    JsonValidationReport,
    add_error,
    child_path,
    error_diagnostic,
    format_report_diagnostics,
    read_json_file,
    warn_unknown_keys,
)
from core.utils.atomic import atomic_write_text

FORMAT_VERSION_FIELD = "format_version"

#: Every durable JSON document under this contract: a stable kind name and the
#: document's data-dir relative location, where ``*`` matches within one path
#: segment. ``vbot doctor config`` validates each kind through its owner. A new
#: document joins here, and with it the data snapshot set unless it is listed in
#: :data:`DOCUMENTS_OUTSIDE_SNAPSHOTS`.
DURABLE_DOCUMENTS: Mapping[str, str] = MappingProxyType(
    {
        "settings": "settings.json",
        "agent": "agents/*/agent.json",
        "agent_order": "agents/order.json",
        "agent_prompt_layout": "agents/*/prompts/layout.json",
        "prompt_layout": "prompts/layout.json",
        "channel": "channels/*/channel.json",
        "project": "projects/*/project.json",
        "cron_jobs": "cron/jobs.json",
        "bootstrap_jobs": "bootstrap/jobs.json",
        "calendar_events": "calendar/events.json",
        "calendar_actions": "calendar/actions.json",
        "skill_policy": "skills/policy.json",
        "terminal_launch_history": "terminals/launch-history.json",
        "terminal_groups": "terminals/groups.json",
        "oauth_token": "oauth/*.json",
        "mcp_connections": "extension-data/mcp/connections.json",
        "attachment_metadata": "artifacts/attachments/*.json",
        "speech_artifact_metadata": "artifacts/speech/*.json",
    }
)

#: Kinds that data snapshots leave out. Each is the sidecar of a blob stored beside
#: it: a snapshot holds no blobs, and restoring its document set would delete every
#: sidecar created after the snapshot while its blob stays.
DOCUMENTS_OUTSIDE_SNAPSHOTS: frozenset[str] = frozenset(
    {"attachment_metadata", "speech_artifact_metadata"}
)

#: The JSON document set of a data snapshot: every other durable document.
SNAPSHOT_DOCUMENTS: Mapping[str, str] = MappingProxyType(
    {
        kind: pattern
        for kind, pattern in DURABLE_DOCUMENTS.items()
        if kind not in DOCUMENTS_OUTSIDE_SNAPSHOTS
    }
)

ShapeKind = Literal["object", "map", "list", "opaque"]
DocumentValidator = Callable[[Any], list[JsonDiagnostic]]


@dataclass(frozen=True, eq=False)
class JsonShape:
    """The object levels of a JSON value that its owner models.

    - ``object``: ``fields`` are the declared keys; every other key is unknown.
      ``nested`` gives the shape of declared keys whose values are modeled too.
    - ``map``: every key is data. ``values`` is the shape of each value;
      ``known`` overrides it for specific keys. With ``drop_empty`` an entry
      whose value is an empty object once the unknown fields are merged back is
      not written: the owner keeps such an entry (``{}``) when this vBot models
      none of its fields, and it disappears only when it holds no field at all.
    - ``list``: an array of ``items``; entries are matched across versions by
      their ``key`` field.
    - ``opaque``: the owner writes the whole value; nothing inside is preserved.
    """

    kind: ShapeKind
    fields: frozenset[str] = frozenset()
    nested: Mapping[str, JsonShape] = field(default_factory=dict)
    values: JsonShape | None = None
    known: Mapping[str, JsonShape] = field(default_factory=dict)
    items: JsonShape | None = None
    key: str | None = None
    drop_empty: bool = False


OPAQUE = JsonShape("opaque")


def json_object(
    fields: Iterable[str],
    nested: Mapping[str, JsonShape] | None = None,
) -> JsonShape:
    """Return the shape of an object with declared ``fields``."""

    declared = frozenset(fields)
    nested_shapes = dict(nested or {})
    undeclared = sorted(set(nested_shapes) - declared)
    if undeclared:
        raise ValueError(f"nested shapes for undeclared fields: {', '.join(undeclared)}")
    return JsonShape("object", fields=declared, nested=nested_shapes)


def json_map(
    values: JsonShape = OPAQUE,
    *,
    known: Mapping[str, JsonShape] | None = None,
    drop_empty: bool = False,
) -> JsonShape:
    """Return the shape of an object whose keys are data rather than fields.

    ``drop_empty`` leaves out entries that are empty objects after the merge.
    """

    return JsonShape("map", values=values, known=dict(known or {}), drop_empty=drop_empty)


def json_list(items: JsonShape, *, key: str) -> JsonShape:
    """Return the shape of an array whose entries are identified by ``key``."""

    return JsonShape("list", items=items, key=key)


def json_document(
    fields: Iterable[str],
    nested: Mapping[str, JsonShape] | None = None,
) -> JsonShape:
    """Return a document root shape: an object that also declares ``format_version``."""

    return json_object({*fields, FORMAT_VERSION_FIELD}, nested)


class JsonDocumentWriteError(JsonConfigValidationError):
    """Raised when a document on disk failed to load and must not be overwritten."""


@dataclass(frozen=True, eq=False)
class JsonDocumentFormat:
    """How one kind of durable JSON document is versioned, preserved, and guarded.

    ``validate`` decides whether the file on disk may be overwritten: any error
    refuses the write. Single-record documents pass their full validator;
    collections that keep invalid entries verbatim pass a document-level one.
    """

    name: str
    version: int
    shape: JsonShape
    validate: DocumentValidator
    sort_keys: bool = False


def validate_format_version(
    diagnostics: list[JsonDiagnostic],
    data: Mapping[str, Any],
    version: int,
    *,
    path: str = "$",
) -> bool:
    """Check ``format_version`` against the version this vBot reads.

    Returns whether the rest of the document may be validated: fields of another
    version have another meaning.
    """

    field_path = child_path(path, FORMAT_VERSION_FIELD)
    if FORMAT_VERSION_FIELD not in data:
        # Imported at call time: the database kernel imports this module.
        from core.database.errors import generation_1_conversion_hint

        add_error(diagnostics, field_path, f"is required. {generation_1_conversion_hint()}")
        return False
    value = data[FORMAT_VERSION_FIELD]
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        add_error(diagnostics, field_path, "must be a positive integer")
        return False
    if value > version:
        add_error(
            diagnostics,
            field_path,
            f"is {value}, but this vBot reads version {version}; the file was written "
            "by a newer vBot and is left unchanged",
        )
        return False
    if value < version:
        add_error(
            diagnostics,
            field_path,
            f"is {value}, but this vBot reads version {version}; convert the file first",
        )
        return False
    return True


def validate_collection_root(
    diagnostics: list[JsonDiagnostic],
    data: Any,
    *,
    version: int,
    shape: JsonShape,
    collection: str,
    label: str = "field",
) -> list[Any] | None:
    """Check the root of a document that holds a named array of entries.

    Returns the entries, or ``None`` when the document cannot be read as this
    version. Owners that keep invalid entries verbatim use the root check alone
    as the write guard of the document.
    """

    if not isinstance(data, dict):
        diagnostics.append(
            error_diagnostic("$", f"Expected a JSON object, got {type(data).__name__}")
        )
        return None
    if not validate_format_version(diagnostics, data, version):
        return None
    warn_unknown_keys(diagnostics, "$", data, shape.fields, label)
    entries_path = child_path("$", collection)
    if collection not in data:
        add_error(diagnostics, entries_path, "is required")
        return None
    entries = data[collection]
    if not isinstance(entries, list):
        add_error(diagnostics, entries_path, "must be an array")
        return None
    return entries


def strip_unknown_fields(value: Any, shape: JsonShape) -> Any:
    """Return a copy of ``value`` without the fields ``shape`` does not declare."""

    if shape.kind == "object" and isinstance(value, dict):
        return {
            key: _strip_child(item, shape.nested.get(key))
            for key, item in value.items()
            if key in shape.fields
        }
    if shape.kind == "map" and isinstance(value, dict):
        return {
            key: _strip_child(item, _map_value_shape(shape, key)) for key, item in value.items()
        }
    if shape.kind == "list" and isinstance(value, list):
        return [_strip_child(item, shape.items) for item in value]
    return copy.deepcopy(value)


def warn_unknown_fields(
    diagnostics: list[JsonDiagnostic],
    path: str,
    value: Any,
    shape: JsonShape,
    *,
    label: str = "field",
) -> None:
    """Append a warning for every field ``shape`` does not declare, at every level."""

    if shape.kind == "object" and isinstance(value, dict):
        warn_unknown_keys(diagnostics, path, value, shape.fields, label)
        for key, child in shape.nested.items():
            if key in value:
                warn_unknown_fields(
                    diagnostics, child_path(path, key), value[key], child, label=label
                )
    elif shape.kind == "map" and isinstance(value, dict):
        for key, item in value.items():
            value_shape = _map_value_shape(shape, key)
            if value_shape is not None:
                warn_unknown_fields(
                    diagnostics, child_path(path, str(key)), item, value_shape, label=label
                )
    elif shape.kind == "list" and isinstance(value, list) and shape.items is not None:
        for index, item in enumerate(value):
            warn_unknown_fields(diagnostics, f"{path}[{index}]", item, shape.items, label=label)


def preserve_unknown_fields(previous: Any, current: Any, shape: JsonShape) -> Any:
    """Return ``current`` with the unknown fields of ``previous`` merged back.

    Merging happens only where ``current`` still has the modeled object: an owner
    that removes an object or a list entry removes its unknown fields too. A field
    ``current`` sets itself always wins.
    """

    if shape.kind == "object":
        if not isinstance(previous, dict) or not isinstance(current, dict):
            return current
        merged = {
            key: _preserve_child(previous, key, item, shape.nested.get(key))
            for key, item in current.items()
        }
        for key, item in previous.items():
            if key not in shape.fields and key not in merged:
                merged[key] = copy.deepcopy(item)
        return merged
    if shape.kind == "map":
        if not isinstance(previous, dict) or not isinstance(current, dict):
            return current
        return {
            key: _preserve_child(previous, key, item, _map_value_shape(shape, key))
            for key, item in current.items()
        }
    if shape.kind == "list":
        if not isinstance(previous, list) or not isinstance(current, list):
            return current
        assert shape.items is not None and shape.key is not None
        earlier = _unique_entries(previous, shape.key)
        return [_preserve_entry(earlier, entry, shape.items, shape.key) for entry in current]
    return current


def check_json_document_writable(path: Path, fmt: JsonDocumentFormat) -> Any:
    """Return the decoded document on disk, or ``None`` when there is none.

    Raises :class:`JsonDocumentWriteError` when the file exists but fails to load
    under ``fmt.validate``: it must be repaired or reset before any write.
    """

    report, data = read_json_file(path, fmt.validate, missing_ok=True)
    if not report.exists:
        return None
    if not report.ok:
        raise JsonDocumentWriteError(_refusal_message(path, fmt, report))
    return data


def write_json_document(
    path: Path,
    body: Mapping[str, Any],
    fmt: JsonDocumentFormat,
    *,
    data_dir: Path | None = None,
    reset: bool = False,
    mode: int | None = None,
) -> None:
    """Atomically write one document, preserving the unknown fields on disk.

    ``body`` holds the owner's modeled fields; ``format_version`` is set here.
    Entries of ``drop_empty`` maps that are still empty objects after the merge
    are left out. ``reset=True`` replaces the file without the load guard or preservation, for
    an explicit user reset. Raises :class:`JsonDocumentWriteError` when the file
    on disk failed to load, ``TypeError``/``ValueError`` for an unserializable
    body, and ``OSError`` for write failures.
    """

    payload = {key: value for key, value in body.items() if key != FORMAT_VERSION_FIELD}
    if not reset:
        previous = check_json_document_writable(path, fmt)
        if previous is not None:
            payload = preserve_unknown_fields(previous, payload, fmt.shape)
            payload.pop(FORMAT_VERSION_FIELD, None)
    payload = _drop_empty_entries(payload, fmt.shape)
    text = render_json_document(payload, version=fmt.version, sort_keys=fmt.sort_keys)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, text, data_dir=data_dir, mode=mode)


def render_json_document(
    payload: Mapping[str, Any],
    *,
    version: int,
    sort_keys: bool = False,
) -> str:
    """Serialize a document with ``format_version`` first and a trailing newline."""

    body = _sorted_json(dict(payload)) if sort_keys else dict(payload)
    body.pop(FORMAT_VERSION_FIELD, None)
    document = {FORMAT_VERSION_FIELD: version, **body}
    return json.dumps(document, ensure_ascii=False, indent=2) + "\n"


def snapshot_document_paths(data_dir: Path) -> tuple[str, ...]:
    """Every existing snapshot document in ``data_dir`` as a relative POSIX path.

    Only regular files reached through real directories count. Symbolic links,
    Windows junctions and names starting with ``.`` (staging files) never match, so
    a data snapshot neither copies nor replaces them. Operational listing failures raise
    ``OSError``; a missing directory simply holds no documents.
    """

    root = Path(data_dir)
    found: set[str] = set()
    for pattern in SNAPSHOT_DOCUMENTS.values():
        found.update(_matching_documents(root, tuple(pattern.split("/")), ()))
    return tuple(sorted(found))


def is_snapshot_document_path(path: str) -> bool:
    """Whether ``path`` is a relative POSIX path the snapshot document set may contain."""

    if not isinstance(path, str) or not path or any(char in path for char in "\\:\0"):
        return False
    parts = path.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        return False
    return any(
        len(parts) == len(pattern) and all(map(_segment_matches, parts, pattern))
        for pattern in (tuple(value.split("/")) for value in SNAPSHOT_DOCUMENTS.values())
    )


def _is_link(status: os.stat_result) -> bool:
    """A symbolic link or a Windows junction; other reparse points are ordinary files."""
    junction: int | None = getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", None)
    tag: int | None = getattr(status, "st_reparse_tag", None)
    return stat.S_ISLNK(status.st_mode) or (junction is not None and tag == junction)


def _segment_matches(name: str, pattern: str) -> bool:
    return not name.startswith(".") and fnmatch.fnmatchcase(name, pattern)


def _matching_documents(
    directory: Path, parts: tuple[str, ...], prefix: tuple[str, ...]
) -> Iterator[str]:
    segment, rest = parts[0], parts[1:]
    if any(char in segment for char in "*?["):
        try:
            with os.scandir(directory) as entries:
                names = sorted(
                    entry.name for entry in entries if _segment_matches(entry.name, segment)
                )
        except (FileNotFoundError, NotADirectoryError):
            return
    else:
        names = [segment]
    for name in names:
        try:
            status = os.lstat(directory / name)
        except (FileNotFoundError, NotADirectoryError):
            continue
        if _is_link(status):
            continue
        if rest:
            if stat.S_ISDIR(status.st_mode):
                yield from _matching_documents(directory / name, rest, (*prefix, name))
        elif stat.S_ISREG(status.st_mode):
            yield "/".join((*prefix, name))


def _refusal_message(path: Path, fmt: JsonDocumentFormat, report: JsonValidationReport) -> str:
    details = "; ".join(format_report_diagnostics(report))
    return (
        f"Refusing to overwrite {fmt.name} {path}: the file failed to load ({details}). "
        "Repair it or reset it explicitly."
    )


def _drop_empty_entries(value: Any, shape: JsonShape | None) -> Any:
    """Leave out the empty entries of every ``drop_empty`` map within ``value``."""

    if shape is None:
        return value
    if shape.kind == "object" and isinstance(value, dict):
        return {
            key: _drop_empty_entries(item, shape.nested.get(key)) for key, item in value.items()
        }
    if shape.kind == "map" and isinstance(value, dict):
        entries = {
            key: _drop_empty_entries(item, _map_value_shape(shape, key))
            for key, item in value.items()
        }
        if shape.drop_empty:
            return {key: item for key, item in entries.items() if item != {}}
        return entries
    if shape.kind == "list" and isinstance(value, list):
        return [_drop_empty_entries(item, shape.items) for item in value]
    return value


def _map_value_shape(shape: JsonShape, key: Any) -> JsonShape | None:
    known = shape.known.get(key)
    return known if known is not None else shape.values


def _strip_child(value: Any, shape: JsonShape | None) -> Any:
    if shape is None:
        return copy.deepcopy(value)
    return strip_unknown_fields(value, shape)


def _preserve_child(
    previous: Mapping[str, Any], key: str, current: Any, shape: JsonShape | None
) -> Any:
    if shape is None or key not in previous:
        return current
    return preserve_unknown_fields(previous[key], current, shape)


def _unique_entries(entries: list[Any], key: str) -> dict[Any, Any]:
    """Index entries by identity, leaving out identities that are not unique."""

    index: dict[Any, Any] = {}
    duplicates: set[Any] = set()
    for entry in entries:
        identity = _identity(entry, key)
        if identity is None:
            continue
        if identity in index:
            duplicates.add(identity)
        index[identity] = entry
    for identity in duplicates:
        del index[identity]
    return index


def _preserve_entry(earlier: Mapping[Any, Any], entry: Any, shape: JsonShape, key: str) -> Any:
    identity = _identity(entry, key)
    if identity is None or identity not in earlier:
        return entry
    return preserve_unknown_fields(earlier[identity], entry, shape)


def _identity(entry: Any, key: str) -> Any:
    if not isinstance(entry, dict):
        return None
    identity = entry.get(key)
    if isinstance(identity, bool) or not isinstance(identity, str | int):
        return None
    return identity


def _sorted_json(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _sorted_json(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [_sorted_json(item) for item in value]
    return value
