"""session.fork: provenance, scope, re-homing and failures."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.chat import ChatMessage, ChatSessionError
from core.prompts.pinned_context import PINNED_SKILL_CATALOG_SLOT
from core.sessions import FORK_SOURCE_META_KEY, SeenSkillsUpdate, SessionAddress
from tests.server.rpc.session_methods_test_support import FakeSessions, stub_session_state
from tests.server.rpc_test_support import (
    StubAdapter,
    make_state,
    resource_changes,
    rpc_error,
    rpc_result,
)


@pytest.mark.asyncio
async def test_side_chat_launch_retries_one_fork_and_binds_it_to_its_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    state = make_state(tmp_path, StubAdapter())
    sessions = state.runtime.chat_sessions
    parent = sessions.create("coder", session_id="s1").address
    other = sessions.create("coder", session_id="s2")
    other_generation = other.generation_id
    draft = await rpc_result(
        state, "chat.stream", agent_id="coder", session_id=parent.session_id, content="/btw"
    )
    generation = draft["data"]["navigation"]["source_generation_id"]
    params = {
        "agent_id": "coder",
        "session_id": parent.session_id,
        "launch_id": "launch-1",
        "source_generation_id": generation,
    }

    async def launch():
        return await rpc_result(state, "session.create_side_chat", **params)

    first, second = await asyncio.gather(launch(), launch())
    assert first == second
    child = SessionAddress(first["project_id"], first["agent_id"], first["session_id"])
    assert first["generation_id"] == sessions.get(child).generation_id
    assert (first["generation_id"], child) == await sessions.latest_side_chat(parent)
    assert [message.role for message in sessions.get(child).load()] == ["note"]
    assert not sessions.get(child).run_records()
    renamed = await rpc_result(
        state,
        "chat.stream",
        agent_id=child.agent_id,
        session_id=child.session_id,
        content="/rename Separate topic",
        expected_generation_id=first["generation_id"],
    )
    assert renamed["command_handled"] and renamed["session_id"] == child.session_id
    assert sessions.metadata_value(child, "title") == "Separate topic"
    assert not sessions.metadata_value(parent, "title")
    sent = await rpc_result(
        state,
        "chat.send",
        agent_id=child.agent_id,
        session_id=child.session_id,
        content="Question",
        input_id="first-question",
        expected_generation_id=first["generation_id"],
    )
    receipt = await sessions.lookup_accessor_input_async("first-question")
    assert receipt is not None and receipt.generation_id == first["generation_id"]
    assert sent["session_id"] == child.session_id

    state.runtime.chat_sessions.close()
    state = make_state(tmp_path, StubAdapter())
    sessions = state.runtime.chat_sessions
    assert await launch() == first
    conflict = await rpc_error(
        state,
        "session.create_side_chat",
        **{**params, "session_id": "s2", "source_generation_id": other_generation},
    )
    assert conflict["code"] == "input_conflict"
    await sessions.archive(child)
    vanished = await rpc_error(state, "session.create_side_chat", **params)
    assert vanished["code"] == "domain_error"
    assert await sessions.latest_side_chat(parent) == (generation, None)
    replacement_child = sessions.create(child.agent_id, session_id=child.session_id)
    assert replacement_child.generation_id != first["generation_id"]
    for method, content, input_id in (
        ("chat.send", "/rename Unrelated", "new-command"),
        ("chat.stream", "Question", "new-question"),
        ("chat.send", "Question", "first-question"),
    ):
        error = await rpc_error(
            state,
            method,
            agent_id=child.agent_id,
            session_id=child.session_id,
            content=content,
            input_id=input_id,
            expected_generation_id=first["generation_id"],
        )
        assert error["code"] == "domain_error" and "original Session" in error["message"]
    assert sessions.get(child).load() == []
    assert not sessions.metadata_value(child, "title")
    assert state.runtime.agents.get(child.agent_id).current_session_id != child.session_id
    await sessions.archive(parent)
    replacement = sessions.create("coder", session_id=parent.session_id)
    refused = await rpc_error(
        state, "session.create_side_chat", **{**params, "launch_id": "unsubmitted-draft"}
    )
    assert refused["code"] == "domain_error"
    assert await sessions.latest_side_chat(parent) == (replacement.generation_id, None)


@pytest.mark.asyncio
async def test_fork_same_agent_returns_new_id_with_provenance() -> None:
    state, resolver, sessions = stub_session_state()

    result = await rpc_result(state, "session.fork", agent_id="builder", session_id="s1")

    assert result["session"]["id"] == "fork-1"
    assert result["session"]["agent_id"] == "builder"
    assert result["session"]["fork_source"]["session_id"] == "s1"
    # Without a target the fork stays in the source's scope; Sessions owns which
    # bindings stay behind, so the RPC passes no label or policy of its own.
    assert sessions.forked[0]["target_agent_id"] is None
    assert sessions.forked[0]["target_project_id"] is None
    assert sessions.forked[0]["title"] is None
    assert sessions.forked[0]["run_kind"] is None
    assert resolver.resolved == [(None, "builder")]


@pytest.mark.asyncio
async def test_fork_strips_channel_and_subagent_bindings_but_keeps_title(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())
    sessions = state.runtime.chat_sessions
    source = sessions.create("coder", session_id="s1")
    source.append(ChatMessage.user("hello"))
    sessions.mutate_metadata(
        source.address,
        lambda metadata: metadata.update(
            {
                "title": "Keep",
                "source_channel_id": "chan",
                "platform": "telegram",
                "platform_conv_id": "conversation",
                "is_subagent_session": True,
            }
        ),
    )

    result = await rpc_result(state, "session.fork", agent_id="coder", session_id="s1")

    fork_address = SessionAddress(None, "coder", result["session"]["id"])
    metadata = sessions.get_metadata(fork_address)
    assert metadata["title"] == "Keep"
    assert "source_channel_id" not in metadata
    assert "platform" not in metadata
    assert "platform_conv_id" not in metadata
    assert "is_subagent_session" not in metadata
    assert result["session"]["fork_source"] == metadata[FORK_SOURCE_META_KEY]
    assert result["session"]["fork_source"]["session_id"] == "s1"
    assert [message.content for message in sessions.get(fork_address).load_active()] == ["hello"]


@pytest.mark.asyncio
async def test_fork_of_a_project_session_stays_in_its_project(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())
    resolved: list[tuple[str | None, str]] = []

    async def resolve_agent_async(
        project_id: str | None, agent_id: str, **_options: object
    ) -> None:
        resolved.append((project_id, agent_id))

    state.runtime.agent_resolver = SimpleNamespace(resolve_agent_async=resolve_agent_async)
    sessions = state.runtime.chat_sessions
    source = sessions.create("coder", session_id="s1", project_id="proj")
    source.append(ChatMessage.user("hello"))

    result = await rpc_result(state, "session.fork", agent_id="coder@proj", session_id="s1")

    fork_address = SessionAddress("proj", "coder", result["session"]["id"])
    assert [message.content for message in sessions.get(fork_address).load_active()] == ["hello"]
    assert result["session"]["fork_source"]["session_id"] == "s1"
    assert resolved == [("proj", "coder")]
    # Nothing landed outside the Project.
    assert sessions.list_addresses(None, agent_id="coder") == []


@pytest.mark.asyncio
async def test_fork_to_other_agent_resolves_both_endpoints_and_lands_under_target() -> None:
    state, resolver, sessions = stub_session_state()

    result = await rpc_result(
        state, "session.fork", agent_id="builder", session_id="s1", target_agent_id="reviewer"
    )

    assert result["session"]["agent_id"] == "reviewer"
    assert sessions.forked[0]["target_agent_id"] == "reviewer"
    assert sessions.forked[0]["target_project_id"] is None
    # Both endpoints are resolved before any file work.
    assert resolver.resolved == [(None, "builder"), (None, "reviewer")]
    # The refresh event names the fork under the target agent.
    assert resource_changes(state) == [
        {
            "kind": "sessions",
            "scope": {"project_id": None, "agent_id": "reviewer", "session_id": "fork-1"},
        }
    ]


@pytest.mark.asyncio
async def test_fork_to_other_agent_leaves_the_pinned_skill_catalog_behind(
    tmp_path: Path,
) -> None:
    state = make_state(tmp_path, StubAdapter())
    state.runtime.agents.create("reviewer")
    sessions = state.runtime.chat_sessions
    source = sessions.create("coder", session_id="s1")
    source.append(ChatMessage.user("hello"))
    catalog = {"skills": ["deploy"]}
    sessions.ensure_prompt_pin(source.address, PINNED_SKILL_CATALOG_SLOT, catalog, lambda _: True)
    sessions.record_seen_skills(source.address, SeenSkillsUpdate(("deploy",)))

    same_agent = await rpc_result(state, "session.fork", agent_id="coder", session_id="s1")
    other_agent = await rpc_result(
        state, "session.fork", agent_id="coder", session_id="s1", target_agent_id="reviewer"
    )

    # A fork on the same Agent keeps the prompt-cache-warm catalog; a fork into
    # another Agent leaves it (and the seen Skills) behind so the target pins its own.
    kept = SessionAddress(None, "coder", same_agent["session"]["id"])
    assert sessions.prompt_pin(kept, PINNED_SKILL_CATALOG_SLOT) == catalog
    assert sessions.seen_skills(kept) == frozenset({"deploy"})
    assert other_agent["session"]["agent_id"] == "reviewer"
    moved = SessionAddress(None, "reviewer", other_agent["session"]["id"])
    assert sessions.prompt_pin(moved, PINNED_SKILL_CATALOG_SLOT) is None
    assert sessions.seen_skills(moved) is None
    assert [message.content for message in sessions.get(moved).load_active()] == ["hello"]


def _missing(sessions: FakeSessions) -> None:
    sessions.missing = {"s1"}


def _owner_managed(sessions: FakeSessions) -> None:
    sessions.fork_error = ChatSessionError(
        "This Session is managed by an Extension. Use that Extension to resume it."
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arrange", "message"),
    [
        (_missing, "session does not exist: s1"),
        (
            _owner_managed,
            "This Session is managed by an Extension. Use that Extension to resume it.",
        ),
    ],
    ids=["missing", "owner-managed"],
)
async def test_a_refused_fork_is_a_domain_error_without_a_refresh(
    arrange: Callable[[FakeSessions], None], message: str
) -> None:
    state, _resolver, sessions = stub_session_state()
    arrange(sessions)

    error = await rpc_error(state, "session.fork", agent_id="builder", session_id="s1")

    assert error == {"code": "domain_error", "message": message}
    assert resource_changes(state) == []
