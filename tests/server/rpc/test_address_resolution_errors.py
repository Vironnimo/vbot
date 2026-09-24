"""Resolver-backed RPC entry points report a missing Agent or Project precisely.

These tests run the real Agent resolver over real Agent and Project stores, so a
case-variant address fails inside resolution exactly as it does in production.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from core.chat import CommandDispatcher
from server.rpc.methods import dispatch_rpc
from server.rpc.project_methods import _add_project
from tests.core.chat.chat_loop_support import build_chat_loop
from tests.server.rpc.project_methods_test_support import _make_repo, _make_state, _write_agent


def _resolution_state(tmp_path: Path) -> Any:
    state = _make_state(tmp_path)
    runtime = state.runtime
    runtime.agents.create("coder", "Coder", model="openai/gpt-5.2")
    repo = _make_repo(tmp_path, "vbot", "builder.md")
    _write_agent(repo, "stranded.md", model="ghost/model-x")
    _add_project(state, {"cwd": str(repo), "display_name": "vBot"})
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


def _params(method: str, address: str) -> dict[str, str]:
    if method == "chat.send":
        return {"agent_id": address, "session_id": "default", "content": "Hi"}
    return {"agent_id": address}


_METHODS = ["prompt.preview", "chat.send", "session.create"]


@pytest.mark.asyncio
@pytest.mark.parametrize("method", _METHODS)
@pytest.mark.parametrize(
    ("address", "code"),
    [
        ("Coder", "agent_not_found"),
        ("builder@VBOT", "project_not_found"),
        ("Builder@VBot", "project_not_found"),
        ("Builder@vbot", "agent_not_found"),
    ],
)
async def test_case_variant_address_reports_the_missing_resource(
    tmp_path: Path, method: str, address: str, code: str
) -> None:
    state = _resolution_state(tmp_path)

    response = await dispatch_rpc(state, {"method": method, "params": _params(method, address)})

    assert response["ok"] is False
    assert response["error"]["code"] == code


@pytest.mark.asyncio
@pytest.mark.parametrize("method", _METHODS)
async def test_existing_agent_without_usable_model_stays_a_domain_error(
    tmp_path: Path, method: str
) -> None:
    state = _resolution_state(tmp_path)

    response = await dispatch_rpc(
        state, {"method": method, "params": _params(method, "stranded@vbot")}
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "domain_error"
