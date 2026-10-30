"""Prepared-shell reviewer registration, immutable inputs and fail-closed approval."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from core.extensions import ExtensionRegistry
from tests.core.extensions.extension_test_support import write_extension


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
        request_approval=AsyncMock(return_value=True), revalidate_permissions=AsyncMock()
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
    context = SimpleNamespace(request_approval=AsyncMock(), revalidate_permissions=AsyncMock())
    assert await _review(registry, context, tmp_path)
    context.request_approval.assert_not_awaited()


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
        entered=asyncio.Event(), finished=asyncio.Event(), request_approval=AsyncMock()
    )
    task = asyncio.create_task(_review(registry, context, tmp_path))
    await context.entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await context.finished.wait()
    context.request_approval.assert_not_awaited()
