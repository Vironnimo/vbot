"""CLI grammar for local lifecycle and installation commands."""

from __future__ import annotations

import argparse

from cli._parser_common import (
    AREA_HELP,
    AUTOSTART_HELP,
    DOCTOR_HELP,
    SERVER_COMMANDS,
    SERVER_HELP,
    _add_command_parser,
    _TargetArgument,
)
from core.utils.config import DEFAULT_PORT


def _add_server_parsers(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    server_parser = subparsers.add_parser(
        "server",
        help=AREA_HELP["server"],
        description=AREA_HELP["server"],
    )
    server_subparsers = server_parser.add_subparsers(dest="command", required=True)
    for command in SERVER_COMMANDS:
        _add_command_parser(server_subparsers, command, SERVER_HELP[command], remote=False)


def _add_desktop_parsers(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    desktop_parser = subparsers.add_parser(
        "desktop",
        help=AREA_HELP["desktop"],
        description=(
            f"{AREA_HELP['desktop']}. "
            f"Example: vbot desktop --host 192.168.1.50 --port {DEFAULT_PORT}"
        ),
    )
    desktop_parser.add_argument(
        "--host",
        action=_TargetArgument,
        metavar="<host>",
        help="Server host to open; omitted auto-connects to the last-used server",
    )
    desktop_parser.add_argument(
        "--port",
        action=_TargetArgument,
        type=int,
        metavar="<port>",
        help="Server port to open; omitted auto-connects to the last-used server",
    )


def _add_home_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    home_parser = subparsers.add_parser(
        "home",
        help=AREA_HELP["home"],
        description=f"{AREA_HELP['home']}. Example: vbot home",
    )
    home_parser.add_argument(
        "--data-dir",
        action=_TargetArgument,
        help="Target data directory; defaults to VBOT_DATA_DIR, worktree marker, or ~/.vbot",
    )


def _add_update_parsers(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    update_parser = subparsers.add_parser(
        "update",
        help=AREA_HELP["update"],
        description=(
            "Install the newest version of the configured channel, or an exact release "
            "in X.Y.Z or vX.Y.Z form. An older release keeps current data and must pass "
            "vBot's startup/data check; Extensions are checked on normal load. "
            "The selected version does not change the update channel. "
            "Example: vbot update"
        ),
    )
    update_parser.add_argument(
        "update_action",
        nargs="?",
        type=_update_selector,
        metavar="<version|status|activate>",
        help="Exact release, status, or activate. Omit to update from the configured channel",
    )
    update_parser.add_argument("operation_id", nargs="?")
    update_parser.add_argument(
        "--detach",
        action="store_true",
        help="Return after the independent updater accepts the operation",
    )
    update_parser.add_argument(
        "--package", help="Use this deliberately selected local application package"
    )
    update_parser.add_argument(
        "--no-restart",
        action="store_true",
        help="Prepare the update without activating it; activate it later with: update activate",
    )
    rollback = subparsers.add_parser(
        "rollback",
        help="Return to the previous installation, keeping current data",
        description=(
            "Return to the installation before the last activated version change. "
            "Current data is kept unless --restore-data names its retained snapshot "
            "and --yes confirms the data reset. A reset first saves the current data. "
            "Example: vbot rollback"
        ),
    )
    rollback.add_argument("--detach", action="store_true", help="Return after acceptance")
    rollback.add_argument(
        "--restore-data",
        metavar="<snapshot-id>",
        help="Restore the snapshot named by the failed rollback; omit to keep current data",
    )
    rollback.add_argument("--yes", action="store_true", help="Confirm the selected data reset")


def _update_selector(value: str) -> str:
    if value in {"status", "activate"}:
        return value
    from cli.application.state import normalize_release_version

    try:
        return normalize_release_version(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def check_version_arguments(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if args.area == "rollback":
        if args.yes and args.restore_data is None:
            parser.error("--yes confirms --restore-data; omit it to keep current data")
        return
    if args.area != "update":
        return
    selected_version = args.update_action not in {None, "status", "activate"}
    if selected_version and (args.package or args.operation_id):
        parser.error("a release version cannot be combined with a package or operation id")
    if args.update_action in {"status", "activate"} and (args.package or args.no_restart):
        parser.error("update status and update activate do not accept --package or --no-restart")
    if args.update_action is None and args.operation_id:
        parser.error("an operation id requires update status or update activate")


def _add_application_parsers(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    from cli.application.command import add_parsers

    add_parsers(subparsers)


def _add_uninstall_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    uninstall_parser = subparsers.add_parser(
        "uninstall",
        help=AREA_HELP["uninstall"],
        description=f"{AREA_HELP['uninstall']}. Example: vbot uninstall",
    )
    modes = uninstall_parser.add_mutually_exclusive_group()
    modes.add_argument(
        "--app-only",
        action="store_const",
        const="app-only",
        dest="uninstall_mode",
        help="Remove the application and Autostart while preserving data",
    )
    modes.add_argument(
        "--data-only",
        action="store_const",
        const="data-only",
        dest="uninstall_mode",
        help="Delete the data directory while preserving the application",
    )
    modes.add_argument(
        "--all",
        action="store_const",
        const="all",
        dest="uninstall_mode",
        help="Remove both the application and its data directory",
    )
    uninstall_parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip the confirmation prompt (a removal mode is still required without a TTY)",
    )


def _add_autostart_parsers(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    autostart_parser = subparsers.add_parser(
        "autostart",
        help=AREA_HELP["autostart"],
        description=AREA_HELP["autostart"],
    )
    autostart_subparsers = autostart_parser.add_subparsers(dest="command", required=True)
    for command in ("enable", "disable", "status"):
        _add_command_parser(
            autostart_subparsers,
            command,
            AUTOSTART_HELP[command],
            example=f"autostart {command}",
            remote=False,
        )


def _add_doctor_parsers(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    doctor_parser = subparsers.add_parser(
        "doctor",
        help=AREA_HELP["doctor"],
        description=AREA_HELP["doctor"],
    )
    doctor_subparsers = doctor_parser.add_subparsers(dest="command", required=True)
    for command in ("settings", "config"):
        doctor_command_parser = doctor_subparsers.add_parser(
            command,
            help=DOCTOR_HELP[command],
            description=(
                DOCTOR_HELP[command] + ". Read-only; no server needed. "
                "Reports file paths, errors, warnings and repair recommendations. "
                "Runtime, Provider connectivity and database integrity are not checked. "
                "Exit 0: no errors (warnings can remain); 1: errors or incomplete checks; "
                "2: invalid arguments."
            ),
        )
        doctor_command_parser.add_argument(
            "--data-dir",
            action=_TargetArgument,
            help=(
                "Target vBot data directory; defaults to VBOT_DATA_DIR, worktree marker, or ~/.vbot"
            ),
        )
        doctor_command_parser.add_argument(
            "--json",
            action="store_true",
            dest="json_output",
            help="Print one JSON report with diagnostic codes and recommendations; omit for text",
        )
        doctor_command_parser.add_argument(
            "--limit",
            type=_doctor_limit,
            default=20,
            help="Findings per page (1-100); omit for up to 20 within the text size budget",
        )
        doctor_command_parser.add_argument(
            "--offset",
            type=_doctor_offset,
            default=0,
            help="Skip this many findings (errors first); omit to start at the first finding",
        )


def _doctor_limit(value: str) -> int:
    number = int(value)
    if not 1 <= number <= 100:
        raise argparse.ArgumentTypeError("must be an integer from 1 to 100")
    return number


def _doctor_offset(value: str) -> int:
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must be a non-negative integer")
    return number
