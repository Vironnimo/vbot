"""Recheck a generated artifact's actual destination before publishing it."""

from __future__ import annotations

from pathlib import Path

from core.permissions import PermissionDenied
from core.tools._tool_context import ToolContext
from core.tools.contracts import ToolContractError


async def check_generated_output(context: ToolContext, root: str | Path, path: Path) -> None:
    """Check a direct generated child against the call's fresh authority."""
    await context.revalidate_permissions()
    target_root = context.path_target(root, access="write")
    target = target_root / path.name
    if target.resolve() != path.resolve():
        raise ToolContractError("The output folder changed while generation was running.")
    refusal = context.permissions.check_path(target, "write")
    if refusal is None:
        return
    if refusal.reason == "not_permitted" and context.permission_access.output_write_allowed(
        target, target_root
    ):
        return
    raise PermissionDenied(refusal)
