"""Tests for project-aware addressing on the session RPC handlers.

Coverage (AAA):
- ``session.create`` with a bare agent id creates an identity session and may set
  ``current_session_id`` (byte-identical to before),
- ``session.create`` with ``agent@projekt`` validates through the resolver, creates
  the session under the project anchor, and does NOT touch identity
  ``current_session_id``,
- ``session.create`` with a malformed address is ``invalid_request``,
- ``session.list`` threads the parsed ``project_id`` into the session listing.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core.agents.agents import AgentStore
from core.chat import ChatSessionError
from core.projects.resolver import AgentResolver
from core.projects.store import ProjectStore
from core.runs import ChatRunManager, RunAdmissionBlockedError
from core.sessions import FORK_SOURCE_META_KEY, SessionAddress
from core.tools.terminal_manager import TerminalOwner
from server.events import ServerEventBus
from server.rpc.agent_methods import (
    SESSION_FORK_ALWAYS_STRIP_META_KEYS,
    SESSION_FORK_CROSS_AGENT_STRIP_META_KEYS,
    _create_session,
    _delete_session,
    _fork_session,
    _get_agent,
    _list_session_activity,
    _list_sessions,
    _mark_session_read,
    _rename_session,
    _set_session_compaction_policy,
)
from server.rpc.errors import RpcError


class _FakeResolver:
    def __init__(self) -> None:
        self.resolved: list[tuple[str | None, str]] = []

    def resolve_agent(self, project_id: str | None, agent_id: str) -> Any:
        self.resolved.append((project_id, agent_id))
        return SimpleNamespace(id=agent_id)


class _FakeSessions:
    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []
        self.listed: list[tuple[str, str | None]] = []
        self.renamed: list[tuple[str, str, str, str | None]] = []
        self.marked_read: list[tuple[str, str, str, str | None]] = []
        self.mark_read_result: dict[str, Any] = {
            "latest_completion_run_id": None,
            "has_unread_completion": False,
            "unread_run_id": None,
            "unread_run_status": None,
            "unread_run_at": None,
            "marked_read": True,
        }
        self.archived: list[tuple[str, str, str | None]] = []
        self.got: list[tuple[str, str, str | None]] = []
        self.forked: list[dict[str, Any]] = []
        # Rows returned by list_with_metadata; default keeps the existing
        # listing tests byte-identical. Delete tests override it.
        self.metadata_rows: list[dict[str, Any]] = [{"id": "s1"}]
        self.activity_rows: list[dict[str, Any]] = [
            {
                "id": "s1",
                "latest_completion_run_id": "run-one",
                "has_unread_completion": True,
                "unread_run_id": "run-one",
                "unread_run_status": "completed",
                "unread_run_at": "2026-07-20T10:00:00+00:00",
            }
        ]
        self.activity_error: Exception | None = None
        # Sidecar the source carries; ``fork`` strips the requested keys off it so
        # fork tests can assert what the fork retains. Fork tests override it.
        self.source_metadata: dict[str, Any] = {}
        # Session ids that ``get``/``fork`` should treat as nonexistent.
        self.missing: set[str] = set()
        # Fork metadata keyed by (agent_id, session_id, project_id) for get_metadata.
        self._fork_metadata: dict[tuple[str, str, str | None], dict[str, Any]] = {}
        self.saved_metadata: dict[tuple[str, str, str | None], dict[str, Any]] = {}
        self.archive_started: asyncio.Event | None = None
        self.archive_release: asyncio.Event | None = None

    def create(self, agent_id: str, *, session_id: Any = None, project_id: Any = None) -> Any:
        self.created.append(
            {"agent_id": agent_id, "session_id": session_id, "project_id": project_id}
        )
        return SimpleNamespace(id="new-session")

    def get(self, address: Any) -> Any:
        self.got.append((address.agent_id, address.session_id, address.project_id))
        if address.session_id in self.missing:
            raise ChatSessionError(f"session does not exist: {address.session_id}")
        return SimpleNamespace(id=address.session_id)

    async def archive(self, address: Any) -> Any:
        self.archived.append((address.agent_id, address.session_id, address.project_id))
        if self.archive_started is not None:
            self.archive_started.set()
        if self.archive_release is not None:
            await self.archive_release.wait()
        return SimpleNamespace(id=address.session_id)

    def list_with_metadata(self, agent_id: str, project_id: str | None = None) -> list[Any]:
        self.listed.append((agent_id, project_id))
        return self.metadata_rows

    def list_completion_activity(self, agent_id: str, project_id: str | None = None) -> list[Any]:
        self.listed.append((agent_id, project_id))
        if self.activity_error is not None:
            raise self.activity_error
        return self.activity_rows

    def mark_terminal_run_read(self, address: Any, run_id: str) -> dict[str, Any]:
        self.marked_read.append((address.agent_id, address.session_id, run_id, address.project_id))
        return dict(self.mark_read_result)

    async def fork(
        self,
        source: Any,
        *,
        target_agent_id: str | None = None,
        target_project_id: str | None = None,
        strip_meta_keys: Any = frozenset(),
    ) -> Any:
        source_agent_id = source.agent_id
        session_id = source.session_id
        source_project_id = source.project_id
        self.forked.append(
            {
                "source_agent_id": source_agent_id,
                "session_id": session_id,
                "target_agent_id": target_agent_id,
                "source_project_id": source_project_id,
                "target_project_id": target_project_id,
                "strip_meta_keys": frozenset(strip_meta_keys),
            }
        )
        if session_id in self.missing:
            raise ChatSessionError(f"session does not exist: {session_id}")
        retained = {
            key: value for key, value in self.source_metadata.items() if key not in strip_meta_keys
        }
        retained[FORK_SOURCE_META_KEY] = {
            "agent_id": source_agent_id,
            "session_id": session_id,
            "project_id": source_project_id,
            "forked_at": "2026-07-04T00:00:00+00:00",
            "message_count": 2,
        }
        destination_agent_id = target_agent_id or source_agent_id
        self._fork_metadata[(destination_agent_id, "fork-1", target_project_id)] = retained
        return SimpleNamespace(id="fork-1")

    def get_metadata(self, address: Any) -> dict[str, Any]:
        key = (address.agent_id, address.session_id, address.project_id)
        return self.saved_metadata.get(key, self._fork_metadata.get(key, {}))

    def set_metadata(self, address: Any, metadata: dict[str, Any]) -> None:
        self.saved_metadata[(address.agent_id, address.session_id, address.project_id)] = dict(
            metadata
        )

    def set_title(self, address: Any, title: str) -> str | None:
        self.renamed.append((address.agent_id, address.session_id, title, address.project_id))
        # Mirror the real primitive's blank→None clear so the handler response is realistic.
        normalized = " ".join(title.split())
        return normalized or None


class _FakeTerminalManager:
    def __init__(self) -> None:
        self.closed_scopes: list[Any] = []
        self.closed_agents: list[tuple[str, str | None]] = []

    async def close_scope(self, owner: Any) -> None:
        self.closed_scopes.append(owner)

    async def close_agent_scope(self, agent_id: str, project_id: str | None) -> None:
        self.closed_agents.append((agent_id, project_id))


def _make_state() -> tuple[SimpleNamespace, _FakeResolver, _FakeSessions]:
    resolver = _FakeResolver()
    sessions = _FakeSessions()
    updates: list[dict[str, Any]] = []
    resets: list[tuple[str, str]] = []
    recall_removals: list[tuple[str, str, str | None]] = []
    # The identity current-session pointer agents.get reports; defaults to a
    # session other than the one tests delete, so a delete is not "the current"
    # unless a test opts in by setting it.
    agent_current = {"current_session_id": "other"}

    def _reset_current(agent_id: str, session_id: str) -> Any:
        resets.append((agent_id, session_id))
        return SimpleNamespace(current_session_id="landing")

    async def _remove_session_from_recall(
        agent_id: str, session_id: str, project_id: str | None = None
    ) -> None:
        recall_removals.append((agent_id, session_id, project_id))

    runtime = SimpleNamespace(
        agent_resolver=resolver,
        chat_sessions=sessions,
        terminal_manager=_FakeTerminalManager(),
        agents=SimpleNamespace(
            update=lambda agent_id, **k: updates.append({agent_id: k}),
            reset_current_after_session_removed=_reset_current,
            get=lambda agent_id: SimpleNamespace(
                current_session_id=agent_current["current_session_id"]
            ),
        ),
        remove_session_from_recall=_remove_session_from_recall,
        storage=SimpleNamespace(
            load_compaction_settings=lambda: {
                "enabled": True,
                "trigger": {"type": "context_ratio", "threshold": 0.8},
                "strategy": {
                    "type": "summary_tail",
                    "tail_tokens": 15_000,
                    "summary_model": None,
                },
            }
        ),
    )
    state = SimpleNamespace(
        runtime=runtime,
        event_bus=ServerEventBus(),
        # _state_chat_runs reads state.chat_runs directly (not under runtime).
        chat_runs=ChatRunManager(),
    )
    state._updates = updates  # type: ignore[attr-defined]
    state._resets = resets  # type: ignore[attr-defined]
    state._recall_removals = recall_removals  # type: ignore[attr-defined]
    state._agent_current = agent_current  # type: ignore[attr-defined]
    return state, resolver, sessions


def _sessions_resource_events(state: SimpleNamespace) -> list[dict[str, Any]]:
    return [
        event["payload"] for event in state.event_bus.events if event["type"] == "resource_changed"
    ]


@pytest.mark.asyncio
async def test_create_bare_agent_creates_identity_session() -> None:
    state, resolver, sessions = _make_state()

    result = await _create_session(state, {"agent_id": "builder", "make_current": True})

    assert result == {"agent_id": "builder", "session_id": "new-session"}
    assert resolver.resolved == [(None, "builder")]
    assert sessions.created[0]["project_id"] is None
    # Identity make-current writes the agent's current_session_id.
    assert state._updates == [{"builder": {"current_session_id": "new-session"}}]


@pytest.mark.asyncio
async def test_create_qualified_agent_creates_project_session() -> None:
    state, resolver, sessions = _make_state()

    result = await _create_session(state, {"agent_id": "builder@vbot", "make_current": True})

    assert result == {"agent_id": "builder", "session_id": "new-session"}
    assert resolver.resolved == [("vbot", "builder")]
    assert sessions.created[0]["project_id"] == "vbot"
    # A project config agent has no identity current-session pointer to write.
    assert state._updates == []


@pytest.mark.asyncio
async def test_create_invalid_address_is_invalid_request() -> None:
    state, _resolver, sessions = _make_state()

    with pytest.raises(RpcError) as exc_info:
        await _create_session(state, {"agent_id": "builder@bad project"})

    assert exc_info.value.code == "invalid_request"
    assert sessions.created == []


@pytest.mark.asyncio
async def test_list_qualified_agent_scopes_to_project() -> None:
    state, _resolver, sessions = _make_state()

    result = await _list_sessions(state, {"agent_id": "builder@vbot"})

    assert result["sessions"][0]["id"] == "s1"
    assert result["sessions"][0]["compaction_policy_override"] is None
    assert result["sessions"][0]["compaction_policy_effective"]["enabled"] is True
    assert sessions.listed == [("builder", "vbot")]


@pytest.mark.asyncio
async def test_list_bare_agent_is_identity() -> None:
    state, _resolver, sessions = _make_state()

    await _list_sessions(state, {"agent_id": "builder"})

    assert sessions.listed == [("builder", None)]


@pytest.mark.asyncio
async def test_activity_list_batches_identity_and_project_addresses_in_order() -> None:
    state, resolver, sessions = _make_state()

    result = await _list_session_activity(
        state,
        {
            "agent_ids": [
                "builder",
                "reviewer@vbot",
                "builder",
            ]
        },
    )

    assert result == {
        "agents": [
            {
                "agent_id": "builder",
                "project_id": None,
                "sessions": sessions.activity_rows,
            },
            {
                "agent_id": "reviewer",
                "project_id": "vbot",
                "sessions": sessions.activity_rows,
            },
        ]
    }
    assert resolver.resolved == [(None, "builder"), ("vbot", "reviewer")]
    assert sessions.listed == [("builder", None), ("reviewer", "vbot")]


@pytest.mark.asyncio
async def test_activity_list_accepts_an_empty_address_batch() -> None:
    state, resolver, sessions = _make_state()

    result = await _list_session_activity(state, {"agent_ids": []})

    assert result == {"agents": []}
    assert resolver.resolved == []
    assert sessions.listed == []


@pytest.mark.asyncio
async def test_activity_list_rejects_a_malformed_address_before_storage() -> None:
    state, resolver, sessions = _make_state()

    with pytest.raises(RpcError) as exc_info:
        await _list_session_activity(
            state,
            {"agent_ids": ["builder", "reviewer@bad project"]},
        )

    assert exc_info.value.code == "invalid_request"
    assert resolver.resolved == []
    assert sessions.listed == []


@pytest.mark.asyncio
async def test_activity_list_maps_session_storage_failures() -> None:
    state, _resolver, sessions = _make_state()
    sessions.activity_error = ChatSessionError("activity sidecar unavailable")

    with pytest.raises(RpcError) as exc_info:
        await _list_session_activity(state, {"agent_ids": ["builder"]})

    assert exc_info.value.code == "domain_error"
    assert sessions.listed == [("builder", None)]


@pytest.mark.asyncio
async def test_mark_session_read_acknowledges_exact_project_run() -> None:
    state, resolver, sessions = _make_state()

    result = await _mark_session_read(
        state,
        {"agent_id": "builder@vbot", "session_id": "s1", "run_id": "run-one"},
    )

    assert resolver.resolved == [("vbot", "builder")]
    assert sessions.marked_read == [("builder", "s1", "run-one", "vbot")]
    assert result["agent_id"] == "builder@vbot"
    assert result["marked_read"] is True
    assert _sessions_resource_events(state) == []


@pytest.mark.asyncio
async def test_mark_session_read_stale_ack_does_not_invalidate_sessions() -> None:
    state, _resolver, sessions = _make_state()
    sessions.mark_read_result["marked_read"] = False
    sessions.mark_read_result["has_unread_completion"] = True
    sessions.mark_read_result["latest_completion_run_id"] = "run-newer"
    sessions.mark_read_result["unread_run_id"] = "run-newer"

    result = await _mark_session_read(
        state,
        {"agent_id": "builder", "session_id": "s1", "run_id": "run-old"},
    )

    assert result["unread_run_id"] == "run-newer"
    assert _sessions_resource_events(state) == []


@pytest.mark.asyncio
async def test_session_compaction_policy_override_and_clear() -> None:
    state, _resolver, sessions = _make_state()
    policy = {
        "enabled": True,
        "trigger": {"type": "input_tokens", "tokens": 100_000},
        "strategy": {"type": "continuation"},
    }

    set_result = await _set_session_compaction_policy(
        state,
        {"agent_id": "builder", "session_id": "s1", "policy": policy},
    )
    clear_result = await _set_session_compaction_policy(
        state,
        {"agent_id": "builder", "session_id": "s1", "policy": None},
    )

    assert set_result["override"] == policy
    assert set_result["source"] == "session"
    assert clear_result["override"] is None
    assert clear_result["source"] == "agent_or_global"
    assert sessions.saved_metadata[("builder", "s1", None)] == {}


@pytest.mark.asyncio
async def test_session_compaction_policy_rejects_invalid_shape() -> None:
    state, _resolver, sessions = _make_state()

    with pytest.raises(RpcError) as exc_info:
        await _set_session_compaction_policy(
            state,
            {
                "agent_id": "builder",
                "session_id": "s1",
                "policy": {"enabled": True, "trigger": {"type": "unknown"}},
            },
        )

    assert exc_info.value.code == "invalid_request"
    assert sessions.saved_metadata == {}


@pytest.mark.asyncio
async def test_create_session_publishes_sessions_resource_changed() -> None:
    state, _resolver, _sessions = _make_state()

    await _create_session(state, {"agent_id": "builder", "make_current": True})

    # The single sessions emit point: other windows refresh this agent's session
    # list/marking. Scoped to the agent so windows on a different agent ignore it.
    assert _sessions_resource_events(state) == [
        {"kind": "sessions", "scope": {"agent_id": "builder"}}
    ]


@pytest.mark.asyncio
async def test_create_session_scope_uses_bare_agent_id_for_project_address() -> None:
    state, _resolver, _sessions = _make_state()

    await _create_session(state, {"agent_id": "builder@vbot"})

    # The scope carries the bare agent id (the project rides separately), matching
    # how the queue/session channels are keyed on the client.
    assert _sessions_resource_events(state) == [
        {"kind": "sessions", "scope": {"agent_id": "builder"}}
    ]


@pytest.mark.asyncio
async def test_rename_bare_agent_sets_title() -> None:
    state, _resolver, sessions = _make_state()

    result = await _rename_session(
        state, {"agent_id": "builder", "session_id": "s1", "title": "Release planning"}
    )

    assert result == {"agent_id": "builder", "session_id": "s1", "title": "Release planning"}
    assert sessions.renamed == [("builder", "s1", "Release planning", None)]


@pytest.mark.asyncio
async def test_rename_qualified_agent_scopes_to_project() -> None:
    state, _resolver, sessions = _make_state()

    result = await _rename_session(
        state, {"agent_id": "builder@vbot", "session_id": "s1", "title": "Release planning"}
    )

    assert result["agent_id"] == "builder"
    assert sessions.renamed == [("builder", "s1", "Release planning", "vbot")]


@pytest.mark.asyncio
async def test_rename_without_title_clears() -> None:
    state, _resolver, sessions = _make_state()

    # An absent title field is the clear signal: the handler passes through "".
    result = await _rename_session(state, {"agent_id": "builder", "session_id": "s1"})

    assert result == {"agent_id": "builder", "session_id": "s1", "title": None}
    assert sessions.renamed == [("builder", "s1", "", None)]


@pytest.mark.asyncio
async def test_rename_publishes_sessions_resource_changed() -> None:
    state, _resolver, _sessions = _make_state()

    await _rename_session(state, {"agent_id": "builder", "session_id": "s1", "title": "Hi"})

    assert _sessions_resource_events(state) == [
        {"kind": "sessions", "scope": {"agent_id": "builder"}}
    ]


@pytest.mark.asyncio
async def test_rename_rejects_unsupported_field() -> None:
    state, _resolver, sessions = _make_state()

    with pytest.raises(RpcError) as exc_info:
        await _rename_session(
            state, {"agent_id": "builder", "session_id": "s1", "title": "Hi", "bogus": 1}
        )

    assert exc_info.value.code == "invalid_request"
    assert sessions.renamed == []


@pytest.mark.asyncio
async def test_rename_rejects_non_string_title() -> None:
    state, _resolver, sessions = _make_state()

    with pytest.raises(RpcError) as exc_info:
        await _rename_session(
            state,
            {"agent_id": "builder", "session_id": "s1", "title": 42},
        )

    assert exc_info.value.code == "invalid_request"
    assert sessions.renamed == []


@pytest.mark.asyncio
async def test_delete_bare_agent_archives_and_lands_on_reaimed_current() -> None:
    state, resolver, sessions = _make_state()

    result = await _delete_session(state, {"agent_id": "builder", "session_id": "s1"})

    assert result == {"agent_id": "builder", "session_id": "s1", "next_session_id": "landing"}
    assert resolver.resolved == [(None, "builder")]
    # Archived (not hard-deleted) under the identity scope.
    assert sessions.archived == [("builder", "s1", None)]
    assert state.runtime.terminal_manager.closed_scopes == [TerminalOwner(None, "builder", "s1")]
    # Identity pointer re-aimed through the shared seam; the landing is its result.
    assert state._resets == [("builder", "s1")]
    # Dropped from the recall index immediately (#6).
    assert state._recall_removals == [("builder", "s1", None)]


@pytest.mark.asyncio
async def test_delete_qualified_agent_lands_on_most_recent_remaining() -> None:
    state, _resolver, sessions = _make_state()
    sessions.metadata_rows = [
        {"id": "old", "last_active_at": "2026-01-01T00:00:00+00:00"},
        {"id": "recent", "last_active_at": "2026-06-01T00:00:00+00:00"},
    ]

    result = await _delete_session(state, {"agent_id": "builder@vbot", "session_id": "s1"})

    assert result["next_session_id"] == "recent"
    assert sessions.archived == [("builder", "s1", "vbot")]
    # A project config agent has no identity current pointer to re-aim.
    assert state._resets == []
    assert state._recall_removals == [("builder", "s1", "vbot")]


@pytest.mark.asyncio
async def test_delete_project_session_creates_fresh_when_none_remain() -> None:
    state, _resolver, sessions = _make_state()
    sessions.metadata_rows = []

    result = await _delete_session(state, {"agent_id": "builder@vbot", "session_id": "s1"})

    assert result["next_session_id"] == "new-session"
    assert sessions.created[0]["project_id"] == "vbot"


@pytest.mark.asyncio
async def test_delete_busy_session_is_rejected() -> None:
    state, _resolver, sessions = _make_state()

    async with state.chat_runs.session_admission_guard(
        SessionAddress(project_id=None, agent_id="builder", session_id="s1")
    ):
        with pytest.raises(RpcError) as exc_info:
            await _delete_session(state, {"agent_id": "builder", "session_id": "s1"})

    assert exc_info.value.code == "session_busy"
    # The guard fires before any file work — nothing archived, nothing re-aimed.
    assert sessions.archived == []
    assert state._resets == []


@pytest.mark.asyncio
async def test_delete_session_referenced_by_bootstrap_is_rejected() -> None:
    state, _resolver, sessions = _make_state()
    state.runtime.bootstrap_service = SimpleNamespace(
        list_jobs=lambda: [
            SimpleNamespace(
                id="boot-1",
                agent_id="builder",
                project_id=None,
                session_id="s1",
                status="active",
            )
        ]
    )

    with pytest.raises(RpcError) as exc_info:
        await _delete_session(state, {"agent_id": "builder", "session_id": "s1"})

    assert exc_info.value.code == "session_busy"
    assert "bootstrap:boot-1" in exc_info.value.message
    assert sessions.archived == []


@pytest.mark.asyncio
async def test_delete_guard_rejects_run_while_archive_is_waiting() -> None:
    state, _resolver, sessions = _make_state()
    sessions.archive_started = asyncio.Event()
    sessions.archive_release = asyncio.Event()

    delete_task = asyncio.create_task(
        _delete_session(state, {"agent_id": "builder", "session_id": "s1"})
    )
    await asyncio.wait_for(sessions.archive_started.wait(), timeout=1)

    with pytest.raises(RunAdmissionBlockedError):
        await state.chat_runs.start(
            SessionAddress(project_id=None, agent_id="builder", session_id="s1"),
            lambda _run: asyncio.sleep(0),
        )
    sessions.archive_release.set()
    result = await asyncio.wait_for(delete_task, timeout=1)
    assert result["session_id"] == "s1"


@pytest.mark.asyncio
async def test_delete_missing_session_is_domain_error() -> None:
    state, _resolver, sessions = _make_state()
    sessions.missing = {"gone"}

    with pytest.raises(RpcError) as exc_info:
        await _delete_session(state, {"agent_id": "builder", "session_id": "gone"})

    assert exc_info.value.code == "domain_error"
    assert sessions.archived == []


@pytest.mark.asyncio
async def test_delete_publishes_sessions_resource_changed() -> None:
    state, _resolver, _sessions = _make_state()

    await _delete_session(state, {"agent_id": "builder", "session_id": "s1"})

    assert _sessions_resource_events(state) == [
        {"kind": "sessions", "scope": {"agent_id": "builder"}}
    ]


@pytest.mark.asyncio
async def test_delete_current_identity_session_refreshes_agents() -> None:
    state, _resolver, _sessions = _make_state()
    # The deleted session is the identity agent's current one.
    state._agent_current["current_session_id"] = "s1"

    await _delete_session(state, {"agent_id": "builder", "session_id": "s1"})

    # Re-aiming the current pointer is an agent-config change, so both the session
    # list and agent state refresh; a non-current delete emits only sessions.
    events = _sessions_resource_events(state)
    assert [event["kind"] for event in events] == ["sessions", "agents"]


@pytest.mark.asyncio
async def test_delete_rejects_unsupported_field() -> None:
    state, _resolver, sessions = _make_state()

    with pytest.raises(RpcError) as exc_info:
        await _delete_session(state, {"agent_id": "builder", "session_id": "s1", "bogus": 1})

    assert exc_info.value.code == "invalid_request"
    assert sessions.archived == []


@pytest.mark.asyncio
async def test_fork_same_agent_returns_new_id_with_provenance() -> None:
    state, resolver, sessions = _make_state()
    sessions.source_metadata = {"title": "Keep"}

    result = await _fork_session(state, {"agent_id": "builder", "session_id": "s1"})

    assert result["session"]["id"] == "fork-1"
    assert result["session"]["agent_id"] == "builder"
    assert result["session"]["fork_source"]["session_id"] == "s1"
    # Same agent, so only the always-strip policy applies (catalog keys kept).
    assert sessions.forked[0]["strip_meta_keys"] == SESSION_FORK_ALWAYS_STRIP_META_KEYS
    assert resolver.resolved == [(None, "builder")]


@pytest.mark.asyncio
async def test_fork_strips_channel_and_subagent_bindings_but_keeps_title() -> None:
    state, _resolver, sessions = _make_state()
    sessions.source_metadata = {
        "title": "Keep",
        "source_channel_id": "chan",
        "platform": "telegram",
        "is_subagent_session": True,
    }

    result = await _fork_session(state, {"agent_id": "builder", "session_id": "s1"})

    metadata = sessions.get_metadata(
        SessionAddress(project_id=None, agent_id="builder", session_id=result["session"]["id"])
    )
    assert metadata["title"] == "Keep"
    assert "source_channel_id" not in metadata
    assert "platform" not in metadata
    assert "is_subagent_session" not in metadata


@pytest.mark.asyncio
async def test_fork_to_other_agent_strips_catalog_and_lands_under_target() -> None:
    state, resolver, sessions = _make_state()

    result = await _fork_session(
        state,
        {"agent_id": "builder", "session_id": "s1", "target_agent_id": "reviewer"},
    )

    assert result["session"]["agent_id"] == "reviewer"
    assert sessions.forked[0]["target_agent_id"] == "reviewer"
    # A cross-agent fork additionally strips the pinned-catalog keys.
    assert (
        sessions.forked[0]["strip_meta_keys"]
        == SESSION_FORK_ALWAYS_STRIP_META_KEYS | SESSION_FORK_CROSS_AGENT_STRIP_META_KEYS
    )
    # Both endpoints are resolved before any file work.
    assert resolver.resolved == [(None, "builder"), (None, "reviewer")]
    # The refresh event is scoped to the target agent.
    assert _sessions_resource_events(state) == [
        {"kind": "sessions", "scope": {"agent_id": "reviewer"}}
    ]


@pytest.mark.asyncio
async def test_fork_unknown_session_is_domain_error() -> None:
    state, _resolver, sessions = _make_state()
    sessions.missing = {"gone"}

    with pytest.raises(RpcError) as exc_info:
        await _fork_session(state, {"agent_id": "builder", "session_id": "gone"})

    assert exc_info.value.code == "domain_error"


@pytest.mark.asyncio
async def test_fork_rejects_unsupported_field() -> None:
    state, _resolver, sessions = _make_state()

    with pytest.raises(RpcError) as exc_info:
        await _fork_session(state, {"agent_id": "builder", "session_id": "s1", "bogus": 1})

    assert exc_info.value.code == "invalid_request"
    assert sessions.forked == []


# ---------------------------------------------------------------------------
# agent.get payload: config (raw own values) + effective (per-field value+source).
# Wired against a real AgentStore + AgentResolver so get_raw / effective_config
# are exercised end-to-end rather than stubbed.
# ---------------------------------------------------------------------------


class _UnrestrictedCatalogModel:
    """Catalog-model stub with no connection allowlist (every connection allowed)."""

    connections: tuple[str, ...] = ()

    def allows_connection(self, connection_id: str) -> bool:
        return True


class _PayloadCheckerModels:
    """Model existence probe the resolver's checker uses (unrestricted marker)."""

    def get(self, provider_id: str, model_id: str) -> _UnrestrictedCatalogModel:
        if (provider_id, model_id) == ("openai", "gpt-5.2"):
            return _UnrestrictedCatalogModel()
        raise KeyError(f"{provider_id}/{model_id}")


class _PayloadRuntimeModels:
    """The runtime model registry the context-window lookup reads.

    It always raises ``KeyError`` so ``_resolve_context_window`` degrades to
    ``None`` — the payload test does not assert the window, and a bare-object
    return would trip ``.context_window``.
    """

    def get(self, provider_id: str, model_id: str) -> object:
        raise KeyError(f"{provider_id}/{model_id}")


def _agent_payload_state(tmp_path: Path, defaults: dict[str, Any]) -> SimpleNamespace:
    """Build a real-store state for the agent.get payload path.

    ``defaults`` is the ``defaults.agent`` map both the store (for baking) and the
    resolver's global tier read, so the baked top-level keys and the effective
    ``global_default`` source agree.
    """
    from core.projects.resolver import ModelConfigurationChecker

    data_dir = tmp_path / "data"
    template_dir = tmp_path / "templates"
    template_dir.mkdir(parents=True)
    for filename in ("SOUL.md", "USER.md", "MEMORY.md"):
        (template_dir / filename).write_text(f"# {filename}\n", encoding="utf-8")

    agents = AgentStore(data_dir, template_dir=template_dir, defaults_provider=lambda: defaults)
    projects = ProjectStore(data_dir)
    checker = ModelConfigurationChecker(
        _PayloadCheckerModels(),
        _PayloadProviders(),
        _PayloadCredentials(),
    )
    resolver = AgentResolver(agents, projects, checker, lambda: defaults)
    runtime = SimpleNamespace(
        agents=agents, agent_resolver=resolver, models=_PayloadRuntimeModels()
    )
    return SimpleNamespace(runtime=runtime)


class _PayloadProviders:
    def get(self, provider_id: str) -> object:
        if provider_id == "openai":
            return SimpleNamespace(connections=[SimpleNamespace(id="api-key")])
        raise KeyError(provider_id)


class _PayloadCredentials:
    def has_credentials(self, provider_id: str, connection_id: str | None = None) -> bool:
        return connection_id == "openai:api-key"

    def is_connection_enabled(self, provider_id: str, connection_id: str | None = None) -> bool:
        return True

    def is_usable(self, provider_id: str, connection_id: str | None = None) -> bool:
        return self.has_credentials(provider_id, connection_id)


def test_agent_get_reports_config_and_effective_for_own_value(tmp_path: Path) -> None:
    state = _agent_payload_state(tmp_path, defaults={})
    state.runtime.agents.create("orchestrator", "Orchestrator", model="openai/gpt-5.2")

    result = _get_agent(state, {"id": "orchestrator"})

    # config = raw own values (pre-default-bake); shape check.
    assert set(result["config"]) == {
        "model",
        "fallback_model",
        "temperature",
        "thinking_effort",
        "compaction_policy",
    }
    assert result["config"]["model"] == "openai/gpt-5.2"
    assert result["config"]["fallback_model"] == ""
    assert result["config"]["temperature"] is None
    # effective = per-field {value, source}; the own model wins as source "agent".
    assert result["effective"]["model"] == {"value": "openai/gpt-5.2", "source": "agent"}
    assert result["effective"]["temperature"] == {"value": None, "source": None}


def test_agent_get_effective_reports_global_default_when_own_empty(tmp_path: Path) -> None:
    # With a global default set, the top-level model is baked while config keeps the
    # raw "", and effective attributes the value to the global_default tier.
    state = _agent_payload_state(tmp_path, defaults={"model": "openai/gpt-5.2"})
    state.runtime.agents.create("orchestrator", "Orchestrator")

    result = _get_agent(state, {"id": "orchestrator"})

    assert result["model"] == "openai/gpt-5.2"  # baked top-level key
    assert result["config"]["model"] == ""  # raw own value
    assert result["effective"]["model"] == {"value": "openai/gpt-5.2", "source": "global_default"}
