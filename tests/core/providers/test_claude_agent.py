"""Claude Agent's public adapter and its native process ownership contract."""

from __future__ import annotations

import asyncio
import contextlib
import copy
from types import SimpleNamespace
from typing import Any, override

import pytest
from claude_agent_sdk import AssistantMessage, TextBlock, ToolUseBlock

from core.providers import _claude_agent_sessions as sessions_module
from core.providers._claude_agent_sdk import PendingTools, login_info
from core.providers._claude_agent_sessions import ClaudeAgentSessions
from core.providers._claude_agent_wire import StreamState, record
from core.providers.claude_agent import ClaudeAgentAdapter
from core.providers.errors import ProviderAuthError, ProviderError, ProviderUsageLimitError
from core.providers.providers import AuthConfig, ConnectionConfig, ProviderConfig
from tests.core.providers.claude_agent_test_support import FakeNative

TOOLS: list[dict[str, Any]] = [
    {
        "name": "echo",
        "description": "Echo the given count",
        "parameters": {
            "type": "object",
            "properties": {"count": {"type": "integer"}},
            "required": ["count"],
        },
    }
]
MESSAGES: list[dict[str, Any]] = [
    {"role": "system", "content": "Synthetic vBot Agent identity"},
    {"role": "user", "content": "Work"},
]


@pytest.fixture
def native_registry(monkeypatch: pytest.MonkeyPatch) -> ClaudeAgentSessions:
    from claude_agent_sdk import ClaudeAgentOptions, HookMatcher

    FakeNative.instances = []
    monkeypatch.setattr(
        sessions_module,
        "load_sdk",
        lambda: SimpleNamespace(ClaudeAgentOptions=ClaudeAgentOptions, HookMatcher=HookMatcher),
    )
    monkeypatch.setattr(sessions_module, "executable", lambda: "C:/synthetic/claude.exe")
    return ClaudeAgentSessions(native_factory=FakeNative)  # type: ignore[arg-type]


def adapter(registry: ClaudeAgentSessions) -> ClaudeAgentAdapter:
    return ClaudeAgentAdapter(
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


async def collect(client: ClaudeAgentAdapter, messages: list[dict], **kwargs: Any) -> list[dict]:
    return [part async for part in client.stream(messages, model_id="opus", **kwargs)]


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_count", [1, 2])
async def test_tool_calls_wait_for_the_next_masked_request_and_keep_vbot_prompt(
    native_registry: ClaudeAgentSessions,
    monkeypatch: pytest.MonkeyPatch,
    tool_count: int,
) -> None:
    monkeypatch.setattr(FakeNative, "tool_count", tool_count)
    client = adapter(native_registry)
    context = client.request_context_kwargs(
        agent_id="agent", session_id="session", session_generation_id="one"
    )
    original = copy.deepcopy(MESSAGES)
    try:
        first = await collect(client, MESSAGES, tools=TOOLS, **context)
        native = FakeNative.instances[0]
        assert first[-1] == {"type": "finish", "reason": "tool_calls"}
        calls = [part for part in first if part["type"] == "tool_call_delta"]
        assert len(calls) == tool_count
        assert all(call["arguments_delta"] == '{"count": "2"}' for call in calls)
        assert [part for part in first if part["type"] == "usage"][-1]["output_tokens"] == 3
        assert not native.result_received.is_set()
        assert native.options.system_prompt == MESSAGES[0]["content"]
        assert native.options.tools == [] and native.options.setting_sources == []
        assert native.options.strict_mcp_config and native.options.verbatim_prompts
        assert native.options.extra_args == {"no-session-persistence": None}
        second_messages = MESSAGES + [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": f"tool-{index + 1}", "name": "echo", "arguments": {"count": "2"}}
                    for index in range(tool_count)
                ],
            },
            *[
                {"role": "tool", "tool_call_id": f"tool-{index + 1}", "content": "masked result"}
                for index in range(tool_count)
            ],
        ]
        second = await collect(client, second_messages, tools=TOOLS, **context)
        assert native.result_received.is_set()
        assert [part["text"] for part in second if part["type"] == "content_delta"] == ["Done"]
        usage = [part for part in second if part["type"] == "usage"][-1]
        assert usage["input_tokens"] == 12
        assert usage["output_tokens"] == 4
        assert usage["reported_cost_usd"] == 0
        assert original == MESSAGES
        await client.aclose()
        assert not native.closed
    finally:
        await native_registry.aclose()
    assert all(native.closed for native in FakeNative.instances)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stop_reason,message_stop,expected",
    [
        ("max_tokens", True, "output_truncated"),
        ("model_context_window_exceeded", True, "output_truncated"),
        (None, True, "unknown"),
        (None, False, None),
    ],
)
async def test_cut_tool_messages_close_native_state_instead_of_parking_a_successful_batch(
    native_registry: ClaudeAgentSessions,
    stop_reason: str | None,
    message_stop: bool,
    expected: str | None,
) -> None:
    cut_reason, has_message_stop = stop_reason, message_stop

    class CutNative(FakeNative):
        @override
        async def response(
            self,
            message_id: str,
            blocks: list[Any],
            usage: dict[str, int],
            stop_reason: str | None,
            *,
            message_stop: bool = True,
        ) -> None:
            await super().response(
                message_id, blocks, usage, cut_reason, message_stop=has_message_stop
            )
            if not has_message_stop:
                await self.events.put({"type": "result", "subtype": "success", "is_error": False})

    registry = ClaudeAgentSessions(native_factory=CutNative)  # type: ignore[arg-type]
    client = adapter(registry)
    context = client.request_context_kwargs(agent_id="a", session_id="s")
    try:
        if expected is None:
            with pytest.raises(ProviderError) as failure:
                await collect(client, MESSAGES, tools=TOOLS, **context)
            assert not failure.value.retryable
        else:
            parts = await collect(client, MESSAGES, tools=TOOLS, **context)
            assert parts[-1] == {"type": "finish", "reason": expected}
            assert sum(part["type"] == "finish" for part in parts) == 1
        assert FakeNative.instances[-1].closed
        assert not FakeNative.instances[-1].result_received.is_set()
    finally:
        await registry.aclose()


@pytest.mark.asyncio
async def test_history_edits_generations_and_utility_calls_have_isolated_native_state(
    native_registry: ClaudeAgentSessions,
) -> None:
    client = adapter(native_registry)
    context = client.request_context_kwargs(
        agent_id="a", session_id="s", session_generation_id="one"
    )
    try:
        await collect(client, MESSAGES, **context)
        continued = MESSAGES + [
            {"role": "assistant", "content": "Done"},
            {"role": "user", "content": "Next"},
        ]
        await collect(client, continued, **context)
        assert len(FakeNative.instances) == 1
        assert FakeNative.instances[0].sent[-1][0] == [{"type": "text", "text": "Next"}]
        changed = copy.deepcopy(continued)
        changed[1]["content"] = "Changed"
        await collect(client, changed, **context)
        assert FakeNative.instances[0].closed
        assert len(FakeNative.instances) == 2
        assert any("role" in block.get("text", "") for block in FakeNative.instances[-1].sent[0][0])
        generation = client.request_context_kwargs(
            agent_id="a", session_id="s", session_generation_id="two"
        )
        await collect(client, MESSAGES, **generation)
        assert len(FakeNative.instances) == 3
        utility = client.request_context_kwargs(
            agent_id="a", session_id="s", request_kind="utility"
        )
        assert utility == {}
        await collect(client, MESSAGES, **utility)
        assert FakeNative.instances[-1].closed
        await native_registry.close_sessions({("a", "s")})
        assert all(native.closed for native in FakeNative.instances)
    finally:
        await native_registry.aclose()


@pytest.mark.asyncio
async def test_probe_discovers_native_context_capacity_without_model_input(
    native_registry: ClaudeAgentSessions,
) -> None:
    result = await native_registry.probe()
    assert result["connected"] and result["subscription"]
    assert result["models"] == [{"value": "opus", "contextWindow": 1_000_000}]
    assert FakeNative.instances[0].sent == [] and FakeNative.instances[0].closed
    await native_registry.aclose()


def test_sdk_dataclasses_stream_usage_and_terminal_errors_are_normalized() -> None:
    state = StreamState({"mcp__vbot__echo": "echo"}, subscription=False)
    message = AssistantMessage(
        content=[TextBlock("Answer"), ToolUseBlock("call", "mcp__vbot__echo", {"count": 2})],
        model="opus",
        usage={
            "input_tokens": 10,
            "cache_read_input_tokens": 20,
            "cache_creation_input_tokens": 3,
            "output_tokens": 4,
        },
    )
    parts = state.consume(record(message))
    assert parts[0]["input_tokens"] == 33 and "reported_cost_usd" not in parts[0]
    assert parts[1] == {"type": "content_delta", "text": "Answer"}
    assert parts[2]["name_delta"] == "echo"
    with pytest.raises(ProviderAuthError):
        state.consume(
            {"type": "result", "subtype": "success", "is_error": True, "api_error_status": 401}
        )
    with pytest.raises(ProviderUsageLimitError) as limited:
        state.consume(
            {"type": "result", "subtype": "success", "is_error": True, "api_error_status": 429}
        )
    assert not limited.value.retryable
    with pytest.raises(ProviderError) as failure:
        state.consume(
            {
                "type": "result",
                "subtype": "success",
                "is_error": True,
                "api_error_status": 529,
                "result": "secret",
            }
        )
    assert not failure.value.retryable and "secret" not in str(failure.value)


@pytest.mark.parametrize(
    "account,expected",
    [
        ({"tokenSource": "oauth", "apiProvider": "firstParty"}, True),
        ({"subscriptionType": "Claude Team", "apiProvider": "firstParty"}, True),
        ({"tokenSource": "none", "apiKeySource": "env", "apiProvider": "firstParty"}, False),
        ({"tokenSource": "cloud", "apiProvider": "bedrock"}, False),
    ],
)
def test_native_login_status_never_reports_api_keys_as_subscriptions(
    account: dict, expected: bool
) -> None:
    result = login_info(
        {"claude_code_version": "2.1.257", "account": {**account, "email": "private"}, "models": []}
    )
    assert result["connected"] and result["subscription"] is expected
    assert "private" not in str(result)


def test_context_estimator_reserves_media_instead_of_counting_base64(
    native_registry: ClaudeAgentSessions,
) -> None:
    client = adapter(native_registry)
    small = MESSAGES + [
        {
            "role": "user",
            "content": [{"type": "media", "media_type": "image/png", "base64": "x" * 100}],
        }
    ]
    large = copy.deepcopy(small)
    large[-1]["content"][0]["base64"] = "x" * 100_000
    assert client.estimate_request_input_tokens(
        small, model_id="opus"
    ) == client.estimate_request_input_tokens(large, model_id="opus")


@pytest.mark.asyncio
async def test_native_client_owns_connect_disconnect_and_cancellation_in_one_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from claude_agent_sdk import ClaudeAgentOptions, HookMatcher

    from core.providers._claude_agent_sessions import NativeSession

    connected: list[Any] = []
    sent = asyncio.Event()
    closed = asyncio.Event()

    class Client:
        def __init__(self, options: Any) -> None:
            self.options = options

        async def connect(self) -> None:
            connected.append(asyncio.current_task())

        async def disconnect(self) -> None:
            assert asyncio.current_task() is connected[0]
            closed.set()

        async def get_server_info(self) -> dict:
            return {
                "claude_code_version": "2.1.257",
                "account": {"tokenSource": "oauth", "apiProvider": "firstParty"},
                "models": [],
            }

        async def receive_messages(self) -> Any:
            queue: asyncio.Queue[Any] = asyncio.Queue()
            while True:
                yield await queue.get()

        async def query(self, messages: Any) -> None:
            async for _ in messages:
                sent.set()

        async def interrupt(self) -> None:
            # This fake acknowledges cancellation without a final native result.
            raise ProviderError("synthetic interrupt failure", retryable=False)

    sdk = SimpleNamespace(
        ClaudeAgentOptions=ClaudeAgentOptions, HookMatcher=HookMatcher, ClaudeSDKClient=Client
    )
    monkeypatch.setattr(sessions_module, "load_sdk", lambda: sdk)
    monkeypatch.setattr(sessions_module, "executable", lambda: "C:/synthetic/claude.exe")
    registry = ClaudeAgentSessions(native_factory=NativeSession)
    client = adapter(registry)
    context = client.request_context_kwargs(agent_id="a", session_id="s")
    task = asyncio.create_task(collect(client, MESSAGES, **context))
    try:
        await sent.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert closed.is_set()
    finally:
        await registry.aclose()


@pytest.mark.asyncio
async def test_real_sdk_mcp_bridge_preserves_schema_and_parks_tolerant_calls() -> None:
    from claude_agent_sdk._internal.sdk_mcp_bridge import SdkMcpBridge

    pending = PendingTools(TOOLS)
    bridge = SdkMcpBridge("vbot", pending.mcp_server()["instance"])
    request: asyncio.Task[Any] | None = None
    try:
        await bridge.handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1"},
                },
            }
        )
        await bridge.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
        listed = await bridge.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        assert listed is not None
        assert listed["result"]["tools"][0]["inputSchema"] == TOOLS[0]["parameters"]
        await pending.before(
            {"hook_event_name": "PreToolUse", "tool_use_id": "t1", "tool_name": "mcp__vbot__echo"},
            None,
            {},
        )
        request = asyncio.create_task(
            bridge.handle(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {"name": "echo", "arguments": {"count": "2"}},
                }
            )
        )
        # Fulfilment comes only from the canonical next request, independent of schema mismatch.
        pending.resolve({"tool_call_id": "t1", "content": "masked result"})
        result = await request
        assert not result["result"].get("isError", False)
        assert result["result"]["content"][0]["text"] == "masked result"
        assert pending.waiting == {}
    finally:
        if request and not request.done():
            request.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await request
        await bridge.aclose()


def test_sync_abort_terminates_only_the_sdk_owned_process() -> None:
    from claude_agent_sdk import ClaudeAgentOptions
    from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport

    from core.providers._claude_agent_sdk import abort_client

    killed = []
    process = SimpleNamespace(returncode=None, kill=lambda: killed.append(True))
    transport = SubprocessCLITransport(
        prompt="", options=ClaudeAgentOptions(cli_path="C:/synthetic/claude.exe")
    )
    assert hasattr(transport, "_process")
    transport._process = process  # type: ignore[assignment]
    abort_client(SimpleNamespace(_transport=transport))
    assert killed == [True]
    process.returncode = 0
    abort_client(SimpleNamespace(_transport=SimpleNamespace(_process=process)))
    abort_client(SimpleNamespace())
    assert killed == [True]


@pytest.mark.asyncio
@pytest.mark.parametrize("request_kind", ["chat", "probe"])
@pytest.mark.parametrize("shutdown", ["abort", "aclose"])
async def test_shutdown_during_capacity_eviction_starts_no_native_process(
    native_registry: ClaudeAgentSessions,
    monkeypatch: pytest.MonkeyPatch,
    request_kind: str,
    shutdown: str,
) -> None:
    monkeypatch.setattr(sessions_module, "MAX_SESSIONS", 1)
    client = adapter(native_registry)
    first = client.request_context_kwargs(agent_id="a", session_id="first")
    second = client.request_context_kwargs(agent_id="a", session_id="second")
    await collect(client, MESSAGES, **first)
    native = FakeNative.instances[0]
    evicting = asyncio.Event()
    release = asyncio.Event()
    stopped = native.stop

    async def blocked_stop() -> None:
        evicting.set()
        await release.wait()
        await stopped()

    monkeypatch.setattr(native, "stop", blocked_stop)
    request: asyncio.Task[Any]
    if request_kind == "probe":
        request = asyncio.create_task(native_registry.probe())
    else:
        request = asyncio.create_task(collect(client, MESSAGES, **second))
    closing: asyncio.Task[None] | None = None
    try:
        await evicting.wait()
        if shutdown == "abort":
            native_registry.abort()
        else:
            closing_started = asyncio.Event()

            async def close() -> None:
                closing_started.set()
                await native_registry.aclose()

            closing = asyncio.create_task(close())
            await closing_started.wait()
        release.set()
        if request_kind == "probe":
            result = await request
            assert not result["connected"]
        else:
            with pytest.raises(ProviderError):
                await request
        if closing:
            await closing
        assert len(FakeNative.instances) == 1 and native.closed
        # A Run can still hold this Adapter after Runtime replaces its owner.
        with pytest.raises(ProviderError):
            await collect(client, MESSAGES, **first)
        assert not (await native_registry.probe())["connected"]
        assert len(FakeNative.instances) == 1
    finally:
        release.set()
        if not request.done():
            request.cancel()
        with contextlib.suppress(asyncio.CancelledError, ProviderError):
            await request
        if closing:
            await closing
        await native_registry.aclose()


@pytest.mark.asyncio
async def test_probe_reports_busy_without_replacing_native_login_or_parked_tools(
    native_registry: ClaudeAgentSessions,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sessions_module, "MAX_SESSIONS", 1)
    client = adapter(native_registry)
    context = client.request_context_kwargs(agent_id="a", session_id="s")
    try:
        await collect(client, MESSAGES, tools=TOOLS, **context)
        native = FakeNative.instances[0]
        result = await native_registry.probe()
        assert result["status"] == "busy"
        assert len(FakeNative.instances) == 1 and not native.closed
        assert native.info["connected"] and native.info["subscription"]
        assert not native.pending.waiting["tool-1"].done()
    finally:
        await native_registry.aclose()


@pytest.mark.asyncio
async def test_api_or_cloud_login_never_sends_a_model_request(
    native_registry: ClaudeAgentSessions,
) -> None:
    class BilledNative(FakeNative):
        @override
        async def start(self) -> dict:
            self.info["subscription"] = False
            return self.info

    registry = ClaudeAgentSessions(native_factory=BilledNative)  # type: ignore[arg-type]
    client = adapter(registry)
    try:
        with pytest.raises(ProviderAuthError):
            await collect(client, MESSAGES)
        assert len(FakeNative.instances) == 1
        native = FakeNative.instances[0]
        assert native.sent == [] and native.closed
    finally:
        await registry.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("with_tools", [False, True])
async def test_closing_the_stream_at_its_finish_releases_native_ownership(
    native_registry: ClaudeAgentSessions,
    with_tools: bool,
) -> None:
    client = adapter(native_registry)
    context = client.request_context_kwargs(agent_id="a", session_id="s")
    stream = client.stream(MESSAGES, model_id="opus", tools=TOOLS if with_tools else [], **context)
    try:
        async for delta in stream:
            if delta["type"] == "finish":
                break
        await stream.aclose()
        await client.aclose()
        assert FakeNative.instances[0].closed is with_tools
        if not with_tools:
            continued = MESSAGES + [
                {"role": "assistant", "content": "Done"},
                {"role": "user", "content": "Next"},
            ]
            next_client = adapter(native_registry)
            await collect(next_client, continued, **context)
            assert len(FakeNative.instances) == 1
            assert FakeNative.instances[0].sent[-1][0] == [{"type": "text", "text": "Next"}]
            await next_client.aclose()
    finally:
        await stream.aclose()
        await native_registry.aclose()


@pytest.mark.asyncio
async def test_runtime_close_drains_independent_native_processes_in_parallel(
    native_registry: ClaudeAgentSessions,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = adapter(native_registry)
    for session_id in ("first", "second"):
        await collect(
            client, MESSAGES, **client.request_context_kwargs(agent_id="a", session_id=session_id)
        )
    released = asyncio.Event()
    started: list[asyncio.Event] = []
    for native in FakeNative.instances:
        entered = asyncio.Event()
        started.append(entered)

        async def stop(native: FakeNative = native, entered: asyncio.Event = entered) -> None:
            entered.set()
            await released.wait()
            native.closed = True

        monkeypatch.setattr(native, "stop", stop)
    closing = asyncio.create_task(native_registry.aclose())
    try:
        async with asyncio.timeout(1):
            await asyncio.gather(*(entered.wait() for entered in started))
        assert not any(native.closed for native in FakeNative.instances)
    finally:
        released.set()
        await closing
    assert all(native.closed for native in FakeNative.instances)


@pytest.mark.asyncio
@pytest.mark.parametrize("startup_failure", [False, True])
async def test_concurrent_native_stop_and_cancelled_waiter_finish_one_disconnect(
    startup_failure: bool,
) -> None:
    from core.providers._claude_agent_sessions import NativeSession

    disconnect_started = asyncio.Event()
    release_disconnect = asyncio.Event()
    disconnected = asyncio.Event()
    interrupted = asyncio.Event()
    owners: list[Any] = []

    class Client:
        def __init__(self, options: Any) -> None:
            pass

        async def connect(self) -> None:
            owners.append(asyncio.current_task())
            if startup_failure:
                raise ProviderError("Synthetic startup failure", retryable=False)

        async def get_server_info(self) -> dict:
            return {
                "claude_code_version": "2.1.257",
                "account": {"subscriptionType": "Claude Max"},
                "models": [],
            }

        async def receive_messages(self) -> Any:
            queue: asyncio.Queue[Any] = asyncio.Queue()
            while True:
                yield await queue.get()

        async def disconnect(self) -> None:
            assert asyncio.current_task() is owners[0]
            disconnect_started.set()
            try:
                await release_disconnect.wait()
            except asyncio.CancelledError:
                interrupted.set()
                raise
            disconnected.set()

    native = NativeSession(None, PendingTools([]), SimpleNamespace(ClaudeSDKClient=Client))
    if startup_failure:
        with pytest.raises(ProviderError):
            await native.start()
    else:
        await native.start()
    first = asyncio.create_task(native.stop())
    await disconnect_started.wait()
    second_started = asyncio.Event()

    async def stop_again() -> None:
        second_started.set()
        await native.stop()

    second = asyncio.create_task(stop_again())
    try:
        await second_started.wait()
        first.cancel()
    finally:
        release_disconnect.set()
        with pytest.raises(asyncio.CancelledError):
            await first
        await second
        await native.stop()
    assert disconnected.is_set() and not interrupted.is_set()
