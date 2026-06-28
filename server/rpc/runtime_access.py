"""Runtime service access helpers for RPC handlers."""

from __future__ import annotations

from typing import Any, cast

from core.chat import ChatLoop, CommandDispatcher
from core.chat.content_blocks import ContentBlock
from core.runs import ChatRunManager, RunExecutor


def _streaming_chat_loop(state: Any) -> ChatLoop:
    return cast(ChatLoop, state.streaming_chat_loop)


def _build_streaming_queue_update(
    state: Any,
    agent_id: str,
    session_id: str,
    content: str | list[ContentBlock],
    *,
    input_origin: str | None = None,
    project_id: str | None = None,
) -> tuple[str, RunExecutor, str]:
    streaming_chat_loop = _streaming_chat_loop(state)
    return cast(
        tuple[str, RunExecutor, str],
        streaming_chat_loop.build_queue_update(
            agent_id,
            session_id,
            content,
            input_origin=cast(Any, input_origin),
            project_id=project_id,
        ),
    )


def _state_chat_runs(state: Any) -> ChatRunManager:
    return cast(ChatRunManager, state.chat_runs)


def _state_command_dispatcher(state: Any) -> CommandDispatcher:
    return cast(CommandDispatcher, state.command_dispatcher)
