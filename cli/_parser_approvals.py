"""CLI grammar for user-owned permission decisions."""

from __future__ import annotations

import argparse

from cli._parser_common import _add_command_parser


def add_permission_profile_parsers(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    group = subparsers.add_parser(
        "permission-profiles", help="Manage named Agent permission profiles"
    )
    actions = group.add_subparsers(dest="command", required=True)
    for action, description in (
        ("list", "List the built-in and custom profiles available to Agents"),
        ("show", "Show the file, command and network rules for one profile"),
        ("save", "Create or replace a custom profile for all Agents using its name"),
        ("delete", "Delete a custom profile"),
    ):
        parser = _add_command_parser(actions, action, description)
        if action != "list":
            parser.add_argument("name", help="Exact profile name")
        else:
            parser.add_argument(
                "--limit", type=int, default=40, help="Profiles per page, 1-200. Omit for 40."
            )
            parser.add_argument(
                "--offset", type=int, default=0, help="Profiles to skip. Omit for 0."
            )
        if action == "save":
            rules = parser.add_mutually_exclusive_group(required=True)
            rules.add_argument(
                "--file",
                help="Path to a JSON Permission Rules object. "
                "Omitted fields take their values from the restricted profile.",
            )
            rules.add_argument(
                "--stdin",
                action="store_true",
                help="Read the Permission Rules as one UTF-8 JSON object from stdin.",
            )
        parser.add_argument("--json", action="store_true", help="Print the result as JSON")


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
