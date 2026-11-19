"""The App Agent's private CLI Tool, approvals and Run handoffs."""

import asyncio
import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from core.agents.app_chats import APP_CHAT_PROFILES
from core.tools import ToolContext, ToolRegistry
from core.tools.availability import ToolAccess
from core.tools.shell_environment import RunIdentity
from core.tools.update_handoff import UpdateHandoffs, UpdateHandoffUnavailableError
from core.tools.vbot import register_vbot_tool
from core.tools.vbot_cli_runner import (
    REVIEW_ENV,
    REVIEW_EXIT,
    REVIEWED_INPUTS_ENV,
    VBotAccessReviewRequiredError,
    run_vbot_cli,
)
from tests.core.permissions.command_check_support import restricted
from tests.core.tools.tools_test_support import dispatch_as_executor, make_context


@pytest.mark.asyncio
async def test_vbot_requires_the_app_profile_and_session_grant_and_rechecks_reviewed_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = ToolRegistry()
    profile = APP_CHAT_PROFILES["app"]
    cli = AsyncMock(return_value={"exit_code": 0, "stdout": "accepted", "stderr": ""})
    monkeypatch.setattr("core.tools.vbot.run_vbot_cli", cli)
    handoffs = UpdateHandoffs(tmp_path)
    acknowledgements: list[Callable[[], None]] = []
    access = ToolAccess()

    async def agent(context: ToolContext) -> Any:
        # A Project may contain an unrelated Agent with the same name.
        return SimpleNamespace(
            builtin=None if context.project_id else profile.builtin,
            tool_access=access,
            memory_prompt_mode="agent_user",
            allowed_skills=["vbot-docs"],
            workspace="",
        )

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
            {
                "session_tool_grants": ("vbot",),
                "permissions": restricted(tmp_path),
                "project_id": "other",
            },
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
        reviewed_inputs=None,
        run=RunIdentity(profile.agent_id, context.session_id, None, identity.update_handoff),
    )
    assert len(acknowledgements) == 1
    with pytest.raises(UpdateHandoffUnavailableError):
        handoffs.mint(identity.update_handoff)

    cli.return_value = {"exit_code": 2, "stdout": "", "stderr": "unknown subcommand"}
    rejected = await dispatch_as_executor(registry, context, {"argv": ["unknown"]})
    assert not rejected["ok"]
    assert rejected["error"]["message"] == "vbot exited with code 2.\nunknown subcommand"

    approval = AsyncMock(return_value=False)
    limited = replace(context, permissions=restricted(tmp_path), approval_hook=approval)
    cli.return_value = {"exit_code": 0, "stdout": "ok", "stderr": ""}
    assert (await dispatch_as_executor(registry, limited, {"argv": ["agent", "list"]}))["ok"]
    assert cli.await_args is not None and cli.await_args.kwargs["check_agent_access"]
    approval.assert_not_called()

    edit = {"argv": ["agent", "update", "other", "--permission-profile", "local"]}
    profile_change = {
        "action": "assign",
        "name": "local",
        "target": "other",
        "rules": {"read": "work_area"},
    }
    cli.reset_mock()
    cli.side_effect = VBotAccessReviewRequiredError(profile=profile_change)
    refused = await dispatch_as_executor(registry, limited, edit)
    assert refused["error"]["code"] == "permission_denied"
    cli.assert_awaited_once()
    assert approval.await_args is not None and approval.await_args.args[3] == edit
    first_review = approval.await_args.args[2][0]
    assert first_review.to_payload()["permission_profile"] == profile_change

    approval.return_value = True
    cli.reset_mock()
    changed = {**profile_change, "rules": {"read": "anywhere"}}
    cli.side_effect = [VBotAccessReviewRequiredError(profile=changed), cli.return_value]
    assert (await dispatch_as_executor(registry, replace(limited), edit))["ok"]
    assert [call.kwargs["check_agent_access"] for call in cli.await_args_list] == [True, False]
    assert all(call.args[0] == edit["argv"] for call in cli.await_args_list)
    assert [call.kwargs["reviewed_inputs"] for call in cli.await_args_list] == [
        None,
        {"profile": changed, "files": {}},
    ]
    assert approval.await_args.args[2][0].key != first_review.key

    protected_file = tmp_path.parent / f"{tmp_path.name}-vbot-data" / "settings.json"
    read = {"argv": ["extensions", "run", "demo", "import", str(protected_file)]}
    cli.reset_mock()
    approval.reset_mock()
    cli.side_effect = VBotAccessReviewRequiredError(False, [(str(protected_file), False)])
    refused = await dispatch_as_executor(registry, replace(limited), read)
    assert refused["error"]["code"] == "permission_denied"
    cli.assert_awaited_once()
    approval.assert_not_called()

    allowed = tmp_path / "input.txt"
    read = {"argv": ["extensions", "run", "demo", "import", str(allowed)]}
    cli.reset_mock()
    cli.side_effect = [
        VBotAccessReviewRequiredError(False, [(str(allowed), False)]),
        cli.return_value,
    ]
    assert (await dispatch_as_executor(registry, replace(limited), read))["ok"]
    assert cli.await_args.kwargs["reviewed_inputs"]["files"] == {
        str(allowed): str(allowed.resolve())
    }
    approval.assert_not_called()

    async def revoke(*_):
        nonlocal access
        access = ToolAccess(denied=("vbot",))
        return True

    cli.reset_mock()
    cli.side_effect = VBotAccessReviewRequiredError
    result = await dispatch_as_executor(registry, replace(limited, approval_hook=revoke), edit)
    assert result["error"]["code"] == "tool_not_allowed"
    cli.assert_awaited_once()


@pytest.mark.asyncio
async def test_vbot_cli_transfers_large_reviewed_profiles_and_preserves_stdin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rules = {"deny": [f"Read(~/Verträge/{i})" for i in range(4400)]}
    profile = {"action": "save", "name": "privat", "rules": rules, "base": None}
    body = json.dumps(rules, ensure_ascii=False, indent=2) + "\n"
    argv = ["permission-profiles", "save", "privat", "--stdin"]
    pipe = Mock(drain=AsyncMock())
    monkeypatch.setenv(REVIEWED_INPUTS_ENV, "inherited")

    async def launch(*args, env, **kwargs):
        stdout, stderr = asyncio.StreamReader(), asyncio.StreamReader()
        if marker := env.get(REVIEW_ENV):
            assert REVIEWED_INPUTS_ENV not in env
            envelope = json.dumps({"rights": True, "reads": [], "profile": profile})
            stdout.feed_data(f"{marker}\r\n{envelope}\n".encode())
            code = REVIEW_EXIT
        else:
            assert env[REVIEWED_INPUTS_ENV] == "1"
            code = 0
        stdout.feed_eof()
        stderr.feed_eof()
        return SimpleNamespace(stdin=pipe, stdout=stdout, stderr=stderr, returncode=code)

    monkeypatch.setattr("core.tools.vbot_cli_runner.create_subprocess_exec", launch)
    run = RunIdentity("manager", "session", None)
    with pytest.raises(VBotAccessReviewRequiredError) as pending:
        await run_vbot_cli(
            argv, data_dir=tmp_path, stdin=body, port=22000, run=run, check_agent_access=True
        )
    assert pending.value.profile == profile
    pipe.write.assert_called_once_with(body.encode())
    pipe.reset_mock()
    result = await run_vbot_cli(
        argv,
        data_dir=tmp_path,
        stdin=body,
        port=22000,
        run=run,
        reviewed_inputs={"profile": pending.value.profile, "files": {}},
    )
    assert result["exit_code"] == 0
    frame, remaining = pipe.write.call_args.args[0].split(b"\n", 1)
    assert json.loads(frame) == {"profile": profile, "files": {}}
    assert remaining == body.encode()
