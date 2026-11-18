"""The App Agent's private CLI dispatch and Run handoff."""

from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.agents.app_chats import APP_CHAT_PROFILES
from core.tools import ToolContext, ToolRegistry
from core.tools.shell_environment import RunIdentity
from core.tools.update_handoff import UpdateHandoffs, UpdateHandoffUnavailableError
from core.tools.vbot import register_vbot_tool
from tests.core.permissions.command_check_support import restricted
from tests.core.tools.tools_test_support import dispatch_as_executor, make_context


@pytest.mark.asyncio
async def test_vbot_requires_the_app_identity_grant_and_an_unrestricted_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = ToolRegistry()
    profile = APP_CHAT_PROFILES["app"]
    cli = AsyncMock(return_value={"exit_code": 0, "stdout": "accepted", "stderr": ""})
    monkeypatch.setattr("core.tools.vbot.run_vbot_cli", cli)
    handoffs = UpdateHandoffs(tmp_path)
    acknowledgements: list[Callable[[], None]] = []

    async def agent(context: ToolContext) -> object:
        # A Project may contain an unrelated Agent with the same name.
        return SimpleNamespace(builtin=None if context.project_id else profile.builtin)

    register_vbot_tool(
        registry,
        resolve_agent=agent,
        data_dir=tmp_path,
        server_bind=lambda: ("::1", 12345),
        update_handoffs=handoffs,
    )
    argv = ["config", "set", "server.timezone", "--stdin"]
    stdin = '"Europe/Berlin"\n'
    arguments = {"argv": ["vbot", *argv], "stdin": stdin}
    for fields, code in (
        ({}, "vbot_unavailable"),
        ({"session_tool_grants": ("vbot",), "project_id": "other"}, "tool_not_allowed"),
        (
            {"session_tool_grants": ("vbot",), "permissions": restricted(tmp_path)},
            "permission_denied",
        ),
    ):
        context = make_context("vbot", agent_id=profile.agent_id, **fields)
        assert (await dispatch_as_executor(registry, context, arguments))["error"]["code"] == code
    cli.assert_not_called()

    context = make_context(
        "vbot",
        agent_id=profile.agent_id,
        session_tool_grants=("vbot",),
        result_persisted_hook=acknowledgements.append,
    )
    result = await dispatch_as_executor(registry, context, arguments)
    assert result["ok"] and result["data"] == cli.return_value
    call = cli.await_args
    assert call is not None
    identity = call.kwargs["run"]
    assert isinstance(identity, RunIdentity)
    assert identity.update_handoff
    cli.assert_awaited_once_with(
        argv,
        stdin=stdin,
        data_dir=tmp_path,
        host="::1",
        port=12345,
        run=RunIdentity(profile.agent_id, context.session_id, None, identity.update_handoff),
    )
    assert len(acknowledgements) == 1
    with pytest.raises(UpdateHandoffUnavailableError):
        handoffs.mint(identity.update_handoff)

    cli.return_value = {"exit_code": 2, "stdout": "", "stderr": "unknown subcommand"}
    rejected = await dispatch_as_executor(registry, context, {"argv": ["unknown"]})
    assert not rejected["ok"]
    assert rejected["error"]["message"] == "vbot exited with code 2.\nunknown subcommand"
