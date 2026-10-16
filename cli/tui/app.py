"""A conversation-first Textual accessor for the shared vBot server."""

from __future__ import annotations

import asyncio
import copy
import json
import uuid
from collections import OrderedDict
from collections.abc import Callable, Coroutine
from contextlib import suppress
from datetime import UTC, datetime
from time import monotonic
from typing import Any, override
from urllib.parse import quote

from textual import events, on, work
from textual.app import App, ComposeResult, ScreenStackError
from textual.binding import Binding
from textual.containers import Horizontal
from textual.css.query import NoMatches
from textual.markup import escape
from textual.screen import ModalScreen
from textual.widgets import Markdown, Static, TextArea
from textual.worker import Worker

from cli.tui import composer
from cli.tui.commands import ConversationCommands
from cli.tui.completion import (
    CompletionBar,
    CompletionState,
    Suggestion,
    Trigger,
    command_suggestions,
    file_suggestions,
    matching,
    model_suggestions,
    trigger_for,
)
from cli.tui.dialogs import ExtensionDialog, Picker, TextDialog
from cli.tui.drafts import (
    DraftStore,
    draft_record,
    readdress_saved_inputs,
    restore_draft,
    saved_input_record,
)
from cli.tui.mentions import format_mention, resolve_mentions, retained_mentions
from cli.tui.models import effective_value
from cli.tui.rendering import (
    content_text,
    flatten,
    pretty,
    terminal_text,
    truncated,
)
from cli.tui.state import Conversation, JsonObject, TimelineGapError
from cli.tui.theme import DEFAULT_THEME, THEMES, VBOT_DARK, VBOT_LIGHT
from cli.tui.transcript import Transcript
from cli.tui.transport import ClientError, TerminalClient
from core.utils.ids import new_id

_sleep = asyncio.sleep

# Priority bindings that drive the suggestion popup; without it they belong to
# the focused widget (see `ConversationApp.check_action`).
_COMPLETION_ACTIONS = frozenset(
    {"completion_up", "completion_down", "completion_accept", "completion_close"}
)


def display_path(path: str) -> str:
    """A path on the server's computer, as the file pickers write it."""
    return path.replace("\\", "/")


def short_model(reference: str) -> str:
    """`provider/model::connection` reads as the Model alone in the header."""
    base = reference.partition("::")[0]
    return base.rpartition("/")[2] or base


def compact_tokens(value: int) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}".removesuffix(".0") + "M"
    if value >= 1_000:
        return f"{value // 1_000}k"
    return str(value)


class ConversationApp(App[int]):
    """Keep navigation and drafts local; all conversation decisions use server APIs."""

    TITLE = "vBot"
    # One header row, the transcript, a status row that shows only while it has
    # something to say, the composer and the settings row the WebUI puts under its
    # input. There is no key footer: the composer's placeholder names the entry
    # points, every action is reachable by name through Ctrl+K and F1 lists the
    # keys.
    CSS = """
    Screen { background: $background; }
    #header { height: 1; padding: 0 2; background: $background; }
    #header-title { width: 1fr; height: 1; color: $foreground; text-style: bold; }
    #header-agent { width: auto; height: 1; color: $text-muted; }
    #timeline { height: 1fr; padding: 1 1 0 1; scrollbar-size-vertical: 1; }
    #status { height: 1; padding: 0 2; color: $text-muted; }
    #status.idle { display: none; }
    #status.alert { color: $warning; }
    #status.error { color: $error; }
    #draft-info { height: auto; padding: 0 2; color: $warning; }
    #draft-info.empty { display: none; }
    #composer { height: auto; min-height: 3; max-height: 12; margin: 0 1; padding: 0 1;
                border: round $panel-lighten-2; background: $background; }
    #composer:focus { border: round $accent; }
    #composer .text-area--cursor-line { background: $background; }
    #settings { height: 1; padding: 0 2; }
    #settings-where { width: 1fr; height: 1; color: $text-muted; text-overflow: ellipsis; }
    #settings-model { width: auto; height: 1; color: $text-muted; }
    """
    BINDINGS = [
        Binding("enter", "send", "Send", priority=True),
        Binding("ctrl+s,alt+enter", "send", "Send", show=False, priority=True),
        Binding("shift+enter,ctrl+enter", "newline", "New line", show=False, priority=True),
        Binding("up", "completion_up", "Previous suggestion", show=False, priority=True),
        Binding("down", "completion_down", "Next suggestion", show=False, priority=True),
        Binding("tab", "completion_accept", "Use suggestion", show=False, priority=True),
        Binding("escape", "completion_close", "Close suggestions", show=False, priority=True),
        Binding("f2", "sessions", "Sessions", priority=True),
        Binding("ctrl+p", "sessions", "Sessions", show=False, priority=True),
        Binding("f3", "new", "New", priority=True),
        Binding("ctrl+n", "new", "New", show=False, priority=True),
        Binding("f4", "model", "Model", priority=True),
        Binding("f5", "activity", "Activity", show=False, priority=True),
        Binding("f6", "queue", "Queue", show=False, priority=True),
        Binding("f7", "requests", "Requests", show=False, priority=True),
        Binding("f8", "files", "Files", show=False, priority=True),
        Binding("f9", "commands", "Commands", show=False, priority=True),
        Binding("ctrl+x", "stop", "Stop", priority=True),
        Binding("f10", "stop_all", "Stop all", show=False, priority=True),
        Binding("f11", "conversation_actions", "Actions", show=False, priority=True),
        Binding("f12", "latest", "Latest", show=False, priority=True),
        Binding("ctrl+q", "detach", "Detach", priority=True),
        Binding("ctrl+k", "command_palette", "Commands", show=False, priority=True),
        Binding("ctrl+r", "refresh", "Reconnect", show=False, priority=True),
        Binding("ctrl+o", "older", "Older history", show=False, priority=True),
        Binding("ctrl+b", "previous_conversation", "Parent / back", show=False, priority=True),
        Binding("ctrl+f", "find", "Find in history", show=False, priority=True),
        Binding("ctrl+g", "copy", "Copy / export", show=False, priority=True),
        Binding("ctrl+space", "complete", "Complete", show=False, priority=True),
        Binding("alt+up", "input_history", "Input history", show=False, priority=True),
        Binding("f1", "help", "Help"),
    ]
    # The footer labels the docked palette key by this name, so it must match the
    # binding above; Textual's default (ctrl+p) belongs to Sessions here.
    COMMAND_PALETTE_BINDING = "ctrl+k"
    COMMANDS = App.COMMANDS | {ConversationCommands}

    # Set in `on_mount`; both widgets outlive the screen's lifetime.
    bar: CompletionBar
    transcript: Transcript

    def __init__(
        self,
        client: TerminalClient,
        *,
        agent_id: str | None = None,
        session_id: str | None = None,
        project_id: str | None = None,
        draft_store: DraftStore | None = None,
        restored_drafts: dict[str, JsonObject] | None = None,
    ) -> None:
        super().__init__()
        for theme in THEMES:
            self.register_theme(theme)
        self.theme = DEFAULT_THEME
        self.client = client
        self.initial_agent = agent_id
        self.initial_session = session_id
        self.initial_project = project_id
        self.conversation: Conversation | None = None
        self.views: dict[tuple[str, str | None, bool, str | None], Conversation] = {}
        self.backstack: list[Conversation] = []
        self.roster: list[tuple[str, str]] = []
        self.agents: list[JsonObject] = []
        self.projects: list[JsonObject] = []
        self.stream: Worker[Any] | None = None
        self.stream_path: str | None = None
        self.pending: set[int] = set()
        self.history: list[str] = []
        self.saved_drafts: list[JsonObject] = []
        self.connected = False
        self.loading_views: set[int] = set()
        self.roster_loaded = False
        self.history_cache: OrderedDict[int, Conversation] = OrderedDict()
        self.older_loading: set[int] = set()
        self.render_dirty = False
        self.rendering = False
        self.render_worker: Worker[Any] | None = None
        self.refresh_pending = False
        self.requests: list[JsonObject] = []
        self.queue_items: list[JsonObject] = []
        self.last_notice = ""
        self.notice_level = "info"
        self.notice_expires = 0.0
        self.completion = CompletionState()
        # Catalogs the popup offers from, keyed by conversation and kind; they are
        # fetched once per view while the popup is open and never block typing.
        self.completion_catalogs: dict[tuple[Any, ...], list[Any]] = {}
        self.completion_loading: set[tuple[Any, ...]] = set()
        self._render_lock = asyncio.Lock()
        self.draft_store = draft_store
        self._draft_lock = asyncio.Lock()
        self._draft_save_scheduled = False
        self.recovered_drafts: dict[str, JsonObject] = {}
        for identifier, record in (restored_drafts or {}).items():
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
            restored = restore_draft(identifier, record)
            if restored.key in self.views:
                self.recovered_drafts[identifier] = record
            else:
                self.views[restored.key] = restored

    def _view_changed(self, view: Conversation | None) -> None:
        """Schedule a local draft checkpoint; neither this nor restore sends input."""
        if self.draft_store is not None and not self._draft_save_scheduled:
            self._draft_save_scheduled = True
            self.set_timer(0.75, self._save_drafts_later)

    def _save_drafts_later(self) -> None:
        self._draft_save_scheduled = False
        self._start(self.persist_drafts, group="draft-save")

    async def persist_drafts(self) -> None:
        if self.draft_store is None:
            return
        from core.utils.workers import finish_despite_cancel

        async with self._draft_lock:
            records = copy.deepcopy(self.recovered_drafts)
            for saved in self.saved_drafts:
                records[saved["draft_id"]] = saved_input_record(saved)
            for view in self.views.values():
                record = draft_record(view)
                if record is not None:
                    records[view.draft_id] = record
            remapped = await finish_despite_cancel(
                asyncio.to_thread(self.draft_store.save, records)
            )
            for view in self.views.values():
                view.draft_id = remapped.get(view.draft_id, view.draft_id)
            for saved in self.saved_drafts:
                saved["draft_id"] = remapped.get(saved["draft_id"], saved["draft_id"])
            self.recovered_drafts = {
                remapped.get(identifier, identifier): record
                for identifier, record in self.recovered_drafts.items()
            }

    async def _restore_draft(self) -> None:
        self._save_view()
        choices: list[tuple[str, tuple[str, str]]] = []
        for view in self.views.values():
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
            self._notice("No saved drafts on this server.")
            return
        selection = await self.push_screen_wait(
            Picker("Restore a local draft · nothing is sent", choices)
        )
        if selection is None:
            return
        kind, identifier = selection
        if kind == "view":
            selected_view = next(
                (value for value in self.views.values() if value.draft_id == identifier), None
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
            previous = self.views.get(view.key)
            if previous is not None and previous is not view:
                self._save_view()
                previous_record = draft_record(previous)
                if previous_record is not None:
                    self.recovered_drafts[previous.draft_id] = previous_record
            self.recovered_drafts.pop(identifier, None)
            self.saved_drafts = [
                value for value in self.saved_drafts if value["draft_id"] != identifier
            ]
            self.views[view.key] = view
        await self._open(view)
        self._view_changed(view)

    @override
    def compose(self) -> ComposeResult:
        with Horizontal(id="header"):
            yield Static("", id="header-title", markup=False)
            yield Static("", id="header-agent", markup=False)
        yield Transcript(id="timeline")
        yield Static("", id="status", markup=False, classes="idle")
        yield Static("", id="draft-info", markup=False, classes="empty")
        yield CompletionBar(id="completion")
        yield TextArea(
            placeholder="Message · / commands · @ files · Ctrl+K actions · F1 keys",
            id="composer",
            soft_wrap=True,
            show_line_numbers=False,
        )
        with Horizontal(id="settings"):
            yield Static("", id="settings-where", markup=False)
            yield Static("", id="settings-model", markup=False)

    def on_mount(self) -> None:
        # Cached once: the suggestion popup and the read receipt reach for these on
        # every keystroke and every frame, and a CSS query per frame over a long
        # transcript is not free.
        self.bar = self.query_one(CompletionBar)
        self.transcript = self.query_one("#timeline", Transcript)
        self.transcript.placeholder("Choose an Agent or resume a Session.")
        self.set_interval(0.08, self._render_if_dirty)
        self._start(self._events, group="events", exclusive=True)
        self._start(self._bootstrap)

    @override
    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        # Priority bindings must not send the hidden composer while a form is open.
        if isinstance(self.screen, ModalScreen) and action != "detach":
            return False
        if action == "send" and self.focused is not self._composer():
            return False
        if action in _COMPLETION_ACTIONS and not self.completion.visible:
            # Without a popup these keys belong to the focused widget: the arrows
            # move the composer's cursor, Tab moves focus, Escape does nothing.
            return False
        return super().check_action(action, parameters)

    def _start(
        self,
        factory: Callable[[], Coroutine[Any, Any, Any]],
        *,
        group: str = "action",
        exclusive: bool = False,
    ) -> Worker[Any]:
        async def run() -> Any:
            return await self._guard(factory())

        return self.run_worker(run, group=group, exclusive=exclusive, exit_on_error=False)

    async def _guard(self, coroutine: Coroutine[Any, Any, Any]) -> Any:
        try:
            return await coroutine
        except (ClientError, OSError) as exc:
            self._notice(str(exc), error=True)
        except Exception as exc:
            self._notice(
                f"The action failed ({type(exc).__name__}). The server can still be working.",
                error=True,
            )
            raise
        return None

    async def _bootstrap(self) -> None:
        if self.initial_agent and self.initial_session:
            self._start(self._load_roster, group="roster")
        else:
            await self._load_roster()
        agent = self.initial_agent
        if agent is None:
            if len(self.roster) == 1:
                agent = self.roster[0][1]
            else:
                agent = await self.push_screen_wait(Picker("Choose an Agent", self.roster))
        if agent:
            session = self.initial_session
            if session is None and self.initial_project is None:
                known = next((row for row in self.agents if row["id"] == agent), {})
                session = known.get("current_session_id")
            view = Conversation(
                agent,
                session=session,
                project=self.initial_project,
                project_selected=self.initial_project is not None,
            )
            await self._open(view)
        self.query_one("#composer", TextArea).focus()

    async def _load_roster(self) -> None:
        if self.roster_loaded:
            return
        agents, projects = await asyncio.gather(
            self.client.rpc("agent.list"), self.client.rpc("project.list", {"include_scan": True})
        )
        self.agents = agents.get("agents", [])
        self.projects = projects.get("projects", [])
        self.roster = [
            (f"{row.get('name', row['id'])} · {row['id']}", row["id"])
            for row in self.agents
            if not row.get("builtin")
        ]
        for project in self.projects:
            for member in (project.get("scan") or {}).get("team", []):
                if member.get("available", True):
                    address = f"{member['agent_id']}@{project['project_id']}"
                    self.roster.append(
                        (f"{member.get('display_name', member['agent_id'])} · {address}", address)
                    )
        self.roster_loaded = True
        self._header()

    def _save_view(self) -> None:
        if self.conversation is not None:
            self.conversation.draft = self.query_one("#composer", TextArea).text
            if self.transcript.view is self.conversation:
                self.transcript.remember_position()
            self._cache_history(self.conversation)
            self._view_changed(self.conversation)

    async def _open(self, view: Conversation, *, remember: bool = True) -> None:
        self._save_view()
        if remember and self.conversation is not None and self.conversation is not view:
            self.backstack.append(self.conversation)
            self.backstack = self.backstack[-100:]
        if self.stream:
            self.stream.cancel()
        self.stream, self.stream_path = None, None
        view = self.views.setdefault(view.key, view)
        self._cache_history(view)
        async with self._render_lock:
            self.conversation = view
            self.queue_items = []
            await self.transcript.clear()
        self._view_changed(view)
        self.query_one("#composer", TextArea).load_text(view.draft)
        self._header()
        if view.session or (view.uncertain and view.pending_session_id):
            self.loading_views.add(id(view))
            try:
                if view.items:
                    await self._render()
                else:
                    self.transcript.placeholder("Loading conversation…")
                self._header()
                await self._refresh(view)
                if self.conversation is view:
                    await self._render()
            finally:
                self.loading_views.discard(id(view))
                self._header()
        else:
            self.transcript.placeholder(
                "New conversation. The Session is created when you send its first message."
            )
        self.query_one("#composer", TextArea).focus()

    def _cache_history(self, view: Conversation) -> None:
        """Bound reconstructible Session content; drafts and navigation stay local."""
        view.cache_bytes = len(json.dumps(view.items, ensure_ascii=False).encode("utf-8"))
        self.history_cache[id(view)] = view
        self.history_cache.move_to_end(id(view))
        retained = sum(entry.cache_bytes for entry in self.history_cache.values())
        for identifier, entry in list(self.history_cache.items()):
            if len(self.history_cache) <= 8 and retained <= 16 * 1024 * 1024:
                break
            if entry is self.conversation or entry is view:
                continue
            retained -= entry.cache_bytes
            entry.refresh_revision += 1
            entry.release_history()
            del self.history_cache[identifier]

    async def _read_timeline(self, view: Conversation, revision: int) -> Conversation | None:
        """Drain a read snapshot without publishing a partial or stale catch-up."""
        key = view.key
        candidate = copy.copy(view)
        candidate.facts = dict(view.facts)
        after = view.facts.get("next_after")
        while True:
            params = {**view.params(), "limit": 100, "child_limit": 60}
            if after:
                params["after"] = after
            response = await self.client.rpc("chat.timeline", params)
            if view.refresh_revision != revision or view.key != key or not view.available:
                return None
            candidate.page(response)
            if not response.get("has_newer"):
                return candidate
            next_after = response.get("next_after")
            if not next_after or next_after == after:
                raise ClientError("History catch-up did not advance. Reconnect to try again.")
            after = next_after

    async def _recover_submission(self, view: Conversation) -> bool:
        """Find a pre-addressed first admission through reads only, never by replay."""
        target = view.pending_session_id
        if view.session or not target or not view.uncertain or id(view) in self.pending:
            return False
        key = view.key
        params = {"agent_id": view.agent, "session_id": target}
        try:
            history = await self.client.rpc(
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
        self._move_view(view, view.agent, target)
        view.pending_session_id = None
        if confirmed:
            self._accept_submission(view, view.uncertain_submission or {})
        if self.conversation is view:
            async with self._render_lock:
                await self.transcript.clear()
        self._view_changed(view)
        self._notice(
            "Earlier message found in its Session; reconnected without resending."
            if confirmed
            else (
                "The proposed Session exists. "
                "Inspect the earlier Command's outcome before continuing."
            )
        )
        return True

    def _accept_submission(self, view: Conversation, original: JsonObject) -> None:
        view.uncertain = False
        view.uncertain_submission = None
        if view.draft == original.get("text"):
            view.draft = ""
            if self.conversation is view:
                self.query_one("#composer", TextArea).load_text("")
        submitted_ids = {item["attachment_id"] for item in original.get("attachments", [])}
        view.attachments = [
            item for item in view.attachments if item["attachment_id"] not in submitted_ids
        ]
        view.mentions = [item for item in view.mentions if item not in original.get("mentions", [])]
        self._view_changed(view)

    def _saved_input(self, view: Conversation) -> JsonObject:
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

    def _move_view(self, view: Conversation, agent: str, session: str) -> None:
        self.views.pop(view.key, None)
        view.refresh_revision += 1
        view.agent, view.session = agent, session
        if (
            (previous := self.views.get(view.key)) is not None
            and previous is not view
            and (previous.draft or previous.attachments)
        ):
            self.saved_drafts.append(self._saved_input(previous))
        self.views[view.key] = view
        view.available = True
        self._view_changed(view)

    async def _refresh(self, view: Conversation) -> None:
        if not view.available:
            return
        if not view.session and not await self._recover_submission(view):
            return
        view.refresh_revision += 1
        revision = view.refresh_revision
        key = view.key
        captured_sequence, captured_run = view.sequence, view.stream_run
        candidate, summary, queue = await asyncio.gather(
            self._read_timeline(view, revision),
            self.client.rpc("session.get", view.params()),
            self.client.rpc("chat.queue_list", view.params()),
        )
        if candidate is None or revision != view.refresh_revision or key != view.key:
            return
        response = candidate.facts
        response_sequence = candidate.sequence
        response_run = candidate.stream_run
        if (
            response_run == view.stream_run
            and isinstance(response_sequence, int)
            and response_sequence < view.sequence
        ):
            self._schedule_refresh()
            return
        if (
            response_run is None
            and captured_run == view.stream_run
            and captured_sequence != view.sequence
        ):
            self._schedule_refresh()
            return
        view.items, view.facts = candidate.items, candidate.facts
        self._cache_history(view)
        view.sequence, view.stream_run = candidate.sequence, candidate.stream_run
        view.next_before = candidate.next_before
        session = summary.get("session") or {}
        view.title = session.get("title") or session.get("auto_title") or ""
        view.project = session.get("working_project_id")
        view.overrides = session.get("agent_overrides") or {}
        view.parent = session.get("subagent_parent") or view.parent
        view.completion_run_id = (
            session.get("latest_completion_run_id")
            if session.get("has_unread_completion") is True
            else None
        )
        view.completion_read_failed = None
        self._view_changed(view)
        if self.conversation is not view:
            return
        self.queue_items = queue.get("items", [])
        self.render_dirty = True
        active = response.get("active_run") or {}
        path = active.get("timeline_url")
        if path != self.stream_path:
            if self.stream:
                self.stream.cancel()
            self.stream_path = path
            self.stream = (
                self._start(lambda: self._follow(view, path), group="timeline", exclusive=True)
                if path
                else None
            )
        self._header()

    async def _follow(self, view: Conversation, path: str) -> None:
        delay = 0.5
        while self.conversation is view:
            try:
                async for event in self.client.timeline(path):
                    if self.conversation is not view:
                        return
                    if view.event(event):
                        self.render_dirty = True
                    payload = event.get("payload", event)
                    if payload.get("terminal"):
                        self.stream_path = None
                        self._schedule_refresh()
                        return
                    delay = 0.5
            except ClientError as exc:
                self._notice(str(exc))
            except TimelineGapError:
                continue
            # Read the authoritative state before opening another stream. Never resubmit.
            await _sleep(delay)
            delay = min(10, delay * 2)
            try:
                view.refresh_revision += 1
                revision = view.refresh_revision
                captured_sequence, captured_run = view.sequence, view.stream_run
                candidate = await self._read_timeline(view, revision)
            except ClientError:
                continue
            if self.conversation is not view:
                return
            if candidate is None or revision != view.refresh_revision:
                continue
            if (candidate.stream_run == view.stream_run and candidate.sequence < view.sequence) or (
                candidate.stream_run is None
                and captured_run == view.stream_run
                and captured_sequence != view.sequence
            ):
                continue
            view.refresh_revision += 1
            view.items, view.facts = candidate.items, candidate.facts
            view.stream_run, view.sequence = candidate.stream_run, candidate.sequence
            view.next_before = candidate.next_before
            self.render_dirty = True
            active = view.facts.get("active_run") or {}
            next_path = active.get("timeline_url")
            if not isinstance(next_path, str):
                self.stream_path = None
                return
            path = next_path
            self.stream_path = path

    async def _events(self) -> None:
        delay = 0.5
        while True:
            try:
                async for event in self.client.events():
                    self.connected = True
                    delay = 0.5
                    kind = event.get("type", "")
                    if kind == "resource_changed":
                        resource = event.get("payload") or {}
                        if resource.get("kind") in {"agents", "projects"}:
                            self.roster_loaded = False
                        await self._resource_changed(resource)
                    if (
                        kind in {"connection_ready", "connected", "connection", "state", "snapshot"}
                        or "snapshot" in kind
                    ):
                        self._schedule_refresh()
                        self._start(self._load_requests, group="requests-read", exclusive=True)
                    elif kind == "resource_changed":
                        resource = event.get("payload") or {}
                        if resource.get("kind") in {"agents", "projects", "settings", "models"} or (
                            resource.get("kind") == "sessions"
                            and self._event_targets_view(resource.get("scope") or {})
                        ):
                            self._schedule_refresh()
                        if resource.get("kind") == "extensions":
                            self._start(self._load_requests, group="requests-read", exclusive=True)
                    elif kind in {
                        "run_started",
                        "run_completed",
                        "run_failed",
                        "run_cancelled",
                        "run_interrupted",
                        "queue_changed",
                        "subagent_status_changed",
                        "command_status_changed",
                    }:
                        if self._event_targets_view(event.get("payload") or event):
                            self._schedule_refresh()
                    self._header()
            except (OSError, ClientError) as exc:
                self._notice(f"Reconnecting to {self.client.url} · {exc}")
            except Exception:
                self._notice(f"Event connection lost. Reconnecting to {self.client.url}.")
            self.connected = False
            self._header()
            await _sleep(delay)
            delay = min(10, delay * 2)

    def _event_targets_view(self, payload: JsonObject) -> bool:
        view = self.conversation
        if view is None:
            return False
        agent = payload.get("agent_id")
        session = payload.get("session_id") or payload.get("source_session_id")
        if agent is None and session is None:
            return True
        if agent and payload.get("project_id"):
            agent = f"{agent}@{payload['project_id']}"
        return (agent is None or agent == view.agent) and (
            session is None or session == view.session
        )

    def _schedule_refresh(self) -> None:
        if not self.refresh_pending:
            self.refresh_pending = True
            self.set_timer(0.2, self._refresh_scheduled)

    async def _refresh_scheduled(self) -> None:
        self.refresh_pending = False
        if self.conversation and (
            self.conversation.session
            or (self.conversation.uncertain and self.conversation.pending_session_id)
        ):
            view = self.conversation
            self._start(lambda: self._refresh(view), group="refresh", exclusive=True)

    async def _resource_changed(self, payload: JsonObject) -> None:
        scope = payload.get("scope") or {}
        old, new = scope.get("old_agent_id"), scope.get("new_agent_id")
        if payload.get("kind") == "agents" and old and new:
            if readdress_saved_inputs(self.saved_drafts, self.recovered_drafts, old, new):
                self._view_changed(self.conversation)
            views = {id(view): view for view in [*self.views.values(), *self.backstack]}
            if self.conversation:
                views[id(self.conversation)] = self.conversation
            for view in views.values():
                if view.agent == old:
                    self.views.pop(view.key, None)
                    view.refresh_revision += 1
                    view.agent = new
                    previous = self.views.get(view.key)
                    if (
                        previous is not None
                        and previous is not view
                        and (previous.draft or previous.attachments)
                    ):
                        self.saved_drafts.append(self._saved_input(previous))
                    self.views[view.key] = view
                    self._view_changed(view)
                if (
                    view.parent
                    and view.parent.get("agent_id") == old
                    and not view.parent.get("project_id")
                ):
                    view.parent = {**view.parent, "agent_id": new}
                    self._view_changed(view)
            self._header()
        if payload.get("kind") != "sessions":
            return
        agent = scope.get("agent_id")
        if scope.get("project_id"):
            agent = f"{agent}@{scope['project_id']}"
        deleted = scope.get("deleted_session_id")
        moved_to = scope.get("moved_to_agent_id")
        if agent and moved_to and scope.get("session_id"):
            if readdress_saved_inputs(
                self.saved_drafts,
                self.recovered_drafts,
                agent,
                moved_to,
                session=scope["session_id"],
            ):
                self._view_changed(self.conversation)
            for view in list(self.views.values()):
                if view.agent == agent and view.session == scope["session_id"]:
                    self._move_view(view, moved_to, scope["session_id"])
            self._header()
            return
        if not agent or not deleted:
            return
        for view in list(self.views.values()):
            if view.agent != agent or view.session != deleted:
                continue
            view.refresh_revision += 1
            view.available = False
            view.facts = {key: value for key, value in view.facts.items() if key != "active_run"}
            if view.draft or view.attachments:
                self.saved_drafts.append(self._saved_input(view))
            self._view_changed(view)
            if self.conversation is view:
                await self._open(Conversation(agent, session=scope.get("next_session_id")))
                self._notice(
                    "Conversation archived. Its unsent input is retained in input history."
                )

    async def _load_requests(self) -> None:
        response = await self.client.rpc("extensions.requests")
        self.requests = response.get("requests", [])
        self._header()

    def _render_if_dirty(self) -> None:
        self._completion_if_visible()
        view = self.conversation
        if view is not None and view.facts.get("active_run"):
            # The status row's spinner and elapsed time; the tick can outlive the
            # widgets while the app shuts down.
            with suppress(NoMatches):
                self._update_status(view)
        if (
            self.render_dirty
            and not self.rendering
            and (self.render_worker is None or self.render_worker.is_finished)
        ):
            # Textual cancels Workers before unmounting widgets on detach. A timer
            # callback awaiting nested mounts can otherwise outlive its widgets.
            self.render_worker = self._start(self._render, group="render")

    async def _render(self) -> None:
        async with self._render_lock:
            await self._render_locked()

    async def _render_locked(self) -> None:
        view = self.conversation
        if view is None:
            return
        self.rendering = True
        self.render_dirty = False
        items = view.items
        timeline = self.transcript
        previous = getattr(self, "_rendered_frame", None)
        if timeline.view is view:
            timeline.remember_position()
        try:
            if (
                not view.follow_tail
                and previous
                and previous[0] is view
                and previous[1] is not items
            ):
                view.unread_output = True
            await timeline.show(view)
            self._header()
            self._rendered_frame = (view, items)
            self.call_after_refresh(self._completion_if_visible)
        finally:
            self.rendering = False

    def _completion_if_visible(self) -> None:
        view = self.conversation
        frame = getattr(self, "_rendered_frame", None)
        timeline = getattr(self, "transcript", None)
        if (
            view is None
            or not view.available
            or not view.session
            or isinstance(self.screen, ModalScreen)
            or not self.app_focus
            or timeline is None
            or not timeline.is_vertical_scroll_end
            or frame is None
            or frame[0] is not view
            or frame[1] is not view.items
        ):
            return
        completed = view.completion_run_id
        if not completed or completed in {
            view.completion_read,
            view.completion_read_pending,
            view.completion_read_failed,
        }:
            return
        completed_row = next(
            (
                row
                for row in view.items
                if row.get("runId", row.get("run_id")) == completed
                and row.get("status") in {"completed", "failed", "cancelled", "interrupted"}
            ),
            None,
        )
        if view.facts.get("has_newer") or completed_row is None or timeline.view is not view:
            return
        children = completed_row.get("items") or []
        if children and not timeline.displays(str(children[-1]["id"])):
            return
        view.completion_read_pending = completed
        self._start(lambda: self._mark_completion_read(view, completed), group="read-receipt")

    async def _mark_completion_read(self, view: Conversation, run_id: str) -> None:
        key = view.key
        try:
            if self.conversation is not view or isinstance(self.screen, ModalScreen):
                return
            await self.client.rpc("session.mark_read", {**view.params(), "run_id": run_id})
            if view.key == key:
                view.completion_read = run_id
        except ClientError:
            view.completion_read_failed = run_id
            raise
        finally:
            if view.completion_read_pending == run_id:
                view.completion_read_pending = None

    def _header(self) -> None:
        view = self.conversation
        if view is None:
            return
        # The Project belongs to the row under the composer (as in the WebUI), so
        # the header names the conversation and its Agent alone. These rows are one
        # line tall whatever they say, so their update must not ask for a layout: a
        # reflow costs as much work as the transcript is long.
        title = view.title or "Untitled conversation"
        width = self.size.width
        agent = truncated(view.agent, max(8, width // 3))
        self._show("#header-agent", agent)
        self._show("#header-title", truncated(title, max(8, width - len(agent) - 6)), layout=False)
        # The Project and directory give way to the Model row and never wrap: the
        # row is one line tall, so a wrapped path would hide below it.
        context = truncated(self._context_text(view), max(12, width - 16))
        self._show(
            "#settings-where",
            truncated(self._where_text(view), max(12, width - len(context) - 6)),
            layout=False,
        )
        # The Model row has an automatic width, so it needs its layout to become
        # visible at all: only the full-width rows can be updated without one.
        self._show("#settings-model", context)
        self._update_status(view)
        draft: list[str] = []
        if view.attachments:
            draft.append(
                "Local uploads: " + ", ".join(item["filename"] for item in view.attachments)
            )
        if view.mentions:
            draft.append("Server files: " + ", ".join(view.mentions))
        info = self._show("#draft-info", " · ".join(draft))
        info.set_class(not draft, "empty")

    def _show(self, selector: str, text: str, *, layout: bool = True) -> Static:
        """Update one chrome row only when its text changed."""
        widget = self.query_one(selector, Static)
        text = terminal_text(text)
        if getattr(widget, "_shown_text", None) != text:
            widget._shown_text = text  # type: ignore[attr-defined]
            widget.update(text, layout=layout)
        return widget

    def _where_text(self, view: Conversation) -> str:
        """The Project the conversation works in and its server-side directory.

        Mirrors the WebUI's composer row: the Project on the left, the Model and
        its context load on the right. A draft without a chosen Project works
        where the Agent's default Project does; an Agent without one works in its
        Workspace.
        """
        project_id = view.project if (view.session or view.project_selected) else None
        if project_id is None and view.session is None:
            address_project = view.agent.partition("@")[2] if "@" in view.agent else ""
            project_id = address_project or self._agent_default_project(view.agent)
        if project_id:
            project = next(
                (row for row in self.projects if row.get("project_id") == project_id),
                None,
            )
            name = str((project or {}).get("display_name") or project_id)
            cwd = (project or {}).get("cwd")
            return f"{name} · {display_path(str(cwd))}" if cwd else name
        workspace = self._agent_workspace(view.agent)
        if workspace:
            return f"Workspace · {display_path(workspace)}"
        return "Workspace"

    def _agent_row(self, agent: str) -> JsonObject | None:
        identifier = agent.partition("@")[0]
        return next(
            (row for row in self.agents if row.get("id") == identifier),
            None,
        )

    def _agent_default_project(self, agent: str) -> str | None:
        row = self._agent_row(agent)
        value = (row or {}).get("root_project_id")
        return value if isinstance(value, str) and value else None

    def _agent_workspace(self, agent: str) -> str | None:
        row = self._agent_row(agent)
        value = (row or {}).get("workspace")
        return value if isinstance(value, str) and value else None

    def _context_text(self, view: Conversation) -> str:
        """The lasting settings and context load, so the status row stays transient."""
        parts: list[str] = []
        model = effective_value(self, view, "model")
        if isinstance(model, str) and model:
            parts.append(short_model(model))
        effort = effective_value(self, view, "thinking_effort")
        if isinstance(effort, str) and effort:
            parts.append(effort)
        context = view.facts.get("context_usage") or {}
        used = context.get("input_tokens", context.get("total_tokens", context.get("tokens")))
        window = context.get("context_window")
        if isinstance(used, int):
            usage = f"{compact_tokens(used)} context"
            if isinstance(window, int) and window > 0:
                usage = f"{compact_tokens(used)}/{compact_tokens(window)} ({used * 100 // window}%)"
            parts.append(usage)
        return " · ".join(parts)

    def _update_status(self, view: Conversation) -> None:
        """One row carries the single most urgent message instead of every fact."""
        message, level = self._status_message(view)
        status = self._show("#status", message, layout=False)
        status.set_class(not message, "idle")
        status.set_class(level == "alert", "alert")
        status.set_class(level == "error", "error")

    def _status_message(self, view: Conversation) -> tuple[str, str]:
        if self.last_notice and monotonic() < self.notice_expires:
            return self.last_notice, self.notice_level
        if not self.connected:
            return f"Connecting to {self.client.url}…", "alert"
        if id(view) in self.loading_views:
            return "Updating conversation…" if view.items else "Loading conversation…", "info"
        if view.uncertain:
            return (
                "Send outcome unknown · Ctrl+R reloads the Session; Send offers recovery",
                "alert",
            )
        if view.parent:
            return "Sub-Agent Session · replies go to you · Ctrl+B returns to the parent", "info"
        active = view.facts.get("active_run") or {}
        if active:
            spinner = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"[int(monotonic() * 10) % 10]
            elapsed = self._run_elapsed(view)
            status = str(active.get("status") or "working")
            label = "Working" if status in {"running", "working"} else status.capitalize()
            return f"{spinner} {label}{elapsed} · Ctrl+X stops", "info"
        if self.queue_items:
            return f"{len(self.queue_items)} queued message(s) · F6", "info"
        if self.requests:
            return f"{len(self.requests)} Extension request(s) · F7", "info"
        return "", "info"

    @staticmethod
    def _run_elapsed(view: Conversation) -> str:
        """` · 1m 05s` since the running Run started, from its timeline row."""
        for item in reversed(view.items):
            if item.get("type") == "assistant_run" and item.get("status") == "running":
                started = item.get("startTimestamp") or item.get("timestamp")
                try:
                    begun = datetime.fromisoformat(str(started))
                except ValueError:
                    return ""
                seconds = max(0, int((datetime.now(UTC) - begun).total_seconds()))
                minutes, seconds = divmod(seconds, 60)
                return f" · {minutes}m {seconds:02d}s" if minutes else f" · {seconds}s"
        return ""

    def _notice(self, message: str, *, error: bool = False) -> None:
        """Show one message in one place: the status row, plus a toast when it failed."""
        message = terminal_text(message)
        self.last_notice = message
        self.notice_level = "error" if error else "info"
        delay = 10 if error else 6
        self.notice_expires = monotonic() + delay
        if error:
            self.notify(escape(message), severity="error", timeout=delay)
        self._header()
        self.set_timer(delay, self._expire_notice)

    def _expire_notice(self) -> None:
        self.notice_expires = 0.0
        self._header()

    def _require_conversation(self, action: str) -> Conversation | None:
        """Explain a key that cannot act yet instead of returning silently."""
        view = self.conversation
        if view is None or not view.available:
            self._notice(
                f"{action} needs an open conversation. Start one with F3 or pick one with F2."
            )
            return None
        return view

    def _require_session(self, action: str) -> Conversation | None:
        """Like `_require_conversation`, for keys that need the stored Session."""
        view = self._require_conversation(action)
        if view is None:
            return None
        if not view.session:
            self._notice(
                f"{action} needs the Session a first message creates. "
                "Send the draft, or press Ctrl+R to reload."
            )
            return None
        return view

    @on(TextArea.Changed, "#composer")
    def draft_changed(self, event: TextArea.Changed) -> None:
        self._grow_composer(event.text_area)
        self._refresh_completion(event.text_area)
        if self.conversation:
            self.conversation.draft = event.text_area.text
            mentions = retained_mentions(self.conversation.draft, self.conversation.mentions)
            if mentions != self.conversation.mentions:
                self.conversation.mentions = mentions
                self._header()
            self._view_changed(self.conversation)

    def _modal_owns_the_screen(self) -> bool:
        """Whether a modal screen holds the terminal, which keeps the popup out of it.

        A composer may change before the app's first screen is on the stack.
        """
        try:
            return isinstance(self.screen, ModalScreen)
        except ScreenStackError:
            return False

    def _composer(self) -> TextArea | None:
        """The composer, or None while a conversation change rebuilds the screen."""
        try:
            return self.query_one("#composer", TextArea)
        except NoMatches:
            return None

    def _refresh_completion(self, editor: TextArea | None = None) -> None:
        """Offer the values the draft's token stands for, as the WebUI composer does."""
        view = self.conversation
        composer = editor if editor is not None else self._composer()
        if (
            view is None
            or not view.available
            or composer is None
            or not composer.is_mounted
            or self._modal_owns_the_screen()
        ):
            self._close_completion()
            return
        text = composer.text
        row, column = composer.cursor_location
        trigger = trigger_for(text, row, column)
        if trigger is None or text == self.completion.dismissed:
            self._close_completion()
            return
        self.completion.trigger = trigger
        self.completion.row = row
        candidates = self._completion_candidates(view, trigger)
        self.completion.pending = candidates is None
        suggestions = matching(candidates or [], trigger.query)
        if not suggestions:
            self.bar.hide()
            self.completion.kind = ""
            self.completion.suggestions = []
            return
        kinds = {"commands": "Commands and Skills", "models": "Models", "files": "Server files"}
        title = f"{kinds[trigger.kind]} · ↑↓ choose · Enter use · Esc close"
        self.completion.kind = trigger.kind
        self.completion.suggestions = suggestions
        self.completion.index = 0
        self.completion.title = title
        self.bar.show(title, suggestions, 0)

    def _completion_candidates(
        self, view: Conversation, trigger: Trigger
    ) -> list[Suggestion] | None:
        """Return the suggestions for the token, or None while their catalog loads."""
        key = (view.key, trigger.kind)
        catalog = self.completion_catalogs.get(key)
        if catalog is None:
            self._load_completion_catalog(view, trigger.kind)
            return None
        if trigger.kind == "commands":
            return command_suggestions(catalog)
        if trigger.kind == "models":
            return model_suggestions(catalog)
        return file_suggestions(catalog)

    def _load_completion_catalog(self, view: Conversation, kind: str) -> None:
        """Read the catalog the popup needs, once per conversation and kind."""
        key = (view.key, kind)
        if key in self.completion_loading:
            return
        self.completion_loading.add(key)
        if len(self.completion_catalogs) > 40:
            self.completion_catalogs.clear()

        async def load() -> None:
            try:
                if kind == "commands":
                    response = await self.client.rpc(
                        "chat.commands", {**view.params(draft=True), "reply_surface": "tui"}
                    )
                    items = response.get("items", [])
                elif kind == "models":
                    items = (await self.client.rpc("model.list", {"task": "chat"})).get(
                        "models", []
                    )
                else:
                    items = (await self.client.rpc("files.list", view.params(draft=True))).get(
                        "files", []
                    )
            except ClientError, OSError:
                items = []
            finally:
                self.completion_loading.discard(key)
            self.completion_catalogs[key] = list(items)
            if self.conversation is view and view.available:
                self._refresh_completion()

        self._start(load)

    def _close_completion(self) -> None:
        self.completion.kind = ""
        self.completion.suggestions = []
        self.completion.trigger = None
        self.completion.pending = False
        if bar := getattr(self, "bar", None):
            bar.hide()

    def _move_completion(self, step: int) -> None:
        total = len(self.completion.suggestions)
        if not total:
            return
        self.completion.index = (self.completion.index + step) % total
        self.bar.options.highlighted = self.completion.index

    async def accept_completion(self) -> None:
        """Take the highlighted suggestion, replacing the token it stands for."""
        view = self.conversation
        trigger = self.completion.trigger
        suggestion = self.completion.selected()
        if view is None or trigger is None or suggestion is None:
            return
        editor = self.query_one("#composer", TextArea)
        row = self.completion.row
        if suggestion.kind in {"command", "skill"}:
            # A command without an argument runs as soon as it is chosen; one with
            # an argument waits for the operator's text.
            insertion = f"/{suggestion.value}" if suggestion.send else f"/{suggestion.value} "
        elif suggestion.kind == "model":
            insertion = suggestion.value
        else:
            insertion = format_mention(suggestion.value) + " "
            if suggestion.value not in view.mentions:
                view.mentions.append(suggestion.value)
        editor.replace(insertion, (row, trigger.start), (row, trigger.end))
        editor.focus()
        self._close_completion()
        self._grow_composer(editor)
        self._view_changed(view)
        self._header()
        if suggestion.send:
            self.action_send()

    def action_completion_up(self) -> None:
        self._move_completion(-1)

    def action_completion_down(self) -> None:
        self._move_completion(1)

    async def action_completion_accept(self) -> None:
        await self.accept_completion()

    def action_completion_close(self) -> None:
        """Close the popup until the draft changes again, as the WebUI does."""
        composer = self._composer()
        if composer is not None:
            self.completion.dismissed = composer.text
        self._close_completion()

    def action_newline(self) -> None:
        """Shift+Enter and Ctrl+Enter break the line; Enter sends."""
        self.query_one("#composer", TextArea).insert("\n")

    def _grow_composer(self, editor: TextArea) -> None:
        """Follow the draft instead of keeping a fixed block of empty rows."""
        wanted = max(3, min(10, editor.text.count("\n") + 3))
        if editor.styles.height != wanted:
            editor.styles.height = wanted

    @on(Markdown.LinkClicked)
    @on(Transcript.LinkClicked)
    def link_clicked(self, event: Markdown.LinkClicked | Transcript.LinkClicked) -> None:
        self._start(
            lambda: self.push_screen_wait(
                TextDialog("Link · copy and open on your own computer", event.href, read_only=True)
            )
        )

    @work(group="send", exclusive=False, exit_on_error=False)
    async def action_send(self) -> None:
        view = self.conversation
        if view is None or id(view) in self.pending:
            return
        if self.completion.visible:
            # Enter takes the highlighted suggestion instead of sending the
            # half-typed token; while its catalog is still loading, it waits.
            await self.accept_completion()
            return
        if self.completion.pending:
            # The catalog behind the token is still being read; wait for it rather
            # than sending a half-typed `@path` or `/model` argument as text.
            return
        if not view.available:
            self._notice("This conversation is no longer available. Choose a live Session first.")
            return
        text = self.query_one("#composer", TextArea).text
        if view.uncertain:
            if await self._guard(self._recover_submission(view)):
                await self._guard(self._refresh(view))
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
            choice = await self.push_screen_wait(
                Picker(
                    "Previous send has an unknown outcome. Inspect the Session and Queue first.",
                    choices,
                )
            )
            if self.conversation is not view:
                return
            if choice == "review":
                await self.push_screen_wait(
                    TextDialog("Submission with unknown outcome", pretty(original), read_only=True)
                )
                return
            if choice in {"accepted", "sessionless"}:
                if choice == "accepted" and not view.session:
                    self._notice(
                        "The created Session is not confirmed yet. "
                        "Reconnect or select it before continuing."
                    )
                    return
                if choice == "sessionless":
                    view.pending_session_id = None
                self._accept_submission(view, original)
                self._header()
                return
            if choice != "restore":
                return
            later_draft = self._saved_input(view)
            if any(
                later_draft[key] != original.get(key) for key in ("text", "attachments", "mentions")
            ):
                self.saved_drafts.append(later_draft)
            view.draft = original["text"]
            view.attachments = original["attachments"]
            view.mentions = original["mentions"]
            view.uncertain = False
            view.uncertain_submission = None
            self.query_one("#composer", TextArea).load_text(view.draft)
            self._view_changed(view)
            self._notice("Earlier draft restored. Later draft is in input history (Alt+Up).")
            return
        if not text.strip() and not view.attachments:
            self._notice("Nothing to send yet · write a message, or attach a file with F8.")
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
        self._view_changed(view)
        mutation_started = False
        try:
            mention_params = view.params(draft=True)
            request_agent = view.agent
            mention_paths = await resolve_mentions(self.client, mention_params, text)
            if not view.available or view.agent != request_agent:
                self._notice(
                    "The conversation changed while resolving files. Your draft is retained."
                )
                return
            if mention_paths:
                request["file_mentions"] = mention_paths
            submitted["request"] = copy.deepcopy(request)
            view.uncertain = True
            view.uncertain_submission = submitted
            try:
                await self.persist_drafts()
            except (OSError, ValueError) as exc:
                view.uncertain = False
                view.uncertain_submission = None
                # The composer is the authority; its change message may still be
                # queued, so mirror it here before persisting the view's draft.
                view.draft = text
                self._view_changed(view)
                self._notice(
                    f"The pending submission could not be saved: {exc}. Your message was not sent.",
                    error=True,
                )
                return
            mutation_started = True
            result = await self.client.rpc("chat.stream", request)
        except ClientError as exc:
            view.uncertain = mutation_started and exc.state == "unknown"
            view.uncertain_submission = submitted if view.uncertain else None
            self._view_changed(view)
            self._notice(str(exc) + " Your draft is retained.", error=True)
            self._schedule_refresh()
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
        if self.conversation is view and self.query_one("#composer", TextArea).text == text:
            self.query_one("#composer", TextArea).load_text("")
        if result.get("session_id") and view.session is None:
            self.views.pop(view.key, None)
            view.session = result["session_id"]
            view.pending_session_id = None
            self.views[view.key] = view
            if self.conversation is view:
                async with self._render_lock:
                    await self.transcript.clear()
        elif view.session is None:
            view.pending_session_id = None
        self._view_changed(view)
        if result.get("command_handled") and self.conversation is view:
            await self._command_result(view, result)
        else:
            self._notice("Queued" if result.get("queued") else "Message accepted")
        if view.session:
            await self._guard(self._refresh(view))

    async def _command_result(self, view: Conversation, result: JsonObject) -> None:
        data = result.get("data") or {}
        navigation = data.get("navigation") or {}
        if navigation.get("kind") == "open_extension_page":
            route = "/".join(
                quote(part, safe="") for part in str(navigation.get("route", "")).split("/") if part
            )
            page = f"extension:{navigation['extension']}:{navigation['page']}"
            await self.push_screen_wait(
                TextDialog(
                    "Open the Extension page in a browser",
                    f"{self.client.url}/#{page}" + (f"/{route}" if route else ""),
                    read_only=True,
                )
            )
        elif navigation.get("kind") == "new_session":
            await self._open(Conversation(navigation.get("agent_id", view.agent)))
        elif data.get("session_id") and (
            data["session_id"] != view.session or data.get("agent_id", view.agent) != view.agent
        ):
            if data["session_id"] == view.session:
                self._move_view(view, data.get("agent_id", view.agent), data["session_id"])
                self._header()
            else:
                await self._open(
                    Conversation(data.get("agent_id", view.agent), session=data["session_id"])
                )
        if result.get("reply"):
            await self.push_screen_wait(
                TextDialog("Command result", result["reply"], read_only=True)
            )

    @work(group="navigation", exclusive=True, exit_on_error=False)
    async def action_sessions(self) -> None:
        await self._guard(self._select_session())

    async def _select_session(self) -> None:
        origin = self.conversation
        await self._load_roster()
        if self.conversation is not origin:
            return
        addresses = [address for _, address in self.roster]
        if not addresses:
            self._notice("No Agents are configured. Create one with vbot agent create.")
            return
        batches = [addresses[index : index + 100] for index in range(0, len(addresses), 100)]
        cursors: list[Any] = [None] * len(batches)
        first_page = True
        collected: list[tuple[str, Any]] = []
        while True:
            for index, addresses_batch in enumerate(batches):
                if not first_page and not cursors[index]:
                    continue
                params: JsonObject = {"agent_ids": addresses_batch, "limit": 100}
                if cursors[index]:
                    params["cursor"] = cursors[index]
                response = await self.client.rpc("session.list", params)
                if self.conversation is not origin:
                    return
                for row in response.get("sessions", []):
                    active = "● " if row.get("has_active_run") else ""
                    unread = "• " if row.get("has_unread_completion") is True else ""
                    title = (
                        row.get("title")
                        or row.get("auto_title")
                        or f"Untitled · {str(row['id'])[:8]}"
                    )
                    label = f"{active}{unread}{title} · {row['agent_address']}"
                    project = row.get("working_project_id")
                    if project:
                        label += f" · {project}"
                    collected.append((label, row))
                cursors[index] = response.get("next_cursor")
            first_page = False
            collected.sort(key=lambda entry: entry[1].get("updated_at", ""), reverse=True)
            choices = collected + ([("Load older Sessions…", "older")] if any(cursors) else [])
            row = await self.push_screen_wait(
                Picker("Sessions · search title, Agent or Project", choices)
            )
            if self.conversation is not origin:
                return
            if row == "older":
                continue
            if isinstance(row, dict):
                view = Conversation(
                    row["agent_address"], session=row["id"], parent=row.get("subagent_parent")
                )
                if known := self.views.get(view.key):
                    known.available = True
                await self._open(view)
            return

    @work(group="navigation", exclusive=True, exit_on_error=False)
    async def action_new(self) -> None:
        await self._guard(self._new())

    async def _new(self) -> None:
        origin = self.conversation
        await self._load_roster()
        if self.conversation is not origin:
            return
        agent = await self.push_screen_wait(Picker("New conversation · choose Agent", self.roster))
        if not agent or self.conversation is not origin:
            return
        project: str | None = None
        selected = False
        if "@" not in agent:
            choices: list[tuple[str, Any]] = [
                ("Agent's default Project", (False, None)),
                ("Agent's Workspace", (True, None)),
            ]
            choices.extend(
                (
                    f"{row['display_name']} · {row['project_id']} · {row['cwd']}",
                    (True, row["project_id"]),
                )
                for row in self.projects
            )
            choice = await self.push_screen_wait(
                Picker("Working Project · server filesystem", choices)
            )
            if choice is None or self.conversation is not origin:
                return
            selected, project = choice
        # Keep an existing unsent draft instead of replacing its text silently.
        candidate = Conversation(agent, project=project, project_selected=selected)
        existing = self.views.get(candidate.key)
        if existing and (existing.draft or existing.attachments):
            await self._open(existing)
            self._notice("Resumed this Agent's unsent draft.")
        else:
            self.views.pop(candidate.key, None)
            await self._open(candidate)

    @work(group="navigation", exclusive=True, exit_on_error=False)
    async def action_previous_conversation(self) -> None:
        if self.backstack:
            await self._guard(self._open(self.backstack.pop(), remember=False))
        elif self.conversation and self.conversation.parent:
            parent = self.conversation.parent
            agent = parent["agent_id"]
            if parent.get("project_id"):
                agent += "@" + parent["project_id"]
            await self._guard(
                self._open(Conversation(agent, session=parent["session_id"]), remember=False)
            )

    @work(group="model", exclusive=True, exit_on_error=False)
    async def action_model(self) -> None:
        await self._guard(self._model())

    async def _model(self) -> None:
        from cli.tui.models import menu

        # Model and effort are also draft settings, so this works before a Session
        # exists; only a missing conversation makes the key meaningless.
        if self._require_conversation("Model settings") is None:
            return
        await menu(self)

    def action_theme_dark(self) -> None:
        self.theme = VBOT_DARK.name

    def action_theme_light(self) -> None:
        self.theme = VBOT_LIGHT.name

    @work(group="queue", exclusive=True, exit_on_error=False)
    async def action_queue(self) -> None:
        await self._guard(self._queue())

    async def _queue(self) -> None:
        view = self._require_session("The Queue")
        if view is None:
            return
        key = view.key
        response = await self.client.rpc("chat.queue_list", view.params())
        if self.conversation is not view or view.key != key or not view.available:
            return
        self.queue_items = response.get("items", [])
        self._header()
        choices = [
            (f"{item.get('status', 'queued')} · {content_text(item.get('content'))}", item)
            for item in self.queue_items
        ]
        if not choices:
            self._notice("The Queue is empty.")
            return
        item = await self.push_screen_wait(Picker("Queue · shared with every accessor", choices))
        if not item or self.conversation is not view or view.key != key or not view.available:
            return
        actions = [("Remove from Queue", "remove")]
        if item.get("steerable") and not item.get("steering"):
            actions.insert(0, ("Steer into the current Run", "steer"))
        if item.get("editable"):
            actions.insert(1, ("Edit queued message", "update"))
        action = await self.push_screen_wait(Picker("Queued message", actions))
        if not action or self.conversation is not view or view.key != key or not view.available:
            return
        params = {**view.params(), "item_id": item["id"]}
        if action == "update":
            text = await self.push_screen_wait(
                TextDialog("Edit queued message", content_text(item.get("content")))
            )
            if (
                text is None
                or self.conversation is not view
                or view.key != key
                or not view.available
            ):
                return
            params["content"] = text
            params["reply_surface"] = "tui"
            mentions = await resolve_mentions(self.client, view.params(), text)
            if self.conversation is not view or view.key != key or not view.available:
                return
            if mentions:
                params["file_mentions"] = mentions
        await self.client.rpc(f"chat.queue_{action}", params)
        await self._refresh(view)

    @work(group="stop", exclusive=True, exit_on_error=False)
    async def action_stop(self) -> None:
        await self._guard(self._stop(False))

    @work(group="stop", exclusive=True, exit_on_error=False)
    async def action_stop_all(self) -> None:
        await self._guard(self._stop(True))

    async def _stop(self, all_work: bool) -> None:
        view = self._require_session("Stopping work")
        if view is None:
            return
        key = view.key
        if all_work:
            await self.client.rpc("chat.stop_all", view.params())
            self._notice("Stop all requested. This Session's queued messages remain queued.")
        else:
            current = await self.client.rpc("chat.timeline", {**view.params(), "limit": 1})
            if self.conversation is not view or view.key != key or not view.available:
                return
            run = current.get("active_run") or {}
            run_id = run.get("run_id", run.get("id"))
            if not run_id:
                self._notice(
                    "No active Run. Background work can still continue; use Stop all to end it."
                )
                return
            await self.client.rpc("chat.cancel", {"run_id": run_id, "reason": "user"})
            self._notice("Stop requested for this Run. Background work continues.")
        await self._refresh(view)

    @work(group="refresh", exclusive=True, exit_on_error=False)
    async def action_refresh(self) -> None:
        if self.conversation:
            await self._guard(self._refresh(self.conversation))
        else:
            await self._guard(self._bootstrap())

    @work(group="older", exclusive=True, exit_on_error=False)
    async def action_older(self) -> None:
        await self._guard(self._older())

    @on(Transcript.LoadOlder)
    def older_history_requested(self) -> None:
        if self.conversation and id(self.conversation) not in self.older_loading:
            self.action_older()

    @on(Transcript.LoadHistory)
    def run_history_requested(self, event: Transcript.LoadHistory) -> None:
        view = self.conversation
        if view is not None:
            self._start(lambda: self._load_run_history(view, event.item), group="run-history")

    @on(Transcript.Leave)
    def transcript_left(self) -> None:
        self.query_one("#composer", TextArea).focus()

    def on_text_selected(self, event: events.TextSelected) -> None:
        """Copy on select, as terminals do: the mouse belongs to the app here."""
        text = self.screen.get_selected_text()
        if text and text.strip():
            self.copy_to_clipboard(text)
            self._notice("Copied to the clipboard")

    async def _load_run_history(self, view: Conversation, gap: JsonObject) -> None:
        if view is None or id(view) in self.older_loading:
            # Busy with another page: the gap asks again when it next shows.
            self.transcript.history_failed(gap)
            return
        key, revision = view.key, view.refresh_revision
        self.older_loading.add(id(view))
        try:
            page = await self.client.rpc(
                "chat.timeline_children",
                {
                    **view.params(),
                    "run_id": gap["run_id"],
                    "before": gap["before"],
                    "limit": 100,
                    "history_generation": view.facts.get("history_generation"),
                },
            )
            if (
                self.conversation is view
                and view.key == key
                and view.refresh_revision == revision
                and view.child_page(page)
            ):
                await self._render()
            else:
                self.transcript.history_failed(gap)
        except ClientError as exc:
            self._notice(str(exc), error=True)
        finally:
            self.older_loading.discard(id(view))

    async def _older(self) -> None:
        view = self.conversation
        if not view or not view.next_before:
            self._notice("All available history is loaded.")
            return
        if id(view) in self.older_loading:
            return
        key, revision = view.key, view.refresh_revision
        generation = view.facts.get("history_generation")
        self.older_loading.add(id(view))
        try:
            page = await self.client.rpc(
                "chat.timeline",
                {**view.params(), "limit": 100, "before": view.next_before, "child_limit": 60},
            )
        finally:
            self.older_loading.discard(id(view))
        if self.conversation is not view or key != view.key or revision != view.refresh_revision:
            return
        if generation and page.get("history_generation", generation) != generation:
            self._schedule_refresh()
            return
        view.page(page, older=True)
        await self._render()

    @work(group="requests", exclusive=True, exit_on_error=False)
    async def action_requests(self) -> None:
        await self._guard(self._requests())

    async def _requests(self) -> None:
        await self._load_requests()
        if not self.requests:
            self._notice("No Extension requests are waiting.")
            return
        request = await self.push_screen_wait(
            Picker(
                "Extension requests",
                [
                    (
                        f"{row['extension']} · {row.get('kind', '')} · "
                        f"{row.get('payload', {}).get('message', row['id'])}",
                        row,
                    )
                    for row in self.requests
                ],
            )
        )
        if not request:
            return
        values: JsonObject = {}
        problem = ""
        outcome_unknown = False
        while True:
            dialog = ExtensionDialog(
                request,
                values=values,
                problem=problem,
                outcome_unknown=outcome_unknown,
            )
            response = await self.push_screen_wait(dialog)
            if response is None:
                return
            try:
                await self.client.rpc(
                    "extensions.operation",
                    {
                        "name": request["extension"],
                        "operation": request["response_operation"],
                        "arguments": {"request_id": request["id"], "response": response},
                    },
                )
            except ClientError as exc:
                values = dialog.form_values
                outcome_unknown = exc.state == "unknown"
                problem = (
                    "The previous response outcome is unknown. No retry was sent; check that "
                    "this request is still pending before responding again.\n" + str(exc)
                    if outcome_unknown
                    else str(exc)
                )
                continue
            await self._load_requests()
            return

    @work(group="activity", exclusive=True, exit_on_error=False)
    async def action_activity(self) -> None:
        await self._guard(self._activity())

    async def _activity(self) -> None:
        view = self._require_session("Activity")
        if view is None:
            return
        key = view.key
        work_state = await self.client.rpc("chat.work", view.params())
        if self.conversation is not view or view.key != key or not view.available:
            return
        choices: list[tuple[str, Any]] = []
        seen: set[str] = set()
        for item in flatten(view.items):
            if item.get("type") != "tool_call":
                continue
            result = item.get("result") or {}
            if isinstance(result, str):
                try:
                    result = json.loads(result)
                except ValueError:
                    continue
            if not isinstance(result, dict):
                continue
            data = result.get("data") or {}
            if not isinstance(data, dict):
                continue
            if item.get("name") == "subagent" and data.get("id") and data["id"] not in seen:
                seen.add(data["id"])
                description = (item.get("arguments") or {}).get("description") or data.get(
                    "agent_id", data["id"]
                )
                choices.append(
                    (f"Sub-Agent · {description} · {data.get('status', '')}", ("subagent", data))
                )
            if data.get("command_id") and data["command_id"] not in seen:
                seen.add(data["command_id"])
                command = (item.get("arguments") or {}).get("command", data["command_id"])
                choices.append((f"Command · {command}", ("command", data)))
        for row in view.facts.get("reflection_runs", []):
            choices.append(
                (
                    f"Background review · {row.get('kind', '')} · {row.get('status', '')}",
                    ("detail", row),
                )
            )
        for entry in work_state.get("running", []):
            kind = entry["kind"]
            identity = entry["id"]
            choices = [
                choice
                for choice in choices
                if not (
                    choice[1][0] == kind
                    and choice[1][1].get("id", choice[1][1].get("command_id")) == identity
                )
            ]
            data = dict(entry)
            if kind == "command":
                data["command_id"] = identity
            choices.insert(
                0, (f"{kind.capitalize()} · {entry['label']} · {entry['status']}", (kind, data))
            )
        choices.append(("Inspect all current background work", ("work", work_state)))
        selected = await self.push_screen_wait(
            Picker("Activity · selected conversation and its Sub-Agents", choices)
        )
        if not selected or self.conversation is not view or view.key != key or not view.available:
            return
        kind, data = selected
        if kind == "subagent":
            child = data["agent_id"]
            if data.get("project_id") and "@" not in child:
                child += "@" + data["project_id"]
            result = await self.client.rpc(
                "subagent.inspect",
                {"agent_id": child, "session_id": data["session_id"], "id": data["id"]},
            )
            if self.conversation is not view or view.key != key or not view.available:
                return
            choices = [
                ("Open Sub-Agent Session", "open"),
                ("Inspect work and outcome", "detail"),
                ("Stop this Sub-Agent and its background work", "stop"),
            ]
            action = await self.push_screen_wait(Picker("Sub-Agent", choices))
            if self.conversation is not view or view.key != key or not view.available:
                return
            target = result.get("agent_id", data.get("agent_id"))
            project = result.get("project_id", data.get("project_id"))
            if project and "@" not in target:
                target += "@" + project
            session = result.get("session_id", data.get("session_id"))
            if action == "open":
                await self._open(
                    Conversation(
                        target,
                        session=session,
                        parent={"agent_id": view.agent, "session_id": view.session},
                    )
                )
            elif action == "stop":
                await self.client.rpc("chat.stop_all", {"agent_id": target, "session_id": session})
                self._notice("Sub-Agent tree stop requested.")
            elif action == "detail":
                await self.push_screen_wait(
                    TextDialog("Sub-Agent activity", pretty(result), read_only=True)
                )
        elif kind == "command":
            action = await self.push_screen_wait(
                Picker(
                    "Background command",
                    [("Read current output", "command_output"), ("Stop command", "stop_command")],
                )
            )
            if action and self.conversation is view and view.key == key and view.available:
                result = await self.client.rpc(
                    f"chat.{action}",
                    {
                        "session_id": data.get("session_id", view.session),
                        "command_id": data["command_id"],
                    },
                )
                await self.push_screen_wait(
                    TextDialog("Command output", pretty(result), read_only=True)
                )
        elif kind == "terminal":
            target = data["agent_id"]
            if data.get("project_id") and "@" not in target:
                target += "@" + data["project_id"]
            await self._open(Conversation(target, session=data["session_id"]))
        else:
            result = data
            await self.push_screen_wait(
                TextDialog("Background work", pretty(result), read_only=True)
            )

    @work(group="completion", exclusive=True, exit_on_error=False)
    async def action_commands(self) -> None:
        await self._guard(composer.complete(self, commands=True))

    @work(group="completion", exclusive=True, exit_on_error=False)
    async def action_complete(self) -> None:
        await self._guard(composer.complete(self))

    @work(group="files", exclusive=True, exit_on_error=False)
    async def action_files(self) -> None:
        await self._guard(composer.files(self))

    @work(group="conversation-actions", exclusive=True, exit_on_error=False)
    async def action_conversation_actions(self) -> None:
        from cli.tui import actions

        await self._guard(actions.menu(self))

    def action_latest(self) -> None:
        self.transcript.scroll_to_latest()

    @work(group="find", exclusive=True, exit_on_error=False)
    async def action_find(self) -> None:
        await self._guard(composer.find(self))

    @work(group="copy", exclusive=True, exit_on_error=False)
    async def action_copy(self) -> None:
        await self._guard(composer.copy(self))

    @work(group="input-history", exclusive=True, exit_on_error=False)
    async def action_input_history(self) -> None:
        await self._guard(composer.input_history(self))

    @work(group="help", exclusive=True, exit_on_error=False)
    async def action_help(self) -> None:
        await self._guard(composer.help(self))

    @work(group="detach", exclusive=True, exit_on_error=False)
    async def action_detach(self) -> None:
        self._save_view()
        try:
            await self.persist_drafts()
        except (OSError, ValueError) as exc:
            self._notice(
                f"Drafts could not be saved: {exc}. Copy them before leaving or retry Ctrl+Q.",
                error=True,
            )
            return
        self.exit(0)
