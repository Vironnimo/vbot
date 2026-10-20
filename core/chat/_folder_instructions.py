"""Folder instructions: the instruction files of the subfolders an Agent works in.

When the calls of a Tool batch worked in a subfolder of the Working Project,
Chat persists one note per folder instruction file that governs those paths
(``core/projects/instructions.py``) right after the batch's Tool Results. The
notes visible since the latest Compaction checkpoint are the ledger: a file shown
there is not shown again, and a file a Compaction summarized away is shown again
the next time the Agent works in its folder. Each note keeps the file as it was
rendered then, so every later request repeats it byte for byte.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from core.chat._message_history import effective_compaction_messages
from core.chat.messages import ChatMessage
from core.projects import FolderInstructions
from core.prompts.blocks import ToolAvailability, render_prompt_file

FOLDER_INSTRUCTIONS_NOTE_PREFIX = "[folder-instructions] "
_NOTE_VERSION = 1


@dataclass(frozen=True)
class FolderInstructionsNote:
    """One folder instruction file as the Agent received it."""

    # Absolute path of the file, the ledger key.
    file: str
    # The file and its folder relative to the repository, with ``/`` separators.
    label: str
    folder: str
    # The file as ``render_prompt_file`` framed it.
    text: str

    def note_content(self) -> str:
        payload = {
            "v": _NOTE_VERSION,
            "file": self.file,
            "label": self.label,
            "folder": self.folder,
            "text": self.text,
        }
        return FOLDER_INSTRUCTIONS_NOTE_PREFIX + json.dumps(
            payload, ensure_ascii=False, separators=(",", ":")
        )

    def reminder_text(self) -> str:
        return (
            f"Folder instructions for `{self.folder}/` from `{self.label}`. They apply to "
            "work in that folder and below; where they conflict with your Project Context "
            "or the instructions of an enclosing folder, follow them.\n\n"
            f"{self.text}"
        )


def is_folder_instructions_note(message: ChatMessage) -> bool:
    return (
        message.role == "note"
        and isinstance(message.content, str)
        and message.content.startswith(FOLDER_INSTRUCTIONS_NOTE_PREFIX)
    )


def folder_instructions_from_note(message: ChatMessage) -> FolderInstructionsNote | None:
    """Parse a ``[folder-instructions]`` note; ``None`` for other or malformed notes."""
    if not is_folder_instructions_note(message):
        return None
    assert isinstance(message.content, str)
    try:
        payload = json.loads(message.content.removeprefix(FOLDER_INSTRUCTIONS_NOTE_PREFIX))
    except ValueError:
        return None
    if not isinstance(payload, dict) or payload.get("v") != _NOTE_VERSION:
        return None
    fields = [payload.get(name) for name in ("file", "label", "folder", "text")]
    values = [value for value in fields if isinstance(value, str) and value]
    if len(values) != len(fields):
        return None
    file, label, folder, text = values
    return FolderInstructionsNote(file=file, label=label, folder=folder, text=text)


def folder_instruction_notes(
    finder: FolderInstructions,
    worked_paths: Sequence[Path],
    session_messages: list[ChatMessage],
    *,
    tool_available: ToolAvailability,
    on_read: Callable[[Path], None] | None = None,
) -> list[ChatMessage]:
    """Return the notes for the folder instruction files *worked_paths* lead to.

    Files a note since the latest Compaction checkpoint already showed are left
    out, and so is a file that cannot be read; one too large to inline shows its
    frame with the reader's notice. Blocking: reads the filesystem.
    """
    seen = [
        Path(note.file)
        for message in effective_compaction_messages(session_messages)
        if (note := folder_instructions_from_note(message)) is not None
    ]
    notes: list[ChatMessage] = []
    for file in finder.files_for(worked_paths, seen=seen):
        label = _relative(file, finder.root)
        text = render_prompt_file(file, label, tool_available=tool_available, on_read=on_read)
        if text is None:
            continue
        note = FolderInstructionsNote(
            file=file.as_posix(),
            label=label,
            folder=_relative(file.parent, finder.root),
            text=text,
        )
        notes.append(ChatMessage.note(note.note_content()))
    return notes


def _relative(path: Path, root: Path) -> str:
    return Path(os.path.relpath(path, root)).as_posix()
