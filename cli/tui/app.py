"""A conversation-first Textual accessor for the shared vBot server."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Coroutine
from contextlib import suppress
from time import monotonic
from typing import Any, override

from textual import events, on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.css.query import NoMatches
from textual.markup import escape
from textual.screen import ModalScreen
from textual.widgets import Markdown, Static, TextArea
from textual.worker import Worker

from cli.tui import agent_secrets, composer
from cli.tui.commands import ConversationCommands
from cli.tui.completion import (
    CompletionBar,
)
from cli.tui.conversation import ConversationController
from cli.tui.dialogs import ExtensionDialog, Picker, TextDialog
from cli.tui.drafts import DraftStore, holds_secret, kept_draft
from cli.tui.mentions import resolve_mentions, retained_mentions
from cli.tui.models import effective_value
from cli.tui.rendering import (
    content_text,
    flatten,
    pretty,
    terminal_text,
    truncated,
)
from cli.tui.state import Conversation, JsonObject
from cli.tui.submissions import SubmissionController
from cli.tui.suggestions import ComposerSuggestions
from cli.tui.theme import DEFAULT_THEME, THEMES, VBOT_DARK, VBOT_LIGHT
from cli.tui.transcript import Transcript
from cli.tui.transport import ClientError, TerminalClient

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
        self.conversation: Conversation | None = None
        self.render_dirty = False
        self.rendering = False
        self.render_worker: Worker[Any] | None = None
        self.requests: list[JsonObject] = []
        # Every pending Secret request; `agent_secrets.requests_for` picks this
        # conversation's.
        self.secret_requests: list[JsonObject] = []
        self.queue_items: list[JsonObject] = []
        self.last_notice = ""
        self.notice_level = "info"
        self.notice_expires = 0.0
        self._copied_selection: object = None
        self._render_lock = asyncio.Lock()
        self.navigation = ConversationController(
            self, agent=agent_id, session=session_id, project=project_id
        )
        self.submissions = SubmissionController(self, store=draft_store, restored=restored_drafts)
        self.suggestions = ComposerSuggestions(self)

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
        self._start(self.navigation.events, group="events", exclusive=True)
        self._start(self.navigation.bootstrap)

    @override
    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        # Priority bindings must not send the hidden composer while a form is open.
        if isinstance(self.screen, ModalScreen) and action != "detach":
            return False
        if action == "send" and self.focused is not self._composer():
            return False
        if action in _COMPLETION_ACTIONS and not self.suggestions.state.visible:
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

    async def _load_requests(self) -> None:
        response = await self.client.rpc("extensions.requests")
        self.requests = response.get("requests", [])
        self._header()

    def _render_if_dirty(self) -> None:
        if not self.screen_stack:
            return
        self.navigation.completion_if_visible()
        view = self.conversation
        if view is not None and (
            view.facts.get("active_run") or self.navigation.activity.of(view) is not None
        ):
            # The status row's spinner and elapsed work span; the tick can outlive
            # the widgets while the app shuts down.
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
            self.call_after_refresh(self.navigation.completion_if_visible)
        finally:
            self.rendering = False

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
                (row for row in self.navigation.projects if row.get("project_id") == project_id),
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
            (row for row in self.navigation.agents if row.get("id") == identifier),
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
        if not self.navigation.connected:
            return f"Connecting to {self.client.url}…", "alert"
        if id(view) in self.navigation.loading_views:
            return "Updating conversation…" if view.items else "Loading conversation…", "info"
        if view.uncertain:
            return (
                "Send outcome unknown · Ctrl+R reloads the Session; Send offers recovery",
                "alert",
            )
        # The Agent's Run waits on the answer, so the request outranks its spinner.
        if waiting := agent_secrets.status_text(self, view):
            return waiting, "alert"
        if view.parent:
            return "Sub-Agent Session · replies go to you · Ctrl+B returns to the parent", "info"
        if work := self.navigation.activity.status(view):
            return work, "info"
        if self.queue_items:
            return f"{len(self.queue_items)} queued message(s) · F6", "info"
        if self.requests:
            return f"{len(self.requests)} Extension request(s) · F7", "info"
        return "", "info"

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
        self.suggestions.refresh(event.text_area)
        if self.conversation:
            # A `/secret` command with its value stays in the composer alone.
            self.conversation.draft = kept_draft(event.text_area.text)
            mentions = retained_mentions(self.conversation.draft, self.conversation.mentions)
            if mentions != self.conversation.mentions:
                self.conversation.mentions = mentions
                self._header()
            self.submissions.changed(self.conversation)

    def _composer(self) -> TextArea | None:
        """The composer, or None while a conversation change rebuilds the screen."""
        try:
            return self.query_one("#composer", TextArea)
        except NoMatches:
            return None

    def action_completion_up(self) -> None:
        self.suggestions.move(-1)

    def action_completion_down(self) -> None:
        self.suggestions.move(1)

    async def action_completion_accept(self) -> None:
        await self.suggestions.accept()

    def action_completion_close(self) -> None:
        """Close the popup until the draft changes again, as the WebUI does."""
        composer = self._composer()
        if composer is not None:
            self.suggestions.state.dismissed = composer.text
        self.suggestions.close()

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
        await self.submissions.send()

    @work(group="navigation", exclusive=True, exit_on_error=False)
    async def action_sessions(self) -> None:
        await self._guard(self.navigation.select_session())

    @work(group="navigation", exclusive=True, exit_on_error=False)
    async def action_new(self) -> None:
        await self._guard(self.navigation.new())

    @work(group="navigation", exclusive=True, exit_on_error=False)
    async def action_previous_conversation(self) -> None:
        await self._guard(self.navigation.previous())

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
            if holds_secret(text):
                # The Queue keeps its messages; the composer sends the command alone.
                self._notice(
                    "A /secret command cannot replace a queued message. Send it from the composer."
                )
                return
            params["content"] = text
            params["reply_surface"] = "tui"
            mentions = await resolve_mentions(self.client, view.params(), text)
            if self.conversation is not view or view.key != key or not view.available:
                return
            if mentions:
                params["file_mentions"] = mentions
        await self.client.rpc(f"chat.queue_{action}", params)
        await self.navigation.refresh(view)

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
        await self.navigation.refresh(view)

    @work(group="refresh", exclusive=True, exit_on_error=False)
    async def action_refresh(self) -> None:
        if self.conversation:
            await self._guard(self.navigation.refresh(self.conversation))
        else:
            await self._guard(self.navigation.bootstrap())

    @work(group="older", exclusive=True, exit_on_error=False)
    async def action_older(self) -> None:
        await self._guard(self.navigation.older())

    @on(Transcript.LoadOlder)
    def older_history_requested(self) -> None:
        if self.conversation and id(self.conversation) not in self.navigation.older_loading:
            self.action_older()

    @on(Transcript.LoadHistory)
    def run_history_requested(self, event: Transcript.LoadHistory) -> None:
        view = self.conversation
        if view is not None:
            self._start(
                lambda: self.navigation.load_run_history(view, event.item), group="run-history"
            )

    @on(Transcript.LoadToolCall)
    def tool_call_requested(self, event: Transcript.LoadToolCall) -> None:
        view = self.conversation
        if view is not None:
            self._start(lambda: self.navigation.load_tool_call(view, event.item), group="tool-call")

    @on(Transcript.Leave)
    def transcript_left(self) -> None:
        self.query_one("#composer", TextArea).focus()

    def on_text_selected(self, event: events.TextSelected) -> None:
        """Copy on select, as terminals do: the mouse belongs to the app here."""
        selections = self.screen.selections
        if selections is self._copied_selection:
            # A double click reports its selection along with the mouse release.
            return
        text = self.screen.get_selected_text()
        if text and text.strip():
            self._copied_selection = selections
            self.copy_to_clipboard(text)
            self._notice("Copied to the clipboard")

    @work(group="requests", exclusive=True, exit_on_error=False)
    async def action_requests(self) -> None:
        await self._guard(self._requests())

    async def _requests(self) -> None:
        await asyncio.gather(self._load_requests(), agent_secrets.load_requests(self))
        # This conversation's Secret requests come first: its Run waits on them.
        choices: list[tuple[str, Any]] = [
            (
                f"Secret · {row.get('name', '')} · {row.get('description') or 'the Agent waits'}",
                ("secret", row),
            )
            for row in agent_secrets.requests_for(self, self.conversation)
        ]
        choices += [
            (
                f"{row['extension']} · {row.get('kind', '')} · "
                f"{row.get('payload', {}).get('message', row['id'])}",
                row,
            )
            for row in self.requests
        ]
        if not choices:
            self._notice("No Secret or Extension requests are waiting.")
            return
        request = await self.push_screen_wait(Picker("Requests", choices))
        if not request:
            return
        if isinstance(request, tuple):
            await agent_secrets.answer(self, request[1])
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
                await self.navigation.open(
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
            await self.navigation.open(Conversation(target, session=data["session_id"]))
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
        self.navigation.save_view()
        try:
            await self.submissions.persist()
        except (OSError, ValueError) as exc:
            self._notice(
                f"Drafts could not be saved: {exc}. Copy them before leaving or retry Ctrl+Q.",
                error=True,
            )
            return
        self.exit(0)
