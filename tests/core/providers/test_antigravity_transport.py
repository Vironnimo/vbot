"""Antigravity's public subprocess boundary: isolate, stream, and hand back denied Tools."""

from __future__ import annotations

import asyncio
import io
import json
import os
from contextlib import aclosing
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio

from core.providers import _antigravity_bridge as bridge
from core.providers import _antigravity_transport as transport
from core.providers.errors import ProviderError
from core.storage import TemporaryFileManager
from core.utils import python_process

_MODEL = "model-low"
_ARGS = {"sample": "value"}
_DEFINITION = {
    "name": "signal",
    "description": "Return a sample",
    "inputSchema": {"type": "object", "properties": {"sample": {"type": "string"}}},
}


class _Input(bytearray):
    def write(self, data: bytes) -> None:
        self.extend(data)

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        pass


class _Child:
    def __init__(self) -> None:
        self.stdin = _Input()
        self.stdout = asyncio.StreamReader()
        self.returncode: int | None = None

    def event(self, kind: str, **payload: Any) -> None:
        self.stdout.feed_data(json.dumps({"event": kind, kind: payload}).encode() + b"\n")


@pytest_asyncio.fixture
async def child(monkeypatch: pytest.MonkeyPatch) -> _Child:
    process = _Child()

    async def kill(target: _Child) -> None:
        assert target is process
        target.returncode = -1
        target.stdout.feed_eof()

    async def wait(target: _Child) -> int | None:
        return target.returncode

    monkeypatch.setattr(transport, "kill_process_tree_async", kill)
    monkeypatch.setattr(transport, "wait_for_exit", wait)
    return process


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        "call",
        "mixed",
        "replay",
        "missing_replay_file",
        "missing_replay_step",
        "missing_receipt",
        "missing_denial",
        "pending_native",
        "duplicate_id",
        "missing_guard",
        "missing_inventory",
        "fallback",
    ],
)
async def test_turn_requires_native_isolation_and_confirmed_tool_denial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, child: _Child, case: str
) -> None:
    workspace: list[Path] = []
    parts = (
        ('{"role":"user","content":"älter"}', '{"role":"tool","content":"📁"}')
        if "replay" in case
        else ()
    )
    runtime = tmp_path / "vBot package" / "runtime"
    monkeypatch.setattr(
        python_process,
        "sys",
        SimpleNamespace(platform="win32", executable=str(runtime / "vBot.Server.exe")),
    )

    async def launch(*args: str, cwd: Path, env: dict[str, str], **_: Any) -> _Child:
        root = cwd.parent
        workspace.append(root)
        assert "--disable-slash-commands" in args
        assert "--dangerously-skip-permissions" not in args
        assert Path(env["HOME"]) == root / "home"
        assert env["PATH"].split(os.pathsep)[0] == str(runtime)
        assert env["AGY_CLI_DISABLE_AUTO_UPDATE"] == "true"
        mcp = json.loads((root / "home/.gemini/config/mcp_config.json").read_text())
        assert mcp["mcpServers"]["vbot"]["command"] == str(runtime / "python.exe")
        hooks = json.loads((cwd / ".agents/hooks.json").read_text())["vbot"]
        assert hooks["PreInvocation"][0]["command"].startswith("python.exe -I ")
        assert hooks["PreToolUse"][0]["hooks"][0]["command"].startswith("python.exe -I ")
        settings = json.loads(Path(args[args.index("--settings") + 1]).read_text())
        assert settings["useG1Credits"] is False and settings["toolPermission"] == "strict"
        assert set(settings["permissions"]["deny"]) == {
            "command(*)",
            "write_file(*)",
            "read_file(*)",
            "mcp(*)",
            "read_url(*)",
            "execute_url(*)",
            "download_url(*)",
        }
        assert (cwd / ".git/HEAD").is_file()
        assert (cwd / ".agents/agents/vbot/agent.md").read_text().endswith("Full\n# vBot")
        assert json.loads((root / "tools.json").read_text()) == [_DEFINITION]
        child.event(
            "init",
            agent="default" if case == "fallback" else "vbot",
            permission_mode="strict",
            cwd=str(cwd),
            model=_MODEL,
        )
        child.event("step_update", step_type="user_input", state="DONE", step_index=0)
        if case != "missing_inventory":
            (root / "listed").touch()
        if case not in {"missing_guard", "missing_inventory"}:
            assert hooks["PreInvocation"][0]["command"].endswith(
                "../.. replay" if parts else "../.."
            )
            if case == "missing_replay_file":
                (root / "replay.json").unlink()
                assert bridge._guard("before-tools", root, replay=True) == {
                    "terminationBehavior": "terminate"
                }
            else:
                assert bridge._guard("before-tools", root, replay=bool(parts)) == (
                    {"injectSteps": [{"userMessage": part} for part in parts]} if parts else {}
                )
                assert bridge._guard("before-tools", root, replay=bool(parts)) == {
                    "terminationBehavior": "terminate"
                }
                for index in range(1, len(parts) + 1):
                    # A repeated acknowledgement cannot stand in for a missing step.
                    child.event(
                        "step_update",
                        step_type="user_input",
                        state="DONE",
                        step_index=1 if case == "missing_replay_step" else index,
                    )
        child.event("step_update", step_type="agent_response", state="ACTIVE", text_delta="text")

        def tool_step(index: int, tool: str, call_id: str) -> None:
            # The native name can first arrive in the terminal step update.
            child.event("step_update", step_type="tool", step_index=index, state="ACTIVE")
            if case == "pending_native":
                return
            event = {
                "stepIdx": index,
                "toolCall": {
                    "name": tool,
                    "args": {**_ARGS, "toolSummary": "native"},
                    "id": call_id,
                },
            }
            if case != "missing_receipt":
                monkeypatch.setattr(bridge.sys, "stdin", io.StringIO(json.dumps(event)))
                assert bridge._guard("guard", root) == {"decision": "deny"}
            child.event(
                "step_update",
                step_type="tool",
                step_index=index,
                tool_name=tool,
                state="DONE" if case == "missing_denial" else "ERROR",
                tool_info={
                    "parameters": _ARGS,
                    "error": {"message": "tool call denied by pre-tool hook:"},
                },
            )

        if case in {"mixed", "duplicate_id"}:
            # Same canonical name as the advertised Tool, but a native call, completed first.
            tool_step(3, "signal", "c1" if case == "duplicate_id" else "c2")
        tool_step(
            2,
            "manage_task"
            if case in {"missing_receipt", "missing_denial", "pending_native"}
            else "mcp_vbot_signal",
            "c1",
        )
        child.event(
            "result",
            status="SUCCESS",
            response="text",
            usage={
                "input_tokens": 5,
                "cache_read_tokens": 10,
                "output_tokens": 4,
                "thinking_tokens": 2,
            },
        )
        return child

    monkeypatch.setattr(transport, "create_subprocess_exec", launch)
    stream = transport.stream_turn(
        "/agy",
        model=_MODEL,
        system_prompt="Full\n# vBot",
        prompt="ask",
        tools=[_DEFINITION],
        temporary_files=TemporaryFileManager(tmp_path),
        replay_steps=parts,
    )
    if case in {"call", "mixed", "replay"}:
        deltas = [delta async for delta in stream]
        if case == "mixed":
            native = deltas.pop(-2)
            rejection = native.pop("rejection")
            assert native == {
                "type": "tool_call_delta",
                "id": "c2",
                "name_delta": "signal",
                "arguments_delta": json.dumps(_ARGS),
            }
            assert rejection["code"] == "tool_unavailable"
            assert "vbot MCP server" in rejection["message"]
            assert rejection["fingerprint"]
        assert deltas == [
            {"type": "content_delta", "text": "text"},
            {
                "type": "usage",
                "reported_cost_usd": 0.0,
                "input_tokens": 15,
                "output_tokens": 4,
                "reasoning_tokens": 2,
                "cache_read_tokens": 10,
            },
            {
                "type": "tool_call_delta",
                "id": "c1",
                "name_delta": "signal",
                "arguments_delta": json.dumps(_ARGS),
            },
            {"type": "finish", "reason": "tool_calls"},
        ]
    else:
        expected = {
            "missing_guard": "pre-invocation hook",
            "missing_inventory": "Tool inventory",
            "missing_replay_step": "complete vBot conversation",
            "missing_replay_file": "pre-invocation hook",
        }
        seen = []
        with pytest.raises(ProviderError, match=expected.get(case)):
            async for delta in stream:
                seen.append(delta)
        assert all(delta["type"] != "tool_call_delta" for delta in seen)
    if case == "fallback":
        assert not child.stdin
    else:
        assert json.loads(child.stdin)["message"] == {"content": "ask"}
    assert child.returncode is not None
    assert not workspace[0].exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("wire_input", "cache_read", "model_input", "checkpoint"),
    [
        (5, 10, 15, None),
        (5, 10, 15, {"input_tokens": 3, "output_tokens": 1}),
        (0, 0, None, {"input_tokens": 3, "output_tokens": 1}),
        (None, 0, None, {}),
        (0, 10, 10, {"input_tokens": 3, "output_tokens": 1}),
    ],
)
async def test_stream_delivers_model_usage_before_the_native_turn_total(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    child: _Child,
    wire_input: int | None,
    cache_read: int,
    model_input: int | None,
    checkpoint: dict[str, int] | None,
) -> None:
    agent_usage = {"output_tokens": 4, "cache_read_tokens": cache_read}
    if wire_input is not None:
        agent_usage["input_tokens"] = wire_input

    async def launch(*_args: str, cwd: Path, **_kwargs: Any) -> _Child:
        child.event("init", agent="vbot", permission_mode="strict", cwd=str(cwd), model=_MODEL)
        (cwd.parent / "invocation").touch()
        child.event(
            "step_update",
            step_type="agent_response",
            state="DONE",
            usage=agent_usage,
        )
        return child

    monkeypatch.setattr(transport, "create_subprocess_exec", launch)
    async with aclosing(
        transport.stream_turn(
            "/agy",
            model=_MODEL,
            system_prompt="prompt",
            prompt="turn",
            tools=[],
            temporary_files=TemporaryFileManager(tmp_path),
        )
    ) as stream:
        model_counts = {"cache_read_tokens": cache_read, "output_tokens": 4}
        if model_input is not None:
            model_counts["input_tokens"] = model_input
        assert await anext(stream) == {"type": "usage", "reported_cost_usd": 0.0, **model_counts}
        if checkpoint is not None:
            child.event(
                "step_update",
                step_type="checkpoint",
                state="DONE",
                usage=checkpoint,
            )
        # A turn can supply input even when the physical model step omitted it.
        total_input = (wire_input or 0) + (3 if checkpoint is not None else 0)
        total_output = 5 if checkpoint else 4
        child.event(
            "result",
            status="SUCCESS",
            response="",
            usage={
                "input_tokens": total_input,
                "cache_read_tokens": cache_read,
                "output_tokens": total_output,
            },
        )
        if checkpoint is not None:
            assert await anext(stream) == {
                "type": "usage",
                "reported_cost_usd": 0.0,
                "input_tokens": total_input + cache_read,
                "cache_read_tokens": cache_read,
                "output_tokens": total_output,
                "last_request_usage": model_counts,
            }
        assert [event async for event in stream] == [{"type": "finish", "reason": "stop"}]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    [
        "",
        "wrong_conversation",
        "spent_tokens",
        "old_ack",
        "missing_guard",
        "ran_model",
        "missing_baseline",
    ],
)
async def test_native_continuation_restores_packets_without_spending_or_reusing_receipts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, child: _Child, fault: str
) -> None:
    native_id = str(uuid4())
    starts: list[Path] = []
    counts = {
        "input_tokens": 10,
        "cache_read_tokens": 4,
        "output_tokens": 5,
        "thinking_tokens": 3,
        "total_tokens": 15,
    }

    async def launch(*args: str, cwd: Path, **_: Any) -> _Child:
        turn = len(starts)
        state = Path(args[args.index("--settings") + 1]).parent
        assert state.parent == cwd.parent and not (state / "invocation").exists()
        assert not any(path.exists() for path in starts)
        starts.append(state)
        assert ("--conversation" in args) == (turn > 0)
        if turn:
            assert args[args.index("--conversation") + 1] == native_id
        identifier = str(uuid4()) if turn == 1 and fault == "wrong_conversation" else native_id
        child.stdout.feed_data(
            json.dumps(
                {
                    "event": "init",
                    "conversation_id": identifier,
                    "init": {
                        "agent": "vbot",
                        "permission_mode": "strict",
                        "cwd": str(cwd),
                        "model": _MODEL,
                    },
                }
            ).encode()
            + b"\n"
        )
        child.returncode = 0
        hooks = json.loads((cwd / ".agents/hooks.json").read_text())["vbot"]
        assert state.name in hooks["PreInvocation"][0]["command"]
        assert state.name in hooks["PreToolUse"][0]["hooks"][0]["command"]
        offset = [0, 2, 6][turn]
        if turn:
            child.event("step_update", step_type="system_message", step_index=offset, state="DONE")
            offset += 1
        child.event("step_update", step_type="user_input", step_index=offset, state="DONE")
        if turn == 1 and fault != "missing_baseline":
            if fault != "missing_guard":
                assert bridge._guard("seed", state, replay=True) == {
                    "injectSteps": [{"userMessage": "history-A"}, {"userMessage": "history-B"}],
                    "terminationBehavior": "terminate",
                }
            child.event("step_update", step_type="user_input", step_index=offset + 1, state="DONE")
            child.event(
                "step_update",
                step_type="user_input",
                step_index=1 if fault == "old_ack" else offset + 2,
                state="DONE",
            )
            if fault == "ran_model":
                child.event(
                    "step_update", step_type="agent_response", step_index=offset + 3, state="ACTIVE"
                )
        else:
            assert bridge._guard("before", state) == {}
            child.event(
                "step_update",
                step_type="agent_response",
                step_index=offset + 1,
                state="DONE",
                usage=counts,
            )
        total = {k: v * (2 if turn == 2 else 1) for k, v in counts.items()}
        if fault == "missing_baseline":
            total = {} if turn == 0 else {k: v * 2 for k, v in counts.items()}
        if turn == 1 and fault == "spent_tokens":
            total["input_tokens"] += 1
        child.event(
            "result", status="SUCCESS", conversation_id=identifier, response="", usage=total
        )
        return child

    monkeypatch.setattr(transport, "create_subprocess_exec", launch)
    manager = TemporaryFileManager(tmp_path)
    with manager.provider_directory() as root:
        # Markers from a previous subprocess cannot authorize a new one.
        (root / "invocation").touch()
        conversation = transport.NativeConversation(root)

        async def request(*batch: tuple[str, ...]) -> list[dict[str, Any]]:
            return [
                event
                async for event in transport.stream_turn(
                    "/agy",
                    model=_MODEL,
                    system_prompt="test",
                    prompt="next",
                    tools=[],
                    temporary_files=manager,
                    conversation=conversation,
                    seed_batches=batch,
                )
            ]

        expected = [
            {
                "type": "usage",
                "reported_cost_usd": 0.0,
                "input_tokens": 14,
                "cache_read_tokens": 4,
                "output_tokens": 5,
                "reasoning_tokens": 3,
            },
            {"type": "finish", "reason": "stop"},
        ]
        assert await request() == expected
        assert conversation.conversation_id == native_id
        if fault == "missing_baseline":
            assert await request() == expected
        elif fault:
            with pytest.raises(ProviderError):
                await request(("history-A", "history-B"))
            assert len(starts) == 2
        else:
            assert await request(("history-A", "history-B")) == expected
            assert len(starts) == 3
        assert not any(path.exists() for path in starts)


@pytest.mark.asyncio
async def test_cancelling_a_turn_during_process_creation_reaps_it_and_releases_its_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    child: _Child,
) -> None:
    spawning, finish = asyncio.Event(), asyncio.Event()
    roots: list[Path] = []

    async def launch(*_args: str, cwd: Path, **_kwargs: Any) -> _Child:
        roots.append(cwd.parent)
        spawning.set()
        await finish.wait()
        return child

    monkeypatch.setattr(transport, "create_subprocess_exec", launch)
    async with aclosing(
        transport.stream_turn(
            "/agy",
            model=_MODEL,
            system_prompt="prompt",
            prompt="turn",
            tools=[],
            temporary_files=TemporaryFileManager(tmp_path),
        )
    ) as stream:
        first = asyncio.create_task(anext(stream))
        await spawning.wait()
        first.cancel()
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await first
    assert child.returncode is not None
    assert not roots[0].exists()
