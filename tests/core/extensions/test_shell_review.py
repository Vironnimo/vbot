"""Prepared-shell reviewer registration, immutable inputs and fail-closed approval."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from core.extensions import ExtensionRegistry
from core.permissions import ApprovalRequirement, PermissionBoundary
from core.tools.contracts import ToolContractError
from tests.core.extensions.extension_test_support import tool_context, write_extension


def _registry(tmp_path: Path, body: str, *, active: bool = True) -> ExtensionRegistry:
    write_extension(
        tmp_path,
        "reviewer",
        (
            "async def review(context, *, request):\n"
            + "\n".join("    " + line for line in body.splitlines())
            + "\ndef register(api):\n"
            + f"    api.register_shell_reviewer(review, active=lambda: {active!r})\n"
        ),
    )
    return ExtensionRegistry.load(tmp_path)


async def _review(registry: ExtensionRegistry, context: Any, tmp_path: Path) -> bool:
    return await registry.review_shell(
        context,
        {"command": "original-command", "workdir": str(tmp_path)},
        command="original-command",
        shell="powershell",
        cwd=tmp_path,
        environment={"PUBLIC_PATH": "original-path"},
    )


@pytest.mark.asyncio
async def test_review_cannot_rewrite_the_command_or_its_one_time_approval(tmp_path: Path) -> None:
    registry = _registry(
        tmp_path,
        (
            "request['command'] = 'changed-command'\n"
            "request['evidence']['environment']['PUBLIC_PATH'] = 'changed-path'\n"
            "return {'decision': 'ask'}"
        ),
    )
    context = SimpleNamespace(
        invocation_approved=Mock(return_value=False),
        request_approval=AsyncMock(return_value=True),
        revalidate_permissions=AsyncMock(),
    )
    assert await _review(registry, context, tmp_path)
    call = context.request_approval.await_args
    target = json.loads(call.args[1][0].target)
    assert target["command"] == "original-command"
    assert target["evidence"]["environment"] == {"PUBLIC_PATH": "original-path"}
    assert call.kwargs["arguments"]["command"] == "original-command"
    context.revalidate_permissions.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        "return {'decision': 'ask'}",
        "return {'decision': 'deny'}",
        "return None",
        "raise RuntimeError('private-input-sentinel')",
        "import asyncio\nraise asyncio.CancelledError()",
        "context.registry.retire_registration()\nreturn {'decision': 'allow'}",
    ],
)
async def test_no_unusable_or_retired_judgment_can_bypass_confirmation(
    tmp_path: Path, body: str, caplog: pytest.LogCaptureFixture
) -> None:
    registry = _registry(tmp_path, body)
    context = SimpleNamespace(
        invocation_approved=Mock(return_value=False),
        registry=registry,
        request_approval=AsyncMock(return_value=False),
        revalidate_permissions=AsyncMock(),
    )
    assert not await _review(registry, context, tmp_path)
    context.request_approval.assert_awaited_once()
    context.revalidate_permissions.assert_not_awaited()
    assert "private-input-sentinel" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("active", "body"),
    [
        (False, "raise AssertionError('inactive reviewer ran')"),
        (True, "return {'decision': 'allow'}"),
    ],
)
async def test_inactive_reviewers_and_valid_allows_need_no_confirmation(
    tmp_path: Path, active: bool, body: str
) -> None:
    registry = _registry(tmp_path, body, active=active)
    context = SimpleNamespace(
        invocation_approved=Mock(return_value=False),
        request_approval=AsyncMock(),
        revalidate_permissions=AsyncMock(),
    )
    assert await _review(registry, context, tmp_path)
    context.request_approval.assert_not_awaited()
    if active:
        context.revalidate_permissions.assert_awaited_once_with(force_refresh=True)
    else:
        context.revalidate_permissions.assert_not_awaited()


@pytest.mark.asyncio
async def test_caller_cancellation_never_becomes_a_confirmation_request(tmp_path: Path) -> None:
    registry = _registry(
        tmp_path,
        (
            "import asyncio\n"
            "context.entered.set()\n"
            "try:\n"
            "    await asyncio.Future()\n"
            "finally:\n"
            "    context.finished.set()"
        ),
    )
    context = SimpleNamespace(
        invocation_approved=Mock(return_value=False),
        entered=asyncio.Event(),
        finished=asyncio.Event(),
        request_approval=AsyncMock(),
    )
    task = asyncio.create_task(_review(registry, context, tmp_path))
    await context.entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await context.finished.wait()
    context.request_approval.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [None, "arguments", "cwd", "computer", "call", "revoked"])
async def test_prior_operator_consent_covers_only_the_same_live_invocation(
    tmp_path: Path, change: str | None
) -> None:
    registry = _registry(tmp_path, "return {'decision': 'ask'}")
    approve = AsyncMock(return_value=True)
    refresh = AsyncMock(return_value=(PermissionBoundary.UNRESTRICTED, {"computer_id": "server"}))
    context = replace(
        tool_context("bash", tmp_path), approval_hook=approve, approval_refresh_hook=refresh
    )
    arguments = {"command": "original-command", "workdir": str(tmp_path)}
    assert await context.authorize(
        [ApprovalRequirement.review("prepared")],
        reason="Permission Rules",
        arguments=arguments,
        cwd=tmp_path,
    )
    approve.reset_mock()
    if change == "arguments":
        arguments["command"] = "changed-command"
    elif change == "cwd":
        tmp_path = tmp_path / "changed"
    elif change == "computer":
        context.execution["computer_id"] = "other"
        refresh.return_value = (PermissionBoundary.UNRESTRICTED, context.execution)
    elif change == "call":
        context = replace(context, tool_call_id="sibling")
    elif change == "revoked":

        async def revoked():
            raise ToolContractError("The authority changed")

        context.add_permission_recheck(revoked)

    async def review():
        return await registry.review_shell(
            context,
            arguments,
            command=arguments["command"],
            shell="powershell",
            cwd=tmp_path,
            environment={},
        )

    if change == "revoked":
        with pytest.raises(ToolContractError, match="authority changed"):
            await review()
    else:
        assert await review()
    assert approve.await_count == int(change in {"arguments", "cwd", "computer", "call"})
