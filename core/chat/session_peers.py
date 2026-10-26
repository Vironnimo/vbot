"""Project-scoped peer discovery and observation over Sessions and Runs."""

from __future__ import annotations

import asyncio
import base64
import binascii
import builtins
import json
from collections.abc import Callable
from contextlib import aclosing
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from core.agents import LIBRARIAN_AGENT_ID, LIVE_BACKEND_AGENT_ID, LIVE_VOICE_AGENT_ID
from core.runs import ASSISTANT_OUTPUT_EVENT, USER_MESSAGE_EVENT, Run
from core.sessions import (
    SessionAddress,
    SessionConversationMessage,
    SessionConversationPage,
    SessionListCursor,
    SessionListFilters,
    SessionProjectGrouping,
)
from core.sessions.errors import SessionNotFoundError
from core.subagents.links import ancestors, descendants

if TYPE_CHECKING:
    from core.activity import SessionActivity
    from core.runs import ChatRunManager
    from core.sessions import ChatSessionManager
    from core.tools import ToolContext

JsonObject = dict[str, Any]
_BUILTINS = (LIBRARIAN_AGENT_ID, LIVE_BACKEND_AGENT_ID, LIVE_VOICE_AGENT_ID)
_FILTERS = SessionListFilters(include_memory_reflections=False, include_skill_reflections=False)
_MAX_SCAN = 1000


class SessionPeerError(Exception):
    """A refused peer operation with its Tool-visible recovery guidance."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class SessionPeer:
    address: SessionAddress
    generation_id: str
    id: str
    title: str
    working_project_id: str


class SessionPeers:
    """Coordinate peer reads; Chat owns admitted messages and Runs own waiting work."""

    def __init__(
        self, sessions: ChatSessionManager, activity: SessionActivity, runs: ChatRunManager
    ) -> None:
        self.sessions = sessions
        self.activity = activity
        self.runs = runs

    def reference(self, address: SessionAddress, generation_id: str) -> str:
        """Represent one exact canonical address/generation, never an authority token."""
        return _encode(
            "session",
            [
                self.sessions.history_id,
                address.project_id,
                address.agent_id,
                address.session_id,
                generation_id,
            ],
        )

    async def own(self, context: ToolContext) -> SessionPeer:
        address = _caller(context)
        return await self.sessions.run_async(self._peer, context, address, None, True)

    async def resolve(self, context: ToolContext, id: str) -> SessionPeer:
        address, generation = self._decode_reference(id)
        return await self.sessions.run_async(self._peer, context, address, generation, False)

    def _decode_reference(self, id: str) -> tuple[SessionAddress, str]:
        value = _decode(id, "session")
        if (
            not isinstance(value, list)
            or len(value) != 5
            or value[0] != self.sessions.history_id
            or value[1] is not None
            and not isinstance(value[1], str)
            or not all(isinstance(part, str) and part for part in value[2:])
        ):
            raise _unavailable()
        return SessionAddress(value[1], value[2], value[3]), value[4]

    def _scope(self, context: ToolContext) -> str:
        project = context.working_project_id
        if not project:
            raise SessionPeerError(
                "project_required",
                "This Session has no Working Project. "
                "Start a Session in a Project to contact its Sessions.",
            )
        binding = self.sessions.working_project(_caller(context))
        if binding.archived or binding.project_id != project:
            raise _unavailable()
        return project

    def _readable(self, context: ToolContext, address: SessionAddress) -> bool:
        if not context.permissions.restricted:
            return True
        caller = _caller(context)
        if (address.project_id, address.agent_id) != (caller.project_id, caller.agent_id):
            return False
        if not any(layer.source != "agent" for layer in context.permissions.layers):
            return True
        chain = ancestors(self.sessions, caller, strict=True)
        root = chain[-1] if chain else caller
        members = {root, *(link.session for link in descendants(self.sessions, root, strict=True))}
        return address in members

    def _peer(
        self,
        context: ToolContext,
        address: SessionAddress,
        generation: str | None,
        own: bool,
    ) -> SessionPeer:
        project = self._scope(context)
        if not own and address == _caller(context):
            raise SessionPeerError(
                "current_session",
                "This is your Session. Use its existing conversation; "
                "use list to find another Session.",
            )
        if not self._readable(context, address):
            raise SessionPeerError(
                "permission_denied",
                "Your Permission Rules do not allow reading this Session. "
                "Use list to find Sessions you can access.",
            )
        if address.project_id is None and address.agent_id in _BUILTINS:
            raise _unavailable()
        # Capture the generation before reading mutable descriptors. A later
        # replacement then fails the content projection's generation check;
        # cross-Session reads never acquire the recipient's write lock while
        # Chat can hold the caller's write lease across a Tool batch.
        version = self.sessions.list_history_versions([address]).get(address)
        if version is None or generation is not None and version[0] != generation:
            raise _unavailable()
        summary = self.sessions.summary(address)
        if summary is None or summary.get("working_project_id") != project:
            raise _unavailable()
        binding = self.sessions.working_project(address)
        if binding.archived or self.sessions.temporary_binding(address) is not None:
            raise _unavailable()
        descriptor = self.sessions.descriptor_sources([address]).get(address)
        if descriptor is None or descriptor.recall_visibility == "hidden":
            raise _unavailable()
        return SessionPeer(
            address=address,
            generation_id=version[0],
            id=self.reference(address, version[0]),
            title=str(summary.get("title") or summary.get("auto_title") or "Untitled Session")[
                :200
            ],
            working_project_id=project,
        )

    async def list(
        self, context: ToolContext, *, cursor: str | None = None, limit: int = 10
    ) -> JsonObject:
        project = await self.sessions.run_async(self._scope, context)
        state = self._list_cursor(cursor, project)
        # Activity owns the complete non-idle set, so old but still working
        # Sessions precede recent idle Sessions even beyond the first DB page.
        active = sorted({address for address, _ in self.activity.snapshot()}, key=_address_key)
        active.extend(
            sorted(
                {_run_address(run) for run in self.runs.active_runs()} - set(active),
                key=_address_key,
            )
        )
        active_set = set(active)
        items: builtins.list[JsonObject] = []
        if state.get("phase") == "active":
            after = state.get("after")
            for address in sorted(active, key=_address_key):
                if after is not None and _address_key(address) <= tuple(after):
                    continue
                state["after"] = list(_address_key(address))
                row = await self._list_item(context, address)
                if row is not None:
                    items.append(row)
                    if len(items) == limit:
                        return self._listed(items, project, state)
            state = {"project": project, "phase": "idle"}
        grouping = SessionProjectGrouping((project,), _BUILTINS)
        scanned = 0
        while len(items) < limit and scanned < _MAX_SCAN:
            raw_cursor = state.get("after")
            db_cursor = SessionListCursor(*raw_cursor) if raw_cursor else None
            page = await self.sessions.run_async(
                self.sessions.list_project_page,
                grouping,
                project,
                limit=min(50, _MAX_SCAN - scanned),
                cursor=db_cursor,
                filters=_FILTERS,
            )
            if not page.sessions:
                return self._listed(items, project, None)
            for summary in page.sessions:
                scanned += 1
                address = SessionAddress(summary["project_id"], summary["agent_id"], summary["id"])
                state["after"] = [
                    summary["last_active_at"],
                    address.project_id,
                    address.agent_id,
                    address.session_id,
                ]
                if address not in active_set:
                    row = await self._list_item(context, address)
                    if row is not None:
                        items.append(row)
                if len(items) == limit:
                    return self._listed(items, project, state)
            if page.next_cursor is None:
                return self._listed(items, project, None)
        return self._listed(items, project, state)

    async def _list_item(self, context: ToolContext, address: SessionAddress) -> JsonObject | None:
        try:
            peer = await self.sessions.run_async(self._peer, context, address, None, False)
            page = await self._page(peer, limit=1, max_text_chars=300, include_peer_messages=False)
            result = self._identity(peer)
            result["cursor"] = self._cursor(peer, page)
            if page.messages:
                result["progress"] = page.messages[-1].text
                result["progress_role"] = page.messages[-1].role
                if page.messages[-1].truncated:
                    result["progress_truncated"] = True
            return result
        except SessionPeerError, SessionNotFoundError:
            return None

    def _listed(
        self, items: builtins.list[JsonObject], project: str, state: JsonObject | None
    ) -> JsonObject:
        result: JsonObject = {"project": project, "items": items}
        if state is not None:
            result["next_cursor"] = _encode("sessions", state)
        if not items:
            result["content"] = (
                "No other accessible Sessions were found on this page. "
                "Pass next_cursor to continue."
                if state is not None
                else "No other accessible Sessions were found in this Working Project."
            )
        return result

    def _list_cursor(self, cursor: str | None, project: str) -> JsonObject:
        if cursor is None:
            return {"project": project, "phase": "active"}
        value = _decode(cursor, "sessions")
        if (
            not isinstance(value, dict)
            or value.get("project") != project
            or value.get("phase") not in {"active", "idle"}
        ):
            raise _bad_cursor()
        after = value.get("after")
        size = 3 if value["phase"] == "active" else 4
        if after is not None and (
            not isinstance(after, list)
            or len(after) != size
            or not all(
                isinstance(part, str) or value["phase"] == "idle" and index == 1 and part is None
                for index, part in enumerate(after)
            )
        ):
            raise _bad_cursor()
        return value

    async def read(
        self, context: ToolContext, id: str, *, cursor: str | None = None, limit: int = 8
    ) -> JsonObject:
        peer = await self.resolve(context, id)
        before = None
        if cursor is not None:
            continuation = _decode(cursor, "older")
            if (
                not isinstance(continuation, dict)
                or continuation.get("id") != id
                or not _integer(continuation.get("before"))
            ):
                raise _bad_cursor()
            before = continuation["before"]
        peer = await self.sessions.run_async(
            self._peer, context, peer.address, peer.generation_id, False
        )
        page = await self._page(peer, limit=limit, before_sequence=before)
        result = self._read_result(peer, page)
        if page.has_more and page.messages:
            result["next_cursor"] = _encode(
                "older", {"id": id, "before": page.messages[0].sequence}
            )
        return result

    def _identity(self, peer: SessionPeer) -> JsonObject:
        state = self.activity.state(peer.address).state
        if (
            self.runs.active_run(
                agent_id=peer.address.agent_id,
                session_id=peer.address.session_id,
                project_id=peer.address.project_id,
            )
            is not None
        ):
            state = "working"
        return {
            "id": peer.id,
            "agent_id": peer.address.agent_id,
            "title": peer.title,
            "activity": state,
        }

    async def _page(self, peer: SessionPeer, **kwargs: Any) -> SessionConversationPage:
        try:
            return await self.sessions.run_async(
                self.sessions.conversation_page,
                peer.address,
                expected_generation_id=peer.generation_id,
                **kwargs,
            )
        except SessionNotFoundError as error:
            raise _unavailable() from error

    def _cursor(self, peer: SessionPeer, page: SessionConversationPage) -> str:
        return _encode(
            "follow",
            {
                "id": peer.id,
                "next": page.next_sequence,
                "response": page.latest_response_sequence,
                "completion": page.latest_completion_sequence,
            },
        )

    def _read_result(self, peer: SessionPeer, page: SessionConversationPage) -> JsonObject:
        result = self._identity(peer)
        result["cursor"] = self._cursor(peer, page)
        if page.latest_user is not None:
            result["task"] = _message_data(page.latest_user)
        result["messages"] = [_message_data(message) for message in page.messages]
        if page.latest_completion_status is not None:
            result["last_run_status"] = page.latest_completion_status
        return result

    async def wait(
        self,
        context: ToolContext,
        id: str,
        *,
        cursor: str | None = None,
        timeout: int = 60,
    ) -> JsonObject:
        peer = await self.resolve(context, id)
        caller = _caller(context)
        changed = asyncio.Event()
        caller_input = asyncio.Event()
        cancelled = False
        subscriptions: builtins.list[Callable[[], None]] = []
        watchers: dict[str, asyncio.Task[None]] = {}

        def cancel() -> None:
            nonlocal cancelled
            cancelled = True
            changed.set()

        def input_queued(address: SessionAddress) -> None:
            if address == caller:
                caller_input.set()
                changed.set()

        async def observe(run: Run, after_sequence: int) -> None:
            async with aclosing(run.subscribe(after_sequence=after_sequence)) as events:
                async for event in events:
                    if event.type in {ASSISTANT_OUTPUT_EVENT, USER_MESSAGE_EVENT}:
                        changed.set()

        def started(run: Run) -> None:
            if _run_address(run) == peer.address and run.id not in watchers:
                watchers[run.id] = asyncio.create_task(observe(run, run.last_sequence))

        def finished(run: Run) -> None:
            if _run_address(run) == peer.address:
                changed.set()

        subscriptions.append(self.runs.add_run_started_callback(started))
        subscriptions.append(self.runs.add_run_finished_callback(finished))
        subscriptions.append(self.runs.add_input_queued_callback(input_queued))
        subscriptions.append(
            self.activity.add_changed_callback(
                lambda address, _state, _finished: (
                    changed.set() if address == peer.address else None
                )
            )
        )
        context.on_cancel(cancel)
        for run in self.runs.active_runs():
            started(run)
        try:
            initial = await self._page(peer, limit=1)
            baseline = self._follow_cursor(cursor, peer, initial)
            deadline = asyncio.get_running_loop().time() + timeout
            while True:
                changed.clear()
                if cancelled or context.is_cancelled() or context.was_cancelled_by_user():
                    raise asyncio.CancelledError
                if caller_input.is_set() or self.runs.list_queued(
                    caller.agent_id, caller.session_id, project_id=caller.project_id
                ):
                    return {
                        "status": "input_received",
                        "cursor": _encode("follow", baseline),
                        "content": "A message reached your Session. Continue to receive it.",
                    }
                # Reauthorization and generation checks also cover archive/move
                # while a wait was suspended.
                peer = await self.resolve(context, id)
                page = await self._page(
                    peer, limit=20, after_sequence=baseline["next"], include_peer_messages=False
                )
                if page.cursor_floor_sequence > baseline["next"]:
                    raise SessionPeerError(
                        "history_changed",
                        "The Session history changed. "
                        "Read the Session again and use its new cursor.",
                    )
                if (page.latest_response_sequence, page.latest_completion_sequence) != (
                    baseline["response"],
                    baseline["completion"],
                ):
                    result = self._identity(peer)
                    result["status"] = "changed"
                    result["cursor"] = self._cursor(peer, page)
                    result["messages"] = [_message_data(message) for message in page.messages]
                    completion = page.latest_completion_sequence
                    if page.has_more:
                        next_sequence = page.messages[-1].sequence + 1
                        if completion is not None and completion >= next_sequence:
                            completion = baseline["completion"]
                        result["cursor"] = _encode(
                            "follow",
                            {
                                "id": peer.id,
                                "next": next_sequence,
                                "response": page.messages[-1].sequence,
                                "completion": completion,
                            },
                        )
                        result["has_more"] = True
                        result["content"] = (
                            "More new messages are available. "
                            "Use wait with this cursor to continue."
                        )
                    if completion != baseline["completion"]:
                        result["run_status"] = page.latest_completion_status
                    return result
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    return {
                        "status": "unchanged",
                        "cursor": self._cursor(peer, page),
                        "content": "No new message or completion arrived before the wait ended.",
                    }
                try:
                    await asyncio.wait_for(changed.wait(), timeout=remaining)
                except TimeoutError:
                    # Recheck the database once at the deadline so a committed
                    # result whose publication raced timeout is still returned.
                    continue
        finally:
            for remove in subscriptions:
                remove()
            for task in watchers.values():
                task.cancel()
            if watchers:
                await asyncio.gather(*watchers.values(), return_exceptions=True)

    def _follow_cursor(
        self, cursor: str | None, peer: SessionPeer, page: SessionConversationPage
    ) -> JsonObject:
        value = (
            _decode(cursor, "follow")
            if cursor is not None
            else _decode(self._cursor(peer, page), "follow")
        )
        if (
            not isinstance(value, dict)
            or value.get("id") != peer.id
            or not _integer(value.get("next"))
            or value["next"] > page.next_sequence
            or not {"response", "completion"}.issubset(value)
            or any(
                value[key] is not None and (not _integer(value[key]) or value[key] >= value["next"])
                for key in ("response", "completion")
            )
        ):
            raise _bad_cursor()
        return value


def _caller(context: ToolContext) -> SessionAddress:
    return SessionAddress(context.project_id, context.agent_id, context.session_id)


def _run_address(run: Run) -> SessionAddress:
    return SessionAddress(run.project_id, run.agent_id, run.session_id)


def _address_key(address: SessionAddress) -> tuple[str, str, str]:
    return address.project_id or "", address.agent_id, address.session_id


def _message_data(message: SessionConversationMessage) -> JsonObject:
    result: JsonObject = {
        "role": message.role,
        "text": message.text,
        "timestamp": message.timestamp,
    }
    if message.sender_kind == "session":
        result["sender_kind"] = "session"
        result["source_session"] = message.sender_id
    if message.truncated:
        result["truncated"] = True
    return result


def _encode(prefix: str, value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return prefix + ":" + base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode(value: str, prefix: str) -> Any:
    if not isinstance(value, str) or len(value) > 8192 or not value.startswith(prefix + ":"):
        raise _bad_cursor() if prefix != "session" else _unavailable()
    encoded = value[len(prefix) + 1 :]
    try:
        return json.loads(
            base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True)
        )
    except (ValueError, UnicodeError, binascii.Error) as error:
        raise (_bad_cursor() if prefix != "session" else _unavailable()) from error


def _integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _bad_cursor() -> SessionPeerError:
    return SessionPeerError(
        "invalid_cursor",
        "This cursor does not match this operation or Session. "
        "Use the cursor returned by list or read for that operation.",
    )


def _unavailable() -> SessionPeerError:
    return SessionPeerError(
        "session_unavailable",
        "This Session is no longer reachable in your Working Project. "
        "Use list to find an available Session.",
    )
