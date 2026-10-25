"""Memory scopes, budgets, prompt declarations and mutation result contracts."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from core.memory._history import MemoryRevertConflict, MemoryRevision
from core.utils.errors import VBotError
from core.utils.log_conditions import LoggedConditions
from core.utils.logging import get_logger

_LOGGER = get_logger("memory")


# Memory files whose prompt section the process has logged as cut to its budget.
_PROMPT_CUT_CONDITIONS = LoggedConditions()


MemoryScope = Literal["user", "agent", "project"]


MemoryPromptMode = Literal["off", "agent", "agent_user"]


MEMORY_SCOPES: tuple[MemoryScope, ...] = ("user", "agent", "project")


PERSONAL_MEMORY_SCOPES: tuple[MemoryScope, ...] = ("user", "agent")


MEMORY_FILES: dict[MemoryScope, str] = {
    "user": "USER.md",
    "agent": "MEMORY.md",
    "project": "MEMORY.md",
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


MEMORY_PROMPT_MODE_SCOPES: dict[MemoryPromptMode, tuple[MemoryScope, ...]] = {
    MEMORY_PROMPT_MODE_OFF: (),
    MEMORY_PROMPT_MODE_AGENT: ("agent",),
    MEMORY_PROMPT_MODE_AGENT_USER: ("agent", "user"),
}


# The heading each scope renders under in the memory block. The file itself holds only
# the entries (bare "- " bullets); this label is added at render time, so the model
# never sees the backing filename — it edits memory through the tool, not the file.
# "Agent"/"User" map transparently onto the tool's scope enum.
MEMORY_SCOPE_LABELS: dict[MemoryScope, str] = {
    "agent": "# Agent Memory",
    "user": "# User Profile",
    "project": "# Project Memory",
}


# Rendered in place of the bullet list for a scope with no entries (a not-yet-created
# or emptied file), so the model still sees that the scope exists.
_EMPTY_SCOPE_TEXT = "No entries yet."


_BULLET_PREFIX = "- "


_WHITESPACE_PATTERN = re.compile(r"\s+")


_MAX_ENTRY_LENGTH = 2_000


# A hand-edited file can exceed the normal entry budgets. Bound every Memory
# read before decoding, as other prompt files do, and never rewrite a partial read.
_MAX_FILE_BYTES = 128 * 1024


# Per-scope total budget over the tool-managed entries (sum of normalized entry
# content lengths), bounding how much pinned memory is injected into every prompt.
# Both exceed _MAX_ENTRY_LENGTH so a single normal add into an empty scope always
# fits; agent notes accumulate more than the user profile, so its budget is larger.
_MAX_SCOPE_BUDGET: dict[MemoryScope, int] = {"agent": 4_000, "user": 3_000, "project": 3_000}


# The budget also caps what the System Prompt shows of a scope, because a hand-edited
# file can exceed it. The prompt shows the entries that fit, in file order, and this
# notice in place of the rest. The label above keeps the true total, so the Agent
# also sees that the scope is over budget.
_PROMPT_CUT_NOTICE = (
    "[Not shown: {count} {noun} ({chars} characters) that do not fit the "
    "{budget}-character limit of this section.{list_hint}]"
)


# Appended to the notice only when the Agent can call the memory Tool.
_PROMPT_CUT_LIST_HINT = (
    " Call `{memory_tool}` with action `list` and scope `{scope}` to see every entry."
)


# Behavioral guidance rendered at the top of the pinned-memory block. It is the
# editable default text of the ``memory:guidance`` block (D6): the memory domain
# declares the block, so the guidance ships with its owner instead of as a
# hardcoded prompt string. Because the guidance is the block's own non-empty text
# and the block is gated on "memory tool enabled" (not "memory files non-empty"),
# it now appears whenever ``memory_prompt_mode != off`` — including before the first
# entry, when the agent needs it most (this is the empty-memory fix from D5).
# Complements the memory tool's description with routing and writing quality. It
# states the same routing rule as the Skill guidance and the review briefs
# (``learning-routing.md``): task lessons and task-specific preferences live in
# the task's Skill, Memory keeps only what matters in every Session. The one
# non-obvious writing rule (declarative facts round-trip safely, imperative
# self-instructions do not) gets a user and a project example.
_MEMORY_GUIDANCE = (
    "Memory holds facts that matter in every future Session, whatever the task. Lessons "
    "about how to do one kind of task, including the user's preferences and corrections for "
    "that work, belong in that task's Skill, not in Memory, even when you cannot change "
    "Skills. User Memory holds who the user is and preferences that apply to all of their "
    "work, such as the language to reply in. Agent Memory holds stable environment facts; "
    "Project Memory holds only important, lasting project decisions, user preferences and "
    "corrections, not descriptions of the repository, its technology or workflows. Leave out "
    "task progress, completed-work logs, facts that are easy to look up again, transient "
    "failures, guesses and secrets. Write declarative facts, not instructions to yourself: "
    '"User prefers concise answers", not "Always answer concisely"; "Project uses pytest '
    'with xdist", not "Run tests with pytest -n 4". Leave an equivalent entry alone, replace '
    "a superseded one and merge overlapping ones; the entries below can be older than the "
    "stored ones. When you can change Memory, save a worthwhile fact in the same turn, and "
    "say it was saved only after the Tool result confirms it. A fact that belongs in Memory "
    "never goes into a Skill, even when you cannot change Memory."
)


# The ``memory:guidance`` block id and owner. The owner ``memory`` is gate 2's
# input: the block renders only when the memory tool is enabled for the agent
# (``memory_prompt_mode != off``), resolved by the manager's owner-active seam.
MEMORY_BLOCK_ID = "memory:guidance"


MEMORY_BLOCK_OWNER = "memory"


# The producer name the embedded ``{generated:memory_files}`` marker expands to.
# The manager registers this producer (its closure reads the agent/mode); the file
# reading itself lives in :func:`read_memory_files` so the data half stays in the
# memory domain.
MEMORY_FILES_PRODUCER_NAME = "memory_files"


# The block's default text: the guidance prose, then the embedded files marker, all
# inside the ``<memory>`` wrapper — one sortable unit (D2). Whenever the block renders
# (memory tool on), the marker renders each selected scope under its heading label with
# its entries, or the empty-scope placeholder when a scope has none — so the framing is
# identical before and after the lazily-created file exists.
_MEMORY_BLOCK_TEMPLATE = "<memory>\n{guidance}\n\n{{generated:{producer}}}\n</memory>"


class MemoryError(VBotError, ValueError):
    """Raised when a memory operation cannot be completed."""


class MemoryBudgetError(MemoryError):
    """A growing mutation would push a scope past its character budget."""

    def __init__(self, scope: MemoryScope, total: int, budget: int) -> None:
        self.scope: MemoryScope = scope
        self.total = total
        self.budget = budget
        super().__init__(
            f"Memory '{scope}' scope would hold {total}/{budget} characters; free at least "
            f"{total - budget} characters by removing or shortening entries first."
        )


class MemoryMatchError(MemoryError):
    """Entry text identified no entry, or more than one, in one scope.

    ``matches`` holds the matching entries of an ambiguous locator and is empty
    when nothing matched; ``entries`` is the scope's current entry list so a caller
    can show the Agent what it can address instead.
    """

    def __init__(
        self,
        scope: MemoryScope,
        old_text: str,
        *,
        matches: Sequence[str],
        entries: Sequence[str],
    ) -> None:
        self.scope: MemoryScope = scope
        self.old_text = old_text
        self.matches = tuple(matches)
        self.entries = tuple(entries)
        if self.matches:
            message = (
                f"old_text matches {len(self.matches)} entries in memory scope '{scope}'; "
                "use a part that appears in only one entry."
            )
        else:
            message = f"No entry in memory scope '{scope}' contains the old_text."
        super().__init__(message)


class MemoryRevertError(MemoryError):
    """Reverting would overwrite later changes; nothing was changed."""

    def __init__(self, conflicts: Sequence[MemoryRevertConflict]) -> None:
        self.conflicts = tuple(conflicts)
        lines = ["Nothing was reverted: later changes built on these ones."]
        for conflict in self.conflicts:
            later = ", ".join(str(revision) for revision in conflict.later)
            noun = "revisions" if len(conflict.later) > 1 else "revision"
            if not conflict.missing:
                lines.append(
                    f"- Revision {conflict.revision}: {noun} {later} changed the entry "
                    f'"{conflict.text}" since.'
                )
                continue
            reason = f"{noun} {later} changed it since" if later else "it was changed since"
            lines.append(
                f'- Revision {conflict.revision}: no entry reads "{conflict.text}"; {reason}.'
            )
        lines.append(
            "Revert the later revisions together with these ones, or change the entries directly."
        )
        super().__init__("\n".join(lines))


class MemoryRevertIncompleteError(MemoryError):
    """A revert failed after writing some scopes, and restoring them failed too.

    ``changed`` names the scopes that keep their reverted entries; every other
    scope is unchanged. The history records those scopes' reverted entries as
    ``revert`` revisions (``revisions``; fewer when the history cannot record),
    so a later revert of the same revisions takes back only the rest.
    ``failure`` is the write error that stopped the revert.
    """

    def __init__(self, changed: Sequence[MemoryScope], failure: Exception) -> None:
        self.changed: tuple[MemoryScope, ...] = tuple(changed)
        self.failure = failure
        self.revisions: tuple[MemoryRevision, ...] = ()
        names = " and ".join(self.changed)
        super().__init__(
            f"The revert failed part-way ({failure}). The {names} Memory could not be "
            "restored and keeps its reverted entries; the other Memory is unchanged."
        )


@dataclass(frozen=True)
class MemoryRevertResult:
    """What a revert changed: the scopes it wrote and the revisions that recorded them.

    ``revisions`` holds one ``revert`` revision per recorded scope. It is shorter
    than ``changed`` when the history could not record a change; those files
    changed all the same.
    """

    changed: tuple[MemoryScope, ...]
    revisions: tuple[MemoryRevision, ...]


@dataclass(frozen=True)
class _RevertPlan:
    """A checked revert: each scope's file, content and entries before and after it."""

    paths: dict[MemoryScope, Path]
    originals: dict[MemoryScope, bytes | None]
    current: dict[MemoryScope, list[str]]
    reverted: dict[MemoryScope, list[str]]
    targets: list[MemoryRevision]
    changed: list[MemoryScope]


@dataclass(frozen=True)
class MemoryWriter:
    """Who changes Memory, recorded in the log line and the Memory history.

    ``actor`` is ``tool``, ``rpc`` or ``internal`` (direct in-process callers);
    ``session_id``/``run_id`` name the Run of a Tool change and ``run_kind``
    its Run kind.
    """

    agent_id: str | None = None
    actor: str = "internal"
    session_id: str | None = None
    run_id: str | None = None
    run_kind: str | None = None
    project_id: str | None = None


_INTERNAL_WRITER = MemoryWriter()


@dataclass(frozen=True)
class MemoryTextChange:
    """Outcome of one entry addressed by its text: previous text and new text or None.

    ``revision`` is the Memory history revision that recorded the change, when
    one was recorded.
    """

    scope: MemoryScope
    previous: str
    current: str | None
    revision: int | None = None


@dataclass(frozen=True)
class MemoryEntry:
    """One tool-managed pinned memory entry.

    A mutation's result also names the Memory history ``revision`` that
    recorded it, when one was recorded; listed entries carry none.
    """

    id: int
    scope: MemoryScope
    content: str
    revision: int | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "scope": self.scope,
            "content": self.content,
        }


def validate_memory_scope(scope: object) -> MemoryScope:
    if scope == "user":
        return "user"
    if scope == "agent":
        return "agent"
    if scope == "project":
        return "project"
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
