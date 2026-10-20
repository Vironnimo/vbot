"""Contracts of folder instruction notes: their ledger and their rendering."""

from __future__ import annotations

from pathlib import Path

from core.chat import ChatMessage
from core.chat._folder_instructions import (
    FOLDER_INSTRUCTIONS_NOTE_PREFIX,
    folder_instruction_notes,
)
from core.chat.wire_shaping import note_request_messages
from core.projects import FolderInstructions


def test_a_compaction_makes_shown_folder_instructions_due_again(tmp_path: Path) -> None:
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "AGENTS.md").write_text("App <rules>", encoding="utf-8")
    finder = FolderInstructions.for_project(tmp_path, [])
    worked = [tmp_path / "app" / "main.py"]

    def notes(history: list[ChatMessage]) -> list[ChatMessage]:
        return folder_instruction_notes(finder, worked, history, tool_available=lambda _: False)

    [note] = notes([])
    assert notes([note]) == []
    checkpoint = ChatMessage.compaction_checkpoint(
        summary="Summary", projection=[], compacted_token_count=10
    )
    assert [again.content for again in notes([note, checkpoint])] == [note.content]

    # The note renders as a System Reminder; a malformed one is left out.
    malformed = ChatMessage.note(FOLDER_INSTRUCTIONS_NOTE_PREFIX + "{")
    [request] = note_request_messages([malformed, note])
    content = str(request["content"])
    assert content.count("<system-reminder>") == 1
    assert "Folder instructions for `app/` from `app/AGENTS.md`." in content
    assert '<file name="app/AGENTS.md">\nApp <rules>\n</file>' in content
