"""``chat.history``, ``chat.reflections`` and ``subagent.inspect`` read contracts."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core.automation import LearningChanges
from core.chat import ChatMessage, ChatSessionManager, ToolCall
from core.chat.messages import ModelFallback
from core.database import write_bootstrap_marker
from core.memory import MemoryService, MemoryWriter
from core.projects import AgentResolutionError
from core.runs import ChatRunManager, RunAdmission, RunKind
from core.sessions import ChatSession
from core.settings.normalizers import normalize_compaction_settings
from core.skills import SkillAuthoringService
from core.tools.tools import tool_success
from server.file_delivery import FileDelivery
from server.rpc import chat_methods
from tests.core.sessions.history_fixtures import append_tool_fixture, complete_run, seed_history
from tests.server.rpc.chat_methods_test_support import call

TIMING = {
    "started_at": "2026-07-24T10:00:00+00:00",
    "completed_at": "2026-07-24T10:00:01+00:00",
    "duration_ms": 1000,
}


class _HistoryAgents:
    """Agent store and resolver behind History reads: Identity Agents only."""

    def __init__(self) -> None:
        self.current_session_id = "session-one"
        self.compaction_policy: dict[str, Any] | None = None
        self.model = ""

    def get(self, _agent_id: str) -> SimpleNamespace:
        return SimpleNamespace(current_session_id=self.current_session_id)

    def resolve_agent(self, project_id: str | None, agent_id: str) -> SimpleNamespace:
        if project_id is not None:
            raise AgentResolutionError(f"agent '{agent_id}@{project_id}' not found")
        return SimpleNamespace(compaction_policy=self.compaction_policy, model=self.model)


@dataclass
class _History:
    state: SimpleNamespace
    sessions: ChatSessionManager
    agents: _HistoryAgents

    def session(self, session_id: str = "session-one", **kwargs: Any) -> ChatSession:
        return self.sessions.create("coder", session_id=session_id, **kwargs)

    async def read(self, **params: Any) -> dict[str, Any]:
        response = await call(self.state, "chat.history", **{"agent_id": "coder", **params})
        assert response["ok"] is True, response
        result: dict[str, Any] = response["result"]
        return result


@pytest.fixture
def history(tmp_path: Path) -> Iterator[_History]:
    write_bootstrap_marker(tmp_path)
    sessions = ChatSessionManager(tmp_path)
    agents = _HistoryAgents()
    (tmp_path / "agents" / "coder").mkdir(parents=True)
    memory = MemoryService(history_root=tmp_path / "agents")
    state = SimpleNamespace(
        runtime=SimpleNamespace(
            agents=agents,
            agent_resolver=agents,
            storage=SimpleNamespace(
                load_compaction_settings=lambda: normalize_compaction_settings(None)
            ),
            chat_sessions=sessions,
            memory=memory,
            learning_changes=LearningChanges(
                memory=memory,
                skills=SkillAuthoringService(),
                skill_home=lambda agent_id: tmp_path / "agents" / agent_id / "skills",
                run_active=lambda run_id: False,
            ),
        ),
        chat_runs=ChatRunManager(persistence=sessions),
        file_delivery=FileDelivery(),
    )
    try:
        yield _History(state, sessions, agents)
    finally:
        sessions.close()


def _user(index: int) -> ChatMessage:
    return replace(ChatMessage.user(f"Message {index}"), id=f"message-{index:03d}")


def _summary(run_id: str, message_id: str) -> ChatMessage:
    return replace(
        ChatMessage.run_summary(
            run_id=run_id, status="completed", timing=TIMING, iteration_count=1
        ),
        id=message_id,
    )


def _background_bash(session: ChatSession, call_id: str, process_id: str) -> None:
    append_tool_fixture(
        session,
        ChatMessage.tool(
            tool_call_id=call_id,
            name="bash",
            content=json.dumps(
                tool_success(
                    {"process_id": process_id, "status": "running", "delivery": "automatic"}
                )
            ),
        ),
    )


def _completion_note(process_id: str, status: str) -> str:
    return (
        "Automatic completion delivery\n\n"
        f"### Bash process — {status}\n"
        f"Process ID: {process_id}\n"
        "Command: make"
    )


# ---------------------------------------------------------------------------
# Projection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_history_defaults_to_the_current_session_and_strips_opaque_metadata(
    history: _History,
) -> None:
    history.agents.current_session_id = "current-one"
    history.session("current-one").append(
        ChatMessage.assistant(
            model="openai/gpt-5.2",
            content="Hello",
            reasoning="visible",
            reasoning_meta={"secret": "opaque"},
        )
    )

    result = await history.read()

    assert result["session_id"] == "current-one"
    assert result["messages"][0]["reasoning"] == "visible"
    assert "reasoning_meta" not in result["messages"][0]


@pytest.mark.asyncio
async def test_history_hides_notes(history: _History) -> None:
    session = history.session().start_run("run-one")
    session.append(ChatMessage.user(content="Visible request"))
    session.add_note("Internal reminder")
    session.add_note("Sub-agent batch completed.\n\nResults:\n- worker/sub-session: Done")
    session.add_note(
        "Model switch note for the Model",
        model_fallback=ModelFallback(from_model="openai/gpt-5.2", to_model="anthropic/claude"),
    )
    session.append(ChatMessage.assistant(model="openai/gpt-5.2", content="Visible response"))

    result = await history.read(session_id="session-one")

    assert [message["role"] for message in result["messages"]] == [
        "user",
        "model_fallback",
        "assistant",
    ]
    # A Model fallback note appears only as its display notice, inside its Run.
    fallback = result["messages"][1]
    assert {key: fallback[key] for key in ("from_model", "to_model", "history_run_id")} == {
        "from_model": "openai/gpt-5.2",
        "to_model": "anthropic/claude",
        "history_run_id": "run-one",
    }
    assert "content" not in fallback
    assert "Internal reminder" not in str(result["messages"])
    assert "Sub-agent batch" not in str(result["messages"])
    assert "Model switch note" not in str(result["messages"])


@pytest.mark.asyncio
async def test_history_reports_usage_per_message_and_for_the_whole_session(
    history: _History, monkeypatch: pytest.MonkeyPatch
) -> None:
    windows = {"openai/gpt-5.2": 400_000}
    monkeypatch.setattr(
        chat_methods, "_resolve_context_window", lambda _state, model: windows.get(model)
    )
    history.agents.model = "openai/gpt-5.2"
    session = history.session()
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
    session.append(ChatMessage.assistant(model="openai/gpt-5.2", content="Unmeasured"))
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
                "context_usage": {
                    "tokens": 2100,
                    "estimated": True,
                    "provider_input_tokens": 2000,
                    "provider_output_tokens": 100,
                },
            },
        )
    )

    full = await history.read()
    # A page is a slice; the whole-Session totals still cover the whole transcript.
    page = await history.read(limit=1)

    messages = full["messages"]
    assert "usage" not in messages[0]
    assert messages[1]["usage"] == {
        "input_tokens": 1000,
        "output_tokens": 50,
        "cache_read_tokens": 800,
        "reasoning_tokens": 20,
    }
    assert "usage" not in messages[2]
    assert [message["content"] for message in page["messages"]] == ["Two"]
    for result in (full, page):
        assert result["session_usage"] == {
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
        # A Context that records no window fills the one of the Agent's Model.
        assert result["context_usage"] == {
            "tokens": 2100,
            "estimated": True,
            "provider_input_tokens": 2000,
            "provider_output_tokens": 100,
            "context_window": 400_000,
        }

    # The window recorded with the Context, of the Model that answered, wins.
    session.append(
        ChatMessage.assistant(
            model="anthropic/claude",
            content="Three",
            usage={
                "input_tokens": 2200,
                "output_tokens": 10,
                "context_usage": {"tokens": 2210, "estimated": False, "context_window": 128_000},
            },
        )
    )
    assert (await history.read())["context_usage"] == {
        "tokens": 2210,
        "estimated": False,
        "context_window": 128_000,
    }


@pytest.mark.asyncio
async def test_history_includes_compaction_checkpoints_tool_timing_and_run_summaries(
    history: _History,
) -> None:
    timing = {
        "started_at": "2026-05-03T14:30:01+00:00",
        "completed_at": "2026-05-03T14:30:02+00:00",
        "duration_ms": 1000,
    }
    session = history.session().start_run("run-one")
    user_message = ChatMessage.user(content="Run this")
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
    complete_run(
        session,
        ChatMessage.run_summary(
            run_id="run-one", status="completed", timing=timing, iteration_count=1
        ),
    )

    messages = (await history.read())["messages"]

    assert [message["role"] for message in messages] == [
        "user",
        "compaction_checkpoint",
        "assistant",
        "tool",
        "assistant",
        "run_summary",
    ]
    checkpoint = messages[1]
    assert checkpoint["content"] == "Compacted context summary"
    assert checkpoint["projection"][1]["id"] == user_message.id
    assert checkpoint["usage"] == {"compacted_token_count": 321}
    # Stored timing comes back in the canonical UTC timestamp form.
    canonical_timing = {
        "started_at": "2026-05-03T14:30:01.000000Z",
        "completed_at": "2026-05-03T14:30:02.000000Z",
        "duration_ms": 1000,
    }
    assert messages[3]["timing"] == canonical_timing
    assert messages[5]["run_id"] == "run-one"
    assert messages[5]["status"] == "completed"
    assert messages[5]["timing"] == canonical_timing


@pytest.mark.asyncio
async def test_history_projects_the_active_edit_lineage_but_keeps_raw_usage(
    history: _History,
) -> None:
    session = history.session()
    first_user = replace(ChatMessage.user("original"), id="user-original")
    first_answer = ChatMessage.assistant(
        model="openai/gpt-5.2",
        content="old answer",
        usage={"input_tokens": 10, "output_tokens": 2},
    )
    edited_user = replace(ChatMessage.user("edited"), id="user-edited")
    edited_answer = ChatMessage.assistant(
        model="openai/gpt-5.2",
        content="new answer",
        usage={"input_tokens": 20, "output_tokens": 3},
    )
    session.append_many([first_user, first_answer])
    session.apply_edit(first_user.id, [edited_user, edited_answer])

    result = await history.read()

    assert [message["content"] for message in result["messages"]] == ["edited", "new answer"]
    assert result["messages"][0]["editable"] is True
    assert result["session_usage"]["input_tokens"] == 30
    assert result["session_usage"]["output_tokens"] == 5


@pytest.mark.asyncio
async def test_history_reports_background_bash_statuses_of_the_records_it_returns(
    history: _History,
) -> None:
    session = history.session()
    _background_bash(session, "bash-one", "process-one")
    _background_bash(session, "bash-two", "process-two")
    session.add_note(_completion_note("process-two", "failed"))
    session.add_note("Skill context: unrelated")

    # A replacement read carries the complete map, completion notes applied.
    first = await history.read(limit=1)
    assert first["background_bash_statuses"] == {
        "process-one": "running",
        "process-two": "failed",
    }
    assert all(message["role"] != "note" for message in first["messages"])

    # An incremental read carries only the appended records' statuses.
    session.add_note(_completion_note("process-one", "completed"))
    delta = await history.read(after=first["next_after"])
    assert delta["incremental"] is True
    assert delta["background_bash_statuses"] == {"process-one": "completed"}

    # An older page carries none.
    older = await history.read(before=first["next_before"])
    assert "background_bash_statuses" not in older


# ---------------------------------------------------------------------------
# Paging and cursors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_history_pages_newest_first_without_loading_the_whole_session(
    history: _History, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = history.session()
    for index in range(1, 7):
        session.append(_user(index))

    def fail_full_load(self: ChatSession) -> list[ChatMessage]:
        raise AssertionError("chat.history must use its bounded Session read model")

    monkeypatch.setattr(ChatSession, "load", fail_full_load)
    monkeypatch.setattr(ChatSession, "load_active", fail_full_load)

    newest = await history.read(limit=2)
    older = await history.read(limit=2, before=newest["next_before"])
    # A public message id remains accepted as the older-page input.
    by_message_id = await history.read(limit=2, before="message-005")

    assert [message["id"] for message in newest["messages"]] == ["message-005", "message-006"]
    assert newest["has_more"] is True
    assert newest["next_before"].startswith("vh1.")
    assert [message["id"] for message in older["messages"]] == ["message-003", "message-004"]
    assert [message["id"] for message in by_message_id["messages"]] == [
        "message-003",
        "message-004",
    ]
    assert by_message_id["has_more"] is True


@pytest.mark.asyncio
async def test_history_cursor_is_positional_when_message_ids_repeat(history: _History) -> None:
    history.session().append_many(
        [
            replace(ChatMessage.user("zero"), id="zero"),
            replace(ChatMessage.user("older duplicate"), id="duplicate"),
            replace(ChatMessage.user("two"), id="two"),
            replace(ChatMessage.user("newer duplicate"), id="duplicate"),
            replace(ChatMessage.user("four"), id="four"),
        ]
    )

    first = await history.read(limit=2)
    second = await history.read(limit=2, before=first["next_before"])

    assert [message["content"] for message in second["messages"]] == ["older duplicate", "two"]


@pytest.mark.asyncio
async def test_history_expands_the_page_to_complete_the_oldest_run_segment(
    history: _History,
) -> None:
    messages = [
        replace(ChatMessage.user("first"), id="first-user"),
        replace(
            ChatMessage.assistant(model="openai/gpt-5.2", content="first result"),
            id="first-assistant",
        ),
        _summary("run-one", "first-summary"),
        replace(ChatMessage.user("second"), id="second-user"),
        replace(
            ChatMessage.assistant(model="openai/gpt-5.2", content="second result"),
            id="second-assistant",
        ),
        _summary("run-two", "second-summary"),
    ]
    seed_history(history.session(), messages)

    result = await history.read(limit=2)

    # Completing a Run writes its summary with a store-assigned id.
    assert [message["id"] for message in result["messages"]] == [
        "second-user",
        "second-assistant",
        messages[5].id,
    ]
    assert result["has_more"] is True


@pytest.mark.asyncio
async def test_history_expanded_page_cursor_skips_the_excluded_run_boundary(
    history: _History,
) -> None:
    messages = [
        replace(ChatMessage.user("first"), id="first-user"),
        _summary("run-one", "first-summary"),
        replace(ChatMessage.note("internal boundary"), id="boundary-note"),
        replace(ChatMessage.user("second"), id="second-user"),
        _summary("run-two", "second-summary"),
    ]
    seed_history(history.session(), messages)

    newest = await history.read(limit=1)
    older = await history.read(limit=1, before=newest["next_before"])

    # Completing a Run writes its summary with a store-assigned id.
    assert [message["id"] for message in newest["messages"]] == ["second-user", messages[4].id]
    assert [message["id"] for message in older["messages"]] == ["first-user", messages[1].id]
    assert older["has_more"] is False


@pytest.mark.asyncio
async def test_history_keeps_the_active_tail_segment_together(history: _History) -> None:
    seed_history(
        history.session(),
        [
            replace(ChatMessage.user("completed"), id="completed-user"),
            replace(
                ChatMessage.assistant(model="openai/gpt-5.2", content="done"),
                id="completed-assistant",
            ),
            _summary("run-one", "completed-summary"),
            replace(ChatMessage.user("active"), id="active-user"),
            replace(
                ChatMessage.assistant(model="openai/gpt-5.2", content="partial"),
                id="active-assistant",
            ),
        ],
    )

    result = await history.read(limit=1)

    assert [message["id"] for message in result["messages"]] == ["active-user", "active-assistant"]
    assert result["has_more"] is True


@pytest.mark.asyncio
async def test_history_increments_after_a_cursor_and_resets_after_an_edit(
    history: _History,
) -> None:
    session = history.session().start_run("run-one")
    user = ChatMessage.user("question")
    session.append(user)

    first = await history.read(limit=1)
    assert first["messages"][0]["history_run_id"] == "run-one"
    assert first["messages"][0]["history_sequence"] == 0

    session.append(
        ChatMessage.assistant(model="test", content="answer", reasoning_meta={"secret": "private"})
    )
    delta = await history.read(after=first["next_after"], limit=1)
    assert delta["incremental"] is True
    assert delta["history_reset"] is False
    assert [message["history_sequence"] for message in delta["messages"]] == [1]
    assert delta["messages"][0]["history_run_id"] == "run-one"
    assert "reasoning_meta" not in delta["messages"][0]
    assert delta["history_generation"] == first["history_generation"]

    replacement = ChatMessage.user("edited")
    session.apply_edit(user.id, [replacement])
    reset = await history.read(after=delta["next_after"], limit=10)
    assert reset["incremental"] is False
    assert reset["history_reset"] is True
    assert [message["id"] for message in reset["messages"]] == [replacement.id]
    assert reset["messages"][0]["history_sequence"] == 3


@pytest.mark.asyncio
async def test_unchanged_after_read_returns_only_the_cursor_in_one_worker_hop(
    history: _History, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = history.session()
    session.append(ChatMessage.user("Hello"))

    first = await history.read()
    assert {"session_usage", "background_bash_statuses", "compaction_policy"} <= set(first)
    assert first["reflection_runs"] == []
    # Read but absent: an explicit null clears the caller's value.
    assert first["context_usage"] is None

    # Performance contract (server map): one Session-pool read and no projection
    # of an unchanged page, observable only at the worker pools.
    session_hops: list[str] = []
    projection_hops: list[str] = []
    session_pool = history.sessions.run_async
    projection_pool = chat_methods._CHAT_RPC_WORKERS.run

    async def session_hop(function: Any, *args: Any, **kwargs: Any) -> Any:
        session_hops.append(function.__name__)
        return await session_pool(function, *args, **kwargs)

    async def projection_hop(function: Any, *args: Any, **kwargs: Any) -> Any:
        projection_hops.append(function.__name__)
        return await projection_pool(function, *args, **kwargs)

    def unexpected(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("an unchanged read must not recompute Session facts")

    monkeypatch.setattr(history.sessions, "run_async", session_hop)
    monkeypatch.setattr(chat_methods._CHAT_RPC_WORKERS, "run", projection_hop)
    monkeypatch.setattr(chat_methods, "_session_compaction_policy", unexpected)
    monkeypatch.setattr(chat_methods, "_read_reflection_runs", unexpected)
    monkeypatch.setattr(chat_methods, "background_bash_statuses", unexpected)

    unchanged = await history.read(session_id=session.id, after=first["next_after"])

    assert session_hops == ["_read_chat_history"]
    assert projection_hops == []
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
    appended = await history.read(session_id=session.id, after=first["next_after"])
    assert [message["content"] for message in appended["messages"]] == ["Reply"]
    assert appended["incremental"] is True
    assert {"session_usage", "context_usage", "compaction_policy"} <= set(appended)
    assert appended["background_bash_statuses"] == {}
    # Reflection Runs arrive with a full read; live events keep them current.
    assert "reflection_runs" not in appended


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "params",
    [{"before": "message-missing"}, {"limit": chat_methods.MAX_CHAT_HISTORY_LIMIT + 1}],
)
async def test_history_rejects_an_unknown_cursor_or_an_oversized_page(
    history: _History, params: dict[str, Any]
) -> None:
    history.session().append(_user(1))

    response = await call(history.state, "chat.history", agent_id="coder", **params)

    assert response["ok"] is False
    assert response["error"]["code"] == "invalid_request"


# ---------------------------------------------------------------------------
# Live state: active Run, reflection Runs, Compaction Policy
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_history_includes_the_active_run_descriptor(history: _History) -> None:
    session = history.session()
    started = asyncio.Event()
    release = asyncio.Event()

    async def execute(_run: Any) -> str:
        started.set()
        await release.wait()
        return "done"

    active_run = await history.state.chat_runs.start(session.address, execute)
    await started.wait()
    try:
        result = await history.read()
    finally:
        release.set()
        await active_run.wait()

    descriptor = result["active_run"]
    assert descriptor["run_id"] == active_run.id
    assert descriptor["agent_id"] == "coder"
    assert descriptor["session_id"] == "session-one"
    assert descriptor["status"] == "running"
    assert descriptor["sse_url"] == f"/api/runs/{active_run.id}/events"
    assert [event["type"] for event in descriptor["events"]] == ["run_started"]


@pytest.mark.asyncio
async def test_history_never_pairs_an_earlier_page_with_a_later_idle_state(
    history: _History, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = history.session()
    release = asyncio.Event()

    async def execute(_run: Any) -> str:
        await release.wait()
        session.append(ChatMessage.assistant(model="openai/test", content="Completed answer"))
        return "done"

    run = await history.state.chat_runs.start(session.address, execute)
    original = history.sessions.run_async
    injected = False

    async def complete_during_the_durable_read(function: Any, *args: Any, **kwargs: Any) -> Any:
        nonlocal injected
        result = await original(function, *args, **kwargs)
        if function.__name__ == "_read_chat_history" and not injected:
            injected = True
            release.set()
            await run.wait()
        return result

    monkeypatch.setattr(history.sessions, "run_async", complete_during_the_durable_read)
    try:
        result = await history.read()
        assert injected
        assert result.get("active_run", {}).get("run_id") == run.id or any(
            message.get("content") == "Completed answer" for message in result["messages"]
        )
    finally:
        release.set()
        await run.wait()


@pytest.mark.asyncio
async def test_reflection_runs_restore_running_and_durable_reviews(
    history: _History, tmp_path: Path
) -> None:
    source = history.session("source")
    fork = await history.sessions.fork(source.address, run_kind=RunKind.SKILL_REFLECTION)
    release = asyncio.Event()

    async def execute(review: Any) -> str:
        await release.wait()
        # What the review saved; its row reports it once it finished.
        history.state.runtime.memory.add_entry(
            tmp_path / "agents" / "coder" / "workspace",
            "user",
            "Prefers short answers.",
            writer=MemoryWriter(agent_id="coder", actor="tool", run_id=review.id),
        )
        return "done"

    run = await history.state.chat_runs.start(
        fork.address,
        execute,
        admission=RunAdmission(
            run_kind=RunKind.SKILL_REFLECTION,
            contributes_to_agent_activity=False,
            source_session_id=source.id,
        ),
    )
    try:
        live = await call(history.state, "chat.reflections", agent_id="coder", session_id="source")
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
        durable = await call(history.state, method, agent_id="coder", session_id="source")
        [row] = durable["result"]["reflection_runs"]
        assert (row["run_id"], row["status"]) == (run.id, "completed")
        assert row["outcome"] == {"memory": 1, "skills": 0, "undone": False}
    history.session("unrelated")
    unrelated = await call(
        history.state, "chat.reflections", agent_id="coder", session_id="unrelated"
    )
    assert unrelated["result"]["reflection_runs"] == []


@pytest.mark.asyncio
async def test_history_reports_the_session_effective_compaction_policy(history: _History) -> None:
    session = history.session()
    session.append(ChatMessage.user("Hello"))

    inherited = await history.read()
    assert inherited["compaction_policy"]["enabled"] is True
    assert inherited["compaction_policy"]["trigger"] == {"type": "context_ratio", "threshold": 0.8}

    history.agents.compaction_policy = {
        "enabled": False,
        "trigger": {"type": "context_ratio", "threshold": 0.6},
        "strategy": {"type": "continuation"},
    }
    assert (await history.read())["compaction_policy"]["enabled"] is False

    session_policy = {
        "enabled": True,
        "trigger": {"type": "input_tokens", "tokens": 50_000},
        "strategy": {"type": "continuation"},
    }
    history.sessions.mutate_metadata(
        session.address,
        lambda metadata: metadata.__setitem__("compaction_policy", session_policy),
    )
    current = await history.read()
    assert current["compaction_policy"]["enabled"] is True
    assert current["compaction_policy"]["trigger"] == {"type": "input_tokens", "tokens": 50_000}

    older = await history.read(before=current["messages"][0]["id"])
    assert "compaction_policy" not in older


@pytest.mark.asyncio
async def test_history_omits_the_policy_when_the_agent_no_longer_resolves(
    history: _History,
) -> None:
    session = history.sessions.create("former", project_id="vbot", session_id="orphaned")
    session.append(ChatMessage.user("Hello"))

    result = await history.read(agent_id="former@vbot", session_id="orphaned")

    assert result["messages"]
    assert "compaction_policy" not in result


@pytest.mark.asyncio
async def test_subagent_inspect_dispatches_the_exact_qualified_work_address() -> None:
    calls: list[tuple[str, str, str, str | None]] = []

    class Subagents:
        async def inspect(
            self, agent_id: str, session_id: str, work_id: str, *, project_id: str | None = None
        ) -> dict[str, Any]:
            calls.append((agent_id, session_id, work_id, project_id))
            return {"id": work_id, "status": "completed", "result": "done"}

    state = SimpleNamespace(runtime=SimpleNamespace(subagents=Subagents()))

    response = await call(
        state,
        "subagent.inspect",
        id="sub-work-one",
        agent_id="worker@project-one",
        session_id="child-session",
    )

    assert response["result"]["result"] == "done"
    assert calls == [("worker", "child-session", "sub-work-one", "project-one")]
