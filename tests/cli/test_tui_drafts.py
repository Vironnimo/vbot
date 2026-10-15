"""Local drafts survive detach without losing concurrent input or replaying a send."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from textual.widgets import TextArea

from cli.tui.app import ConversationApp
from cli.tui.drafts import DraftStore, draft_record, restore_draft, saved_input_record
from cli.tui.state import Conversation, JsonObject
from core.config_validation import JsonConfigValidationError
from tests.cli.test_tui import Server


def test_draft_storage_preserves_addresses_pending_input_and_unknown_fields(tmp_path: Path) -> None:
    store = DraftStore("http://localhost:8420/", directory=tmp_path)
    assert store.load() == {}
    view = Conversation(
        "main@project",
        draft="Pending text",
        uncertain=True,
        pending_session_id="ses_example",
        attachments=[{"attachment_id": "att-one", "filename": "file.txt"}],
        mentions=["folder/file.py"],
        overrides={"model": "provider/model"},
        uncertain_submission={"text": "Earlier text", "attachments": [], "mentions": []},
    )
    record = draft_record(view)
    assert record is not None
    store.save({view.draft_id: record})
    document = json.loads(store.path.read_text())
    document["future"] = {"enabled": True}
    document["drafts"][view.draft_id]["future_draft"] = "keep"
    store.path.write_text(json.dumps(document), encoding="utf-8")
    reopened = DraftStore("http://localhost:8420", directory=tmp_path)
    restored = restore_draft(view.draft_id, reopened.load()[view.draft_id])
    assert draft_record(restored) == record
    restored.draft = "Later text"
    changed = draft_record(restored)
    assert changed is not None
    reopened.save({view.draft_id: changed})
    written = json.loads(store.path.read_text())
    assert written["future"] == {"enabled": True}
    assert written["drafts"][view.draft_id]["future_draft"] == "keep"
    assert DraftStore("http://localhost:8421", directory=tmp_path).load() == {}


@pytest.mark.parametrize("action", ["edit", "clear", "same_pending"])
def test_concurrent_accessors_preserve_conflicting_drafts(tmp_path: Path, action: str) -> None:
    first = DraftStore("http://server", directory=tmp_path)
    view = Conversation("main", session="session-one", draft="Original")
    original = draft_record(view)
    assert original is not None
    first.save({view.draft_id: original})
    second = DraftStore("http://server", directory=tmp_path)
    second.load()
    theirs = {**original, "draft": "Other terminal", "uncertain": action == "same_pending"}
    first.save({view.draft_id: theirs})
    ours = theirs if action == "same_pending" else {**original, "draft": "This terminal"}
    remapped = second.save({view.draft_id: ours} if action != "clear" else {})
    observer = DraftStore("http://server", directory=tmp_path)
    records = observer.load()
    assert records[view.draft_id] == theirs
    if action != "clear":
        assert records[remapped[view.draft_id]] == ours
        if action == "same_pending":
            # Acceptance by one accessor cannot erase an identical pending attempt
            # owned by the other accessor, even after its unchanged checkpoint.
            first.save({})
            second.save({remapped[view.draft_id]: ours})
            assert observer.load() == {remapped[view.draft_id]: ours}
        else:
            # A later local checkpoint updates the fork, not the other terminal's input.
            second.save({remapped[view.draft_id]: {**ours, "draft": "Local continuation"}})
            assert observer.load()[view.draft_id] == theirs
    else:
        assert remapped == {}
        assert len(records) == 1


@pytest.mark.parametrize("invalid", ["syntax", "future", "submission"])
def test_unreadable_drafts_are_never_replaced(tmp_path: Path, invalid: str) -> None:
    store = DraftStore("http://server", directory=tmp_path)
    view = Conversation("main", draft="Keep me")
    record = draft_record(view)
    assert record is not None
    store.save({view.draft_id: record})
    document = json.loads(store.path.read_text())
    if invalid == "future":
        document["format_version"] = 2
    elif invalid == "submission":
        document["drafts"][view.draft_id]["uncertain_submission"] = {"text": []}
    content = "{" if invalid == "syntax" else json.dumps(document)
    store.path.write_text(content, encoding="utf-8")
    reader = DraftStore("http://server", directory=tmp_path)
    with pytest.raises(JsonConfigValidationError):
        reader.load()
    with pytest.raises(JsonConfigValidationError):
        reader.save({view.draft_id: record})
    assert store.path.read_text() == content


@pytest.mark.asyncio
async def test_detach_restores_drafts_and_edit_recovery_without_sending(tmp_path: Path) -> None:
    server = Server()
    store = DraftStore(server.url, directory=tmp_path)
    app = ConversationApp(server, agent_id="main", draft_store=store, restored_drafts=store.load())
    saved_edit: JsonObject = {
        "draft_id": "edit-input",
        "agent": "main",
        "session": "session-one",
        "project": None,
        "project_selected": False,
        "text": "Edited text",
        "attachments": [],
        "mentions": [],
        "operation": "chat.edit",
        "message_id": "msg-one",
        "uncertain": True,
        "request": {"content": "Edited text"},
    }
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.query_one("#composer", TextArea).load_text("Keep this draft after detach")
        await pilot.pause()
        app.saved_drafts.append(copy.deepcopy(saved_edit))
        await pilot.press("ctrl+q")
        await pilot.pause()
    next_server = Server()
    next_store = DraftStore(server.url, directory=tmp_path)
    restored = next_store.load()
    assert restored["edit-input"] == saved_input_record(saved_edit)
    reopened = ConversationApp(
        next_server,
        agent_id="main",
        draft_store=next_store,
        restored_drafts=restored,
    )
    async with reopened.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        assert reopened.query_one("#composer", TextArea).text == "Keep this draft after detach"
        assert reopened.saved_drafts == [saved_edit]
        assert not any(method in {"chat.stream", "chat.edit"} for method, _ in next_server.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["conflict", "displaced"])
async def test_restore_draft_keeps_input_and_follows_rename_during_picker(
    tmp_path: Path, source: str
) -> None:
    server = Server()
    current = Conversation("main", session="session-one", draft="First draft")
    other = Conversation("main", session="session-one", draft="Second draft")
    first, second = draft_record(current), draft_record(other)
    assert first is not None and second is not None
    store = DraftStore(server.url, directory=tmp_path)
    store.save({current.draft_id: first, other.draft_id: second})
    app = ConversationApp(server, agent_id="main", draft_store=store, restored_drafts=store.load())
    if source == "displaced":
        app.recovered_drafts.pop(other.draft_id)
        app.saved_drafts.append({**app._saved_input(other), "draft_id": other.draft_id})
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        worker = app._start(app._restore_draft)
        await pilot.pause()
        await app._resource_changed(
            {
                "kind": "agents",
                "scope": {
                    "old_agent_id": "main",
                    "new_agent_id": "renamed",
                },
            }
        )
        await pilot.press("down", "enter")
        await worker.wait()
        assert app.query_one("#composer", TextArea).text == "Second draft"
        assert app.conversation is not None and app.conversation.agent == "renamed"
        assert app.recovered_drafts[current.draft_id]["draft"] == "First draft"
        assert app.recovered_drafts[current.draft_id]["agent"] == "renamed"
        await app.persist_drafts()
        assert len(store.load()) == 2
        assert not any(method == "chat.stream" for method, _ in server.calls)
