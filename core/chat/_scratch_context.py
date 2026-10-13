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
SCRATCH_NOTE_PREFIX = "[scratch-area] "
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


def scratch_note(folder: ScratchFolder, *, cleared: bool, process_tools: bool) -> ChatMessage:
    content = (
        f"Your Scratch folder for intermediate work is {folder.own.as_posix()}.\n"
        f"Scratch Area: {folder.area.as_posix()}. "
        "Other folders in this area belong to this conversation's Parent or Sub-Agents. "
        "Within this area, write only in your own folder; read the others when useful. "
        "When delegating, put the inputs in your own folder and pass their paths; "
        "let each Sub-Agent keep its intermediate files in its own Scratch folder "
        "and report its result paths.\n"
    )
    if process_tools:
        content += (
            "Temporary files of your processes already go to tmp/ in your folder; "
            "VBOT_SCRATCH_DIR names your folder in commands.\n"
        )
    content += (
        "vBot deletes this area after 7 days without activity. "
        "You do not need to clean it up; you can delete files you have finished with.\n"
        "Scratch is temporary working space, not Memory or a delivery location. "
        "Put results the user will keep and files you hand to the user in the working "
        "directory or Workspace; put notes needed in a later conversation in the "
        "Workspace or Memory."
    )
    if cleared:
        content += (
            "\nYour Scratch folder has been recreated or changed. "
            "Do not assume files at earlier Scratch paths still exist; "
            "use the folder above for new work."
        )
    return ChatMessage.note(SCRATCH_NOTE_PREFIX + content)


def preserve_scratch_note(
    checkpoint: ChatMessage, session_messages: list[ChatMessage]
) -> ChatMessage:
    """Keep the latest announcement verbatim, including across mid-Run Compaction."""
    announcement = next(
        (
            message
            for message in reversed(effective_compaction_messages(session_messages))
            if message.role == "note"
            and isinstance(message.content, str)
            and message.content.startswith(SCRATCH_NOTE_PREFIX)
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
