"""Pinned memory service backed by agent workspace Markdown files."""

from __future__ import annotations

import os
import re
import threading
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Literal

from core.utils.errors import VBotError

MemoryScope = Literal["user", "agent"]
MemoryPromptMode = Literal["off", "agent", "agent_user"]

MEMORY_SCOPES = ("user", "agent")
MEMORY_FILES: dict[MemoryScope, str] = {
    "user": "USER.md",
    "agent": "MEMORY.md",
}
MEMORY_PROMPT_MODE_OFF: Literal["off"] = "off"
MEMORY_PROMPT_MODE_AGENT: Literal["agent"] = "agent"
MEMORY_PROMPT_MODE_AGENT_USER: Literal["agent_user"] = "agent_user"
DEFAULT_MEMORY_PROMPT_MODE: MemoryPromptMode = MEMORY_PROMPT_MODE_AGENT_USER
MEMORY_PROMPT_MODES: tuple[MemoryPromptMode, ...] = (
    MEMORY_PROMPT_MODE_OFF,
    MEMORY_PROMPT_MODE_AGENT,
    MEMORY_PROMPT_MODE_AGENT_USER,
)
MEMORY_PROMPT_FILES: dict[MemoryPromptMode, tuple[str, ...]] = {
    MEMORY_PROMPT_MODE_OFF: (),
    MEMORY_PROMPT_MODE_AGENT: (MEMORY_FILES["agent"],),
    MEMORY_PROMPT_MODE_AGENT_USER: (MEMORY_FILES["agent"], MEMORY_FILES["user"]),
}
MEMORY_SECTION_HEADING = "## Entries"
_BULLET_PREFIX = "- "
_WHITESPACE_PATTERN = re.compile(r"\s+")
_MAX_ENTRY_LENGTH = 2_000
# Per-scope total budget over the tool-managed entries (sum of normalized entry
# content lengths), bounding how much pinned memory is injected into every prompt.
# Both exceed _MAX_ENTRY_LENGTH so a single normal add into an empty scope always
# fits; agent notes accumulate more than the user profile, so its budget is larger.
_MAX_SCOPE_BUDGET: dict[MemoryScope, int] = {"agent": 4_000, "user": 3_000}
# Behavioral guidance rendered at the top of the pinned-memory block, so it appears
# only when memory is enabled (the block collapses to "" otherwise). Complements the
# memory tool's WHEN/SKIP description with the one writing-quality rule that is not
# obvious: declarative facts round-trip safely, imperative self-instructions do not.
# NOTE: this text is hardcoded today; it is a candidate to become user-editable once
# the System Prompt assembly is reworked (see stuff/HANDOFF-system-prompt-architecture.md).
_MEMORY_GUIDANCE = (
    "Write durable, declarative facts here, not instructions to yourself: "
    '"User prefers concise answers" (good), not "Always answer concisely" (bad) — '
    "imperative notes get re-read as standing directives in later sessions and can "
    "override the user's current request."
)


class MemoryError(VBotError, ValueError):
    """Raised when a memory operation cannot be completed."""


@dataclass(frozen=True)
class MemoryEntry:
    """One tool-managed pinned memory entry."""

    id: int
    scope: MemoryScope
    content: str

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "scope": self.scope,
            "content": self.content,
        }


class FilePinnedMemoryBackend:
    """Store pinned memory entries in USER.md and MEMORY.md workspace files."""

    # Tool calls in one assistant turn run concurrently (and the memory handler runs
    # in a worker thread), so two mutations to the same file would otherwise read the
    # same starting list and the last writer would clobber the other's entry — a silent
    # lost update that os.replace's atomicity does not prevent. Serialize the
    # read-modify-write per file, process-wide and keyed by canonical path so every
    # backend instance shares the same lock for a given file.
    _file_locks: ClassVar[dict[str, threading.Lock]] = {}
    _file_locks_guard: ClassVar[threading.Lock] = threading.Lock()

    @classmethod
    def _file_lock(cls, path: Path) -> threading.Lock:
        key = os.path.normcase(os.path.abspath(os.fspath(path)))
        with cls._file_locks_guard:
            lock = cls._file_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                cls._file_locks[key] = lock
            return lock

    def list_entries(self, workspace: Path, scope: MemoryScope) -> list[MemoryEntry]:
        validated_scope = validate_memory_scope(scope)
        _preamble, entries, _suffix = _read_memory_parts(self._path(workspace, validated_scope))
        return _memory_entries(validated_scope, entries)

    def add_entry(self, workspace: Path, scope: MemoryScope, content: str) -> MemoryEntry:
        validated_scope = validate_memory_scope(scope)
        path = self._path(workspace, validated_scope)
        with self._file_lock(path):
            preamble, entries, suffix = _read_memory_parts(path)
            normalized = _normalize_entry_content(content)
            if normalized in entries:
                existing_index = entries.index(normalized) + 1
                return MemoryEntry(id=existing_index, scope=validated_scope, content=normalized)

            previous_total = sum(len(entry) for entry in entries)
            entries.append(normalized)
            _enforce_scope_budget(validated_scope, entries, previous_total)
            _write_memory_parts(path, preamble, entries, suffix)
            return MemoryEntry(id=len(entries), scope=validated_scope, content=normalized)

    def replace_entry(
        self,
        workspace: Path,
        scope: MemoryScope,
        entry_id: int,
        content: str,
    ) -> MemoryEntry:
        validated_scope = validate_memory_scope(scope)
        path = self._path(workspace, validated_scope)
        with self._file_lock(path):
            preamble, entries, suffix = _read_memory_parts(path)
            index = _entry_index(entry_id, entries)
            normalized = _normalize_entry_content(content)
            previous_total = sum(len(entry) for entry in entries)
            entries[index] = normalized
            _enforce_scope_budget(validated_scope, entries, previous_total)
            _write_memory_parts(path, preamble, entries, suffix)
            return MemoryEntry(id=entry_id, scope=validated_scope, content=normalized)

    def remove_entry(self, workspace: Path, scope: MemoryScope, entry_id: int) -> MemoryEntry:
        validated_scope = validate_memory_scope(scope)
        path = self._path(workspace, validated_scope)
        with self._file_lock(path):
            preamble, entries, suffix = _read_memory_parts(path)
            index = _entry_index(entry_id, entries)
            removed = entries.pop(index)
            _write_memory_parts(path, preamble, entries, suffix)
            return MemoryEntry(id=entry_id, scope=validated_scope, content=removed)

    def build_prompt_block(self, workspace: Path, mode: MemoryPromptMode) -> str:
        validated_mode = validate_memory_prompt_mode(mode)
        blocks = [
            _read_prompt_file(Path(workspace) / filename)
            for filename in MEMORY_PROMPT_FILES[validated_mode]
        ]
        visible_blocks = [block for block in blocks if block]
        if not visible_blocks:
            return ""
        sections = [_MEMORY_GUIDANCE, *visible_blocks]
        return "<memory>\n" + "\n\n".join(sections) + "\n</memory>"

    def _path(self, workspace: Path, scope: MemoryScope) -> Path:
        workspace_path = Path(workspace)
        return workspace_path / MEMORY_FILES[scope]


class MemoryService:
    """Small facade for pinned memory operations."""

    def __init__(self, backend: FilePinnedMemoryBackend | None = None) -> None:
        self._backend = backend or FilePinnedMemoryBackend()

    def list_entries(self, workspace: Path, scope: MemoryScope) -> list[MemoryEntry]:
        return self._backend.list_entries(workspace, scope)

    def add_entry(self, workspace: Path, scope: MemoryScope, content: str) -> MemoryEntry:
        return self._backend.add_entry(workspace, scope, content)

    def replace_entry(
        self,
        workspace: Path,
        scope: MemoryScope,
        entry_id: int,
        content: str,
    ) -> MemoryEntry:
        return self._backend.replace_entry(workspace, scope, entry_id, content)

    def remove_entry(self, workspace: Path, scope: MemoryScope, entry_id: int) -> MemoryEntry:
        return self._backend.remove_entry(workspace, scope, entry_id)

    def build_prompt_block(self, workspace: Path, mode: MemoryPromptMode) -> str:
        return self._backend.build_prompt_block(workspace, mode)


def validate_memory_scope(scope: object) -> MemoryScope:
    if scope == "user":
        return "user"
    if scope == "agent":
        return "agent"
    supported = ", ".join(MEMORY_SCOPES)
    raise MemoryError(f"scope must be one of: {supported}")


def validate_memory_prompt_mode(mode: object) -> MemoryPromptMode:
    if mode == MEMORY_PROMPT_MODE_OFF:
        return MEMORY_PROMPT_MODE_OFF
    if mode == MEMORY_PROMPT_MODE_AGENT:
        return MEMORY_PROMPT_MODE_AGENT
    if mode == MEMORY_PROMPT_MODE_AGENT_USER:
        return MEMORY_PROMPT_MODE_AGENT_USER
    supported = ", ".join(MEMORY_PROMPT_MODES)
    raise MemoryError(f"memory_prompt_mode must be one of: {supported}")


def _read_prompt_file(path: Path) -> str:
    try:
        content = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""
    except OSError as exc:
        raise MemoryError(f"failed to read memory prompt file {path}: {exc}") from exc
    return f'<file name="{path.name}">\n{content}\n</file>'


def _read_memory_parts(path: Path) -> tuple[str, list[str], str]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return _default_preamble(path.name), [], ""
    except OSError as exc:
        raise MemoryError(f"failed to read memory file {path}: {exc}") from exc

    return _parse_memory_text(text)


def _parse_memory_text(text: str) -> tuple[str, list[str], str]:
    lines = text.splitlines()
    heading_index = _entries_heading_index(lines)
    if heading_index is None:
        return text.rstrip(), [], ""

    next_heading_index = _next_heading_index(lines, heading_index + 1)
    section_end = next_heading_index if next_heading_index is not None else len(lines)
    preamble = "\n".join(lines[:heading_index]).rstrip()
    section_lines = lines[heading_index + 1 : section_end]
    suffix = "\n".join(lines[section_end:]).rstrip() if next_heading_index is not None else ""
    entries = [
        _strip_entry_bullet(line) for line in section_lines if line.startswith(_BULLET_PREFIX)
    ]
    return preamble, [entry for entry in entries if entry], suffix


def _entries_heading_index(lines: list[str]) -> int | None:
    for index, line in enumerate(lines):
        if line.strip().casefold() == MEMORY_SECTION_HEADING.casefold():
            return index
    return None


def _next_heading_index(lines: list[str], start_index: int) -> int | None:
    for index in range(start_index, len(lines)):
        line = lines[index].strip()
        if line.startswith("## ") and line.casefold() != MEMORY_SECTION_HEADING.casefold():
            return index
    return None


def _strip_entry_bullet(line: str) -> str:
    # Entries are normalized to a single line and written behind a "- " prefix, so a
    # leading-dash entry round-trips by stripping the prefix exactly once. No "\-"
    # unescaping: nothing writes it, and replacing it would corrupt literal "\-" content.
    return line.removeprefix(_BULLET_PREFIX).strip()


def _write_memory_parts(path: Path, preamble: str, entries: list[str], suffix: str) -> None:
    text = _render_memory_text(preamble, entries, suffix)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.tmp")
    try:
        temp_path.write_text(text, encoding="utf-8")
        os.replace(temp_path, path)
    except OSError as exc:
        raise MemoryError(f"failed to write memory file {path}: {exc}") from exc
    finally:
        with suppress(OSError):
            temp_path.unlink(missing_ok=True)


def _render_memory_text(preamble: str, entries: list[str], suffix: str) -> str:
    blocks: list[str] = []
    if preamble.strip():
        blocks.append(preamble.strip())

    entry_lines = [MEMORY_SECTION_HEADING, ""]
    if entries:
        entry_lines.extend(f"{_BULLET_PREFIX}{_escape_entry_content(entry)}" for entry in entries)
    else:
        entry_lines.append("No tool-managed memory entries are recorded yet.")
    blocks.append("\n".join(entry_lines))

    if suffix.strip():
        blocks.append(suffix.strip())
    return "\n\n".join(blocks).rstrip() + "\n"


def _escape_entry_content(content: str) -> str:
    return content.replace("\n", " ").replace("\r", " ").strip()


def _normalize_entry_content(content: object) -> str:
    if not isinstance(content, str):
        raise MemoryError("content must be a string")
    normalized = _WHITESPACE_PATTERN.sub(" ", content).strip()
    if not normalized:
        raise MemoryError("content must be a non-empty string")
    if len(normalized) > _MAX_ENTRY_LENGTH:
        raise MemoryError(f"content must be at most {_MAX_ENTRY_LENGTH} characters")
    return normalized


def _enforce_scope_budget(scope: MemoryScope, entries: list[str], previous_total: int) -> None:
    """Reject a mutation that pushes a scope's tool-managed entries past its budget.

    A non-increasing change (shrink or remove) is always allowed so the model can
    always dig out of an over-budget state — e.g. after the budget is lowered or
    pre-existing entries already exceed it.
    """
    budget = _MAX_SCOPE_BUDGET[scope]
    total = sum(len(entry) for entry in entries)
    if total > budget and total > previous_total:
        raise MemoryError(
            f"Memory '{scope}' scope is full ({total}/{budget} characters). "
            "Remove or shorten an entry before adding (action='list', then 'remove')."
        )


def _entry_index(entry_id: int, entries: list[str]) -> int:
    if not isinstance(entry_id, int) or isinstance(entry_id, bool):
        raise MemoryError("entry_id must be an integer")
    if entry_id < 1 or entry_id > len(entries):
        raise MemoryError(f"entry_id must be between 1 and {len(entries)}")
    return entry_id - 1


def _memory_entries(scope: MemoryScope, entries: list[str]) -> list[MemoryEntry]:
    return [
        MemoryEntry(id=index, scope=scope, content=entry) for index, entry in enumerate(entries, 1)
    ]


def _default_preamble(filename: str) -> str:
    if filename == "USER.md":
        return (
            "# User Profile\n\n"
            "Use this file for stable facts about the user: preferences, communication style, "
            "expectations, workflow habits, and durable context."
        )
    return (
        "# Agent Memory\n\n"
        "Use this file for stable agent/workflow notes that should guide future sessions."
    )
