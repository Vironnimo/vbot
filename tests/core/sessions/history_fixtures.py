"""Build relational test data from compact, readable conversation fixtures.

Fixture summaries declare complete Runs: each one completes its admitted Run the
way the Run manager does. Missing Tool declarations are supplied here so
search/report tests can focus on the result they exercise, and Tool results
carry the outcome facts Chat derives from their envelopes. This builder is
test-only; production writes must already identify their Run and invocation.
"""

from dataclasses import replace
from datetime import datetime

from core.chat._step_outcomes import tool_result_facts
from core.chat.messages import ChatMessage, ToolCall
from core.runs import Run, RunKind
from core.sessions import ChatSession, ChatSessionManager, SessionAddress
from core.sessions._types import SessionRunCompletion
from core.utils.ids import new_id


async def admit_run(
    manager: ChatSessionManager,
    address: SessionAddress,
    run_kind: RunKind = RunKind.USER,
    *,
    run_id: str | None = None,
) -> Run:
    """Admit a Run of *run_kind* the way the Run manager does; it stays running.

    Admission is what records a Session's Run kind.
    """
    run = Run(
        run_id=run_id or new_id("run"),
        agent_id=address.agent_id,
        session_id=address.session_id,
        project_id=address.project_id,
        run_kind=run_kind,
    )
    await manager.start_run(run)
    return run


def history_revision(manager: ChatSessionManager, address: SessionAddress) -> int:
    """Return a live Session's history revision from the batched freshness lookup."""
    return manager.list_history_versions([address])[address][1]


def complete_run(session: ChatSession, summary: ChatMessage) -> ChatMessage:
    """Complete *summary*'s admitted Run with its fields; return the stored summary.

    The store writes the summary entry itself, so the returned Message carries
    the stored id and canonical timing.
    """
    assert summary.role == "run_summary" and summary.run_id and summary.timing is not None
    assert summary.status is not None
    session._store.finish_run(
        session.address,
        SessionRunCompletion(
            run_id=summary.run_id,
            status=summary.status,
            timing=summary.timing,
            iteration_count=summary.iteration_count or 0,
            work_id=summary.work_id,
            change_stats=summary.change_stats,
        ),
    )
    return session.load()[-1]


def seed_history(session: ChatSession, messages: list[ChatMessage]) -> None:
    offset = 0
    while offset < len(messages):
        end = next(
            (i + 1 for i in range(offset, len(messages)) if messages[i].role == "run_summary"),
            len(messages),
        )
        segment = messages[offset:end]
        summary = segment[-1] if segment[-1].role == "run_summary" else None
        run_id = summary.run_id if summary else new_id("run")
        assert run_id is not None
        writer = session.start_run(run_id)
        # Declare shorthand results on their preceding assistant Model step.
        parent_index = None
        for index, message in enumerate(segment):
            if message.role == "assistant":
                parent_index = index
            elif message.role == "tool" and parent_index is not None:
                parent = segment[parent_index]
                calls = list(parent.tool_calls or [])
                if not any(call.id == message.tool_call_id for call in calls):
                    assert message.tool_call_id and message.name
                    calls.append(ToolCall(id=message.tool_call_id, name=message.name))
                    segment[parent_index] = replace(parent, tool_calls=calls)
        declared: dict[str, ChatMessage] = {}
        for index, message in enumerate(segment, start=offset):
            message = replace(message, run_id=run_id)
            messages[index] = message
            if message.role == "assistant":
                declared.update((call.id, message) for call in message.tool_calls or [])
            if message.role == "tool":
                call_id = message.tool_call_id
                assert call_id is not None and message.name is not None
                assistant = declared.get(call_id)
                if assistant is None:
                    assistant = ChatMessage.assistant(
                        model="fixture",
                        content=None,
                        timestamp=datetime.fromisoformat(message.timestamp),
                        tool_calls=[ToolCall(id=call_id, name=message.name)],
                    )
                    assistant = replace(assistant, run_id=run_id)
                    writer.append(assistant)
                writer.assistant_message_id = assistant.id
            if message.role == "run_summary":
                messages[index] = complete_run(writer, message)
                continue
            if message.role == "tool":
                writer.append_many([message], tool_results=tool_result_facts([message]))
                continue
            writer.append(message)
        offset = end


def declare_tool(session: ChatSession, message: ChatMessage) -> None:
    """Declare the exact invocation a standalone result fixture will complete."""
    assert message.tool_call_id and message.name
    assistant = ChatMessage.assistant(
        model="fixture",
        content=None,
        timestamp=datetime.fromisoformat(message.timestamp),
        tool_calls=[ToolCall(id=message.tool_call_id, name=message.name)],
    )
    session.append(assistant)
    session.assistant_message_id = assistant.id


def append_tool_fixture(session: ChatSession, message: ChatMessage) -> None:
    declare_tool(session, message)
    session.append(message)
