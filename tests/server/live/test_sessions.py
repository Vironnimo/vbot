"""Live Session Tools on the real RPC dispatcher: the shapes they read from
``session.*`` and ``chat.*`` are the ones those methods produce."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from core.tools.terminal_manager import TerminalManager
from server.live._tools import LiveToolExecutor
from server.rpc.dispatcher import dispatch_method
from server.rpc.methods import METHODS
from tests.server.rpc_test_support import JsonObject, StubAdapter, make_state


async def _ui(action: str, args: JsonObject) -> JsonObject:
    return {"applied": True}


async def _eventually(predicate: Any) -> None:
    deadline = time.monotonic() + 5
    while not predicate():
        assert time.monotonic() < deadline, "timed out"
        await asyncio.sleep(0.01)


async def _ok(executor: LiveToolExecutor, tool: str, **arguments: Any) -> str:
    result = await executor.execute(tool, arguments)
    assert result["ok"] is True, result
    return str(result["data"]["content"])


@pytest.mark.asyncio
async def test_a_live_session_works_through_the_real_session_and_chat_methods(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    adapter = StubAdapter(
        stream_deltas=[
            [
                {"type": "content_delta", "text": "Checked: the PDF export works on Windows."},
                {"type": "finish", "reason": "stop"},
            ],
        ],
        block=True,
    )
    state = make_state(tmp_path, adapter)
    terminals = TerminalManager(sweep_interval_seconds=3600, data_dir=tmp_path / "terminals")
    terminals.start()
    state.runtime.terminal_manager = terminals

    async def rpc(method: str, params: JsonObject) -> JsonObject:
        # The test runtime keeps no Project list; every other method is real.
        if method == "project.list":
            return {"projects": []}
        return await dispatch_method(state, method, params, METHODS)

    executor = LiveToolExecutor(
        rpc=rpc,
        ui=_ui,
        app_context=lambda: {"view": "chat", "selected_agent_id": "coder"},
        is_active=lambda: True,
        started_at=datetime.now(UTC),
        end_call=lambda: None,
    )

    try:
        await _run_a_session(executor, adapter, state)
    finally:
        await terminals.aclose()


async def _run_a_session(executor: LiveToolExecutor, adapter: StubAdapter, state: Any) -> None:
    started = await _ok(
        executor, "start_agent_session", agent="coder", task="Check the PDF export."
    )
    assert started.startswith("Started a Session at Coder Agent with the task: s1.")
    await adapter.request_started.wait()
    (session,) = state.runtime.chat_sessions.list("coder")
    stored = session.load()
    assert [(item.content, item.input_origin) for item in stored if item.role == "user"] == [
        ("Check the PDF export.", "live_voice")
    ]

    assert "- s1 Coder Agent: working" in await _ok(executor, "overview")
    assert "waits in its Queue" in await _ok(
        executor, "send_message", target="s1", text="Also try Linux."
    )
    read = await _ok(executor, "read", target="s1")
    assert read.startswith("s1 at Coder Agent, working. Latest messages, quoted:")
    assert "> Check the PDF export." in read

    assert "Stopped the current work of s1" in await _ok(executor, "stop", target="coder")
    adapter.release.set()
    await _eventually(lambda: not state.chat_runs.active_runs())
    # The queued message ran after the stop and answered.
    overview = await _ok(executor, "overview", agent="coder")
    assert '- s1 Coder Agent: finished: "Checked: the PDF export works on Windows."' in overview
