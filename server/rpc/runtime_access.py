"""Runtime service access helpers for RPC handlers."""

from __future__ import annotations

from typing import Any, cast

from core.chat import ChatError, ChatLoop, CommandDispatcher
from core.chat.content_blocks import ContentBlock
from core.runs import ChatRunManager, RunExecutor


def _streaming_chat_loop(state: Any) -> Any:
    chat_loop = getattr(state, "streaming_chat_loop", None)
    if chat_loop is not None:
        return chat_loop
    runtime = getattr(state, "runtime", None)
    if runtime is not None:
        try:
            chat_loop = runtime.streaming_chat_loop
        except AttributeError:
            chat_loop = getattr(runtime, "_streaming_chat_loop", None)
        except RuntimeError:
            if runtime.__class__.__name__ == "Runtime" and runtime.__class__.__module__.startswith(
                "core.runtime"
            ):
                raise
            chat_loop = getattr(runtime, "_streaming_chat_loop", None)
        if chat_loop is not None:
            state.streaming_chat_loop = chat_loop
            return chat_loop
    chat_loop = ChatLoop(state.runtime, streaming=True)
    state.streaming_chat_loop = chat_loop
    return chat_loop


def _build_streaming_queue_update(
    state: Any,
    agent_id: str,
    session_id: str,
    content: str | list[ContentBlock],
    input_origin: str | None = None,
) -> tuple[str, RunExecutor, str]:
    streaming_chat_loop = _streaming_chat_loop(state)
    build_queue_update = getattr(streaming_chat_loop, "build_queue_update", None)
    if not callable(build_queue_update):
        raise ChatError("streaming chat loop cannot update queued runs")
    if input_origin is None:
        return cast(tuple[str, RunExecutor, str], build_queue_update(agent_id, session_id, content))
    return cast(
        tuple[str, RunExecutor, str],
        build_queue_update(agent_id, session_id, content, input_origin=input_origin),
    )


def _state_chat_runs(state: Any) -> ChatRunManager:
    chat_runs = getattr(state, "chat_runs", None)
    if isinstance(chat_runs, ChatRunManager):
        return chat_runs

    runtime = getattr(state, "runtime", None)
    if runtime is not None:
        runtime_chat_runs = getattr(runtime, "chat_runs", None)
        if isinstance(runtime_chat_runs, ChatRunManager):
            state.chat_runs = runtime_chat_runs
            return runtime_chat_runs

        try:
            runtime_chat_runs = runtime.chat_run_manager
        except AttributeError:
            runtime_chat_runs = getattr(runtime, "_chat_run_manager", None)
        except RuntimeError:
            if runtime.__class__.__name__ == "Runtime" and runtime.__class__.__module__.startswith(
                "core.runtime"
            ):
                raise
            runtime_chat_runs = getattr(runtime, "_chat_run_manager", None)

        if isinstance(runtime_chat_runs, ChatRunManager):
            runtime.chat_runs = runtime_chat_runs
            state.chat_runs = runtime_chat_runs
            return runtime_chat_runs

    fallback_chat_runs = ChatRunManager()
    state.chat_runs = fallback_chat_runs
    if runtime is not None:
        runtime.chat_runs = fallback_chat_runs
    return fallback_chat_runs


def _state_command_dispatcher(state: Any) -> CommandDispatcher:
    command_dispatcher = getattr(state, "command_dispatcher", None)
    if isinstance(command_dispatcher, CommandDispatcher):
        return command_dispatcher

    runtime = getattr(state, "runtime", None)
    if runtime is not None:
        try:
            command_dispatcher = runtime.command_dispatcher
        except AttributeError:
            command_dispatcher = getattr(runtime, "_command_dispatcher", None)
        except RuntimeError:
            if runtime.__class__.__name__ == "Runtime" and runtime.__class__.__module__.startswith(
                "core.runtime"
            ):
                raise
            command_dispatcher = getattr(runtime, "_command_dispatcher", None)

        if isinstance(command_dispatcher, CommandDispatcher):
            state.command_dispatcher = command_dispatcher
            return command_dispatcher

    fallback_dispatcher = CommandDispatcher(ChatRunManager())
    state.command_dispatcher = fallback_dispatcher
    return fallback_dispatcher
