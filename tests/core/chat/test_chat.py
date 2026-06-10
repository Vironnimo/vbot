"""Phase 2 chat validation tests."""

import asyncio
from pathlib import Path

import pytest

from core.chat import ChatMessage, ChatMessageValidationError
from core.chat.chat import ChatLoop, _validate_assistant_message
from core.runs import RunCancelledError
from core.tools import ToolContext, ToolRegistry, tool_success


def test_validate_assistant_message_allows_reasoning_only() -> None:
    message = ChatMessage.assistant(
        model="openai/gpt-5.2",
        content=None,
        reasoning="thinking only",
    )

    _validate_assistant_message(message)


def test_validate_assistant_message_allows_reasoning_meta_only() -> None:
    message = ChatMessage.assistant(
        model="openai/gpt-5.2",
        content=None,
        reasoning_meta={"provider": "opaque"},
    )

    _validate_assistant_message(message)


def test_validate_assistant_message_rejects_truly_empty_assistant() -> None:
    message = ChatMessage.assistant(
        model="openai/gpt-5.2",
        content=None,
    )

    with pytest.raises(ChatMessageValidationError, match="content, reasoning, reasoning_meta"):
        _validate_assistant_message(message)


@pytest.mark.asyncio
async def test_cancel_during_tool_dispatch_persists_all_sibling_tool_results(
    tmp_path: Path,
) -> None:
    # Arrange: three sibling tool calls. The run is cancelled after the
    # dispatch has returned all three results but before the persist loop
    # yields back to the outer agentic loop. The persist loop must record
    # every sibling result before honoring the cancel, so a later request
    # never sees a dangling tool_calls turn in the session history.
    from tests.core.chat.test_chat_loop import StubAdapter, StubAgent, StubRuntime

    def make_handler(label: str):
        async def handler(_context: ToolContext, _arguments: dict) -> dict:
            # Yield so the cancel task can race with dispatch. The
            # post-dispatch code path (the persist loop) must then record
            # all sibling results before honoring the cancel.
            await asyncio.sleep(0.5)
            return tool_success({"sibling": label})

        return handler

    tools = ToolRegistry()
    tools.register("first_tool", "Fast tool.", {"type": "object"}, make_handler("first"))
    tools.register("second_tool", "Fast tool.", {"type": "object"}, make_handler("second"))
    tools.register("third_tool", "Fast tool.", {"type": "object"}, make_handler("third"))

    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = StubAdapter(
        [
            {
                "content": None,
                "tool_calls": [
                    {"id": "call_first", "name": "first_tool", "arguments": {}},
                    {"id": "call_second", "name": "second_tool", "arguments": {}},
                    {"id": "call_third", "name": "third_tool", "arguments": {}},
                ],
            }
        ]
    )
    runtime = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter, tools=tools)
    runtime.chat_sessions.create("coder", session_id="session-one")

    # Act: start the run, then cancel it from a background task. The
    # cancel races with the in-flight tool dispatch; the persist loop
    # must persist all three sibling results before honoring the cancel.
    run = await ChatLoop(runtime).start_run("coder", "Multi", session_id="session-one")

    async def fire_cancel_after_dispatch() -> None:
        # Yield briefly so the chat loop runs the tool dispatch first.
        # Then flip the cancel flag directly (without cancelling the
        # chat-loop task) — that lets the gather finish and the persist
        # loop record every sibling result before `raise_if_cancelled`
        # is honored at the end of the persist block.
        await asyncio.sleep(0.1)
        run.cancel_requested = True

    cancel_task = asyncio.create_task(fire_cancel_after_dispatch())

    with pytest.raises(RunCancelledError):
        await run.wait()
    await cancel_task

    # Assert: all three tool results are persisted even though the run
    # ended cancelled, and the session never carries a dangling tool_calls
    # turn that would brick future provider requests.
    session = runtime.chat_sessions.get("coder", "session-one")
    persisted = session.load()
    tool_results = [message for message in persisted if message.role == "tool"]
    assert len(tool_results) == 3
    assert {message.tool_call_id for message in tool_results} == {
        "call_first",
        "call_second",
        "call_third",
    }
    # Run summary marks the run as cancelled.
    assert persisted[-1].role == "run_summary"
    assert persisted[-1].status == "cancelled"
