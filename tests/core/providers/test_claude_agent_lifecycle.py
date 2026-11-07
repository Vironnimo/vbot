"""Native request files and bounded, reconstructible process admission."""

from __future__ import annotations

import asyncio
import copy
from pathlib import Path
from types import SimpleNamespace
from typing import Any, override

import claude_agent_sdk as sdk
import pytest

from core.providers import _claude_agent_sessions as sessions_module
from core.providers._claude_agent_sdk import PendingTools, options
from core.providers._claude_agent_sessions import ClaudeAgentSessions, NativeSession
from core.providers.errors import ProviderError
from core.storage import TemporaryFileManager
from tests.core.providers.claude_agent_test_support import (
    MESSAGES,
    TOOLS,
    FakeNative,
    adapter,
    collect,
)
from tests.core.providers.claude_agent_test_support import (
    native_registry as native_registry,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("startup_failure", [False, True])
async def test_native_prompt_file_avoids_argv_limits_and_obeys_private_lease_lifetime(
    tmp_path: Path, startup_failure: bool
) -> None:
    from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport

    prompt = "Synthetic private prompt. " * 3000
    owner: list[Any] = []

    class Client:
        def __init__(self, options: Any) -> None:
            self.options = options

        async def connect(self) -> None:
            owner.append(asyncio.current_task())
            if startup_failure:
                raise sdk.ProcessError("Synthetic startup crash", exit_code=1)

        async def get_server_info(self) -> dict:
            return {
                "claude_code_version": "2.1.302",
                "account": {"subscriptionType": "pro"},
                "models": [],
            }

        async def receive_messages(self) -> Any:
            queue: asyncio.Queue[Any] = asyncio.Queue()
            while True:
                yield await queue.get()

        async def disconnect(self) -> None:
            assert asyncio.current_task() is owner[0]

    manager = TemporaryFileManager(tmp_path)
    native = NativeSession(
        options(sdk, "not-launched", system=prompt),
        PendingTools([]),
        SimpleNamespace(ClaudeSDKClient=Client, ProcessError=sdk.ProcessError),
        temporary_files=manager,
    )
    try:
        if startup_failure:
            with pytest.raises(ProviderError) as error:
                await native.start()
            assert error.value.retryable
        else:
            await native.start()
        configured = native.client.options
        assert configured.system_prompt["type"] == "file"
        prompt_file = Path(configured.system_prompt["path"])
        command = SubprocessCLITransport(prompt="", options=configured)._build_command()
        assert "--system-prompt-file" in command and "--system-prompt" not in command
        assert not any(prompt in argument for argument in command)
        assert len(" ".join(command)) < 32_767
        assert configured.env["DISABLE_AUTOUPDATER"] == "1"
        if not startup_failure:
            assert prompt_file.read_text(encoding="utf-8") == prompt
            manager.sweep()
            assert prompt_file.is_file()
            cwd = Path(configured.cwd)
            assert cwd.is_relative_to(manager.root) and not any(cwd.iterdir())
    finally:
        await native.stop()
    assert not prompt_file.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "visible_carrier", [None, "block_start", "assistant", "prior_step", "native_error"]
)
async def test_native_process_crashes_retry_only_before_current_step_output(
    tmp_path: Path, visible_carrier: str | None
) -> None:
    class Client:
        def __init__(self, options: Any) -> None:
            pass

        async def connect(self) -> None:
            pass

        async def get_server_info(self) -> dict:
            return {
                "claude_code_version": "2.1.302",
                "account": {"subscriptionType": "pro"},
                "models": [],
            }

        async def receive_messages(self) -> Any:
            if visible_carrier == "block_start":
                yield {
                    "type": "stream_event",
                    "event": {
                        "type": "content_block_start",
                        "content_block": {"type": "text", "text": "Visible text"},
                    },
                }
            elif visible_carrier == "assistant":
                yield {"type": "assistant", "message": {"content": [{"type": "text", "text": "X"}]}}
            elif visible_carrier == "native_error":
                yield {"type": "assistant", "error": "rate_limit", "content": []}
            raise sdk.ProcessError("Synthetic private diagnostic", exit_code=1)

        async def disconnect(self) -> None:
            pass

    native = NativeSession(
        options(sdk, "not-launched"),
        PendingTools([]),
        SimpleNamespace(ClaudeSDKClient=Client, ProcessError=sdk.ProcessError),
        temporary_files=TemporaryFileManager(tmp_path),
    )
    if visible_carrier == "prior_step":
        native._visible_output = True
        native._native_failure = True
        native.begin_step()
    try:
        await native.start()
        while True:
            event = await native.events.get()
            if isinstance(event, ProviderError):
                assert event.retryable is (visible_carrier in {None, "prior_step"})
                assert "private diagnostic" not in str(event)
                break
    finally:
        await native.stop()


@pytest.mark.asyncio
async def test_parked_process_is_evictable_and_reconstructs_from_completed_canonical_tools(
    native_registry: ClaudeAgentSessions, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sessions_module, "MAX_SESSIONS", 1)

    class RebuildingNative(FakeNative):
        @override
        async def send(self, blocks: list[dict], *, priority: bool = False) -> None:
            if any('"role": "assistant"' in block.get("text", "") for block in blocks):
                self.sent.append((copy.deepcopy(blocks), priority))
                await self.answer()
            else:
                await super().send(blocks, priority=priority)

    registry = ClaudeAgentSessions(native_factory=RebuildingNative)  # type: ignore[arg-type]
    client = adapter(registry)
    first = client.request_context_kwargs(agent_id="a", session_id="first")
    second = client.request_context_kwargs(agent_id="a", session_id="second")
    try:
        await collect(client, MESSAGES, tools=TOOLS, **first)
        parked = FakeNative.instances[0]
        await collect(client, MESSAGES, **second)
        assert parked.closed and not parked.result_received.is_set()
        continued = MESSAGES + [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "tool-1", "name": "echo", "arguments": {"count": "2"}}],
            },
            {"role": "tool", "tool_call_id": "tool-1", "content": "masked result"},
        ]
        parts = await collect(client, continued, tools=TOOLS, **first)
        assert parts[-1] == {"type": "finish", "reason": "stop"}
        rebuilt = FakeNative.instances[-1]
        assert rebuilt is not parked
        assert any("masked result" in block.get("text", "") for block in rebuilt.sent[0][0])
    finally:
        await registry.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("resolution", ["release", "cancel", "timeout"])
async def test_full_active_pool_waits_cancellably_and_wakes_when_a_request_completes(
    native_registry: ClaudeAgentSessions, monkeypatch: pytest.MonkeyPatch, resolution: str
) -> None:
    monkeypatch.setattr(sessions_module, "MAX_SESSIONS", 1)
    entered, release_first, waiting = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class HoldingNative(FakeNative):
        @override
        async def send(self, blocks: list[dict], *, priority: bool = False) -> None:
            if self is FakeNative.instances[0]:
                entered.set()
                await release_first.wait()
            await super().send(blocks, priority=priority)

    registry = ClaudeAgentSessions(native_factory=HoldingNative)  # type: ignore[arg-type]
    client = adapter(registry)
    first = asyncio.create_task(
        collect(client, MESSAGES, **client.request_context_kwargs(agent_id="a", session_id="first"))
    )
    original_wait = registry._capacity_changed.wait

    async def wait() -> bool:
        waiting.set()
        return await original_wait()

    monkeypatch.setattr(registry._capacity_changed, "wait", wait)
    second: asyncio.Task[Any] | None = None
    try:
        await entered.wait()
        if resolution == "timeout":
            monkeypatch.setattr(sessions_module, "CAPACITY_WAIT_SECONDS", 0)
        second = asyncio.create_task(
            collect(
                client, MESSAGES, **client.request_context_kwargs(agent_id="a", session_id="second")
            )
        )
        await waiting.wait()
        assert len(FakeNative.instances) == 1
        if resolution == "release":
            assert (await registry.probe())["status"] == "busy"
        if resolution == "cancel":
            second.cancel()
            with pytest.raises(asyncio.CancelledError):
                await second
        elif resolution == "timeout":
            with pytest.raises(sessions_module.ClaudeAgentBusyError) as error:
                await second
            assert error.value.retryable
        release_first.set()
        await first
        if resolution == "release":
            await second
            assert len(FakeNative.instances) == 2 and FakeNative.instances[0].closed
        else:
            assert len(FakeNative.instances) == 1
    finally:
        release_first.set()
        first.cancel()
        if second is not None:
            second.cancel()
        await asyncio.gather(
            first, *([second] if second is not None else []), return_exceptions=True
        )
        await registry.aclose()
