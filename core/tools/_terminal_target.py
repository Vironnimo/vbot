"""Computer selection and Session authority for the Terminal Tool."""

from __future__ import annotations

from core.computers import ComputerError, ComputerInfo, normalize_execution
from core.tools._workdir import is_project_workdir
from core.tools.arguments import optional_string
from core.tools.contracts import ToolContractError
from core.tools.terminal_manager import (
    TerminalInfo,
    TerminalManager,
    TerminalOwner,
)
from core.tools.tools import JsonObject, ToolContext

REMOTE_RESTRICTED = (
    "Nothing was started, sent or shown. This Session has Permission Rules, which check "
    "paths and commands on the server and cannot check another computer. Use a terminal on "
    "the server within those rules, or ask the user to choose an Agent with full access."
)


def allowed_computers(context: ToolContext) -> set[str]:
    return set(normalize_execution(context.execution)["allowed_computer_ids"])


def check_terminal_target(
    manager: TerminalManager,
    context: ToolContext,
    info: TerminalInfo,
    arguments: JsonObject,
) -> None:
    """An existing id selects its original computer, subject to today's grants."""
    if info.computer_id not in allowed_computers(context):
        raise ComputerError(
            "computer_not_allowed",
            "This terminal belongs to a computer that is not enabled for this Session. "
            "Nothing was sent or shown. The user can enable it in the Session's computer settings.",
        )
    target = arguments.get("computer_id")
    if (
        isinstance(target, str)
        and target
        and target != info.computer_id
        and target.casefold() != info.computer_name.casefold()
    ):
        raise ToolContractError(
            f"Nothing was sent: {info.terminal_id} belongs to {info.computer_name}, not {target}. "
            "Existing terminals stay on their original computer. Use its terminal_id "
            "without computer_id, or start a terminal on the intended computer."
        )
    if info.computer_id != "server" and context.permissions.restricted:
        raise ComputerError("permission_denied", REMOTE_RESTRICTED)
    if manager.computers is not None and arguments["action"] not in {"status", "detach"}:
        manager.computers.resolve(context, info.computer_id, "terminal")


async def start_remote(
    manager: TerminalManager,
    context: ToolContext,
    arguments: JsonObject,
    computer: ComputerInfo,
    *,
    command: str | None,
    args: list[str],
    columns: int,
    rows: int,
) -> TerminalInfo:
    if context.permissions.restricted:
        raise ComputerError("permission_denied", REMOTE_RESTRICTED)
    workdir = optional_string(arguments.get("workdir"), field_name="workdir")
    if workdir and is_project_workdir(workdir):
        raise ToolContractError(
            "No terminal was started: project: paths belong to the server. Pass a directory "
            "on the selected computer, or omit workdir for its home directory."
        )
    return await manager.spawn_remote(
        TerminalOwner(context.project_id, context.agent_id, context.session_id),
        computer,
        command=command,
        arguments=args,
        workdir=workdir,
        columns=columns,
        rows=rows,
        origin_run_id=context.run_id,
        execution_owner=context.execution_owner,
    )


def optional_string_array(value: object, *, field_name: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ToolContractError(f"terminal was not run: {field_name} must be an array of strings.")
    if any("\x00" in item for item in value):
        raise ToolContractError(
            f"terminal was not run: {field_name} must not contain NUL characters."
        )
    return list(value)
