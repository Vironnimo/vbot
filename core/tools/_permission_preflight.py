"""Preflight explicit Tool targets before any part of a call takes effect."""

from __future__ import annotations

import sys
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
from core.permissions.commands.commands import CommandPlan, ShellKind, command_pattern_matches
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
    computer = str(context.execution.get("computer_id", "server"))
    original = command if command is not None else " ".join(argv or ())

    async def assess() -> tuple[
        CommandPlan | None,
        tuple[PermissionRefusal, ...],
        list[ApprovalRequirement],
        tuple[ApprovalRequirement, ...],
    ]:
        boundary = context.permissions
        if command is not None:
            assert shell is not None
            plan, refusals = await checker.prepare_line(
                boundary,
                command,
                shell=shell,
                cwd=cwd,
                env=env,
                home=Path.home(),
                workdir_parameter=workdir_parameter,
            )
        else:
            plan, refusals = await checker.prepare_argv(
                boundary, argv or (), cwd=cwd, env=env, home=Path.home()
            )

        def collect() -> tuple[
            CommandPlan | None,
            tuple[PermissionRefusal, ...],
            list[ApprovalRequirement],
            tuple[ApprovalRequirement, ...],
        ]:
            for hard_refusal in refusals:
                if hard_refusal.reason != "not_permitted":
                    raise PermissionDenied(hard_refusal)
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
            missing: list[ApprovalRequirement] = []

            def add(item: ApprovalRequirement) -> None:
                if item not in missing:
                    missing.append(item)

            identities = [
                ApprovalRequirement.path(boundary, cwd, "start_dir", computer_id=computer)
            ]
            if plan is None:
                return plan, refusals, missing, tuple(identities)
            if any(item.access == "command" for item in refusals):
                windows = (
                    boundary.context.platform if boundary.context else sys.platform
                ) == "win32"
                for simple in plan.commands:
                    if any(
                        layer.commands == "listed"
                        and not any(
                            command_pattern_matches(pattern, simple, case_insensitive=windows)
                            for pattern in layer.shell_allow
                        )
                        for layer in boundary.layers
                    ):
                        identity = [(simple.name.casefold() if windows else simple.name, True)]
                        identity.extend((word.text, word.static) for word in simple.words)
                        add(
                            ApprovalRequirement.command(
                                original,
                                shell=shell or "argv",
                                cwd=cwd,
                                computer_id=computer,
                                identity=identity,
                            )
                        )
            root_refusal = boundary.check_path(cwd, "start_dir")
            if root_refusal is not None and root_refusal.reason == "not_permitted":
                add(ApprovalRequirement.path(boundary, cwd, "start_dir", computer_id=computer))
            for item in plan.paths:
                path = checker.command_path(boundary, item)
                identities.append(
                    ApprovalRequirement.path(
                        boundary,
                        path,
                        item.kind,
                        computer_id=computer,
                        subtree=item.subtree,
                        glob=item.glob,
                        pruned=item.pruned,
                        git=item.git,
                    )
                )
                refusal = boundary.check_path(
                    path,
                    item.kind,
                    subtree=item.subtree,
                    glob=item.glob,
                    pruned=item.pruned,
                    git=item.git,
                )
                if refusal is not None and refusal.reason == "not_permitted":
                    add(
                        ApprovalRequirement.path(
                            boundary,
                            path,
                            item.kind,
                            computer_id=computer,
                            subtree=item.subtree,
                            glob=item.glob,
                            pruned=item.pruned,
                            git=item.git,
                        )
                    )
            for url in plan.urls:
                refusal = boundary.check_url(url, by_program=True)
                if refusal is not None and refusal.reason == "not_permitted":
                    add(ApprovalRequirement.url(url, computer_id=computer))
            for host in plan.hosts:
                refusal = boundary.check_host(host, "connect", by_program=True)
                if refusal is not None and refusal.reason == "not_permitted":
                    add(ApprovalRequirement.host(host, "connect", computer_id=computer))
            return plan, refusals, missing, tuple(identities)

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
