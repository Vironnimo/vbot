"""A conversation-first Textual accessor for the shared vBot server."""

from __future__ import annotations

import asyncio
import copy
import json
from collections.abc import Callable, Coroutine
from typing import Any, override
from urllib.parse import quote

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.markup import escape
from textual.screen import ModalScreen
from textual.widgets import Button, Footer, Markdown, Static, TextArea
from textual.worker import Worker

from cli.tui import composer
from cli.tui.dialogs import ExtensionDialog, Picker, TextDialog
from cli.tui.rendering import (
    TimelineEntry,
    content_text,
    flatten,
    pretty,
    terminal_text,
)
from cli.tui.state import Conversation, JsonObject, TimelineGapError
from cli.tui.transport import ClientError, TerminalClient


class ConversationApp(App[int]):
    """Keep navigation and drafts local; all conversation decisions use server APIs."""

    TITLE = "vBot"
    CSS = """
    Screen { background: $background; }
    #identity { height: auto; min-height: 2; padding: 0 1; background: $panel; color: $text; }
    #toolbar { height: 3; padding: 0 1; }
    #toolbar Button { min-width: 9; width: auto; margin-right: 1; }
    #timeline { height: 1fr; padding: 1; scrollbar-gutter: stable; }
    #empty { color: $text-muted; margin: 2 4; }
    #status, #draft-info { height: auto; min-height: 1; padding: 0 1; color: $text-muted; }
    #draft-info { color: $warning; }
    #composer { height: 6; max-height: 14; border: round $accent; margin: 0 1; }
    #sendbar { height: 3; padding: 0 1; }
    #sendbar Button { margin-right: 1; min-width: 10; }
    #sendbar Static { width: 1fr; padding: 1 0; color: $text-muted; }
    """
    BINDINGS = [
        Binding("ctrl+s,alt+enter", "send", "Send", priority=True),
        Binding("ctrl+p,f2", "sessions", "Sessions", priority=True),
        Binding("ctrl+n,f3", "new", "New", priority=True),
        Binding("f4", "model", "Model", priority=True),
        Binding("f5", "activity", "Activity", priority=True),
        Binding("f6", "queue", "Queue", priority=True),
        Binding("f7", "requests", "Requests", priority=True),
        Binding("f8", "files", "Files", priority=True),
        Binding("f9", "commands", "Commands", priority=True),
        Binding("ctrl+x", "stop", "Stop Run", priority=True),
        Binding("f10", "stop_all", "Stop all", priority=True),
        Binding("ctrl+q", "detach", "Detach", priority=True),
        Binding("ctrl+r", "refresh", "Reconnect", show=False, priority=True),
        Binding("ctrl+o", "older", "Older history", show=False, priority=True),
        Binding("ctrl+b", "previous_conversation", "Parent / back", show=False, priority=True),
        Binding("ctrl+f", "find", "Find in history", show=False, priority=True),
        Binding("ctrl+g", "copy", "Copy / export", show=False, priority=True),
        Binding("ctrl+space", "complete", "Complete", show=False, priority=True),
        Binding("alt+up", "input_history", "Input history", show=False, priority=True),
        Binding("f1", "help", "Help", show=False),
    ]

    def __init__(
        self,
        client: TerminalClient,
        *,
        agent_id: str | None = None,
        session_id: str | None = None,
        project_id: str | None = None,
    ) -> None:
        super().__init__()
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
        self.rows: dict[str, TimelineEntry] = {}
        self.stream: Worker[Any] | None = None
        self.stream_path: str | None = None
        self.pending: set[int] = set()
        self.history: list[str] = []
        self.saved_drafts: list[JsonObject] = []
        self.connected = False
        self.render_dirty = False
        self.rendering = False
        self.render_worker: Worker[Any] | None = None
        self.refresh_pending = False
        self.requests: list[JsonObject] = []
        self.queue_items: list[JsonObject] = []
        self.last_notice = "Connecting…"
        self._render_lock = asyncio.Lock()

    @override
    def compose(self) -> ComposeResult:
        yield Static(f"vBot · {self.client.url}", id="identity", markup=False)
        with Horizontal(id="toolbar"):
            for button_id, label in [
                ("sessions", "Sessions"),
                ("new", "New"),
                ("model", "Model"),
                ("activity", "Activity"),
                ("queue", "Queue"),
                ("requests", "Requests"),
                ("files", "Files"),
            ]:
                yield Button(label, id=button_id)
        with VerticalScroll(id="timeline"):
            yield Static(
                "Choose an Agent or resume a Session.\nF1 shows the keyboard controls.",
                id="empty",
                markup=False,
            )
        yield Static("Connecting…", id="status", markup=False)
        yield Static("", id="draft-info", markup=False)
        yield TextArea(
            placeholder="Write a message · Enter inserts a line · Ctrl+S sends", id="composer"
        )
        with Horizontal(id="sendbar"):
            yield Button("Send", id="send", variant="primary")
            yield Button("Stop Run", id="stop")
            yield Button("Stop all", id="stop-all", variant="error")
            yield Static(
                "Ctrl+Space completes /commands, Skills and @server files · Ctrl+Q detaches",
                markup=False,
            )
        yield Footer()

    def on_mount(self) -> None:
        self.set_interval(0.08, self._render_if_dirty)
        self._start(self._events, group="events", exclusive=True)
        self._start(self._bootstrap)

    @override
    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        # Priority bindings must not send the hidden composer while a form is open.
        if isinstance(self.screen, ModalScreen) and action != "detach":
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

    def _save_view(self) -> None:
        if self.conversation is not None:
            self.conversation.draft = self.query_one("#composer", TextArea).text
            timeline = self.query_one("#timeline", VerticalScroll)
            self.conversation.scroll_y = timeline.scroll_y
            self.conversation.follow_tail = timeline.is_vertical_scroll_end

    async def _open(self, view: Conversation, *, remember: bool = True) -> None:
        self._save_view()
        if remember and self.conversation is not None and self.conversation is not view:
            self.backstack.append(self.conversation)
        if self.stream:
            self.stream.cancel()
        self.stream, self.stream_path = None, None
        view = self.views.setdefault(view.key, view)
        timeline = self.query_one("#timeline", VerticalScroll)
        async with self._render_lock:
            self.conversation = view
            self.queue_items = []
            await timeline.remove_children()
            self.rows.clear()
        self.query_one("#composer", TextArea).load_text(view.draft)
        self._header()
        if view.session:
            await self._refresh(view)
            await self._render()
        else:
            await timeline.mount(
                Static(
                    "New conversation. The Session is created when you send its first message.",
                    markup=False,
                )
            )
            self._notice("Ready")
        self.query_one("#composer", TextArea).focus()

    async def _refresh(self, view: Conversation) -> None:
        if not view.session:
            return
        view.refresh_revision += 1
        revision = view.refresh_revision
        captured_sequence, captured_run = view.sequence, view.stream_run
        params = {**view.params(), "limit": 100}
        if view.facts.get("next_after"):
            params["after"] = view.facts["next_after"]
        response, summary, queue = await asyncio.gather(
            self.client.rpc("chat.timeline", params),
            self.client.rpc("session.get", view.params()),
            self.client.rpc("chat.queue_list", view.params()),
        )
        if revision != view.refresh_revision:
            return
        response_sequence = response.get("timeline_sequence")
        response_run = (response.get("active_run") or {}).get("run_id")
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
        view.page(response)
        session = summary.get("session") or {}
        view.title = session.get("title") or session.get("auto_title") or view.session
        view.project = session.get("working_project_id")
        view.overrides = session.get("agent_overrides") or {}
        view.parent = session.get("subagent_parent") or view.parent
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
        completed = session.get("latest_completion_run_id")
        if completed and session.get("has_unread_completion") is True:
            await self.client.rpc("session.mark_read", {**view.params(), "run_id": completed})

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
            await asyncio.sleep(delay)
            delay = min(10, delay * 2)
            try:
                revision = view.refresh_revision
                params = {**view.params(), "limit": 100}
                if view.facts.get("next_after"):
                    params["after"] = view.facts["next_after"]
                response = await self.client.rpc("chat.timeline", params)
            except ClientError:
                continue
            if self.conversation is not view:
                return
            if revision != view.refresh_revision:
                continue
            view.refresh_revision += 1
            view.page(response)
            self.render_dirty = True
            active = response.get("active_run") or {}
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
                    if self.last_notice == "Connecting…":
                        self.last_notice = "Ready"
                    delay = 0.5
                    kind = event.get("type", "")
                    if (
                        kind in {"connection_ready", "connected", "connection", "state", "snapshot"}
                        or "snapshot" in kind
                        or kind == "resource_changed"
                    ):
                        self._schedule_refresh()
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
                        self._schedule_refresh()
                    self._header()
            except (OSError, ClientError) as exc:
                self._notice(f"Reconnecting to {self.client.url} · {exc}")
            except Exception:
                self._notice(f"Event connection lost. Reconnecting to {self.client.url}.")
            self.connected = False
            self._header()
            await asyncio.sleep(delay)
            delay = min(10, delay * 2)

    def _schedule_refresh(self) -> None:
        if not self.refresh_pending:
            self.refresh_pending = True
            self.set_timer(0.2, self._refresh_scheduled)

    async def _refresh_scheduled(self) -> None:
        self.refresh_pending = False
        if self.conversation and self.conversation.session:
            view = self.conversation
            self._start(lambda: self._refresh(view), group="refresh", exclusive=True)

    async def _load_requests(self) -> None:
        response = await self.client.rpc("extensions.requests")
        self.requests = response.get("requests", [])
        self._header()

    def _render_if_dirty(self) -> None:
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
        items = copy.deepcopy(view.items)
        timeline = self.query_one("#timeline", VerticalScroll)
        follow = timeline.is_vertical_scroll_end if self.rows else view.follow_tail
        scroll = timeline.scroll_y if self.rows else view.scroll_y
        try:
            wanted = {str(item["id"]) for item in items}
            for key in list(self.rows):
                if key not in wanted:
                    await self.rows.pop(key).remove()
            for item in items:
                key = str(item["id"])
                if key not in self.rows:
                    widget = self.rows[key] = TimelineEntry()
                    await timeline.mount(widget)
                await self.rows[key].update_item(item)
            for index, item in enumerate(items):
                widget = self.rows[str(item["id"])]
                if index == 0:
                    timeline.move_child(widget, before=0)
                else:
                    timeline.move_child(widget, after=self.rows[str(items[index - 1]["id"])])
            if follow:
                self.call_after_refresh(timeline.scroll_end, animate=False)
            else:
                self.call_after_refresh(timeline.scroll_to, y=scroll, animate=False)
            self._header()
        finally:
            self.rendering = False

    def _header(self) -> None:
        view = self.conversation
        if view is None:
            return
        status = "connected" if self.connected else "connecting"
        self.query_one("#identity", Static).update(
            terminal_text(
                f"{view.title} · {view.agent} · {view.project or 'Workspace'}\n"
                f"{self.client.url} · {status}"
            )
        )
        active = view.facts.get("active_run") or {}
        usage = view.facts.get("session_usage") or {}
        context = view.facts.get("context_usage") or {}
        details = [self.last_notice]
        if active:
            details.append(f"Run {active.get('status', 'working')}")
        if view.overrides.get("model"):
            details.append(str(view.overrides["model"]))
        if view.overrides.get("thinking_effort"):
            details.append(str(view.overrides["thinking_effort"]))
        if context:
            used = context.get("input_tokens", context.get("total_tokens", context.get("tokens")))
            details.append(
                f"Context {used:,}" if isinstance(used, int) else "Context available (F4)"
            )
            if context.get("context_window"):
                details[-1] += f" / {context['context_window']:,}"
        if usage:
            details.append(
                f"Usage {usage.get('input_tokens', 0):,} in · {usage.get('output_tokens', 0):,} out"
            )
        self.query_one("#status", Static).update(" · ".join(details))
        draft = []
        if view.uncertain:
            draft.append(
                "Send outcome unknown. F6 / Ctrl+R inspect; Send offers explicit recovery."
            )
        if view.parent:
            draft.append(
                "Sub-Agent Session: sending here directs its following answers to you. "
                "Ctrl+B returns."
            )
        if view.attachments:
            draft.append(
                "Local uploads: " + ", ".join(item["filename"] for item in view.attachments)
            )
        if view.mentions:
            draft.append("Server files: " + ", ".join(view.mentions))
        self.query_one("#draft-info", Static).update(" · ".join(draft))
        self.query_one("#queue", Button).label = f"Queue ({len(self.queue_items)})"
        self.query_one("#requests", Button).label = f"Requests ({len(self.requests)})"

    def _notice(self, message: str, *, error: bool = False) -> None:
        message = terminal_text(message)
        self.last_notice = message
        self.notify(
            escape(message), severity="error" if error else "information", timeout=8 if error else 4
        )
        self._header()

    @on(Button.Pressed)
    def button_pressed(self, event: Button.Pressed) -> None:
        name = (event.button.id or "").replace("-", "_")
        action = getattr(self, f"action_{name}", None)
        if action is not None:
            action()

    @on(TextArea.Changed, "#composer")
    def draft_changed(self, event: TextArea.Changed) -> None:
        if self.conversation:
            self.conversation.draft = event.text_area.text

    @on(Markdown.LinkClicked)
    def link_clicked(self, event: Markdown.LinkClicked) -> None:
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
        text = self.query_one("#composer", TextArea).text
        if view.uncertain:
            original = view.uncertain_submission or {
                "text": text,
                "attachments": [],
                "mentions": [],
            }
            choice = await self.push_screen_wait(
                Picker(
                    "Previous send has an unknown outcome. Inspect the Session and Queue first.",
                    [
                        ("Return to conversation", "return"),
                        ("Review the exact earlier submission", "review"),
                        ("The earlier message was accepted", "accepted"),
                        ("It was not accepted: restore the earlier draft", "restore"),
                    ],
                )
            )
            if choice == "review":
                await self.push_screen_wait(
                    TextDialog("Submission with unknown outcome", pretty(original), read_only=True)
                )
                return
            if choice == "accepted":
                view.uncertain = False
                view.uncertain_submission = None
                if view.draft == original["text"]:
                    view.draft = ""
                    self.query_one("#composer", TextArea).load_text("")
                submitted_ids = {item["attachment_id"] for item in original["attachments"]}
                view.attachments = [
                    item for item in view.attachments if item["attachment_id"] not in submitted_ids
                ]
                view.mentions = [item for item in view.mentions if item not in original["mentions"]]
                self._header()
                return
            if choice != "restore":
                return
            later_draft = {
                "text": view.draft,
                "attachments": copy.deepcopy(view.attachments),
                "mentions": list(view.mentions),
            }
            if later_draft != original:
                self.saved_drafts.append(later_draft)
            view.draft = original["text"]
            view.attachments = original["attachments"]
            view.mentions = original["mentions"]
            view.uncertain = False
            view.uncertain_submission = None
            self.query_one("#composer", TextArea).load_text(view.draft)
            self._notice("Earlier draft restored. Later draft is in input history (Alt+Up).")
            return
        if not text.strip() and not view.attachments:
            return
        request = view.submission(text)
        submitted = {
            "text": text,
            "attachments": copy.deepcopy(view.attachments),
            "mentions": list(view.mentions),
        }
        submitted_attachments = {item["attachment_id"] for item in view.attachments}
        submitted_mentions = set(view.mentions)
        self.pending.add(id(view))
        try:
            result = await self.client.rpc("chat.stream", request)
        except ClientError as exc:
            view.uncertain = exc.state == "unknown"
            if view.uncertain:
                view.uncertain_submission = submitted
            self._notice(str(exc) + " Your draft is retained.", error=True)
            self._schedule_refresh()
            return
        finally:
            self.pending.discard(id(view))
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
            self.views[view.key] = view
            if self.conversation is view:
                async with self._render_lock:
                    await self.query_one("#timeline", VerticalScroll).remove_children()
                    self.rows.clear()
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
        await self._load_roster()
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
                for row in response.get("sessions", []):
                    active = "● " if row.get("has_active_run") else ""
                    unread = "• " if row.get("has_unread_completion") is True else ""
                    title = row.get("title") or row.get("auto_title") or row["id"]
                    label = (
                        f"{active}{unread}{title} · {row['agent_address']} · "
                        f"{row.get('working_project_id') or 'Workspace'}"
                    )
                    collected.append((label, row))
                cursors[index] = response.get("next_cursor")
            first_page = False
            collected.sort(key=lambda entry: entry[1].get("updated_at", ""), reverse=True)
            choices = collected + ([("Load older Sessions…", "older")] if any(cursors) else [])
            row = await self.push_screen_wait(
                Picker("Sessions · search title, Agent or Project", choices)
            )
            if row == "older":
                continue
            if isinstance(row, dict):
                view = Conversation(
                    row["agent_address"], session=row["id"], parent=row.get("subagent_parent")
                )
                await self._open(view)
            return

    @work(group="navigation", exclusive=True, exit_on_error=False)
    async def action_new(self) -> None:
        await self._guard(self._new())

    async def _new(self) -> None:
        await self._load_roster()
        agent = await self.push_screen_wait(Picker("New conversation · choose Agent", self.roster))
        if not agent:
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
            if choice is None:
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
        view = self.conversation
        if view is None:
            return
        operation = await self.push_screen_wait(
            Picker(
                "Session settings",
                [
                    ("Choose Model", "model"),
                    ("Choose thinking effort", "thinking_effort"),
                    ("Inspect context and Usage", "usage"),
                ],
            )
        )
        if operation == "usage":
            await self.push_screen_wait(
                TextDialog(
                    "Context and Usage",
                    pretty(
                        {
                            key: view.facts.get(key)
                            for key in ("context_usage", "session_usage", "compaction_policy")
                        }
                    ),
                    read_only=True,
                )
            )
            return
        if operation == "model":
            response = await self.client.rpc("model.list", {"task": "chat"})
            choices = [
                (
                    f"{row.get('name', row['model_id'])} · {row['provider_id']}/{row['model_id']}",
                    f"{row['provider_id']}/{row['model_id']}",
                )
                for row in response.get("models", [])
            ]
        elif operation == "thinking_effort":
            choices = [("Provider default", "")] + [
                (value, value)
                for value in ("none", "minimal", "low", "medium", "high", "xhigh", "max")
            ]
        else:
            return
        value = await self.push_screen_wait(
            Picker(f"Choose {operation.replace('_', ' ')}", choices)
        )
        if value is None:
            return
        changes = {operation: value}
        if operation == "thinking_effort":
            changes = {"thinking_effort": value or None, "thinking_effort_default": not bool(value)}
        if view.session:
            result = await self.client.rpc(
                "session.set_agent_overrides",
                {**view.params(), "agent_overrides": changes},
            )
            view.overrides = result.get("agent_overrides") or {}
        else:
            for key, replacement in changes.items():
                if replacement is None:
                    view.overrides.pop(key, None)
                else:
                    view.overrides[key] = replacement
        self._notice("Session setting saved" if view.session else "Draft setting selected")

    @work(group="queue", exclusive=True, exit_on_error=False)
    async def action_queue(self) -> None:
        await self._guard(self._queue())

    async def _queue(self) -> None:
        view = self.conversation
        if view is None or not view.session:
            return
        response = await self.client.rpc("chat.queue_list", view.params())
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
        if not item:
            return
        actions = [("Remove from Queue", "remove")]
        if item.get("steerable") and not item.get("steering"):
            actions.insert(0, ("Steer into the current Run", "steer"))
        if item.get("editable"):
            actions.insert(1, ("Edit queued message", "update"))
        action = await self.push_screen_wait(Picker("Queued message", actions))
        if not action:
            return
        params = {**view.params(), "item_id": item["id"]}
        if action == "update":
            text = await self.push_screen_wait(
                TextDialog("Edit queued message", content_text(item.get("content")))
            )
            if text is None:
                return
            params["content"] = text
            params["reply_surface"] = "tui"
        await self.client.rpc(f"chat.queue_{action}", params)
        await self._refresh(view)

    @work(group="stop", exclusive=True, exit_on_error=False)
    async def action_stop(self) -> None:
        await self._guard(self._stop(False))

    @work(group="stop", exclusive=True, exit_on_error=False)
    async def action_stop_all(self) -> None:
        await self._guard(self._stop(True))

    async def _stop(self, all_work: bool) -> None:
        view = self.conversation
        if view is None or not view.session:
            return
        if all_work:
            await self.client.rpc("chat.stop_all", view.params())
            self._notice("Stop all requested. This Session's queued messages remain queued.")
        else:
            current = await self.client.rpc("chat.timeline", {**view.params(), "limit": 1})
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

    async def _older(self) -> None:
        view = self.conversation
        if not view or not view.next_before:
            self._notice("All available history is loaded.")
            return
        timeline = self.query_one("#timeline", VerticalScroll)
        old_height, scroll = timeline.virtual_size.height, timeline.scroll_y
        page = await self.client.rpc(
            "chat.timeline", {**view.params(), "limit": 100, "before": view.next_before}
        )
        view.page(page, older=True)
        await self._render()
        self.call_after_refresh(
            lambda: timeline.scroll_to(
                y=scroll + timeline.virtual_size.height - old_height, animate=False
            )
        )

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
        response = await self.push_screen_wait(ExtensionDialog(request))
        if response is None:
            return
        await self.client.rpc(
            "extensions.operation",
            {
                "name": request["extension"],
                "operation": request["response_operation"],
                "arguments": {"request_id": request["id"], "response": response},
            },
        )
        await self._load_requests()

    @work(group="activity", exclusive=True, exit_on_error=False)
    async def action_activity(self) -> None:
        await self._guard(self._activity())

    async def _activity(self) -> None:
        view = self.conversation
        if not view or not view.session:
            return
        work_state = await self.client.rpc("chat.work", view.params())
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
        if not selected:
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
            choices = [
                ("Open Sub-Agent Session", "open"),
                ("Inspect work and outcome", "detail"),
                ("Stop this Sub-Agent and its background work", "stop"),
            ]
            action = await self.push_screen_wait(Picker("Sub-Agent", choices))
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
            if action:
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

    def action_detach(self) -> None:
        self.exit(0)
