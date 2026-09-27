"""Slash commands over ``chat.send`` / ``chat.stream``.

The RPC layer only routes a prepared command to Chat's command dispatcher and
projects the neutral outcome (reply, output hint, data, navigation, resource
changes, or a primary Run). Command workflows are Chat contracts tested under
``tests/core/chat/``; the real-stack ``/new`` and ``/compact`` tests below check
the RPC responses around the stores and the Compaction Run.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core.chat import (
    ChatMessage,
    CommandDispatcher,
    CommandExecutionContext,
    CommandFeedback,
    CommandNavigation,
    CommandOutcome,
    CommandResourceChange,
    ExtensionCommandContext,
    PreparedCommand,
    ReplySurface,
)
from core.chat.errors import ChatError
from core.runs import ChatRunManager
from core.sessions import SessionAddress
from tests.server.rpc.chat_methods_test_support import (
    _RecordingLoop,
    call,
    chat_state,
    resource_changes,
)
from tests.server.rpc_test_support import (
    JsonObject,
    RecordingCompactionService,
    StubAdapter,
    make_state,
)

WEBUI = ReplySurface.webui()


def test_transport_layers_do_not_own_command_workflows() -> None:
    root = Path(__file__).parents[3]
    chat_source = (root / "server" / "rpc" / "chat_methods.py").read_text(encoding="utf-8")
    channel_source = (root / "core" / "channels" / "engine.py").read_text(encoding="utf-8")
    combined = f"{chat_source}\n{channel_source}"

    for forbidden in (
        "Command" + "Action",
        "Command" + "Handled",
        "Dispatch" + "Result",
        "_handle_command_" + "action",
        "unsupported command " + "action",
    ):
        assert forbidden not in combined
    for server_owned_workflow in (
        "HANDOFF_FRAGMENT_NAME",
        "LEARN_FRAGMENT_NAME",
        "AGENT_TAKEOVER_NOTE",
        "_build_handoff_prompt",
        "_build_learn_prompt",
        "_session_move_block_reason",
    ):
        assert server_owned_workflow not in chat_source
    for command in ("compact", "handoff", "learn", "reflect", "new", "rename", "model"):
        assert f'case "{command}"' not in combined


# ---------------------------------------------------------------------------
# Routing and outcome projection
# ---------------------------------------------------------------------------


class _NoticeDispatcher(CommandDispatcher):
    def __init__(self) -> None:
        super().__init__(ChatRunManager())
        self.calls: list[tuple[str, str, str, ReplySurface]] = []

    async def execute(
        self, prepared: PreparedCommand, context: CommandExecutionContext
    ) -> CommandOutcome:
        self.calls.append(
            (context.agent_id, context.session_id, f"/{prepared.name}", context.reply_surface)
        )
        return CommandOutcome(
            command=prepared.name, feedback=CommandFeedback(kind="notice", text="Run cancelled.")
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["chat.send", "chat.stream"])
async def test_slash_command_is_executed_instead_of_starting_a_run(method: str) -> None:
    loop = _RecordingLoop()
    state = chat_state(loop)
    state.command_dispatcher = _NoticeDispatcher()

    response = await call(
        state, method, agent_id="agent-1", session_id="session-1", content="/stop"
    )

    assert response == {
        "ok": True,
        "result": {"command_handled": True, "reply": "Run cancelled.", "output": "toast"},
    }
    assert state.command_dispatcher.calls == [("agent-1", "session-1", "/stop", WEBUI)]
    assert loop.start_calls == []


def _session_offer_outcome(
    context: ExtensionCommandContext, argument: str | None
) -> CommandOutcome:
    return CommandOutcome(
        command="workflow",
        feedback=CommandFeedback(kind="notice", text="Session is ready."),
        navigation=CommandNavigation(
            kind="offer_session", agent_id="reviewer", session_id="session-two", project_id="vbot"
        ),
    )


def _page_outcome(context: ExtensionCommandContext, argument: str | None) -> CommandOutcome:
    return CommandOutcome(
        command="workflow",
        feedback=CommandFeedback(kind="notice", text="Workflow is ready."),
        navigation=CommandNavigation(
            kind="open_extension_page", extension="fixture", page="overview", route="items/one"
        ),
    )


async def _detail_outcome(context: ExtensionCommandContext, argument: str | None) -> CommandOutcome:
    # A change reported while running and again in the outcome is published once.
    # (Async so the report reaches the bus on the Event Loop, not from a worker.)
    change = CommandResourceChange(kind="commands")
    context.report_change(change)
    return CommandOutcome(
        command="workflow",
        feedback=CommandFeedback(kind="detail", text=f"Workflow {argument} ready."),
        resource_changes=(change,),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("handler", "result", "changes"),
    [
        pytest.param(
            _detail_outcome,
            {"command_handled": True, "reply": "Workflow review ready.", "output": "transient"},
            [{"kind": "commands"}],
            id="detail",
        ),
        pytest.param(
            _page_outcome,
            {
                "command_handled": True,
                "reply": "Workflow is ready.",
                "output": "action",
                "data": {
                    "command": "workflow",
                    "navigation": {
                        "kind": "open_extension_page",
                        "extension": "fixture",
                        "page": "overview",
                        "route": "items/one",
                    },
                },
            },
            [],
            id="extension-page",
        ),
        pytest.param(
            _session_offer_outcome,
            {
                "command_handled": True,
                "reply": "Session is ready.",
                "output": "action",
                "data": {
                    "command": "workflow",
                    "session_id": "session-two",
                    "agent_id": "reviewer@vbot",
                },
            },
            [],
            id="session-offer",
        ),
    ],
)
async def test_command_outcome_is_projected_generically(
    handler: Any, result: JsonObject, changes: list[JsonObject]
) -> None:
    loop = _RecordingLoop()
    state = chat_state(loop)
    state.command_dispatcher.register_extension_command(
        "fixture",
        name="workflow",
        description="Start the workflow.",
        handler=handler,
        page_ids=frozenset({"overview"}),
    )

    response = await call(
        state, "chat.stream", agent_id="agent-1", session_id="session-1", content="/workflow review"
    )

    assert response == {"ok": True, "result": result}
    assert resource_changes(state, "commands") == changes
    assert loop.start_calls == []


# ---------------------------------------------------------------------------
# Built-in commands on the real stack
# ---------------------------------------------------------------------------


async def _occupy(state: SimpleNamespace, session_id: str = "session-one") -> Any:
    started = asyncio.Event()
    release = asyncio.Event()

    async def execute(_run: Any) -> str:
        started.set()
        await release.wait()
        return "done"

    run = await state.chat_runs.start(
        SessionAddress(project_id=None, agent_id="coder", session_id=session_id), execute
    )
    await started.wait()
    return run, release


@pytest.mark.asyncio
async def test_new_command_opens_and_selects_a_fresh_session(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())
    state.runtime.chat_sessions.create("coder", session_id="session-one")

    response = await call(
        state, "chat.send", agent_id="coder", session_id="session-one", content="/new"
    )

    result = response["result"]
    assert result["command_handled"] is True
    assert result["data"]["command"] == "new"
    new_session_id = result["data"]["session_id"]
    assert new_session_id != "session-one"
    assert state.runtime.agents.get("coder").current_session_id == new_session_id
    address = SessionAddress(project_id=None, agent_id="coder", session_id=new_session_id)
    assert state.runtime.chat_sessions.get(address).load() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("session_busy", [True, False], ids=["run-active", "no-service"])
async def test_compact_command_is_refused_with_a_toast(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, session_busy: bool
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    adapter = StubAdapter()
    compaction_service = RecordingCompactionService() if session_busy else None
    state = make_state(tmp_path, adapter, compaction_service=compaction_service)
    state.runtime.chat_sessions.create("coder", session_id="session-one")
    occupant = await _occupy(state) if session_busy else None
    try:
        response = await call(
            state, "chat.send", agent_id="coder", session_id="session-one", content=" /COMPACT "
        )
    finally:
        if occupant is not None:
            run, release = occupant
            release.set()
            await run.wait()

    result = response["result"]
    assert result["command_handled"] is True
    assert result["output"] == "toast"
    assert result["reply"]
    assert compaction_service is None or compaction_service.calls == 0
    assert adapter.requests == []
    assert adapter.stream_requests == []


@pytest.mark.asyncio
async def test_compact_command_returns_its_compaction_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    state = make_state(tmp_path, StubAdapter(), compaction_service=RecordingCompactionService())
    session = state.runtime.chat_sessions.create("coder", session_id="session-one")
    session.append(ChatMessage.user("Keep this context"))

    response = await call(
        state,
        "chat.stream",
        agent_id="coder",
        session_id="session-one",
        content="/compact keep the API design",
    )

    result = response["result"]
    assert "command_handled" not in result
    assert result["sse_url"] == f"/api/runs/{result['run_id']}/events"
    run = state.chat_runs.get(result["run_id"])
    checkpoint = await run.wait()
    assert checkpoint.role == "compaction_checkpoint"
    assert [event.type for event in run.events] == [
        "run_started",
        "compaction_started",
        "compaction_completed",
        "run_completed",
    ]
    completed = run.events[-2]
    assert completed.payload["message"]["content"] == "Compacted context"
    assert checkpoint.usage is not None
    assert completed.payload["context_tokens_before"] == checkpoint.usage["context_tokens_before"]
    assert completed.payload["context_tokens_after"] == checkpoint.usage["context_tokens_after"]
    assert completed.payload["context_tokens_after"] > 0
    assert completed.payload["checkpoint_id"] == checkpoint.id


@pytest.mark.asyncio
async def test_failed_compaction_run_fails_the_stream_run_and_the_send_request(
    tmp_path: Path,
) -> None:
    adapter = StubAdapter()
    compaction_service = RecordingCompactionService()
    state = make_state(tmp_path, adapter, compaction_service=compaction_service)
    state.runtime.chat_sessions.create("coder", session_id="session-one")
    state.runtime.agents.update("coder", model="")
    params = {"agent_id": "coder", "session_id": "session-one", "content": " /COMPACT "}

    streamed = await call(state, "chat.stream", **params)
    result = streamed["result"]
    assert result["status"] == "running"
    run = state.chat_runs.get(result["run_id"])
    with pytest.raises(ChatError):
        await run.wait()
    assert [event.type for event in run.events] == [
        "run_started",
        "compaction_started",
        "compaction_aborted",
        "run_failed",
    ]

    sent = await call(state, "chat.send", **params)
    assert sent["ok"] is False
    assert sent["error"]["code"] == "domain_error"
    assert compaction_service.calls == 0
    assert adapter.requests == []
    assert adapter.stream_requests == []
