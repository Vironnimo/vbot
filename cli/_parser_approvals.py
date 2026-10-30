"""CLI grammar for user-owned permission decisions."""

from __future__ import annotations

import argparse

from cli._parser_common import _add_command_parser


def _add_approval_parsers(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    group = subparsers.add_parser(
        "approvals", help="Inspect and answer waiting Tool permission requests"
    )
    actions = group.add_subparsers(dest="command", required=True)
    listing = _add_command_parser(
        actions, "list", "List pending permission requests", example="approvals list"
    )
    listing.add_argument(
        "--limit", type=int, default=20, help="Requests per page, 1-200. Omit for 20."
    )
    listing.add_argument("--offset", type=int, default=0, help="Requests to skip. Omit for 0.")
    listing.add_argument("--json", action="store_true", help="Print the snapshot page as JSON")
    for action, description in (
        ("show", "Show the exact prepared call and requested extra access"),
        ("allow", "Allow this exact prepared call once; user-only"),
        ("reject", "Reject the requested extra access for its Run; user-only"),
    ):
        parser = _add_command_parser(
            actions, action, description, example=f"approvals {action} <request-id>"
        )
        parser.add_argument("id", metavar="<request-id>", help="Exact id from approvals list")
        parser.add_argument("--json", action="store_true", help="Print the complete result as JSON")
