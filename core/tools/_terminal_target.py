"""Computer selection and Session authority for the Terminal Tool."""

from __future__ import annotations

from core.computers import ComputerError, ComputerInfo, normalize_execution
from core.tools._workdir import is_project_workdir
from core.tools.arguments import optional_int, optional_string
from core.tools.contracts import ToolContractError
from core.tools.terminal_manager import (
    TERMINAL_DEFAULT_COLUMNS,
    TERMINAL_DEFAULT_ROWS,
    TERMINAL_MAX_COLUMNS,
    TERMINAL_MAX_ROWS,
    TERMINAL_MIN_COLUMNS,
    TERMINAL_MIN_ROWS,
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
    manager: TerminalManager, context: ToolContext, arguments: JsonObject, computer: ComputerInfo
) -> TerminalInfo:
    if context.permissions.restricted:
        raise ComputerError("permission_denied", REMOTE_RESTRICTED)
    command = optional_string(arguments.get("command"), field_name="command")
    args = optional_string_array(arguments.get("args"), field_name="args")
    if command is None and args:
        raise ToolContractError(
            "terminal was not run: args needs command, the program to start with them."
        )
    requested_id = arguments.get("terminal_id")
    if requested_id and any(
        item.terminal_id == requested_id and not item.finished for item in manager.list_terminals()
    ):
        raise ToolContractError(
            "terminal was not run: start opens a new terminal. "
            "Use input with the existing terminal_id."
        )
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
        columns=optional_int(
            arguments.get("columns"),
            field_name="columns",
            default=TERMINAL_DEFAULT_COLUMNS,
            minimum=TERMINAL_MIN_COLUMNS,
            maximum=TERMINAL_MAX_COLUMNS,
        ),
        rows=optional_int(
            arguments.get("rows"),
            field_name="rows",
            default=TERMINAL_DEFAULT_ROWS,
            minimum=TERMINAL_MIN_ROWS,
            maximum=TERMINAL_MAX_ROWS,
        ),
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
