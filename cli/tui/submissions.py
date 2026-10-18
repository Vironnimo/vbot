"""Composer submission and durable local draft recovery.

Pending sends, ordinary drafts and uncertain edit records keep their distinct
shapes. Checkpoints precede admission; recovery reads never replay a mutation.
"""

from __future__ import annotations

import asyncio
import copy
import uuid
from typing import TYPE_CHECKING
from urllib.parse import quote

from textual.widgets import TextArea

from cli.tui.dialogs import Picker, TextDialog
from cli.tui.drafts import DraftStore, draft_record, restore_draft, saved_input_record
from cli.tui.mentions import resolve_mentions
from cli.tui.rendering import pretty
from cli.tui.state import Conversation, JsonObject
from cli.tui.transport import ClientError
from core.utils.ids import new_id

if TYPE_CHECKING:
    from cli.tui.app import ConversationApp


class SubmissionController:
    """Own draft checkpoints, pending admission and explicit send reconciliation."""

    def __init__(
        self,
        app: ConversationApp,
        *,
        store: DraftStore | None,
        restored: dict[str, JsonObject] | None,
    ) -> None:
        self.app = app
        self.pending: set[int] = set()
        self.history: list[str] = []
        self.saved_drafts: list[JsonObject] = []
        self.draft_store = store
        self._draft_lock = asyncio.Lock()
        self._draft_save_scheduled = False
        self.recovered_drafts: dict[str, JsonObject] = {}
        for identifier, record in (restored or {}).items():
            if recovery := record.get("edit_recovery"):
                self.saved_drafts.append(
                    {
                        **{
                            key: copy.deepcopy(record[key])
                            for key in (
                                "agent",
                                "session",
                                "project",
                                "project_selected",
                                "attachments",
                                "mentions",
                            )
                        },
                        **recovery,
                        "text": record["draft"],
                        "draft_id": identifier,
                    }
                )
                continue
            view = restore_draft(identifier, record)
            if view.key in self.app.navigation.views:
                self.recovered_drafts[identifier] = record
            else:
                self.app.navigation.views[view.key] = view

    def changed(self, view: Conversation | None) -> None:
        """Schedule a local draft checkpoint; neither this nor restore sends input."""
        if self.draft_store is not None and not self._draft_save_scheduled:
            self._draft_save_scheduled = True
            self.app.set_timer(0.75, self.save_later)

    def save_later(self) -> None:
        self._draft_save_scheduled = False
        self.app._start(self.persist, group="draft-save")

    async def persist(self) -> None:
        if self.draft_store is None:
            return
        from core.utils.workers import finish_despite_cancel

        async with self._draft_lock:
            records = copy.deepcopy(self.recovered_drafts)
            for saved in self.saved_drafts:
                records[saved["draft_id"]] = saved_input_record(saved)
            for view in self.app.navigation.views.values():
                record = draft_record(view)
                if record is not None:
                    records[view.draft_id] = record
            remapped = await finish_despite_cancel(
                asyncio.to_thread(self.draft_store.save, records)
            )
            for view in self.app.navigation.views.values():
                view.draft_id = remapped.get(view.draft_id, view.draft_id)
            for saved in self.saved_drafts:
                saved["draft_id"] = remapped.get(saved["draft_id"], saved["draft_id"])
            self.recovered_drafts = {
                remapped.get(identifier, identifier): record
                for identifier, record in self.recovered_drafts.items()
            }

    async def restore(self) -> None:
        self.app.navigation.save_view()
        choices: list[tuple[str, tuple[str, str]]] = []
        for view in self.app.navigation.views.values():
            if draft_record(view) is not None:
                choices.append(
                    (
                        f"{view.agent} · {view.session or 'New conversation'} · {view.draft[:100]}",
                        ("view", view.draft_id),
                    )
                )
        records = dict(self.recovered_drafts)
        records.update(
            {
                value["draft_id"]: saved_input_record(value)
                for value in self.saved_drafts
                if value.get("operation") != "chat.edit"
            }
        )
        choices.extend(
            (
                f"{record['agent']} · {record.get('session') or 'New conversation'} · "
                f"{record['draft'][:100]}",
                ("saved", identifier),
            )
            for identifier, record in records.items()
        )
        if not choices:
            self.app._notice("No saved drafts on this server.")
            return
        selection = await self.app.push_screen_wait(
            Picker("Restore a local draft · nothing is sent", choices)
        )
        if selection is None:
            return
        kind, identifier = selection
        if kind == "view":
            selected_view = next(
                (
                    value
                    for value in self.app.navigation.views.values()
                    if value.draft_id == identifier
                ),
                None,
            )
            if selected_view is None:
                return
            view = selected_view
        else:
            current_record = self.recovered_drafts.get(identifier)
            if current_record is None:
                saved = next(
                    (value for value in self.saved_drafts if value["draft_id"] == identifier), None
                )
                if saved is None:
                    return
                current_record = saved_input_record(saved)
            record = current_record
            view = restore_draft(identifier, record)
            previous = self.app.navigation.views.get(view.key)
            if previous is not None and previous is not view:
                self.app.navigation.save_view()
                previous_record = draft_record(previous)
                if previous_record is not None:
                    self.recovered_drafts[previous.draft_id] = previous_record
            self.recovered_drafts.pop(identifier, None)
            self.saved_drafts = [
                value for value in self.saved_drafts if value["draft_id"] != identifier
            ]
            self.app.navigation.views[view.key] = view
        await self.app.navigation.open(view)
        self.changed(view)

    async def recover(self, view: Conversation) -> bool:
        """Find a pre-addressed first admission through reads only, never by replay."""
        target = view.pending_session_id
        if view.session or not target or not view.uncertain or id(view) in self.pending:
            return False
        key = view.key
        params = {"agent_id": view.agent, "session_id": target}
        try:
            history = await self.app.client.rpc(
                "chat.timeline", {**params, "limit": 100, "child_limit": 60}
            )
        except ClientError as exc:
            if exc.code in {"session_not_found", "agent_not_found", "project_not_found"}:
                return False
            raise
        if view.key != key or view.pending_session_id != target or not view.uncertain:
            return False
        confirmed = bool(
            history.get("active_run")
            or history.get("runs")
            or history.get("messages")
            or history.get("items")
        )
        self.app.navigation.move_view(view, view.agent, target)
        view.pending_session_id = None
        if confirmed:
            self.accept(view, view.uncertain_submission or {})
        if self.app.conversation is view:
            async with self.app._render_lock:
                await self.app.transcript.clear()
        self.changed(view)
        self.app._notice(
            "Earlier message found in its Session; reconnected without resending."
            if confirmed
            else (
                "The proposed Session exists. "
                "Inspect the earlier Command's outcome before continuing."
            )
        )
        return True

    def accept(self, view: Conversation, original: JsonObject) -> None:
        view.uncertain = False
        view.uncertain_submission = None
        if view.draft == original.get("text"):
            view.draft = ""
            if self.app.conversation is view:
                self.app.query_one("#composer", TextArea).load_text("")
        submitted_ids = {item["attachment_id"] for item in original.get("attachments", [])}
        view.attachments = [
            item for item in view.attachments if item["attachment_id"] not in submitted_ids
        ]
        view.mentions = [item for item in view.mentions if item not in original.get("mentions", [])]
        self.changed(view)

    def snapshot(self, view: Conversation) -> JsonObject:
        return {
            "text": view.draft,
            "attachments": copy.deepcopy(view.attachments),
            "mentions": list(view.mentions),
            "agent": view.agent,
            "session": view.session,
            "project": view.project,
            "project_selected": view.project_selected,
            "draft_id": str(uuid.uuid4()),
        }

    async def command_result(self, view: Conversation, result: JsonObject) -> None:
        data = result.get("data") or {}
        navigation = data.get("navigation") or {}
        if navigation.get("kind") == "open_extension_page":
            route = "/".join(
                quote(part, safe="") for part in str(navigation.get("route", "")).split("/") if part
            )
            page = f"extension:{navigation['extension']}:{navigation['page']}"
            await self.app.push_screen_wait(
                TextDialog(
                    "Open the Extension page in a browser",
                    f"{self.app.client.url}/#{page}" + (f"/{route}" if route else ""),
                    read_only=True,
                )
            )
        elif navigation.get("kind") == "new_session":
            await self.app.navigation.open(Conversation(navigation.get("agent_id", view.agent)))
        elif data.get("session_id") and (
            data["session_id"] != view.session or data.get("agent_id", view.agent) != view.agent
        ):
            if data["session_id"] == view.session:
                self.app.navigation.move_view(
                    view, data.get("agent_id", view.agent), data["session_id"]
                )
                self.app._header()
            else:
                await self.app.navigation.open(
                    Conversation(data.get("agent_id", view.agent), session=data["session_id"])
                )
        if result.get("reply"):
            await self.app.push_screen_wait(
                TextDialog("Command result", result["reply"], read_only=True)
            )

    async def send(self) -> None:
        view = self.app.conversation
        if view is None or id(view) in self.pending:
            return
        if self.app.suggestions.state.visible:
            # Enter takes the highlighted suggestion instead of sending the
            # half-typed token; while its catalog is still loading, it waits.
            await self.app.suggestions.accept()
            return
        if self.app.suggestions.state.pending:
            # The catalog behind the token is still being read; wait for it rather
            # than sending a half-typed `@path` or `/model` argument as text.
            return
        if not view.available:
            self.app._notice(
                "This conversation is no longer available. Choose a live Session first."
            )
            return
        text = self.app.query_one("#composer", TextArea).text
        if view.uncertain:
            if await self.app._guard(self.recover(view)):
                await self.app._guard(self.app.navigation.refresh(view))
                return
            original = view.uncertain_submission or {
                "text": text,
                "attachments": [],
                "mentions": [],
            }
            choices = [
                ("Return to conversation", "return"),
                ("Review the exact earlier submission", "review"),
                ("The earlier message was accepted", "accepted"),
                ("It was not accepted: restore the earlier draft", "restore"),
            ]
            if not view.session and str(original.get("text", "")).lstrip().startswith("/"):
                choices.append(("The Command completed without a Session", "sessionless"))
            choice = await self.app.push_screen_wait(
                Picker(
                    "Previous send has an unknown outcome. Inspect the Session and Queue first.",
                    choices,
                )
            )
            if self.app.conversation is not view:
                return
            if choice == "review":
                await self.app.push_screen_wait(
                    TextDialog("Submission with unknown outcome", pretty(original), read_only=True)
                )
                return
            if choice in {"accepted", "sessionless"}:
                if choice == "accepted" and not view.session:
                    self.app._notice(
                        "The created Session is not confirmed yet. "
                        "Reconnect or select it before continuing."
                    )
                    return
                if choice == "sessionless":
                    view.pending_session_id = None
                self.accept(view, original)
                self.app._header()
                return
            if choice != "restore":
                return
            later_draft = self.snapshot(view)
            if any(
                later_draft[key] != original.get(key) for key in ("text", "attachments", "mentions")
            ):
                self.saved_drafts.append(later_draft)
            view.draft = original["text"]
            view.attachments = original["attachments"]
            view.mentions = original["mentions"]
            view.uncertain = False
            view.uncertain_submission = None
            self.app.query_one("#composer", TextArea).load_text(view.draft)
            self.changed(view)
            self.app._notice("Earlier draft restored. Later draft is in input history (Alt+Up).")
            return
        if not text.strip() and not view.attachments:
            self.app._notice("Nothing to send yet · write a message, or attach a file with F8.")
            return
        request = view.submission(text)
        if view.session is None:
            view.pending_session_id = view.pending_session_id or new_id("ses")
            request["new_session"]["session_id"] = view.pending_session_id
        submitted: JsonObject = {
            "text": text,
            "attachments": copy.deepcopy(view.attachments),
            "mentions": list(view.mentions),
        }
        submitted_attachments = {item["attachment_id"] for item in view.attachments}
        submitted_mentions = set(view.mentions)
        self.pending.add(id(view))
        self.changed(view)
        mutation_started = False
        try:
            mention_params = view.params(draft=True)
            request_agent = view.agent
            mention_paths = await resolve_mentions(self.app.client, mention_params, text)
            if not view.available or view.agent != request_agent:
                self.app._notice(
                    "The conversation changed while resolving files. Your draft is retained."
                )
                return
            if mention_paths:
                request["file_mentions"] = mention_paths
            submitted["request"] = copy.deepcopy(request)
            view.uncertain = True
            view.uncertain_submission = submitted
            try:
                await self.persist()
            except (OSError, ValueError) as exc:
                view.uncertain = False
                view.uncertain_submission = None
                # The composer is the authority; its change message may still be
                # queued, so mirror it here before persisting the view's draft.
                view.draft = text
                self.changed(view)
                self.app._notice(
                    f"The pending submission could not be saved: {exc}. Your message was not sent.",
                    error=True,
                )
                return
            mutation_started = True
            result = await self.app.client.rpc("chat.stream", request)
        except ClientError as exc:
            view.uncertain = mutation_started and exc.state == "unknown"
            view.uncertain_submission = submitted if view.uncertain else None
            self.changed(view)
            self.app._notice(str(exc) + " Your draft is retained.", error=True)
            self.app.navigation.schedule_refresh()
            return
        finally:
            self.pending.discard(id(view))
        view.uncertain = False
        view.uncertain_submission = None
        if text:
            self.history.append(text)
            self.history = self.history[-100:]
        if view.draft == text:
            view.draft = ""
        view.attachments = [
            item for item in view.attachments if item["attachment_id"] not in submitted_attachments
        ]
        view.mentions = [item for item in view.mentions if item not in submitted_mentions]
        if self.app.conversation is view and self.app.query_one("#composer", TextArea).text == text:
            self.app.query_one("#composer", TextArea).load_text("")
        if result.get("session_id") and view.session is None:
            self.app.navigation.views.pop(view.key, None)
            view.session = result["session_id"]
            view.pending_session_id = None
            self.app.navigation.views[view.key] = view
            if self.app.conversation is view:
                async with self.app._render_lock:
                    await self.app.transcript.clear()
        elif view.session is None:
            view.pending_session_id = None
        self.changed(view)
        if result.get("command_handled") and self.app.conversation is view:
            await self.command_result(view, result)
        else:
            self.app._notice("Queued" if result.get("queued") else "Message accepted")
        if view.session:
            await self.app._guard(self.app.navigation.refresh(view))
