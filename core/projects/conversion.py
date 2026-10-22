"""The converter of ``project.json`` from format version 1 to version 2.

Version 2 changed what a Project's instruction settings mean. In version 1 the
repository's instruction files loaded only through ``auto_load`` (where new
Projects got ``AGENTS.md`` seeded) and through the instruction Sources
``claude.instructions``, ``gemini.instructions`` and ``copilot.instructions``.
In version 2 the main instruction file loads on its own while
``auto_instructions`` is on (``core/projects/instructions.py``), and instruction
files are no Sources. The converter keeps what each Project loaded:

- the files of every enabled instruction Source that exist are appended to
  ``auto_load`` (each once), and the instruction Sources leave ``sources``;
- ``auto_instructions: false`` is written when the main instruction file was
  not among the files the Project loaded.

Builds of the ``main`` channel ran the version 2 meaning on version 1
documents for a while, so a version 1 document may already carry it: one that
holds ``auto_instructions`` (written only when the user switched it off), and
one without an instruction Source that was created after the change, keep their
instruction settings. Only this module knows version 1. It runs in an update's
verification start and through ``vbot data-store convert``, never in normal
startup (``json-documents.md``).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from core.json_documents import (
    DURABLE_DOCUMENTS,
    FORMAT_VERSION_FIELD,
    document_change,
    matching_data_paths,
    render_json_document,
)
from core.projects.instructions import MAIN_INSTRUCTION_FILES, main_instruction_file
from core.projects.projects import PROJECT_FORMAT_VERSION
from core.utils.atomic import atomic_write_text
from core.utils.file_status import is_file_strict

#: The format version this module converts from.
CONVERTED_PROJECT_FORMAT_VERSION = 1

# The removed instruction Sources and the files each loaded, in the order it
# loaded them, as vBot read them up to format version 1.
_INSTRUCTION_SOURCES: dict[str, tuple[str, ...]] = {
    "claude.instructions": ("CLAUDE.md", ".claude/CLAUDE.md"),
    "gemini.instructions": ("GEMINI.md",),
    "copilot.instructions": (".github/copilot-instructions.md",),
}
# When the version 2 meaning shipped (commit f493a26c83): a Project created
# earlier was written with the version 1 meaning.
_MEANING_CHANGED_AT = datetime(2026, 10, 20, 15, 15, 17, tzinfo=UTC)
_CASE_INSENSITIVE = os.name == "nt"
_PROJECT_FILENAME = "project.json"

type ConversionResult = Literal["converted", "current", "invalid"]


@dataclass(frozen=True)
class ProjectConversion:
    """What one conversion pass over a data directory did, by relative path."""

    converted: tuple[str, ...] = ()
    # Older documents that cannot be read as JSON objects; they stay unchanged.
    invalid: tuple[str, ...] = ()


def _converted(data: dict[str, Any]) -> dict[str, Any]:
    """Return the version 2 form of a decoded version 1 ``project.json``.

    Unknown fields stay. Blocking: checks which instruction files the
    repository at ``cwd`` has. A file whose status cannot be checked counts as
    present, as it does for the main instruction file.
    """
    result = {key: value for key, value in data.items() if key != FORMAT_VERSION_FIELD}
    sources = data.get("sources")
    entries = [
        entry
        for entry in (sources if isinstance(sources, list) else [])
        if isinstance(entry, dict) and entry.get("id") in _INSTRUCTION_SOURCES
    ]
    if entries and isinstance(sources, list):
        result["sources"] = [
            entry
            for entry in sources
            if not (isinstance(entry, dict) and entry.get("id") in _INSTRUCTION_SOURCES)
        ]
    cwd = data.get("cwd")
    if (
        "auto_instructions" in data
        or not (entries or _created_before_change(data.get("created_at")))
        or not isinstance(cwd, str)
        or not cwd.strip()
    ):
        return {FORMAT_VERSION_FIELD: PROJECT_FORMAT_VERSION, **result}
    root = Path(cwd)
    auto_load = data.get("auto_load")
    stored = auto_load if isinstance(auto_load, list) else []
    additional = [entry for entry in stored if isinstance(entry, str)]
    enabled = [entry for entry in entries if entry.get("enabled", True) is True]
    loaded_keys = {_identity(root, name) for name in additional}
    appended: list[str] = []
    for entry in enabled:
        for name in _INSTRUCTION_SOURCES[entry["id"]]:
            if _file_present(root / name) and (key := _identity(root, name)) not in loaded_keys:
                loaded_keys.add(key)
                appended.append(name)
    if appended:
        result["auto_load"] = [*stored, *appended]
    main = main_instruction_file(root)
    if main is not None:
        main_loaded = _identity(root, main) in loaded_keys
    else:
        # No main instruction file now: keep loading one that appears when the
        # Project named such a file or had an instruction Source on.
        candidates = {_identity(root, name) for name in MAIN_INSTRUCTION_FILES}
        main_loaded = bool(enabled) or not candidates.isdisjoint(loaded_keys)
    if not main_loaded:
        result["auto_instructions"] = False
    return {FORMAT_VERSION_FIELD: PROJECT_FORMAT_VERSION, **result}


def _convert_file(path: Path, *, snapshot_document: bool = True) -> ConversionResult:
    """Convert one ``project.json`` of version 1 in place; later versions stay as they are.

    ``converted``: rewritten as version 2. ``current``: already version 2 or
    newer, or missing. ``invalid``: not a JSON object of a known version; it
    stays unchanged and ``doctor config`` reports it. A file that cannot be
    read raises ``OSError``. ``snapshot_document`` is false for an archived
    payload, which no data snapshot holds.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return "current"
    try:
        data = json.loads(text)
    except ValueError:
        return "invalid"
    if not isinstance(data, dict):
        return "invalid"
    version = data.get(FORMAT_VERSION_FIELD)
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        return "invalid"
    if version != CONVERTED_PROJECT_FORMAT_VERSION:
        return "current"
    rendered = render_json_document(_converted(data), version=PROJECT_FORMAT_VERSION)
    if snapshot_document:
        with document_change(path):
            atomic_write_text(path, rendered)
    else:
        atomic_write_text(path, rendered)
    return "converted"


def convert_archived_project(anchor: Path) -> ConversionResult:
    """Convert the ``project.json`` of an archived Project Anchor directory in place."""
    return _convert_file(anchor / _PROJECT_FILENAME, snapshot_document=False)


def convert_project_documents(data_dir: Path) -> ProjectConversion:
    """Convert every Project configuration of version 1 in ``data_dir``.

    Idempotent: a converted document is version 2 and stays unchanged on the
    next pass. Archived Projects are converted by ``core.archive``.
    """
    converted: list[str] = []
    invalid: list[str] = []
    for relative in matching_data_paths(data_dir, (DURABLE_DOCUMENTS["project"],)):
        outcome = _convert_file(data_dir / relative)
        if outcome == "converted":
            converted.append(relative)
        elif outcome == "invalid":
            invalid.append(relative)
    return ProjectConversion(tuple(converted), tuple(invalid))


def pending_project_documents(data_dir: Path) -> tuple[str, ...]:
    """The Project configurations in ``data_dir`` that a conversion would change."""
    pending: list[str] = []
    for relative in matching_data_paths(data_dir, (DURABLE_DOCUMENTS["project"],)):
        if project_file_needs_conversion(data_dir / relative):
            pending.append(relative)
    return tuple(pending)


def project_file_needs_conversion(path: Path) -> bool:
    """Whether ``path`` holds a ``project.json`` of version 1. Raises ``OSError``."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError, ValueError:
        return False
    if not isinstance(data, dict):
        return False
    version = data.get(FORMAT_VERSION_FIELD)
    return not isinstance(version, bool) and version == CONVERTED_PROJECT_FORMAT_VERSION


def _created_before_change(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        created = datetime.fromisoformat(value)
    except ValueError:
        return False
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    return created < _MEANING_CHANGED_AT


def _file_present(path: Path) -> bool:
    try:
        return is_file_strict(path)
    except OSError:
        return True


def _identity(root: Path, name: str) -> str:
    text = os.path.normpath(root / name)
    return text.casefold() if _CASE_INSENSITIVE else text
