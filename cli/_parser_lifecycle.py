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
)


def _add_server_parsers(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    server_parser = subparsers.add_parser(
        "server",
        help=AREA_HELP["server"],
        description=AREA_HELP["server"],
    )
    server_subparsers = server_parser.add_subparsers(dest="command", required=True)
    for command in SERVER_COMMANDS:
        _add_command_parser(server_subparsers, command, SERVER_HELP[command])


def _add_desktop_parsers(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    desktop_parser = subparsers.add_parser(
        "desktop",
        help=AREA_HELP["desktop"],
        description=(
            f"{AREA_HELP['desktop']}. Example: vbot desktop --host 192.168.1.50 --port 8420"
        ),
    )
    desktop_parser.add_argument(
        "--host",
        metavar="<host>",
        help="Server host to open; omitted auto-connects to the last-used server",
    )
    desktop_parser.add_argument(
        "--port",
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
        help="Target data directory; defaults to VBOT_DATA_DIR, worktree marker, or ~/.vbot",
    )


def _add_update_parsers(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    update_parser = subparsers.add_parser(
        "update",
        help=AREA_HELP["update"],
        description=f"{AREA_HELP['update']}. Example: vbot update",
    )
    update_parser.add_argument("update_action", nargs="?", choices=("status", "activate"))
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
            autostart_subparsers, command, AUTOSTART_HELP[command], example=f"autostart {command}"
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
            description=DOCTOR_HELP[command],
        )
        doctor_command_parser.add_argument(
            "--data-dir",
            help=(
                "Target vBot data directory; defaults to VBOT_DATA_DIR, worktree marker, or ~/.vbot"
            ),
        )
