"""Grammar for external history reads and explicit imports."""

from __future__ import annotations

import argparse

from cli._parser_common import AREA_HELP, _add_command_parser

SOURCES = ("codex", "claude-code", "opencode", "hermes", "openclaw")


def add_external_parsers(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    group = subparsers.add_parser("external", help=AREA_HELP["external"])
    commands = group.add_subparsers(dest="command", required=True)
    for name, description in (
        ("sources", "List supported sources and their default paths on the server"),
        ("list", "List saved external Sessions without importing or running them"),
        ("show", "Read or search a saved external Session without importing it"),
        ("memories", "Read external Memory files without importing them"),
        ("import-session", "Import a complete external Session and continue it in vBot"),
        ("import-memory", "Import reviewed facts from one external Memory file"),
    ):
        command = _add_command_parser(commands, name, description)
        command.add_argument(
            "--json", action="store_true", help="Print the result or error as JSON"
        )
        if name == "sources":
            continue
        command.add_argument("source", choices=SOURCES, help="Application that owns the source")
        command.add_argument(
            "--path",
            required=name == "import-memory",
            help=(
                "Memory source file on the vBot server."
                if name == "import-memory"
                else "Data directory or exported file on the vBot server. Omit for the "
                "source's default directory."
            ),
        )
        if name in {"show", "import-session"}:
            command.add_argument("session", help="Exact Session id from external list")
        if name in {"list", "show", "memories"}:
            command.add_argument(
                "--limit", type=int, default=30, help="Items per page, 1-100. Omit for 30."
            )
            command.add_argument("--offset", type=int, default=0, help="Items to skip. Omit for 0.")
        if name in {"show", "memories"}:
            command.add_argument(
                "--char-offset",
                type=int,
                default=0,
                help=(
                    "Characters to skip in the first entry. Omit for 0; follow Next for "
                    "complete large results."
                ),
            )
        if name in {"list", "show"}:
            command.add_argument(
                "--query", help="Case-insensitive text filter. Omit to read all items."
            )
        if name == "import-session":
            command.add_argument(
                "--agent", required=True, help="Destination Agent id or agent@project"
            )
            command.add_argument(
                "--working-project",
                help="Project the continued Session works in. Omit for the Agent's default.",
            )
        if name == "import-memory":
            owner = command.add_mutually_exclusive_group(required=True)
            owner.add_argument("--agent", help="Identity Agent that owns the destination Memory")
            owner.add_argument("--project", help="Project that owns the destination Memory")
            command.add_argument(
                "--scope",
                choices=("user", "agent", "project"),
                required=True,
                help="Destination Memory scope",
            )
            command.add_argument(
                "--entries-file",
                help=(
                    "UTF-8 or BOM-marked UTF-16 file of reviewed facts, separated by blank "
                    "lines. Omit to "
                    "import the source paragraphs unchanged."
                ),
            )
