"""Command grammar for the worktree development utility."""

from __future__ import annotations

import argparse

from scripts._worktree_lock import DEFAULT_MERGE_WAIT_TIMEOUT_SECONDS, DEFAULT_REPAIR_WINDOW_SECONDS


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(description="Manage vBot git worktrees")
    subparsers = parser.add_subparsers(dest="command", required=True)

    create_parser = subparsers.add_parser("create", help="Create a worktree")
    create_parser.add_argument("name")
    create_parser.add_argument("--from", dest="from_branch", metavar="BRANCH")

    delete_parser = subparsers.add_parser("delete", help="Delete a worktree")
    delete_parser.add_argument("name")
    delete_parser.add_argument("--force", action="store_true")

    subparsers.add_parser("list", help="List worktrees")

    merge_parser = subparsers.add_parser(
        "merge",
        help="Merge a finished worktree branch into main and remove the worktree",
    )
    merge_parser.add_argument("name")
    merge_parser.add_argument("-m", "--message", default=None, metavar="SUMMARY")
    merge_parser.add_argument(
        "--wait-timeout",
        type=float,
        default=DEFAULT_MERGE_WAIT_TIMEOUT_SECONDS,
        metavar="SECONDS",
    )

    repair_start_parser = subparsers.add_parser(
        "repair-start",
        help="Open a protected repair window after a conflicted merge",
    )
    repair_start_parser.add_argument("name")
    repair_start_parser.add_argument(
        "--window",
        type=float,
        default=DEFAULT_REPAIR_WINDOW_SECONDS,
        metavar="SECONDS",
    )
    repair_start_parser.add_argument(
        "--wait-timeout",
        type=float,
        default=DEFAULT_MERGE_WAIT_TIMEOUT_SECONDS,
        metavar="SECONDS",
    )

    repair_finish_parser = subparsers.add_parser(
        "repair-finish",
        help="Close this task's protected repair window",
    )
    repair_finish_parser.add_argument("name")

    keeper_parser = subparsers.add_parser("keeper-hold", help=argparse.SUPPRESS)
    keeper_parser.add_argument("--task", required=True)
    keeper_parser.add_argument("--deadline", required=True, type=float)
    keeper_parser.add_argument("--lock-path", required=True)
    keeper_parser.add_argument("--holder-path", required=True)
    keeper_parser.add_argument("--release-path", required=True)

    return parser.parse_args(argv)
