"""Tests for rpc chat history."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from core.chat import (
    ChatMessage,
    ToolCall,
)
from core.runs import RunAdmission, RunKind
from core.sessions import SESSION_FORK_ALWAYS_STRIP_META_KEYS, SessionAddress
from server.rpc.methods import dispatch_rpc
from tests.server.rpc_test_support import (
    StubAdapter,
    _no_models_dev_fetch,
    make_state,
)

__all__ = ["_no_models_dev_fetch"]


@pytest.mark.asyncio
async def test_reflections_restore_running_and_durable_reviews(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())
    sessions = state.runtime.chat_sessions
    source = sessions.create("coder", session_id="source")
    fork = await sessions.fork(source.address, strip_meta_keys=SESSION_FORK_ALWAYS_STRIP_META_KEYS)
    sessions.record_run_kind(fork.address, RunKind.SKILL_REFLECTION)
    release = asyncio.Event()

    async def execute(run):
        await release.wait()
        return "done"

    run = await state.chat_runs.start(
        fork.address,
        execute,
        admission=RunAdmission(
            run_kind=RunKind.SKILL_REFLECTION, contributes_to_agent_activity=False
        ),
    )
    try:
        live = await dispatch_rpc(
            state,
            {
                "method": "chat.reflections",
                "params": {
                    "agent_id": "coder",
                    "session_id": source.id,
                },
            },
        )
        assert live["ok"] is True
        assert live["result"]["reflection_runs"] == [
            {
                "run_id": run.id,
                "session_id": fork.id,
                "run_kind": "skill_reflection",
                "status": "running",
                "started_at": run.created_at,
            }
        ]
    finally:
        release.set()
        await run.wait()

    for method in ("chat.history", "chat.reflections"):
        result = await dispatch_rpc(
            state,
            {
                "method": method,
                "params": {
                    "agent_id": "coder",
                    "session_id": source.id,
                },
            },
        )
        assert result["ok"] is True
        assert result["result"]["reflection_runs"][0]["run_id"] == run.id
        assert result["result"]["reflection_runs"][0]["status"] == "completed"
    unrelated = sessions.create("coder", session_id="unrelated")
    result = await dispatch_rpc(
        state,
        {
            "method": "chat.reflections",
            "params": {
                "agent_id": "coder",
                "session_id": unrelated.id,
            },
        },
    )
    assert result["result"]["reflection_runs"] == []


@pytest.mark.asyncio
async def test_chat_history_loads_current_session_and_strips_reasoning_meta(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())
    session = state.runtime.chat_sessions.create("coder", session_id="current-one")
    state.runtime.agents.update("coder", current_session_id="current-one")
    session.append(
        ChatMessage.assistant(
            model="openai/gpt-5.2",
            content="Hello",
            reasoning="visible",
            reasoning_meta={"secret": "opaque"},
        )
    )

    response = await dispatch_rpc(
        state,
        {"method": "chat.history", "params": {"agent_id": "coder"}},
    )

    assert response["ok"] is True
    assert response["result"]["session_id"] == "current-one"
    assert response["result"]["messages"][0]["reasoning"] == "visible"
    assert "reasoning_meta" not in response["result"]["messages"][0]


@pytest.mark.asyncio
async def test_chat_history_includes_whole_session_usage_totals(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())
    session = state.runtime.chat_sessions.create("coder", session_id="usage-session")
    state.runtime.agents.update("coder", current_session_id="usage-session")
    session.append(ChatMessage.user(content="hello"))
    session.append(
        ChatMessage.assistant(
            model="openai/gpt-5.2",
            content="One",
            usage={
                "input_tokens": 1000,
                "output_tokens": 50,
                "cache_read_tokens": 800,
                "reasoning_tokens": 20,
            },
        )
    )
    session.append(
        ChatMessage.assistant(
            model="openai/gpt-5.2",
            content="Two",
            usage={
                "input_tokens": 2000,
                "output_tokens": 100,
                "cache_read_tokens": 1500,
                "cache_write_tokens": 300,
                "reasoning_tokens": 40,
            },
        )
    )

    response = await dispatch_rpc(
        state,
        {"method": "chat.history", "params": {"agent_id": "coder", "limit": 1}},
    )

    assert response["ok"] is True
    # The page is a slice; the totals still cover the whole transcript.
    assert len(response["result"]["messages"]) == 1
    assert response["result"]["session_usage"] == {
        "measured_turns": 2,
        "estimated_turns": 0,
        "cache_turns": 2,
        "input_tokens": 3000,
        "output_tokens": 150,
        "cache_read_tokens": 2300,
        "cache_write_tokens": 300,
        "reasoning_turns": 2,
        "reasoning_tokens": 60,
    }
    assert response["result"]["context_usage"] == {
        "tokens": 2100,
        "estimated": True,
        "provider_input_tokens": 2000,
        "provider_output_tokens": 100,
    }


@pytest.mark.asyncio
async def test_chat_history_includes_active_run_descriptor(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())
    state.runtime.chat_sessions.create("coder", session_id="active-session")
    state.runtime.agents.update("coder", current_session_id="active-session")
    started = asyncio.Event()
    release = asyncio.Event()

    async def _blocking_executor(_run: Any) -> str:
        started.set()
        await release.wait()
        return "done"

    active_run = await state.chat_runs.start(
        SessionAddress(project_id=None, agent_id="coder", session_id="active-session"),
        _blocking_executor,
    )
    await started.wait()

    try:
        response = await dispatch_rpc(
            state,
            {"method": "chat.history", "params": {"agent_id": "coder"}},
        )
    finally:
        release.set()
        await active_run.wait()

    assert response["ok"] is True
    active_run_payload = response["result"]["active_run"]
    assert active_run_payload["run_id"] == active_run.id
    assert active_run_payload["agent_id"] == "coder"
    assert active_run_payload["session_id"] == "active-session"
    assert active_run_payload["status"] == "running"
    assert active_run_payload["sse_url"] == f"/api/runs/{active_run.id}/events"
    assert [event["type"] for event in active_run_payload["events"]] == ["run_started"]


@pytest.mark.asyncio
async def test_chat_history_filters_internal_notes(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())
    session = state.runtime.chat_sessions.create("coder", session_id="note-session")
    state.runtime.agents.update("coder", current_session_id="note-session")
    session.append(ChatMessage.user(content="Visible request"))
    session.append(ChatMessage.note(content="Internal reminder"))
    session.append(
        ChatMessage.assistant(
            model="openai/gpt-5.2",
            content="Visible response",
        )
    )

    response = await dispatch_rpc(
        state,
        {"method": "chat.history", "params": {"agent_id": "coder"}},
    )

    assert response["ok"] is True
    messages = response["result"]["messages"]
    assert [message["role"] for message in messages] == ["user", "assistant"]
    assert "Internal reminder" not in str(messages)


@pytest.mark.asyncio
async def test_chat_history_includes_compaction_checkpoints(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())
    session = state.runtime.chat_sessions.create("coder", session_id="compaction-session")
    state.runtime.agents.update("coder", current_session_id="compaction-session")
    user_message = ChatMessage.user(content="Visible request")
    session.append(user_message)
    session.append(
        ChatMessage.compaction_checkpoint(
            summary="Compacted context summary",
            projection=[user_message],
            compacted_token_count=321,
        )
    )
    session.append(
        ChatMessage.assistant(
            model="openai/gpt-5.2",
            content="Visible response",
        )
    )

    response = await dispatch_rpc(
        state,
        {"method": "chat.history", "params": {"agent_id": "coder"}},
    )

    assert response["ok"] is True
    messages = response["result"]["messages"]
    assert [message["role"] for message in messages] == [
        "user",
        "compaction_checkpoint",
        "assistant",
    ]
    checkpoint = messages[1]
    assert checkpoint["content"] == "Compacted context summary"
    assert checkpoint["projection"][1]["id"] == user_message.id
    assert checkpoint["usage"] == {"compacted_token_count": 321}


@pytest.mark.asyncio
async def test_chat_history_includes_usage_on_assistant_messages(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())
    session = state.runtime.chat_sessions.create("coder", session_id="usage-session")
    state.runtime.agents.update("coder", current_session_id="usage-session")
    session.append(
        ChatMessage.assistant(
            model="openai/gpt-5.2",
            content="Hello",
            usage={
                "input_tokens": 150,
                "output_tokens": 42,
                "cache_write_tokens": 12,
                "reasoning_tokens": 30,
            },
        )
    )
    session.append(
        ChatMessage.user(content="Follow-up"),
    )
    session.append(
        ChatMessage.assistant(
            model="openai/gpt-5.2",
            content="World",
        )
    )

    response = await dispatch_rpc(
        state,
        {"method": "chat.history", "params": {"agent_id": "coder"}},
    )

    assert response["ok"] is True
    messages = response["result"]["messages"]
    assert len(messages) == 3

    # Assistant message with usage includes it in the response
    assert messages[0]["usage"] == {
        "input_tokens": 150,
        "output_tokens": 42,
        "cache_write_tokens": 12,
        "reasoning_tokens": 30,
    }
    assert messages[0]["content"] == "Hello"

    # User message does not carry usage
    assert "usage" not in messages[1]

    # Assistant message without usage has no usage key
    assert "usage" not in messages[2]


@pytest.mark.asyncio
async def test_chat_history_includes_tool_timing_and_run_summary(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())
    session = state.runtime.chat_sessions.create("coder", session_id="timing-session")
    state.runtime.agents.update("coder", current_session_id="timing-session")
    timing = {
        "started_at": "2026-05-03T14:30:01+00:00",
        "completed_at": "2026-05-03T14:30:02+00:00",
        "duration_ms": 1000,
    }
    session = session.start_run("run-one")
    session.append(ChatMessage.user(content="Run this"))
    session.append(
        ChatMessage.assistant(
            model="openai/gpt-5.2",
            content=None,
            tool_calls=[ToolCall(id="call-one", name="read", arguments={"path": "a.txt"})],
        )
    )
    session.append(
        ChatMessage.tool(
            tool_call_id="call-one",
            name="read",
            content='{"ok":true,"error":null,"data":{},"artifacts":[]}',
            timing=timing,
        )
    )
    session.append(ChatMessage.assistant(model="openai/gpt-5.2", content="Done"))
    session.append(
        ChatMessage.run_summary(
            run_id="run-one", status="completed", timing=timing, iteration_count=1
        )
    )

    response = await dispatch_rpc(
        state,
        {"method": "chat.history", "params": {"agent_id": "coder"}},
    )

    assert response["ok"] is True
    messages = response["result"]["messages"]
    assert [message["role"] for message in messages] == [
        "user",
        "assistant",
        "tool",
        "assistant",
        "run_summary",
    ]
    assert messages[2]["timing"] == timing
    assert messages[4]["run_id"] == "run-one"
    assert messages[4]["status"] == "completed"
    assert messages[4]["timing"] == timing


@pytest.mark.asyncio
async def test_history_completion_cannot_pair_earlier_page_with_idle_run(tmp_path, monkeypatch):
    from server.rpc import chat_methods

    finish_during = "_read_chat_history"

    state = make_state(tmp_path, StubAdapter())
    session = state.runtime.chat_sessions.create("coder", session_id="coherent")
    state.runtime.agents.update("coder", current_session_id="coherent")
    release = asyncio.Event()

    async def execute(run):
        await release.wait()
        session.append(ChatMessage.assistant(model="openai/test", content="Completed answer"))
        return "done"

    run = await state.chat_runs.start(session.address, execute)
    original = chat_methods._CHAT_RPC_WORKERS.run
    injected = False

    async def intercepted(function, *args, **kwargs):
        nonlocal injected
        result = await original(function, *args, **kwargs)
        if function.__name__ == finish_during and not injected:
            injected = True
            release.set()
            await run.wait()
        return result

    monkeypatch.setattr(chat_methods._CHAT_RPC_WORKERS, "run", intercepted)
    try:
        response = await dispatch_rpc(
            state, {"method": "chat.history", "params": {"agent_id": "coder"}}
        )
        assert injected
        assert response["ok"] is True
        result = response["result"]
        assert result.get("active_run", {}).get("run_id") == run.id or any(
            message.get("content") == "Completed answer" for message in result["messages"]
        )
    finally:
        release.set()
        await run.wait()


@pytest.mark.asyncio
async def test_chat_history_incremental_projection_and_edit_reset(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())
    session = state.runtime.chat_sessions.create("coder", session_id="timeline")
    await state.runtime.chat_sessions.record_run_start_async(session.address, run_id="run-one")
    session = session.for_run("run-one")
    user = ChatMessage.user("question")
    session.append(user)

    async def history(**params: Any) -> Any:
        response = await dispatch_rpc(
            state,
            {
                "method": "chat.history",
                "params": {
                    "agent_id": "coder",
                    "session_id": session.id,
                    **params,
                },
            },
        )
        assert response["ok"] is True
        return response["result"]

    first = await history(limit=1)
    assert first["messages"][0]["history_run_id"] == "run-one"
    assert first["messages"][0]["history_sequence"] == 0
    session.append(
        ChatMessage.assistant(model="test", content="answer", reasoning_meta={"secret": "private"})
    )
    delta = await history(after=first["next_after"], limit=1)
    assert delta["incremental"] is True
    assert delta["history_reset"] is False
    assert [message["history_sequence"] for message in delta["messages"]] == [1]
    assert delta["messages"][0]["history_run_id"] == "run-one"
    assert "reasoning_meta" not in delta["messages"][0]
    assert delta["history_generation"] == first["history_generation"]
    replacement = ChatMessage.user("edited")
    session.append_many([ChatMessage.history_edit(user.id), replacement])
    reset = await history(after=delta["next_after"], limit=10)
    assert reset["incremental"] is False
    assert reset["history_reset"] is True
    assert [message["id"] for message in reset["messages"]] == [replacement.id]
    assert reset["messages"][0]["history_sequence"] == 3


@pytest.mark.asyncio
async def test_chat_history_reports_the_session_effective_compaction_policy(
    tmp_path: Path,
) -> None:
    state = make_state(tmp_path, StubAdapter())
    sessions = state.runtime.chat_sessions
    session = sessions.create("coder", session_id="policy-session")
    session.append(ChatMessage.user("Hello"))

    async def history(**params: Any) -> dict[str, Any]:
        response = await dispatch_rpc(
            state,
            {
                "method": "chat.history",
                "params": {"agent_id": "coder", "session_id": session.id, **params},
            },
        )
        assert response["ok"] is True
        result: dict[str, Any] = response["result"]
        return result

    inherited = await history()
    assert inherited["compaction_policy"]["enabled"] is True
    assert inherited["compaction_policy"]["trigger"] == {
        "type": "context_ratio",
        "threshold": 0.8,
    }

    agent_policy = {
        "enabled": False,
        "trigger": {"type": "context_ratio", "threshold": 0.6},
        "strategy": {"type": "continuation"},
    }
    state.runtime.agents.update("coder", compaction_policy=agent_policy)
    assert (await history())["compaction_policy"]["enabled"] is False

    session_policy = {
        "enabled": True,
        "trigger": {"type": "input_tokens", "tokens": 50_000},
        "strategy": {"type": "continuation"},
    }
    sessions.mutate_metadata(
        session.address,
        lambda metadata: metadata.__setitem__("compaction_policy", session_policy),
    )
    current = await history()
    assert current["compaction_policy"]["enabled"] is True
    assert current["compaction_policy"]["trigger"] == {
        "type": "input_tokens",
        "tokens": 50_000,
    }

    older = await history(before=current["messages"][0]["id"])
    assert "compaction_policy" not in older


@pytest.mark.asyncio
async def test_chat_history_omits_the_policy_when_the_agent_no_longer_resolves(
    tmp_path: Path,
) -> None:
    state = make_state(tmp_path, StubAdapter())
    session = state.runtime.chat_sessions.create(
        "former", project_id="vbot", session_id="orphaned-session"
    )
    session.append(ChatMessage.user("Hello"))

    response = await dispatch_rpc(
        state,
        {
            "method": "chat.history",
            "params": {"agent_id": "former@vbot", "session_id": session.id},
        },
    )

    assert response["ok"] is True
    assert response["result"]["messages"]
    assert "compaction_policy" not in response["result"]


@pytest.mark.asyncio
async def test_unchanged_after_read_returns_only_the_cursor_in_one_worker_hop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.rpc import chat_methods

    state = make_state(tmp_path, StubAdapter())
    session = state.runtime.chat_sessions.create("coder", session_id="quiet")
    session.append(ChatMessage.user("Hello"))

    async def history(**params: Any) -> dict[str, Any]:
        response = await dispatch_rpc(
            state,
            {
                "method": "chat.history",
                "params": {"agent_id": "coder", "session_id": session.id, **params},
            },
        )
        assert response["ok"] is True
        result: dict[str, Any] = response["result"]
        return result

    first = await history()
    assert {"session_usage", "background_bash_statuses", "compaction_policy"} <= set(first)
    assert first["reflection_runs"] == []
    # Read but absent: an explicit null clears the caller's value.
    assert first["context_usage"] is None

    hops: list[str] = []
    original = chat_methods._CHAT_RPC_WORKERS.run

    async def counted(function: Any, *args: Any, **kwargs: Any) -> Any:
        hops.append(function.__name__)
        return await original(function, *args, **kwargs)

    def unexpected(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("an unchanged read must not recompute Session facts")

    monkeypatch.setattr(chat_methods._CHAT_RPC_WORKERS, "run", counted)
    monkeypatch.setattr(chat_methods, "_session_compaction_policy", unexpected)
    monkeypatch.setattr(chat_methods, "_read_reflection_runs", unexpected)
    monkeypatch.setattr(chat_methods, "background_bash_statuses", unexpected)

    unchanged = await history(after=first["next_after"])

    assert hops == ["_read_chat_history"]
    assert unchanged == {
        "agent_id": "coder",
        "session_id": session.id,
        "messages": [],
        "history_generation": first["history_generation"],
        "runs": [],
        "next_after": first["next_after"],
        "incremental": True,
        "history_reset": False,
        "has_newer": False,
        "has_more": False,
    }

    monkeypatch.undo()
    session.append(ChatMessage.assistant(model="test", content="Reply"))
    appended = await history(after=first["next_after"])
    assert [message["content"] for message in appended["messages"]] == ["Reply"]
    assert appended["incremental"] is True
    assert {"session_usage", "context_usage", "compaction_policy"} <= set(appended)
    assert appended["background_bash_statuses"] == {}
    # Reflection Runs arrive with a full read; live events keep them current.
    assert "reflection_runs" not in appended
