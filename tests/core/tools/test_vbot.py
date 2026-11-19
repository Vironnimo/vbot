"""Skill and profile access to the local CLI, approvals and Run handoffs."""

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from core.agents.app_chats import APP_CHAT_PROFILES
from core.skills.policy import SkillPackageRef
from core.skills.skills import SkillMetadata, SkillRegistry
from core.tools import ToolContext, ToolRegistry
from core.tools.availability import ToolAccess
from core.tools.shell_environment import RunIdentity
from core.tools.update_handoff import UpdateHandoffs, UpdateHandoffUnavailableError
from core.tools.vbot import register_vbot_tool
from core.tools.vbot_cli_runner import VBotAccessReviewRequiredError
from tests.core.permissions.command_check_support import restricted
from tests.core.tools.tools_test_support import dispatch_as_executor, make_context


@pytest.mark.asyncio
async def test_vbot_requires_the_profile_or_skill_grant_and_rechecks_reviewed_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = ToolRegistry()
    profile = APP_CHAT_PROFILES["app"]
    cli = AsyncMock(return_value={"exit_code": 0, "stdout": "accepted", "stderr": ""})
    monkeypatch.setattr("core.tools.vbot.run_vbot_cli", cli)
    handoffs = UpdateHandoffs(tmp_path)
    acknowledgements: list[Callable[[], None]] = []
    docs = SkillMetadata(
        "vbot-docs", "Docs", tmp_path, package_ref=SkillPackageRef("bundled", "vbot-docs")
    )
    skills = SkillRegistry({})

    async def agent(context: ToolContext) -> Any:
        # A Project may contain an unrelated Agent with the same name.
        return SimpleNamespace(
            builtin=None if context.project_id else profile.builtin,
            tool_access=ToolAccess(),
            memory_prompt_mode="agent_user",
            allowed_skills=["vbot-docs"],
            workspace="",
        )

    register_vbot_tool(
        registry,
        resolve_agent=agent,
        resolve_skills=lambda *_: skills,
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
            "tool_not_allowed",
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
        check_agent_access=False,
        run=RunIdentity(profile.agent_id, context.session_id, None, identity.update_handoff),
    )
    assert len(acknowledgements) == 1
    with pytest.raises(UpdateHandoffUnavailableError):
        handoffs.mint(identity.update_handoff)

    cli.return_value = {"exit_code": 2, "stdout": "", "stderr": "unknown subcommand"}
    rejected = await dispatch_as_executor(registry, context, {"argv": ["unknown"]})
    assert not rejected["ok"]
    assert rejected["error"]["message"] == "vbot exited with code 2.\nunknown subcommand"

    skills = SkillRegistry({docs.name: docs})
    approval = AsyncMock(return_value=False)
    limited = replace(
        context, project_id="team", permissions=restricted(tmp_path), approval_hook=approval
    )
    cli.return_value = {"exit_code": 0, "stdout": "ok", "stderr": ""}
    assert (await dispatch_as_executor(registry, limited, {"argv": ["agent", "list"]}))["ok"]
    assert cli.await_args is not None and cli.await_args.kwargs["check_agent_access"]
    approval.assert_not_called()

    edit = {"argv": ["agent", "update", "other", "--permissions", "full-access"]}
    cli.reset_mock()
    cli.side_effect = VBotAccessReviewRequiredError
    refused = await dispatch_as_executor(registry, limited, edit)
    assert refused["error"]["code"] == "permission_denied"
    cli.assert_awaited_once()
    assert approval.await_args is not None and approval.await_args.args[3] == edit

    approval.return_value = True
    cli.reset_mock()
    cli.side_effect = [VBotAccessReviewRequiredError, cli.return_value]
    assert (await dispatch_as_executor(registry, replace(limited), edit))["ok"]
    assert [call.kwargs["check_agent_access"] for call in cli.await_args_list] == [True, False]
    assert all(call.args[0] == edit["argv"] for call in cli.await_args_list)

    protected_file = tmp_path.parent / f"{tmp_path.name}-vbot-data" / "settings.json"
    read = {"argv": ["prompt", "set", "main", "role", "--content-file", str(protected_file)]}
    cli.reset_mock()
    approval.reset_mock()
    cli.side_effect = VBotAccessReviewRequiredError(False, [(str(protected_file), False)])
    refused = await dispatch_as_executor(registry, replace(limited), read)
    assert refused["error"]["code"] == "permission_denied"
    cli.assert_awaited_once()
    approval.assert_not_called()

    async def revoke(*_):
        nonlocal skills
        skills = SkillRegistry({})
        return True

    cli.reset_mock()
    cli.side_effect = VBotAccessReviewRequiredError
    result = await dispatch_as_executor(registry, replace(limited, approval_hook=revoke), edit)
    assert result["error"]["code"] == "tool_not_allowed"
    cli.assert_awaited_once()
