"""Resolver-backed RPC entry points report a missing Agent or Project precisely.

These tests run the real Agent resolver over real Agent and Project stores, so a
case-variant address fails inside resolution exactly as it does in production.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from core.chat import CommandDispatcher
from tests.core.chat.chat_loop_support import build_chat_loop
from tests.server.rpc.project_methods_test_support import _make_repo, _make_state, _write_agent
from tests.server.rpc_test_support import rpc_error, rpc_result


async def _resolution_state(tmp_path: Path) -> Any:
    state = _make_state(tmp_path)
    runtime = state.runtime
    runtime.agents.create("coder", "Coder", model="openai/gpt-5.2")
    repo = _make_repo(tmp_path, "vbot", "builder.md")
    _write_agent(repo, "stranded.md", model="ghost/model-x")
    await rpc_result(state, "project.add", cwd=str(repo), display_name="vBot")
    runtime.chat_sessions = runtime.sessions
    state.chat_loop = build_chat_loop(runtime)
    state.command_dispatcher = CommandDispatcher(
        state.chat_runs,
        agent_resolver=runtime.agent_resolver,
        sessions=runtime.sessions,
        projects=runtime.projects,
        agents=runtime.agents,
    )
    return state


def _params(method: str, address: str) -> dict[str, Any]:
    if method == "chat.send":
        return {"agent_id": address, "session_id": "default", "content": "Hi"}
    return {"agent_id": address}


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["prompt.preview", "chat.send", "session.create"])
@pytest.mark.parametrize(
    ("address", "code"),
    [
        ("Coder", "agent_not_found"),
        ("builder@VBOT", "project_not_found"),
        ("Builder@vbot", "agent_not_found"),
        # An existing Agent without a usable Model is not a missing resource.
        ("stranded@vbot", "domain_error"),
    ],
)
async def test_address_resolution_failures_keep_their_error_code(
    tmp_path: Path, method: str, address: str, code: str
) -> None:
    state = await _resolution_state(tmp_path)

    error = await rpc_error(state, method, **_params(method, address))

    assert error["code"] == code
