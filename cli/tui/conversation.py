"""Conversation navigation, history snapshots and live stream reconciliation.

The controller owns the local view catalog, reading cache and connection workers.
The Textual application provides widgets, worker scheduling and notices.
"""

from __future__ import annotations

import asyncio
import copy
import json
from collections import OrderedDict
from typing import TYPE_CHECKING, Any

from textual.screen import ModalScreen
from textual.widgets import TextArea
from textual.worker import Worker

from cli.tui import agent_secrets
from cli.tui.dialogs import Picker
from cli.tui.drafts import kept_draft, readdress_saved_inputs
from cli.tui.state import Conversation, JsonObject, TimelineGapError
from cli.tui.transport import ClientError

if TYPE_CHECKING:
    from cli.tui.app import ConversationApp

_sleep = asyncio.sleep
_ACCESS_REFUSALS = frozenset({"auth_required", "access_denied"})


class ConversationController:
    """Reconcile server reads with the selected conversation without replaying writes."""

    def __init__(
        self,
        app: ConversationApp,
        *,
        agent: str | None,
        session: str | None,
        project: str | None,
    ) -> None:
        self.app = app
        self.initial_agent = agent
        self.initial_session = session
        self.initial_project = project
        self.views: dict[tuple[str, str | None, bool, str | None], Conversation] = {}
        self.backstack: list[Conversation] = []
        self.roster: list[tuple[str, str]] = []
        self.agents: list[JsonObject] = []
        self.projects: list[JsonObject] = []
        self.stream: Worker[Any] | None = None
        self.stream_path: str | None = None
        self.connected = False
        self.loading_views: set[int] = set()
        self.roster_loaded = False
        self.history_cache: OrderedDict[int, Conversation] = OrderedDict()
        self.older_loading: set[int] = set()
        self.refresh_pending = False

    async def bootstrap(self) -> None:
        if self.initial_agent and self.initial_session:
            self.app._start(self.load_roster, group="roster")
        else:
            await self.load_roster()
        agent = self.initial_agent
        if agent is None:
            if len(self.roster) == 1:
                agent = self.roster[0][1]
            else:
                agent = await self.app.push_screen_wait(Picker("Choose an Agent", self.roster))
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
            await self.open(view)
        self.app.query_one("#composer", TextArea).focus()

    async def load_roster(self) -> None:
        if self.roster_loaded:
            return
        agents, projects = await asyncio.gather(
            self.app.client.rpc("agent.list"),
            self.app.client.rpc("project.list", {"include_scan": True}),
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
        self.app._header()

    def save_view(self) -> None:
        if self.app.conversation is not None:
            self.app.conversation.draft = kept_draft(self.app.query_one("#composer", TextArea).text)
            if self.app.transcript.view is self.app.conversation:
                self.app.transcript.remember_position()
            self.cache_history(self.app.conversation)
            self.app.submissions.changed(self.app.conversation)

    async def open(self, view: Conversation, *, remember: bool = True) -> None:
        self.save_view()
        if remember and self.app.conversation is not None and self.app.conversation is not view:
            self.backstack.append(self.app.conversation)
            self.backstack = self.backstack[-100:]
        if self.stream:
            self.stream.cancel()
        self.stream, self.stream_path = None, None
        view = self.views.setdefault(view.key, view)
        self.cache_history(view)
        async with self.app._render_lock:
            self.app.conversation = view
            self.app.queue_items = []
            await self.app.transcript.clear()
        self.app.submissions.changed(view)
        self.app.query_one("#composer", TextArea).load_text(view.draft)
        self.app._header()
        if view.session or (view.uncertain and view.pending_session_id):
            self.loading_views.add(id(view))
            try:
                if view.items:
                    await self.app._render()
                else:
                    self.app.transcript.placeholder("Loading conversation…")
                self.app._header()
                await self.refresh(view)
                if self.app.conversation is view:
                    await self.app._render()
            finally:
                self.loading_views.discard(id(view))
                self.app._header()
        else:
            self.app.transcript.placeholder(
                "New conversation. The Session is created when you send its first message."
            )
        self.app.query_one("#composer", TextArea).focus()

    def cache_history(self, view: Conversation) -> None:
        """Bound reconstructible Session content; drafts and navigation stay local."""
        view.cache_bytes = len(json.dumps(view.items, ensure_ascii=False).encode("utf-8"))
        self.history_cache[id(view)] = view
        self.history_cache.move_to_end(id(view))
        retained = sum(entry.cache_bytes for entry in self.history_cache.values())
        for identifier, entry in list(self.history_cache.items()):
            if len(self.history_cache) <= 8 and retained <= 16 * 1024 * 1024:
                break
            if entry is self.app.conversation or entry is view:
                continue
            retained -= entry.cache_bytes
            entry.refresh_revision += 1
            entry.release_history()
            del self.history_cache[identifier]

    async def read_timeline(self, view: Conversation, revision: int) -> Conversation | None:
        """Drain a read snapshot without publishing a partial or stale catch-up."""
        key = view.key
        candidate = copy.copy(view)
        candidate.facts = dict(view.facts)
        after = view.facts.get("next_after")
        while True:
            params = {**view.params(), "limit": 100, "child_limit": 60}
            if after:
                params["after"] = after
            response = await self.app.client.rpc("chat.timeline", params)
            if view.refresh_revision != revision or view.key != key or not view.available:
                return None
            candidate.page(response)
            if not response.get("has_newer"):
                return candidate
            next_after = response.get("next_after")
            if not next_after or next_after == after:
                raise ClientError("History catch-up did not advance. Reconnect to try again.")
            after = next_after

    def move_view(self, view: Conversation, agent: str, session: str) -> None:
        self.views.pop(view.key, None)
        view.refresh_revision += 1
        view.agent, view.session = agent, session
        if (
            (previous := self.views.get(view.key)) is not None
            and previous is not view
            and (previous.draft or previous.attachments)
        ):
            self.app.submissions.saved_drafts.append(self.app.submissions.snapshot(previous))
        self.views[view.key] = view
        view.available = True
        self.app.submissions.changed(view)

    async def refresh(self, view: Conversation) -> None:
        if not view.available:
            return
        if not view.session and not await self.app.submissions.recover(view):
            return
        view.refresh_revision += 1
        revision = view.refresh_revision
        key = view.key
        captured_sequence, captured_run = view.sequence, view.stream_run
        candidate, summary, queue = await asyncio.gather(
            self.read_timeline(view, revision),
            self.app.client.rpc("session.get", view.params()),
            self.app.client.rpc("chat.queue_list", view.params()),
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
            self.schedule_refresh()
            return
        if (
            response_run is None
            and captured_run == view.stream_run
            and captured_sequence != view.sequence
        ):
            self.schedule_refresh()
            return
        view.items, view.facts = candidate.items, candidate.facts
        self.cache_history(view)
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
        self.app.submissions.changed(view)
        if self.app.conversation is not view:
            return
        self.app.queue_items = queue.get("items", [])
        self.app.render_dirty = True
        active = response.get("active_run") or {}
        path = active.get("timeline_url")
        if path != self.stream_path:
            if self.stream:
                self.stream.cancel()
            self.stream_path = path
            self.stream = (
                self.app._start(lambda: self.follow(view, path), group="timeline", exclusive=True)
                if path
                else None
            )
        self.app._header()

    async def follow(self, view: Conversation, path: str) -> None:
        delay = 0.5
        while self.app.conversation is view:
            retry_after = 0.0
            try:
                async for event in self.app.client.timeline(path):
                    if self.app.conversation is not view:
                        return
                    if view.event(event):
                        self.app.render_dirty = True
                    payload = event.get("payload", event)
                    if payload.get("terminal"):
                        self.stream_path = None
                        self.schedule_refresh()
                        return
                    delay = 0.5
            except ClientError as exc:
                self.app._notice(str(exc))
                if exc.code in _ACCESS_REFUSALS:
                    return
                retry_after = exc.retry_after or 0.0
            except TimelineGapError:
                continue
            # Read the authoritative state before opening another stream. Never resubmit.
            await _sleep(max(delay, retry_after))
            delay = min(10, delay * 2)
            try:
                view.refresh_revision += 1
                revision = view.refresh_revision
                captured_sequence, captured_run = view.sequence, view.stream_run
                candidate = await self.read_timeline(view, revision)
            except ClientError as exc:
                if exc.code in _ACCESS_REFUSALS:
                    self.app._notice(str(exc))
                    return
                if exc.retry_after is not None:
                    await _sleep(exc.retry_after)
                continue
            if self.app.conversation is not view:
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
            self.app.render_dirty = True
            active = view.facts.get("active_run") or {}
            next_path = active.get("timeline_url")
            if not isinstance(next_path, str):
                self.stream_path = None
                return
            path = next_path
            self.stream_path = path

    async def events(self) -> None:
        delay = 0.5
        refused = False
        while True:
            retry_after = 0.0
            try:
                async for event in self.app.client.events():
                    self.connected = True
                    delay = 0.5
                    refused = False
                    kind = event.get("type", "")
                    if kind == "resource_changed":
                        resource = event.get("payload") or {}
                        if resource.get("kind") in {"agents", "projects"}:
                            self.roster_loaded = False
                        await self.resource_changed(resource)
                    if (
                        kind in {"connection_ready", "connected", "connection", "state", "snapshot"}
                        or "snapshot" in kind
                    ):
                        self.schedule_refresh()
                        self.app._start(
                            self.app._load_requests, group="requests-read", exclusive=True
                        )
                        self.load_secret_requests()
                    elif kind == "resource_changed":
                        resource = event.get("payload") or {}
                        if resource.get("kind") in {"agents", "projects", "settings", "models"} or (
                            resource.get("kind") in {"sessions", "queue"}
                            and self.event_targets_view(resource.get("scope") or {})
                        ):
                            self.schedule_refresh()
                        if resource.get("kind") == "extensions":
                            self.app._start(
                                self.app._load_requests, group="requests-read", exclusive=True
                            )
                        if resource.get("kind") == "agent_secrets":
                            self.load_secret_requests()
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
                        if self.event_targets_view(event.get("payload") or event):
                            self.schedule_refresh()
                    self.app._header()
            except ClientError as exc:
                if exc.code in _ACCESS_REFUSALS:
                    if exc.code == "auth_required" and not refused:
                        # A restarted server wrote a new local access token; the
                        # next connection reads it. A second refusal is final.
                        refused = True
                        continue
                    self.connected = False
                    self.app._header()
                    self.app._notice(f"Event connection closed. {exc}", error=True)
                    return
                self.app._notice(f"Reconnecting to {self.app.client.url} · {exc}")
                retry_after = exc.retry_after or 0.0
            except OSError as exc:
                self.app._notice(f"Reconnecting to {self.app.client.url} · {exc}")
            except Exception:
                self.app._notice(f"Event connection lost. Reconnecting to {self.app.client.url}.")
            self.connected = False
            self.app._header()
            await _sleep(max(delay, retry_after))
            delay = min(10, delay * 2)

    def load_secret_requests(self) -> None:
        """Read the pending Secret requests again; the latest read wins."""
        self.app._start(
            lambda: agent_secrets.load_requests(self.app),
            group="secret-requests-read",
            exclusive=True,
        )

    def event_targets_view(self, payload: JsonObject) -> bool:
        view = self.app.conversation
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

    def schedule_refresh(self) -> None:
        if not self.refresh_pending:
            self.refresh_pending = True
            self.app.set_timer(0.2, self.refresh_scheduled)

    async def refresh_scheduled(self) -> None:
        self.refresh_pending = False
        if self.app.conversation and (
            self.app.conversation.session
            or (self.app.conversation.uncertain and self.app.conversation.pending_session_id)
        ):
            view = self.app.conversation
            self.app._start(lambda: self.refresh(view), group="refresh", exclusive=True)

    async def resource_changed(self, payload: JsonObject) -> None:
        scope = payload.get("scope") or {}
        old, new = scope.get("old_agent_id"), scope.get("new_agent_id")
        if payload.get("kind") == "agents" and old and new:
            if readdress_saved_inputs(
                self.app.submissions.saved_drafts, self.app.submissions.recovered_drafts, old, new
            ):
                self.app.submissions.changed(self.app.conversation)
            views = {id(view): view for view in [*self.views.values(), *self.backstack]}
            if self.app.conversation:
                views[id(self.app.conversation)] = self.app.conversation
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
                        self.app.submissions.saved_drafts.append(
                            self.app.submissions.snapshot(previous)
                        )
                    self.views[view.key] = view
                    self.app.submissions.changed(view)
                if (
                    view.parent
                    and view.parent.get("agent_id") == old
                    and not view.parent.get("project_id")
                ):
                    view.parent = {**view.parent, "agent_id": new}
                    self.app.submissions.changed(view)
            self.app._header()
        if payload.get("kind") != "sessions":
            return
        agent = scope.get("agent_id")
        if scope.get("project_id"):
            agent = f"{agent}@{scope['project_id']}"
        deleted = scope.get("deleted_session_id")
        moved_to = scope.get("moved_to_agent_id")
        if agent and moved_to and scope.get("session_id"):
            if readdress_saved_inputs(
                self.app.submissions.saved_drafts,
                self.app.submissions.recovered_drafts,
                agent,
                moved_to,
                session=scope["session_id"],
            ):
                self.app.submissions.changed(self.app.conversation)
            for view in list(self.views.values()):
                if view.agent == agent and view.session == scope["session_id"]:
                    self.move_view(view, moved_to, scope["session_id"])
            self.app._header()
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
                self.app.submissions.saved_drafts.append(self.app.submissions.snapshot(view))
            self.app.submissions.changed(view)
            if self.app.conversation is view:
                await self.open(Conversation(agent, session=scope.get("next_session_id")))
                self.app._notice(
                    "Conversation archived. Its unsent input is retained in input history."
                )

    def completion_if_visible(self) -> None:
        view = self.app.conversation
        frame = getattr(self.app, "_rendered_frame", None)
        timeline = getattr(self.app, "transcript", None)
        if (
            view is None
            or not view.available
            or not view.session
            or isinstance(self.app.screen, ModalScreen)
            or not self.app.app_focus
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
        self.app._start(lambda: self.mark_completion_read(view, completed), group="read-receipt")

    async def mark_completion_read(self, view: Conversation, run_id: str) -> None:
        key = view.key
        try:
            if self.app.conversation is not view or isinstance(self.app.screen, ModalScreen):
                return
            await self.app.client.rpc("session.mark_read", {**view.params(), "run_id": run_id})
            if view.key == key:
                view.completion_read = run_id
        except ClientError:
            view.completion_read_failed = run_id
            raise
        finally:
            if view.completion_read_pending == run_id:
                view.completion_read_pending = None

    async def select_session(self) -> None:
        origin = self.app.conversation
        await self.load_roster()
        if self.app.conversation is not origin:
            return
        addresses = [address for _, address in self.roster]
        if not addresses:
            self.app._notice("No Agents are configured. Create one with vbot agent create.")
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
                response = await self.app.client.rpc("session.list", params)
                if self.app.conversation is not origin:
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
            row = await self.app.push_screen_wait(
                Picker("Sessions · search title, Agent or Project", choices)
            )
            if self.app.conversation is not origin:
                return
            if row == "older":
                continue
            if isinstance(row, dict):
                view = Conversation(
                    row["agent_address"], session=row["id"], parent=row.get("subagent_parent")
                )
                if known := self.views.get(view.key):
                    known.available = True
                await self.open(view)
            return

    async def new(self) -> None:
        origin = self.app.conversation
        await self.load_roster()
        if self.app.conversation is not origin:
            return
        agent = await self.app.push_screen_wait(
            Picker("New conversation · choose Agent", self.roster)
        )
        if not agent or self.app.conversation is not origin:
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
            choice = await self.app.push_screen_wait(
                Picker("Working Project · server filesystem", choices)
            )
            if choice is None or self.app.conversation is not origin:
                return
            selected, project = choice
        # Keep an existing unsent draft instead of replacing its text silently.
        candidate = Conversation(agent, project=project, project_selected=selected)
        existing = self.views.get(candidate.key)
        if existing and (existing.draft or existing.attachments):
            await self.open(existing)
            self.app._notice("Resumed this Agent's unsent draft.")
        else:
            self.views.pop(candidate.key, None)
            await self.open(candidate)

    async def load_run_history(self, view: Conversation, gap: JsonObject) -> None:
        if view is None or id(view) in self.older_loading:
            # Busy with another page: the gap asks again when it next shows.
            self.app.transcript.history_failed(gap)
            return
        key, revision = view.key, view.refresh_revision
        self.older_loading.add(id(view))
        try:
            page = await self.app.client.rpc(
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
                self.app.conversation is view
                and view.key == key
                and view.refresh_revision == revision
                and view.child_page(page)
            ):
                await self.app._render()
            else:
                self.app.transcript.history_failed(gap)
        except ClientError as exc:
            self.app._notice(str(exc), error=True)
        finally:
            self.older_loading.discard(id(view))

    async def older(self) -> None:
        view = self.app.conversation
        if not view or not view.next_before:
            self.app._notice("All available history is loaded.")
            return
        if id(view) in self.older_loading:
            return
        key, revision = view.key, view.refresh_revision
        generation = view.facts.get("history_generation")
        self.older_loading.add(id(view))
        try:
            page = await self.app.client.rpc(
                "chat.timeline",
                {**view.params(), "limit": 100, "before": view.next_before, "child_limit": 60},
            )
        finally:
            self.older_loading.discard(id(view))
        if (
            self.app.conversation is not view
            or key != view.key
            or revision != view.refresh_revision
        ):
            return
        if generation and page.get("history_generation", generation) != generation:
            self.schedule_refresh()
            return
        view.page(page, older=True)
        await self.app._render()

    async def previous(self) -> None:
        if self.backstack:
            await self.open(self.backstack.pop(), remember=False)
        elif self.app.conversation and self.app.conversation.parent:
            parent = self.app.conversation.parent
            agent = parent["agent_id"]
            if parent.get("project_id"):
                agent += "@" + parent["project_id"]
            await self.open(Conversation(agent, session=parent["session_id"]), remember=False)
