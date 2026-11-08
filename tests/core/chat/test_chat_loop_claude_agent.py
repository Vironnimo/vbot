"""The native Claude Provider through vBot's complete Chat and Tool cycle."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast, override

import pytest
from claude_agent_sdk import ClaudeAgentOptions, HookMatcher

from core.providers import _claude_agent_sessions as sessions_module
from core.providers.claude_agent import ClaudeAgentAdapter, ClaudeAgentSessions
from core.providers.providers import AuthConfig, ConnectionConfig, ProviderConfig
from core.runs import (
    ASSISTANT_OUTPUT_DELTA_EVENT,
    MODEL_STEP_USAGE_EVENT,
    TOOL_CALL_RESULT_EVENT,
    Run,
    RunCancelledError,
)
from core.secrets.agent_secrets import SecretMasker
from core.tools import ToolContext, ToolRegistry, tool_success
from tests.core.chat.chat_loop_support import StubModels, build_chat_loop, history, last_run
from tests.core.chat.chat_loop_tools_test_support import tool_runtime
from tests.core.providers.claude_agent_test_support import (
    FakeNative,
)
from tests.core.providers.claude_agent_test_support import adapter as native_adapter
from tests.core.providers.claude_agent_test_support import (
    native_registry as native_registry,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_count", [1, 2])
async def test_native_tool_calls_use_vbot_normalization_masking_and_stream_accounting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tool_count: int
) -> None:
    raw_result = "synthetic-private-value"
    masker = SecretMasker({raw_result: "masked result"})

    class MaskedTools(ToolRegistry):
        @property
        @override
        def secret_masker(self) -> SecretMasker:
            return masker

    dispatched: list[dict[str, Any]] = []

    async def echo(_context: ToolContext, arguments: dict[str, Any]) -> dict[str, Any]:
        dispatched.append(dict(arguments))
        return tool_success({"output": raw_result})

    tools = MaskedTools()
    tools.register(
        "echo",
        "Echo the given count",
        {
            "type": "object",
            "properties": {"count": {"type": "integer"}},
            "required": ["count"],
            "additionalProperties": False,
        },
        echo,
    )
    FakeNative.instances = []
    monkeypatch.setattr(FakeNative, "tool_count", tool_count)
    monkeypatch.setattr(
        sessions_module,
        "load_sdk",
        lambda: SimpleNamespace(ClaudeAgentOptions=ClaudeAgentOptions, HookMatcher=HookMatcher),
    )
    monkeypatch.setattr(sessions_module, "executable", lambda: "C:/synthetic/claude.exe")
    registry = ClaudeAgentSessions(native_factory=FakeNative)  # type: ignore[arg-type]
    adapter = ClaudeAgentAdapter(
        ProviderConfig(
            id="claude-agent",
            name="Claude Agent",
            adapter="claude_agent",
            base_url="",
            connections=[
                ConnectionConfig(
                    id="subscription",
                    type="external",
                    label="Claude Code login",
                    auth=AuthConfig(header="", prefix=""),
                )
            ],
        ),
        sessions=registry,
    )
    runtime = tool_runtime(
        tmp_path,
        tools,
        [],
        model="claude-agent/opus::subscription",
        allowed_tools=["echo"],
        adapter=adapter,
        adapters_by_connection={"claude-agent:subscription": adapter},
        models=StubModels({("claude-agent", "opus"): 1_000_000}),
    )
    prompt = "Synthetic vBot Agent identity supplied by the normal prompt builder"
    runtime.system_prompts.build_system_prompt = lambda *_args, **_kwargs: prompt
    try:
        answer = await build_chat_loop(runtime).send("coder", "Work", session_id="session-one")

        assert answer.content == "Done"
        # Native Claude supplied a string against an integer schema. The
        # canonical dispatch owner recognizes its meaning and executes once.
        assert dispatched == [{"count": 2}] * tool_count
        assert len(FakeNative.instances) == 1
        native = FakeNative.instances[0]
        assert native.result_received.is_set()
        assert native.options.system_prompt.startswith(prompt)
        assert native.options.tools == []
        assert native.options.setting_sources == []
        assert len(native.sent) == 1

        messages = history(runtime)
        results = [message for message in messages if message.role == "tool"]
        assert [result.tool_call_id for result in results] == [
            f"tool-{index + 1}" for index in range(tool_count)
        ]
        for result in results:
            assert json.loads(cast(str, result.content)) == tool_success(
                {"output": "masked result"}
            )
        assert raw_result not in str([message.to_dict() for message in messages])
        assistants = [message for message in messages if message.role == "assistant"]
        assert len(assistants[0].tool_calls or []) == tool_count
        usages = []
        for message in assistants:
            assert message.usage is not None
            usages.append(message.usage)
        assert [usage["input_tokens"] for usage in usages] == [30, 12]
        assert [usage["output_tokens"] for usage in usages] == [3, 4]

        run = last_run(runtime)
        events = await runtime.timelines.events(run)
        assert sum(event.type == "tool_call_started" for event in events) == tool_count
        assert sum(event.type == TOOL_CALL_RESULT_EVENT for event in events) == tool_count
        assert sum(event.type == MODEL_STEP_USAGE_EVENT for event in events) == 2
        assert events[-1].type == "run_completed"
    finally:
        await registry.aclose()
    assert all(native.closed for native in FakeNative.instances)


@pytest.mark.asyncio
async def test_cancel_between_native_stream_reads_stops_the_native_conversation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    native_registry: ClaudeAgentSessions,
) -> None:
    adapter = native_adapter(native_registry)
    original_stream = adapter.stream
    retained_streams = []

    def retain_stream(*args: Any, **kwargs: Any) -> Any:
        source = original_stream(*args, **kwargs)
        # Keep the iterator alive: native cleanup must not depend on GC.
        retained_streams.append(source)
        return source

    monkeypatch.setattr(adapter, "stream", retain_stream)
    original_emit = Run.emit

    def stop_after_content(run: Run, event_type: str, *args: Any, **kwargs: Any) -> Any:
        result = original_emit(run, event_type, *args, **kwargs)
        if event_type == ASSISTANT_OUTPUT_DELTA_EVENT:
            run.request_cancel("user")
        return result

    monkeypatch.setattr(Run, "emit", stop_after_content)
    runtime = tool_runtime(
        tmp_path,
        ToolRegistry(),
        [],
        model="claude-agent/opus::subscription",
        allowed_tools=[],
        adapter=adapter,
        adapters_by_connection={"claude-agent:subscription": adapter},
        models=StubModels({("claude-agent", "opus"): 1_000_000}),
    )
    runtime.chat_sessions.create("coder", session_id="session-one")
    try:
        run = await build_chat_loop(runtime).start_run("coder", "Work", session_id="session-one")
        with pytest.raises(RunCancelledError):
            await run.wait()

        assert len(FakeNative.instances) == 1
        assert FakeNative.instances[0].closed
        assistants = [message for message in history(runtime) if message.role == "assistant"]
        assert len(assistants) == 1
        assert assistants[0].content == "Done"
        assert assistants[0].interrupted
        assert assistants[0].interruption_cause == "user"
    finally:
        for source in retained_streams:
            await source.aclose()
        await native_registry.aclose()
