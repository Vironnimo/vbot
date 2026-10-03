"""Session RPCs: create, list, get, activity, read marks, rename, Policy, Agent overrides and
Channel links.

Fork and delete live in ``test_session_methods_fork.py`` and
``test_session_methods_delete.py``. Tests on ``stub_session_state`` check what the
RPC hands the Session store; tests on ``make_state`` run against a real store.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from core.agents import AgentNotFoundError
from core.channels import ChannelConfig
from core.chat import ChatMessage
from core.database import write_bootstrap_marker
from core.sessions import FORK_SOURCE_META_KEY, ChatSessionManager, SessionAddress
from core.utils.timestamps import canonical_timestamp
from server.events import ServerEventBus
from server.rpc.errors import RPC_ERROR_DOMAIN
from tests.core.sessions.history_fixtures import settle_run
from tests.server.rpc.session_methods_test_support import FakeSessions, stub_session_state
from tests.server.rpc_test_support import (
    JsonObject,
    StubAdapter,
    call,
    make_state,
    resource_changes,
    rpc_error,
    rpc_result,
)

_POLICY = {
    "enabled": True,
    "trigger": {"type": "input_tokens", "tokens": 100_000},
    "strategy": {"type": "continuation"},
}


def _assert_store_untouched(sessions: FakeSessions) -> None:
    assert sessions.created == []
    assert sessions.renamed == []
    assert sessions.forked == []
    assert sessions.archived == []
    assert sessions.list_page_calls == []
    assert sessions.activity_reads == []
    assert sessions.saved_metadata == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "params", "named"),
    [
        ("session.create", {"agent_id": "builder@bad project"}, "bad project"),
        ("session.get", {"agent_id": "builder", "session_id": "s1", "limit": 1}, "limit"),
        ("session.fork", {"agent_id": "builder", "session_id": "s1", "bogus": 1}, "bogus"),
        ("session.delete", {"agent_id": "builder", "session_id": "s1", "bogus": 1}, "bogus"),
        # An invalid permanent flag archives nothing either.
        (
            "session.delete",
            {"agent_id": "builder", "session_id": "s1", "permanent": "yes"},
            "permanent",
        ),
        (
            "session.rename",
            {"agent_id": "builder", "session_id": "s1", "title": "Hi", "bogus": 1},
            "bogus",
        ),
        ("session.rename", {"agent_id": "builder", "session_id": "s1", "title": 42}, "title"),
        (
            "session.list",
            {
                "agent_id": "builder",
                "required_session": {"agent_id": "reviewer@vbot", "session_id": "s1"},
            },
            "required_session",
        ),
        # The retired ``active_sort`` cursor shape.
        (
            "session.list",
            {
                "agent_id": "builder",
                "cursor": {"active_sort": 2460000.5, "agent_id": "builder", "session_id": "one"},
            },
            "cursor",
        ),
        # Cursor timestamps must be canonical strings: a parseable variant, an
        # unparseable string and a number are all refused.
        *(
            (
                "session.list",
                {
                    "agent_id": "builder",
                    "cursor": {
                        "last_activity_at": timestamp,
                        "agent_id": "builder",
                        "session_id": "one",
                    },
                },
                "last_activity_at",
            )
            for timestamp in ("2026-09-20T10:00:00Z", "yesterday", 2460000.5)
        ),
        (
            "session.activity_list",
            {"agent_ids": ["builder", "reviewer@bad project"]},
            "bad project",
        ),
        (
            "session.set_compaction_policy",
            {
                "agent_id": "builder",
                "session_id": "s1",
                "policy": {"enabled": True, "trigger": {"type": "unknown"}},
            },
            "",
        ),
        ("session.create", {"agent_id": "builder", "agent_overrides": {"speed": 1}}, "speed"),
        # A new Session has nothing to clear.
        ("session.create", {"agent_id": "builder", "agent_overrides": {"model": None}}, "null"),
        (
            "session.create",
            {"agent_id": "builder", "agent_overrides": {"thinking_effort": "extreme"}},
            "thinking_effort",
        ),
        (
            "session.set_agent_overrides",
            {"agent_id": "builder", "session_id": "s1", "agent_overrides": {}},
            "at least one",
        ),
        (
            "session.set_agent_overrides",
            {"agent_id": "builder", "session_id": "s1", "agent_overrides": {"speed": 1}},
            "speed",
        ),
        (
            "session.set_agent_overrides",
            {"agent_id": "builder", "session_id": "s1", "agent_overrides": {"temperature": "hot"}},
            "temperature",
        ),
        (
            "session.set_agent_overrides",
            {
                "agent_id": "builder",
                "session_id": "s1",
                "agent_overrides": {"model": "openai/gpt-mini"},
                "bogus": 1,
            },
            "bogus",
        ),
    ],
)
async def test_malformed_session_requests_are_rejected_before_the_store(
    method: str, params: JsonObject, named: str
) -> None:
    state, resolver, sessions = stub_session_state()

    error = await rpc_error(state, method, **params)

    assert error["code"] == "invalid_request"
    assert named in error["message"]
    assert resolver.resolved == []
    _assert_store_untouched(sessions)
    assert resource_changes(state) == []


# ---------------------------------------------------------------------------
# session.create
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("agent_id", "project_id", "current_updates"),
    [
        pytest.param(
            "builder", None, [{"builder": {"current_session_id": "new-session"}}], id="identity"
        ),
        # A Project Agent has no identity current-Session pointer to write.
        pytest.param("builder@vbot", "vbot", [], id="project"),
    ],
)
async def test_session_create_resolves_the_address_and_publishes_a_scoped_change(
    agent_id: str, project_id: str | None, current_updates: list[JsonObject]
) -> None:
    state, resolver, sessions = stub_session_state()

    result = await rpc_result(state, "session.create", agent_id=agent_id, make_current=True)

    assert result == {"agent_id": "builder", "session_id": "new-session"}
    assert resolver.resolved == [(project_id, "builder")]
    assert sessions.created == [
        {"agent_id": "builder", "session_id": None, "project_id": project_id}
    ]
    assert state._updates == current_updates
    # Scoped to the new Session with the bare Agent id (the Project rides
    # separately), so windows not listing this Agent ignore it.
    assert resource_changes(state) == [
        {
            "kind": "sessions",
            "scope": {"project_id": project_id, "agent_id": "builder", "session_id": "new-session"},
        }
    ]


@pytest.mark.asyncio
async def test_session_create_stores_an_explicit_id_and_makes_it_current(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())

    result = await rpc_result(
        state, "session.create", agent_id="coder", session_id="session-one", make_current=True
    )

    assert result == {"agent_id": "coder", "session_id": "session-one"}
    address = SessionAddress(project_id=None, agent_id="coder", session_id="session-one")
    assert state.runtime.chat_sessions.get(address).id == "session-one"
    assert state.runtime.agents.get("coder").current_session_id == "session-one"


@pytest.mark.asyncio
async def test_session_create_stores_its_agent_overrides(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())
    state.runtime.agent_resolver.models.unusable.add("openai/ghost")
    sessions = state.runtime.chat_sessions
    overrides = {"model": "openai/gpt-mini", "thinking_effort": "high", "temperature": 0.4}

    refused = await rpc_error(
        state,
        "session.create",
        agent_id="coder",
        session_id="ghost",
        agent_overrides={"model": "openai/ghost"},
    )
    result = await rpc_result(
        state,
        "session.create",
        agent_id="coder",
        session_id="session-one",
        agent_overrides=overrides,
    )

    # A Model that cannot run fails before the Session exists.
    assert refused["code"] == "invalid_request"
    assert "openai/ghost" in refused["message"]
    assert not sessions.exists(SessionAddress(None, "coder", "ghost"))
    assert result == {
        "agent_id": "coder",
        "session_id": "session-one",
        "agent_overrides": overrides,
    }
    address = SessionAddress(None, "coder", "session-one")
    assert sessions.metadata_value(address, "agent_overrides") == overrides


# ---------------------------------------------------------------------------
# session.list / session.get / session.activity_list
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("agent_id", "scope"),
    [("builder", ("builder", None)), ("builder@vbot", ("builder", "vbot"))],
    ids=["identity", "project"],
)
async def test_session_list_scopes_one_agent_and_resolves_the_effective_policy(
    agent_id: str, scope: tuple[str, str | None]
) -> None:
    state, _resolver, sessions = stub_session_state()

    result = await rpc_result(state, "session.list", agent_id=agent_id)

    [session] = result["sessions"]
    assert session["id"] == "s1"
    assert session["agent_address"] == agent_id
    assert session["compaction_policy_override"] is None
    assert session["compaction_policy_effective"]["enabled"] is True
    assert sessions.listed == [scope]
    # Without a limit the page is bounded to the default size.
    [page_call] = sessions.list_page_calls
    assert page_call["limit"] == 100


@pytest.mark.asyncio
async def test_session_list_passes_the_bounded_filter_contract_for_an_agent_batch() -> None:
    state, resolver, sessions = stub_session_state()
    sessions.metadata_rows = [
        {
            "id": "s1",
            "created_at": "2026-09-01T10:00:00+00:00",
            "last_active_at": "2026-09-01T10:00:00+00:00",
        }
    ]

    result = await rpc_result(
        state,
        "session.list",
        agent_ids=["builder", "reviewer@vbot"],
        limit=35,
        include_subagents=False,
        include_memory_reflections=False,
        include_skill_reflections=False,
        include_cron=False,
        include_channels=False,
        required_session={"agent_id": "builder", "session_id": "s1"},
    )

    assert [session["agent_address"] for session in result["sessions"]] == [
        "builder",
        "reviewer@vbot",
    ]
    assert all("agent_id" not in session for session in result["sessions"])
    assert all("project_id" not in session for session in result["sessions"])
    assert result["next_cursor"] is None
    assert result["total_count"] == 2
    assert resolver.resolved == [(None, "builder"), ("vbot", "reviewer")]
    [page_call] = sessions.list_page_calls
    assert page_call["scopes"] == [(None, "builder"), ("vbot", "reviewer")]
    assert page_call["limit"] == 35
    assert page_call["filters"].include_subagents is False
    assert page_call["filters"].include_channels is False
    assert page_call["required_address"] == SessionAddress(None, "builder", "s1")


@pytest.mark.asyncio
async def test_session_list_pages_with_a_last_activity_cursor(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())
    sessions = state.runtime.chat_sessions
    for session_id in ("first", "second", "third"):
        sessions.create("coder", session_id=session_id).append(ChatMessage.user(session_id))

    first_page = await rpc_result(state, "session.list", agent_id="coder", limit=2)
    cursor = first_page["next_cursor"]
    second_page = await rpc_result(state, "session.list", agent_id="coder", limit=2, cursor=cursor)

    assert set(cursor) == {"last_activity_at", "agent_id", "session_id"}
    assert canonical_timestamp(cursor["last_activity_at"]) == cursor["last_activity_at"]
    assert cursor["agent_id"] == "coder"
    assert cursor["session_id"] == first_page["sessions"][-1]["id"]
    listed = [row["id"] for row in (*first_page["sessions"], *second_page["sessions"])]
    # The cursor resumes after the first page: every Session is listed exactly once.
    assert len(listed) == len(set(listed)) == first_page["total_count"]
    assert {"first", "second", "third"} <= set(listed)
    assert second_page["next_cursor"] is None


@pytest.mark.asyncio
async def test_session_get_reads_one_summary_by_exact_address(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())
    sessions = state.runtime.chat_sessions
    source = sessions.create("coder", session_id="source")
    source.append(ChatMessage.user("hello"))
    sessions.set_title(source.address, "Release planning")
    fork = await sessions.fork(source.address)
    settle_run(sessions, fork.address, "run-one", completed_at="2026-09-20T10:00:00Z")

    parent = await rpc_result(state, "session.get", agent_id="coder", session_id="source")
    child = await rpc_result(state, "session.get", agent_id="coder", session_id=fork.id)

    assert parent["session"]["id"] == "source"
    assert parent["session"]["agent_address"] == "coder"
    assert parent["session"]["title"] == "Release planning"
    assert "agent_id" not in parent["session"]
    assert "compaction_policy_effective" not in parent["session"]
    assert child["session"][FORK_SOURCE_META_KEY]["session_id"] == "source"
    assert child["session"]["unread_run_id"] == "run-one"


@pytest.mark.asyncio
async def test_session_get_reports_an_absent_session_as_none(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())
    sessions = state.runtime.chat_sessions
    archived = sessions.create("coder", session_id="archived")
    await sessions.archive(archived.address)
    sessions.create("builder", session_id="team", project_id="vbot")

    archived_result = await rpc_result(
        state, "session.get", agent_id="coder", session_id="archived"
    )
    # The Project rides in the address: the same Session id under the identity
    # Agent is a different Session.
    identity_result = await rpc_result(state, "session.get", agent_id="builder", session_id="team")
    team = await rpc_result(state, "session.get", agent_id="builder@vbot", session_id="team")

    assert archived_result == {"session": None}
    assert identity_result == {"session": None}
    assert team["session"]["agent_address"] == "builder@vbot"


@pytest.mark.asyncio
async def test_session_change_stats_and_rows_report_the_sessions_changed_lines(
    tmp_path: Path,
) -> None:
    state = make_state(tmp_path, StubAdapter())
    sessions = state.runtime.chat_sessions
    session = sessions.create("coder", session_id="work")
    await session.start_run("run-one").record_change_stats_async(
        {
            "files": 1,
            "added": 3,
            "removed": 1,
            "paths": ["/repo/a.py"],
            "file_stats": [{"path": "/repo/a.py", "added": 3, "removed": 1}],
        }
    )
    sessions.create("coder", session_id="idle")

    stats = await rpc_result(state, "session.change_stats", agent_id="coder", session_id="work")
    idle = await rpc_result(state, "session.change_stats", agent_id="coder", session_id="idle")
    row = await rpc_result(state, "session.get", agent_id="coder", session_id="work")
    listed = await rpc_result(state, "session.list", agent_id="coder")

    totals = {"files": 1, "added": 3, "removed": 1}
    assert stats == {
        "change_stats": {**totals, "file_stats": [{"path": "/repo/a.py", "added": 3, "removed": 1}]}
    }
    assert idle == {"change_stats": None}
    assert row["session"]["change_stats"] == totals
    assert {item["id"]: item.get("change_stats") for item in listed["sessions"]} == {
        "work": totals,
        "idle": None,
    }
    missing = await rpc_error(state, "session.change_stats", agent_id="coder", session_id="gone")
    assert missing["code"] == RPC_ERROR_DOMAIN


@pytest.mark.asyncio
async def test_activity_list_returns_only_completed_sessions_per_address(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())
    sessions = state.runtime.chat_sessions
    for session_id in ("idle", "unread", "read"):
        sessions.create("coder", session_id=session_id)
    sessions.create("builder", session_id="team", project_id="vbot")
    unread = SessionAddress(project_id=None, agent_id="coder", session_id="unread")
    read = SessionAddress(project_id=None, agent_id="coder", session_id="read")
    team = SessionAddress(project_id="vbot", agent_id="builder", session_id="team")
    settle_run(sessions, unread, "run-unread", "failed", "2026-09-20T10:00:00Z")
    settle_run(sessions, read, "run-read", "completed", "2026-09-20T10:01:00Z")
    sessions.mark_terminal_run_read(read, "run-read")
    settle_run(sessions, team, "run-team", "completed", "2026-09-20T10:02:00Z")

    result = await rpc_result(
        state,
        "session.activity_list",
        agent_ids=["coder", "builder@vbot", "deleted-agent", "coder"],
    )

    assert [
        (agent["agent_id"], agent["project_id"], [row["id"] for row in agent["sessions"]])
        for agent in result["agents"]
    ] == [
        ("coder", None, ["read", "unread"]),
        ("builder", "vbot", ["team"]),
        ("deleted-agent", None, []),
    ]
    rows = {row["id"]: row for row in result["agents"][0]["sessions"]}
    assert rows["read"]["has_unread_completion"] is False
    assert rows["read"]["latest_completion_run_id"] == "run-read"
    assert rows["unread"]["unread_run_status"] == "failed"


@pytest.mark.asyncio
async def test_activity_list_reads_every_address_in_one_store_call_without_resolving() -> None:
    state, resolver, sessions = stub_session_state()

    result = await rpc_result(
        state, "session.activity_list", agent_ids=["builder", "reviewer@vbot", "builder"]
    )
    empty = await rpc_result(state, "session.activity_list", agent_ids=[])

    assert result == {
        "agents": [
            {"agent_id": "builder", "project_id": None, "sessions": sessions.activity_rows},
            {"agent_id": "reviewer", "project_id": "vbot", "sessions": sessions.activity_rows},
        ]
    }
    assert empty == {"agents": []}
    # Activity is a Session-store projection: no per-Agent resolution, one read,
    # and none at all for an empty batch.
    assert resolver.resolved == []
    assert sessions.activity_reads == [[(None, "builder"), ("vbot", "reviewer")]]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "params"),
    [
        ("agent.list", {}),
        ("agent.get", {"id": "coder"}),
        ("agent.reorder", {"agent_ids": ["coder"], "expected_revision": 0}),
        ("session.list", {"agent_id": "coder"}),
        ("session.get", {"agent_id": "coder", "session_id": "s1"}),
        ("session.activity_list", {"agent_ids": ["coder"]}),
        ("session.rename", {"agent_id": "coder", "session_id": "s1", "title": "Renamed"}),
        ("chat.history", {"agent_id": "coder", "session_id": "s1"}),
        ("chat.run_result", {"agent_id": "coder", "session_id": "s1", "run_id": "run-one"}),
    ],
)
async def test_session_work_on_a_closed_session_database_is_a_domain_error(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    method: str,
    params: JsonObject,
) -> None:
    state = make_state(tmp_path, StubAdapter())
    state.runtime.chat_sessions.create("coder", session_id="s1")
    state.runtime.chat_sessions.close()

    with caplog.at_level(logging.ERROR):
        error = await rpc_error(state, method, **params)

    assert error["code"] == RPC_ERROR_DOMAIN
    assert "Unexpected RPC request failure" not in caplog.text


# ---------------------------------------------------------------------------
# session.mark_read / session.rename
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_mark_read_acknowledges_the_exact_run_without_a_list_refresh() -> None:
    state, resolver, sessions = stub_session_state()

    acknowledged = await rpc_result(
        state, "session.mark_read", agent_id="builder@vbot", session_id="s1", run_id="run-one"
    )
    sessions.mark_read_result.update(
        marked_read=False,
        has_unread_completion=True,
        latest_completion_run_id="run-newer",
        unread_run_id="run-newer",
    )
    stale = await rpc_result(
        state, "session.mark_read", agent_id="builder", session_id="s1", run_id="run-old"
    )

    assert resolver.resolved == [("vbot", "builder"), (None, "builder")]
    assert sessions.marked_read == [
        ("builder", "s1", "run-one", "vbot"),
        ("builder", "s1", "run-old", None),
    ]
    assert acknowledged["agent_id"] == "builder@vbot"
    assert acknowledged["marked_read"] is True
    # A stale acknowledgement reports the newer unread Run instead.
    assert stale["marked_read"] is False
    assert stale["unread_run_id"] == "run-newer"
    assert resource_changes(state) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("params", "project_id", "passed_title", "title"),
    [
        pytest.param(
            {"agent_id": "builder", "title": "Release planning"},
            None,
            "Release planning",
            "Release planning",
            id="identity",
        ),
        pytest.param(
            {"agent_id": "builder@vbot", "title": "Release planning"},
            "vbot",
            "Release planning",
            "Release planning",
            id="project",
        ),
        # An absent title is the clear signal: the store receives "".
        pytest.param({"agent_id": "builder"}, None, "", None, id="clear"),
    ],
)
async def test_session_rename_sets_or_clears_the_title(
    params: JsonObject, project_id: str | None, passed_title: str, title: str | None
) -> None:
    state, _resolver, sessions = stub_session_state()

    result = await rpc_result(state, "session.rename", session_id="s1", **params)

    assert result == {"agent_id": "builder", "session_id": "s1", "title": title}
    assert sessions.renamed == [("builder", "s1", passed_title, project_id)]
    assert resource_changes(state) == [
        {
            "kind": "sessions",
            "scope": {"project_id": project_id, "agent_id": "builder", "session_id": "s1"},
        }
    ]


# ---------------------------------------------------------------------------
# session.set_compaction_policy / session.set_agent_overrides / session.link_channel
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_compaction_policy_override_and_clear() -> None:
    state, _resolver, sessions = stub_session_state()

    set_result = await rpc_result(
        state, "session.set_compaction_policy", agent_id="builder", session_id="s1", policy=_POLICY
    )
    clear_result = await rpc_result(
        state, "session.set_compaction_policy", agent_id="builder", session_id="s1", policy=None
    )

    assert set_result["override"] == _POLICY
    assert set_result["source"] == "session"
    assert clear_result["override"] is None
    assert clear_result["source"] == "agent_or_global"
    assert sessions.saved_metadata[("builder", "s1", None)] == {}


@pytest.mark.asyncio
async def test_session_policy_resolution_failure_does_not_persist_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, resolver, sessions = stub_session_state()

    def unresolved(*_args: object) -> None:
        raise AgentNotFoundError("missing Agent")

    monkeypatch.setattr(resolver, "resolve_agent", unresolved)
    error = await rpc_error(
        state,
        "session.set_compaction_policy",
        agent_id="assistant",
        session_id="s1",
        policy=_POLICY,
    )

    assert "missing Agent" in error["message"]
    assert sessions.saved_metadata == {}


@pytest.mark.asyncio
async def test_session_agent_overrides_change_only_the_fields_named(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())
    state.runtime.agent_resolver.models.unusable.add("openai/ghost")
    sessions = state.runtime.chat_sessions
    session = {"agent_id": "coder", "session_id": "session-one"}
    address = SessionAddress(None, "coder", "session-one")
    await rpc_result(
        state,
        "session.create",
        **session,
        agent_overrides={"model": "openai/gpt-mini", "thinking_effort": "high"},
    )

    refused = await rpc_error(
        state, "session.set_agent_overrides", **session, agent_overrides={"model": "openai/ghost"}
    )
    result = await rpc_result(
        state,
        "session.set_agent_overrides",
        **session,
        agent_overrides={"temperature": 0.4, "thinking_effort": None},
    )

    assert refused["code"] == "invalid_request"
    # A value sets its field, null clears one, and fields left out keep their value.
    assert result == {
        **session,
        "agent_overrides": {"model": "openai/gpt-mini", "temperature": 0.4},
        "effective": {
            "model": {"value": "openai/gpt-mini", "source": "session"},
            "thinking_effort": {"value": "", "source": "agent"},
            "temperature": {"value": 0.4, "source": "session"},
        },
    }
    assert sessions.metadata_value(address, "agent_overrides") == result["agent_overrides"]
    assert resource_changes(state, "sessions")[-1]["scope"] == {
        "project_id": None,
        **session,
    }


@pytest.mark.asyncio
async def test_session_link_channel_records_the_reply_target_for_the_channel_agent_only() -> None:
    state, _resolver, sessions = stub_session_state()
    config = ChannelConfig(id="tg-assistant", platform="telegram", agent_id="assistant")
    state.runtime.channel_service = SimpleNamespace(get_channel=AsyncMock(return_value=config))
    key = ("assistant", "s1", None)
    sessions.saved_metadata[key] = {"persisted": "value"}
    link = {"session_id": "s1", "channel_id": "tg-assistant", "platform_conv_id": "12345"}

    refused = await rpc_error(state, "session.link_channel", agent_id="writer", **link)
    assert refused["code"] == "channel_config_error"
    assert "tg-assistant" in refused["message"]
    assert sessions.saved_metadata == {key: {"persisted": "value"}}

    result = await rpc_result(state, "session.link_channel", agent_id="assistant", **link)

    assert result == {"ok": True}
    assert sessions.saved_metadata[key] == {
        "persisted": "value",
        "source_channel_id": "tg-assistant",
        "platform": "telegram",
        "platform_conv_id": "12345",
        "last_reply_target": {"channel_id": "tg-assistant", "platform_target": "12345"},
    }
    # Linking changes metadata only: the Session itself is never loaded, so no
    # note or reminder is written into it.
    assert sessions.got == []


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["link_channel", "set_policy", "clear_policy"])
async def test_session_partial_mutation_preserves_concurrent_title(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    write_bootstrap_marker(tmp_path)
    sessions = ChatSessionManager(tmp_path)
    try:
        session = sessions.create("assistant")
        address = SessionAddress(None, "assistant", session.id)
        sessions.set_metadata(address, {"compaction_policy": _POLICY, "retained": "value"})
        replace = sessions.set_metadata
        mutate = sessions.mutate_metadata_with_previous
        concurrent_writes: list[SessionAddress] = []

        def concurrent_title(target: SessionAddress) -> None:
            # Reproduce another domain committing after any RPC pre-read but
            # immediately before its write. Both write APIs get the same race.
            concurrent_writes.append(target)
            mutate(target, lambda metadata: metadata.update({"title": "Concurrent title"}))

        def replace_after_title(target: SessionAddress, metadata: dict[str, Any]) -> None:
            concurrent_title(target)
            replace(target, metadata)

        def mutate_after_title(
            target: SessionAddress, mutation: Callable[[dict[str, Any]], None]
        ) -> tuple[dict[str, Any], dict[str, Any]]:
            concurrent_title(target)
            return mutate(target, mutation)

        monkeypatch.setattr(sessions, "set_metadata", replace_after_title)
        monkeypatch.setattr(sessions, "mutate_metadata_with_previous", mutate_after_title)
        config = ChannelConfig(id="channel", platform="telegram", agent_id="assistant")

        async def resolve_agent_async(*_args: object) -> SimpleNamespace:
            return SimpleNamespace(compaction_policy=None)

        state = SimpleNamespace(
            runtime=SimpleNamespace(
                chat_sessions=sessions,
                channel_service=SimpleNamespace(get_channel=AsyncMock(return_value=config)),
                agent_resolver=SimpleNamespace(resolve_agent_async=resolve_agent_async),
                storage=SimpleNamespace(load_compaction_settings=lambda: _POLICY),
            ),
            event_bus=ServerEventBus(),
        )
        params = {"agent_id": "assistant", "session_id": session.id}
        if operation == "link_channel":
            response = await call(
                state,
                "session.link_channel",
                **params,
                channel_id="channel",
                platform_conv_id="conversation",
            )
        else:
            policy = None if operation == "clear_policy" else _POLICY
            response = await call(state, "session.set_compaction_policy", **params, policy=policy)

        assert response["ok"] is True, response
        stored = sessions.get_metadata(address)
        assert concurrent_writes == [address]
        assert stored["title"] == "Concurrent title"
        assert stored["retained"] == "value"
        if operation == "link_channel":
            assert stored["source_channel_id"] == "channel"
            assert stored["compaction_policy"] == _POLICY
        elif operation == "clear_policy":
            assert "compaction_policy" not in stored
        else:
            assert stored["compaction_policy"] == _POLICY
    finally:
        sessions.close()
