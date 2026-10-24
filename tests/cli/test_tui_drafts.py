"""Local drafts survive detach without losing concurrent input or replaying a send."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import override

import pytest
from textual.widgets import TextArea

from cli.tui.app import ConversationApp
from cli.tui.drafts import DraftStore, draft_record, restore_draft, saved_input_record
from cli.tui.state import Conversation, JsonObject
from cli.tui.transport import ClientError
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
        uncertain_submission={
            "text": "Earlier text",
            "attachments": [],
            "mentions": [],
            "request": {"input_id": "inp_earlier", "content": "Earlier text"},
        },
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


@pytest.mark.asyncio
@pytest.mark.parametrize("recovery", ["retry", "restore", "edited", "retired"])
async def test_restored_submission_retains_its_identity_until_the_input_changes(
    tmp_path: Path, recovery: str
) -> None:
    class ReceiptServer(Server):
        admitted = False

        @override
        async def rpc(self, method: str, params: JsonObject | None = None) -> JsonObject:
            params = params or {}
            if method == "chat.stream":
                self.calls.append((method, copy.deepcopy(params)))
                if self.fail_send:
                    raise ClientError("Lost response", state="unknown")
                self.admitted = True
                return {
                    "accepted": True,
                    "input_id": params["input_id"],
                    "session_id": params["new_session"]["session_id"],
                    "input_state": "removed",
                    "session_available": recovery != "retired",
                    "agent_id": "renamed",
                }
            if (
                method == "chat.timeline"
                and not self.admitted
                and params.get("session_id") != "session-one"
            ):
                raise ClientError("Not found", code="session_not_found")
            return await super().rpc(method, params)

    server = ReceiptServer()
    server.fail_send = True
    store = DraftStore(server.url, directory=tmp_path)
    app = ConversationApp(server, agent_id="main", project_id="one", draft_store=store)
    async with app.run_test(size=(100, 35)) as pilot:
        await pilot.pause()
        app.query_one("#composer", TextArea).load_text("Original message")
        await app.action_send().wait()
        await app.submissions.persist()
        original = next(params for method, params in server.calls if method == "chat.stream")
        assert original["input_id"]
        await pilot.press("ctrl+q")

    reopened_store = DraftStore(server.url, directory=tmp_path)
    next_server = ReceiptServer()
    reopened = ConversationApp(
        next_server,
        agent_id="main",
        project_id="one",
        draft_store=reopened_store,
        restored_drafts=reopened_store.load(),
    )
    async with reopened.run_test(size=(100, 35)) as pilot:
        await pilot.pause()
        assert not any(method == "chat.stream" for method, _ in next_server.calls)
        assert reopened.conversation is not None and reopened.conversation.uncertain
        earlier_queue_reads = sum(method == "chat.queue_list" for method, _ in next_server.calls)
        worker = reopened.action_send()
        await pilot.pause()
        # Restore keeps the prepared identity, but sends nothing until Enter.
        await pilot.press(*(["down"] * (4 if recovery in {"retry", "retired"} else 3)), "enter")
        await worker.wait()
        if recovery not in {"retry", "retired"}:
            assert not any(method == "chat.stream" for method, _ in next_server.calls)
            if recovery == "edited":
                reopened.query_one("#composer", TextArea).load_text("Edited message")
                await pilot.pause()
            await reopened.action_send().wait()
        sent = [params for method, params in next_server.calls if method == "chat.stream"]
        assert len(sent) == 1
        if recovery == "edited":
            assert sent[0]["input_id"] != original["input_id"]
            assert sent[0]["new_session"]["session_id"] != original["new_session"]["session_id"]
            assert sent[0]["content"] == "Edited message"
        else:
            assert sent[0] == original
        assert not reopened.conversation.uncertain
        assert reopened.conversation.retry_submission is None
        if recovery == "retired":
            assert reopened.conversation.session is None
            assert reopened.conversation.pending_session_id is None
            assert reopened.conversation.agent == "main"
            assert (
                sum(method == "chat.queue_list" for method, _ in next_server.calls)
                == earlier_queue_reads
            )
        else:
            assert reopened.conversation.session == sent[0]["new_session"]["session_id"]
            assert reopened.conversation.agent == "renamed"
            assert any(method == "chat.queue_list" for method, _ in next_server.calls)


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
        app.submissions.saved_drafts.append(copy.deepcopy(saved_edit))
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
        assert reopened.submissions.saved_drafts == [saved_edit]
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
        app.submissions.recovered_drafts.pop(other.draft_id)
        app.submissions.saved_drafts.append(
            {**app.submissions.snapshot(other), "draft_id": other.draft_id}
        )
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        worker = app._start(app.submissions.restore)
        await pilot.pause()
        await app.navigation.resource_changed(
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
        assert app.submissions.recovered_drafts[current.draft_id]["draft"] == "First draft"
        assert app.submissions.recovered_drafts[current.draft_id]["agent"] == "renamed"
        await app.submissions.persist()
        assert len(store.load()) == 2
        assert not any(method == "chat.stream" for method, _ in server.calls)
