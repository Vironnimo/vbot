"""Fail-closed review of a final shell invocation, owned by ExtensionRegistry."""

from __future__ import annotations

import inspect
import json
import sys
from collections.abc import Mapping
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING, Any, Literal

from core.extensions._callbacks import invoke_isolated_handler
from core.extensions._declarations import (
    ExtensionRecord,
    ExtensionUnavailableError,
    ShellReviewerDeclaration,
)
from core.permissions import ApprovalRequirement
from core.tools.tools import ToolContext
from core.utils.logging import get_logger

if TYPE_CHECKING:
    from core.extensions.extensions import ExtensionRegistry

_LOGGER = get_logger("extensions")


async def review_shell(
    registry: ExtensionRegistry,
    context: ToolContext,
    arguments: dict[str, Any],
    *,
    command: str,
    shell: str,
    cwd: Path,
    environment: Mapping[str, str],
) -> bool:
    """Review a snapshot; callbacks can neither transform nor execute this call."""
    if context.invocation_approved(arguments, cwd=cwd):
        await context.revalidate_permissions()
        return True
    prepared = {
        "platform": "windows" if sys.platform == "win32" else "linux",
        "shell": shell,
        "cwd": cwd.as_posix(),
        "command": command,
        "evidence": {"environment": dict(environment)},
    }
    needs_approval = False
    reviewed = False
    for record in registry.records():
        reviewer = record.declarations.shell_reviewer if record.status == "loaded" else None
        if reviewer is None:
            continue
        try:
            decision = await _evaluate(registry, record, reviewer, context, prepared)
            reviewed |= decision != "inactive"
            needs_approval |= decision == "ask"
        except Exception as error:
            # Exceptions can contain commands, user requests or credentials.
            trace = error.__traceback__
            safe_trace = (
                TracebackType(None, trace.tb_frame, trace.tb_lasti, trace.tb_lineno)
                if trace
                else None
            )
            _LOGGER.error(
                "Shell reviewer failed (extension=%s error_type=%s)",
                record.name,
                type(error).__name__,
                exc_info=(RuntimeError, RuntimeError("Shell reviewer failed"), safe_trace),
            )
            needs_approval = True
    if not needs_approval:
        if reviewed:
            await context.revalidate_permissions(force_refresh=True)
        return True
    target = json.dumps(prepared, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    allowed = await context.request_approval(
        "Shell review could not establish that this command is covered by your request.",
        [ApprovalRequirement.review(target, computer_id="server")],
        arguments=arguments,
        cwd=cwd,
    )
    if allowed:
        await context.revalidate_permissions()
    return allowed


async def _evaluate(
    registry: ExtensionRegistry,
    record: ExtensionRecord,
    reviewer: ShellReviewerDeclaration,
    context: ToolContext,
    prepared: dict[str, Any],
) -> Literal["inactive", "allow", "ask"]:
    # Keep per-owner identities in a separate frame: a detached callback must
    # not start using the next iteration's declaration or registration.
    identity = registry.registration_identity(record.name)

    def current() -> None:
        if (
            not registry.is_registration_current(identity)
            or record.declarations.shell_reviewer is not reviewer
        ):
            raise ExtensionUnavailableError("The shell reviewer registration changed")

    def active() -> bool:
        current()
        return reviewer.active()

    enabled = await invoke_isolated_handler(
        active, extension_name=record.name, description="shell review activation", timeout_seconds=1
    )
    if enabled is False:
        return "inactive"
    if enabled is not True:
        return "ask"
    request = json.loads(json.dumps(prepared))

    if inspect.iscoroutinefunction(reviewer.handler):

        async def asynchronous() -> Any:
            current()
            return await reviewer.handler(context, request=request)

        handler = asynchronous
    else:

        def synchronous() -> Any:
            current()
            return reviewer.handler(context, request=request)

        handler = synchronous

    result = await invoke_isolated_handler(
        handler, extension_name=record.name, description="shell review", timeout_seconds=125
    )
    current()
    return "allow" if isinstance(result, dict) and result.get("decision") == "allow" else "ask"
