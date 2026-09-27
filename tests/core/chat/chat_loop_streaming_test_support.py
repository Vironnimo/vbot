"""Shared builders for streaming chat loop tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from core.chat import ChatMessage
from core.runs import PROVIDER_REQUEST_STATUS_EVENT, Run
from tests.core.chat.chat_loop_support import (
    StubAdapter,
    StubAgent,
    StubRuntime,
    build_chat_loop,
    session_address,
)

JsonObject = dict[str, Any]

SESSION_ID = "session-one"


def answer(text: str = "Recovered", *, reasoning: str | None = None) -> list[JsonObject]:
    """One complete streamed answer, optionally preceded by readable reasoning."""
    deltas: list[JsonObject] = []
    if reasoning is not None:
        deltas.append({"type": "reasoning_delta", "text": reasoning})
    return [*deltas, {"type": "content_delta", "text": text}, {"type": "finish", "reason": "stop"}]


def stream_runtime(
    tmp_path: Path,
    adapter: StubAdapter,
    *,
    model: str = "openai/gpt-5.2",
    **runtime_options: Any,
) -> Any:
    """A StubRuntime whose Agent ``coder`` may use every registered Tool."""
    agent = StubAgent(id="coder", model=model, allowed_tools=["*"])
    return StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter, **runtime_options)


async def send_streaming(runtime: Any, message: str = "Hi") -> ChatMessage:
    return await build_chat_loop(runtime, streaming=True).send(
        "coder", message, session_id=SESSION_ID
    )


def history(runtime: Any, session_id: str = SESSION_ID) -> list[ChatMessage]:
    return cast(
        list[ChatMessage], runtime.chat_sessions.get(session_address("coder", session_id)).load()
    )


def last_run(runtime: Any, session_id: str = SESSION_ID) -> Run:
    """The Run that wrote the Session's latest Run summary."""
    summary = next(m for m in reversed(history(runtime, session_id)) if m.role == "run_summary")
    return cast(Run, runtime.chat_runs.get(summary.run_id))


def event_types(run: Run) -> list[str]:
    """Run event types without the Provider request status updates."""
    return [event.type for event in run.events if event.type != PROVIDER_REQUEST_STATUS_EVENT]
