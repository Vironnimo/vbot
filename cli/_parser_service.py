"""Optional service lifecycle on the selected vBot server."""

from __future__ import annotations

import argparse

from cli._parser_common import _add_command_parser


def add_service_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser("service", help="Manage optional services installed by vBot")
    actions = parser.add_subparsers(dest="command", required=True)
    descriptions = {
        "status": "Show an optional service's state, installation plan and pending work",
        "install": "Install and configure an optional service on the server",
        "start": "Start an installed optional service",
        "stop": "Stop an installed optional service",
        "update": "Update an installed optional service and verify its configuration",
        "remove": "Remove the optional service managed by vBot",
    }
    for name, description in descriptions.items():
        action = _add_command_parser(actions, name, description, example=f"service {name} searxng")
        action.add_argument("service_id", choices=("searxng",), help="Service to manage")
        action.add_argument("--json", action="store_true", help="Print one JSON result.")
        if name in {"install", "update"}:
            action.add_argument(
                "--install-prerequisites",
                action="store_true",
                help=(
                    "Allow installing the missing system prerequisites shown by service status. "
                    "Local system authorization can use this terminal; --json never prompts. "
                    "Omit to use existing prerequisites only."
                ),
            )
