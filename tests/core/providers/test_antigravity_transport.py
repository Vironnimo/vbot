"""Antigravity's public subprocess boundary: isolate, stream, and hand back denied Tools."""

from __future__ import annotations

import asyncio
import io
import json
from contextlib import aclosing
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from core.providers import _antigravity_bridge as bridge
from core.providers import _antigravity_transport as transport
from core.providers.errors import ProviderError
from core.storage import TemporaryFileManager

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
@pytest.mark.parametrize("case", ["call", "native", "missing_receipt", "missing_guard", "fallback"])
async def test_turn_requires_native_isolation_and_confirmed_tool_denial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, child: _Child, case: str
) -> None:
    workspace: list[Path] = []
    tool = "manage_task" if case == "native" else "mcp_vbot_signal"

    async def launch(*args: str, cwd: Path, env: dict[str, str], **_: Any) -> _Child:
        root = cwd.parent
        workspace.append(root)
        assert "--disable-slash-commands" in args
        assert "--dangerously-skip-permissions" not in args
        assert Path(env["HOME"]) == root / "home"
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
        child.event("step_update", step_type="user_input", state="DONE")
        (root / "listed").touch()
        if case != "missing_guard":
            assert bridge._guard("before", root) == {}
            assert bridge._guard("before", root) == {"terminationBehavior": "terminate"}
        child.event("step_update", step_type="agent_response", state="ACTIVE", text_delta="text")
        if case != "missing_receipt":
            event = {
                "stepIdx": 2,
                "toolCall": {"name": tool, "args": {**_ARGS, "toolSummary": "native"}, "id": "c1"},
            }
            monkeypatch.setattr(bridge.sys, "stdin", io.StringIO(json.dumps(event)))
            assert bridge._guard("guard", root) == {"decision": "deny"}
        info = {"name": tool, "parameters": _ARGS}
        child.event(
            "step_update",
            step_type="tool",
            step_index=2,
            state="ACTIVE",
            tool_name=tool,
            tool_info=info,
        )
        child.event(
            "step_update",
            step_type="tool",
            step_index=2,
            state="ERROR",
            tool_name=tool,
            tool_info={**info, "error": {"message": "tool call denied by pre-tool hook:"}},
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
    )
    if case == "call":
        assert [delta async for delta in stream] == [
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
        with pytest.raises(ProviderError):
            _ = [delta async for delta in stream]
    if case == "fallback":
        assert not child.stdin
    else:
        assert json.loads(child.stdin)["message"] == {"content": "ask"}
    assert child.returncode is not None
    assert not workspace[0].exists()


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
