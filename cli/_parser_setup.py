"""Shared first-setup entry points, with explicit interactive and scripted forms."""

from __future__ import annotations

import argparse

from cli._parser_common import _add_target_arguments


def add_setup_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser(
        "setup",
        help="Open guided setup, show its saved status, or finish it with current settings",
        description=(
            "Open the shared guided setup in an interactive terminal. Use --browser to open "
            "the WebUI instead. setup status and setup finish never ask for input. "
            "Finishing preserves current settings and suppresses automatic setup in every "
            "accessor; it does not require a working Provider. Example: vbot setup status"
        ),
    )
    parser.add_argument(
        "command",
        nargs="?",
        choices=("open", "status", "finish"),
        default="open",
        help="open shows the guide; status reads its saved state; finish ends it. Omit for open.",
    )
    parser.add_argument(
        "--browser", action="store_true", help="Open the guide in a browser. Omit for the terminal."
    )
    parser.add_argument(
        "--if-needed",
        action="store_true",
        help="Open only while setup is pending. Omit to reopen completed or skipped setup.",
    )
    parser.add_argument(
        "--json", action="store_true", help="Print status or finish as one JSON result."
    )
    _add_target_arguments(parser)
