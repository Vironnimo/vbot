"""Preflight explicit Tool targets before any part of a call takes effect."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from core.permissions import (
    ApprovalRequirement,
    CommandChecker,
    PathAccess,
    PermissionApprovalRejected,
    PermissionDenied,
    PermissionRefusal,
    run_permission_decision,
)
from core.permissions.commands.commands import CommandPlan, ShellKind
from core.tools._tool_context import ToolContext
from core.tools.contracts import JsonObject, ToolContractError


async def authorize_accesses(
    context: ToolContext,
    paths: Sequence[tuple[str | Path, PathAccess, Mapping[str, Any]]],
    urls: Sequence[str],
    *,
    arguments: JsonObject,
    reason: str = "Permission Rules",
    cwd: Path | None = None,
) -> None:
    """Check all explicit paths and URLs together before asking or performing effects."""
    if not context.permissions.restricted:
        return
    computer = str(context.execution.get("computer_id", "server"))

    def prepare() -> tuple[
        list[tuple[Path, PathAccess, dict[str, Any], ApprovalRequirement]],
        list[ApprovalRequirement],
        PermissionRefusal | None,
    ]:
        prepared: list[tuple[Path, PathAccess, dict[str, Any], ApprovalRequirement]] = []
        pending: list[ApprovalRequirement] = []
        first_missing: PermissionRefusal | None = None
        for raw, access, options in paths:
            path = context.path_target(raw, access=access)
            filtered = bool(options.get("filtered", False))
            opts = {
                "base": context.effective_cwd,
                **{k: v for k, v in options.items() if k != "filtered"},
            }
            if filtered and (access != "read" or opts.get("subtree") or opts.get("glob")):
                raise ValueError("filtered roots require a plain read")
            requirement_opts = {**opts, "subtree": True, "filtered": True} if filtered else opts
            identity = ApprovalRequirement.path(
                context.permissions, path, access, computer_id=computer, **requirement_opts
            )
            prepared.append((path, access, opts, identity))
            refusal = context.permissions.check_path(path, access, **opts)
            if refusal is not None:
                if refusal.reason != "not_permitted":
                    raise PermissionDenied(refusal)
                first_missing = first_missing or refusal
                if identity not in pending:
                    pending.append(identity)
        for url in urls:
            refusal = context.permissions.check_url(url)
            if refusal is not None:
                if refusal.reason != "not_permitted":
                    raise PermissionDenied(refusal)
                first_missing = first_missing or refusal
                requirement = ApprovalRequirement.url(url, computer_id=computer)
                if requirement not in pending:
                    pending.append(requirement)

        return prepared, pending, first_missing

    prepared, pending, first_missing = await run_permission_decision(prepare)

    def check_prepared() -> None:
        for path, access, opts, identity in prepared:
            identity_opts = (
                {**opts, "subtree": True, "filtered": True} if identity.filtered else opts
            )
            fresh = ApprovalRequirement.path(
                context.permissions, path, access, computer_id=computer, **identity_opts
            )
            if fresh != identity:
                raise ToolContractError(
                    "The path changed while the Tool was waiting. Call it again."
                )
            refusal = context.permission_access.check_path(path, access, **opts)
            if refusal is not None:
                raise PermissionDenied(refusal)
        for url in urls:
            refusal = context.permission_access.check_url(url)
            if refusal is not None:
                raise PermissionDenied(refusal)

    async def recheck() -> None:
        await run_permission_decision(check_prepared)

    if pending:
        if not await context.authorize(pending, reason=reason, arguments=arguments, cwd=cwd):
            assert first_missing is not None
            raise (
                PermissionApprovalRejected(first_missing)
                if context.approval_rejected
                else PermissionDenied(first_missing)
            )
        await recheck()
    context.add_permission_recheck(recheck)


async def authorize_paths(
    context: ToolContext,
    accesses: Sequence[tuple[str | Path, PathAccess, Mapping[str, Any]]],
    *,
    arguments: JsonObject,
    reason: str = "Permission Rules",
    cwd: Path | None = None,
) -> None:
    await authorize_accesses(context, accesses, (), arguments=arguments, reason=reason, cwd=cwd)


async def authorize_urls(
    context: ToolContext,
    urls: Sequence[str],
    *,
    arguments: JsonObject,
    reason: str = "Permission Rules",
    cwd: Path | None = None,
) -> None:
    await authorize_accesses(context, (), urls, arguments=arguments, reason=reason, cwd=cwd)


async def authorize_command(
    context: ToolContext,
    checker: CommandChecker,
    *,
    arguments: JsonObject,
    cwd: Path,
    env: Mapping[str, str],
    command: str | None = None,
    shell: ShellKind | None = None,
    argv: Sequence[str] | None = None,
    refuse_command: Any = None,
    workdir_parameter: bool = False,
) -> None:
    """Authorize a whole analyzed command; recheck the prepared plan after waiting."""
    if (command is None) == (argv is None) or (command is not None and shell is None):
        raise ValueError("provide a shell command or argv")
    if not context.permissions.restricted:
        return
    computer = str(context.execution.get("computer_id", "server"))

    async def assess() -> tuple[
        CommandPlan | None,
        tuple[PermissionRefusal, ...],
        list[ApprovalRequirement],
        tuple[ApprovalRequirement, ...],
    ]:
        boundary = context.permissions
        if command is not None:
            assert shell is not None
            plan, decision = await checker.prepare_line(
                boundary,
                command,
                shell=shell,
                cwd=cwd,
                env=env,
                home=Path.home(),
                workdir_parameter=workdir_parameter,
                computer_id=computer,
            )
        else:
            plan, decision = await checker.prepare_argv(
                boundary, argv or (), cwd=cwd, env=env, home=Path.home(), computer_id=computer
            )

        def collect() -> tuple[
            CommandPlan | None,
            tuple[PermissionRefusal, ...],
            list[ApprovalRequirement],
            tuple[ApprovalRequirement, ...],
        ]:
            missing: list[ApprovalRequirement] = []
            for refusal, requirement in decision.refused:
                if refusal.reason != "not_permitted" or requirement is None:
                    raise PermissionDenied(refusal)
                if requirement not in missing:
                    missing.append(requirement)
            if plan is not None and refuse_command is not None:
                for simple_command in plan.commands:
                    problem = refuse_command(simple_command)
                    if problem is not None:
                        detail, hint = problem
                        raise PermissionDenied(
                            boundary.refusal(
                                "command",
                                simple_command.source,
                                "unanalyzable",
                                detail=detail,
                                hint=hint,
                            )
                        )
            return plan, decision.refusals, missing, decision.path_identities

        return await run_permission_decision(collect)

    plan, refusals, missing, identities = await assess()
    if missing and not await context.authorize(
        missing, reason="Permission Rules", arguments=arguments, cwd=cwd
    ):
        raise (
            PermissionApprovalRejected(refusals[0])
            if context.approval_rejected
            else PermissionDenied(refusals[0])
        )

    async def recheck() -> None:
        fresh_plan, fresh_refusals, fresh_missing, fresh_identities = await assess()
        if fresh_identities != identities:
            raise ToolContractError(
                "The command's paths changed while the Tool was waiting. Call it again."
            )
        if fresh_plan is not None and plan is not None and fresh_plan != plan:
            raise ToolContractError(
                "The command changed while approval was pending. Call the Tool again."
            )
        if any(not context.permission_access.permits(item) for item in fresh_missing):
            if fresh_refusals:
                raise PermissionDenied(fresh_refusals[0])
            raise ToolContractError(
                "The command's access changed while approval was pending. Call the Tool again."
            )

    if missing:
        await recheck()
    context.add_permission_recheck(recheck)
