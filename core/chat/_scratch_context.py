"""Scratch folder announcements and their Compaction projection."""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import replace
from typing import TYPE_CHECKING

from core.chat._message_history import effective_compaction_messages
from core.chat.messages import ChatMessage
from core.runs import RunKind

if TYPE_CHECKING:
    from core.tools.scratch_areas import ScratchFolder

SCRATCH_ANNOUNCED_META_KEY = "scratch_announced"
_FILE_TOOLS = frozenset({"bash", "terminal", "write", "edit", "apply_patch"})
SCRATCH_RUN_KINDS = frozenset(
    {
        RunKind.USER,
        RunKind.CHANNEL,
        RunKind.CRON,
        RunKind.CALENDAR,
        RunKind.SUBAGENT,
        RunKind.SYSTEM,
    }
)


def uses_scratch(run_kind: RunKind, tool_names: Collection[str]) -> bool:
    return run_kind in SCRATCH_RUN_KINDS and not _FILE_TOOLS.isdisjoint(tool_names)


def scratch_note_visible(messages: list[ChatMessage], note_id: object) -> bool:
    return any(
        message.role == "note" and message.id == note_id
        for message in effective_compaction_messages(messages)
    )


def scratch_note(folder: ScratchFolder) -> ChatMessage:
    return ChatMessage.note(
        f"Your Scratch folder is {folder.own.as_posix()}. "
        "Use it for temporary files such as helper scripts, downloads and intermediate "
        "results, unless the user requests another location. Save deliverables in the "
        "working directory or the location requested by the user.\n\n"
        "Within Scratch, write only in your own folder; you can read the others. "
        "When delegating, pass input paths and let each Sub-Agent use its own Scratch "
        "folder for intermediate files."
    )


def preserve_scratch_note(
    checkpoint: ChatMessage, session_messages: list[ChatMessage], note_id: object
) -> ChatMessage:
    """Keep the latest announcement verbatim, including across mid-Run Compaction."""
    announcement = next(
        (
            message
            for message in reversed(effective_compaction_messages(session_messages))
            if message.role == "note" and message.id == note_id
        ),
        None,
    )
    if announcement is None or checkpoint.projection is None:
        return checkpoint
    if any(entry.get("id") == announcement.id for entry in checkpoint.projection):
        return checkpoint
    # Keep the mandatory summary first and the retained Tool cycles intact.
    return replace(
        checkpoint,
        projection=[
            checkpoint.projection[0],
            announcement.to_dict(),
            *checkpoint.projection[1:],
        ],
    )
