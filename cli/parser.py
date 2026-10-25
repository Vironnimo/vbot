"""Public CLI parser entrypoint; area builders own their command grammar."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Mapping, Sequence
from difflib import get_close_matches
from typing import NoReturn, override

from cli._parser_agent_secrets import _add_agent_secrets_parsers
from cli._parser_agents import (
    _add_agent_parsers,
    _add_archive_parsers,
    _add_chat_parser,
    _add_project_parsers,
    _add_session_parsers,
    _add_tui_parser,
)
from cli._parser_connections import (
    _add_channel_parsers,
    _add_model_parsers,
    _add_provider_parsers,
    _add_task_model_parsers,
)
from cli._parser_content import (
    _add_extensions_parsers,
    _add_librarian_parsers,
    _add_log_parsers,
    _add_memory_parsers,
    _add_prompt_parsers,
    _add_skill_parsers,
    _add_tool_parsers,
)
from cli._parser_data_store import _add_data_store_parsers
from cli._parser_lifecycle import (
    _add_application_parsers,
    _add_autostart_parsers,
    _add_desktop_parsers,
    _add_doctor_parsers,
    _add_home_parser,
    _add_server_parsers,
    _add_uninstall_parser,
    _add_update_parsers,
)
from cli._parser_operations import (
    _add_bootstrap_parsers,
    _add_config_parsers,
    _add_cron_parsers,
    _add_debug_parsers,
    _add_devices_parsers,
    _add_performance_parsers,
    _add_statistics_parsers,
)
from cli._progress import color_enabled, status_line
from cli._recovery import format_command
from core.utils.config import DEFAULT_PORT

AREA_ALIASES = {
    "agents": "agent",
    "projects": "project",
    "sessions": "session",
    "archives": "archive",
    "channels": "channel",
    "tools": "tool",
    "prompts": "prompt",
    "logs": "log",
    "providers": "provider",
    "models": "model",
    "skills": "skill",
    "task-models": "task-model",
    "extension": "extensions",
    "device": "devices",
    "perf": "performance",
    "agent-secret": "agent-secrets",
}


class _CliParser(argparse.ArgumentParser):
    """Give discovery and syntax errors the same command-oriented vocabulary."""

    def __init__(self, *args, **kwargs) -> None:
        kwargs.setdefault("allow_abbrev", False)
        if "color" not in kwargs:
            # Help follows the CLI's color rule rather than Python's own, which would
            # also color a pipe under FORCE_COLOR. Subparsers inherit the decision;
            # a GUI process may have no stdout at all.
            kwargs["color"] = sys.stdout is not None and color_enabled(sys.stdout)
        super().__init__(*args, **kwargs)
        self._error_help_path = self.prog
        self._unknown_options: list[str] = []
        self._selected_parser = self
        self.set_defaults(_command_path=self.prog.removeprefix("vbot "))
        self.add_argument(
            "--output",
            choices=("auto", "human", "plain"),
            default=argparse.SUPPRESS,
            help=(
                "Presentation: auto uses readable terminal output; human also in pipes; "
                "plain preserves data output without progress or extra summaries"
            ),
        )

    @override
    def add_subparsers(self, **kwargs):
        kwargs.setdefault("metavar", "<command>")
        kwargs.setdefault("title", "commands")
        return super().add_subparsers(**kwargs)

    @override
    def error(self, message: str) -> NoReturn:
        help_path = getattr(self, "_error_help_path", self.prog)
        selected = self._selected_parser
        unknown = list(dict.fromkeys(selected._unknown_options))
        if message.startswith("unrecognized arguments:"):
            # Unconsumed values can be credentials. Name the bad flags, not their payloads.
            message = (
                "unrecognized options: " + ", ".join(unknown)
                if unknown
                else ("unexpected extra arguments; check the positional arguments in help")
            )
        hints = []
        for option in unknown[:3]:
            matches = get_close_matches(option, selected._option_string_actions, n=3, cutoff=0.7)
            if matches:
                hints.append(f"For {option}, did you mean {', '.join(matches)}?")
        details = "\n".join(
            [message, *hints, "No command was executed.", f"Help: {help_path} --help"]
        )
        self.exit(2, f"{status_line('error', details, stream=sys.stderr)}\n")

    @override
    def _parse_optional(self, arg_string):
        result = super()._parse_optional(arg_string)
        # A list of (action, option string, separator, explicit argument) interpretations.
        if result is not None and all(item[0] is None for item in result):
            self._unknown_options.append(arg_string.split("=", 1)[0])
        return result

    @override
    def _check_value(self, action, value) -> None:
        if isinstance(action, argparse._SubParsersAction) and value not in action.choices:
            matches = get_close_matches(value, action.choices, n=1)
            hint = f" Did you mean '{matches[0]}'?" if matches else ""
            raise argparse.ArgumentError(action, f"unknown command '{value}'.{hint}")
        try:
            super()._check_value(action, value)
        except argparse.ArgumentError as error:
            choices = [str(choice) for choice in action.choices or ()]
            matches = get_close_matches(str(value), choices, n=3, cutoff=0.7)
            hint = f" Did you mean {', '.join(matches)}?" if matches else ""
            raise argparse.ArgumentError(action, error.message + hint) from None


def build_parser() -> argparse.ArgumentParser:
    """Build the complete discoverable command tree without executing commands."""

    parser: argparse.ArgumentParser = _CliParser(
        prog="vbot",
        description=(
            "Manage vBot: vbot <area> <command> [target] [options]. "
            "Examples: vbot server restart; vbot provider list; vbot provider connect openai. "
            "Commands can have further subcommands. Options refine the action. "
            "Collection names also accept their plural, e.g. vbot providers list."
        ),
        epilog=(
            "Start with vbot <area> --help, then vbot <area> <command> --help. "
            "Use vbot config list [prefix] to discover Settings and vbot config describe <path> "
            "to inspect a setting before changing it. Put --url (or --host and --port) and "
            "--data-dir before the area or after the action, where that command accepts them; "
            "keep the same target on follow-up calls. Read mutation results for saved state "
            "and pending work; exit 0 alone does not prove runtime readiness."
        ),
    )
    subparsers = parser.add_subparsers(dest="area", required=True, title="areas", metavar="<area>")
    _add_server_parsers(subparsers)
    _add_desktop_parsers(subparsers)
    _add_home_parser(subparsers)
    _add_update_parsers(subparsers)
    _add_application_parsers(subparsers)
    _add_uninstall_parser(subparsers)
    _add_autostart_parsers(subparsers)
    _add_agent_parsers(subparsers)
    _add_project_parsers(subparsers)
    _add_session_parsers(subparsers)
    _add_archive_parsers(subparsers)
    _add_chat_parser(subparsers)
    _add_tui_parser(subparsers)
    _add_data_store_parsers(subparsers)
    _add_agent_secrets_parsers(subparsers)
    _add_channel_parsers(subparsers)
    _add_tool_parsers(subparsers)
    _add_prompt_parsers(subparsers)
    _add_log_parsers(subparsers)
    _add_provider_parsers(subparsers)
    _add_model_parsers(subparsers)
    _add_task_model_parsers(subparsers)
    _add_skill_parsers(subparsers)
    _add_memory_parsers(subparsers)
    _add_librarian_parsers(subparsers)
    _add_extensions_parsers(subparsers)
    _add_cron_parsers(subparsers)
    _add_bootstrap_parsers(subparsers)
    _add_devices_parsers(subparsers)
    _add_statistics_parsers(subparsers)
    _add_config_parsers(subparsers)
    _add_debug_parsers(subparsers)
    _add_performance_parsers(subparsers)
    _add_doctor_parsers(subparsers)
    help_parser = subparsers.add_parser("help", help="Show help for any command path")
    help_parser.add_argument("path", nargs="*")
    return parser


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse vBot CLI arguments without prompting for input."""
    parser = build_parser()
    tokens = list(sys.argv[1:] if argv is None else argv)
    root_output, root_target = _take_root_options(parser, tokens)
    if tokens and tokens[0] == "help":
        tokens = [*tokens[1:], "--help"]
    if tokens and tokens[0] in AREA_ALIASES:
        tokens[0] = AREA_ALIASES[tokens[0]]
    _normalize_extension_operation(tokens)
    if tokens and tokens[0] in {"--server", "--restart", "--start", "--stop"}:
        parser.error(
            "Use an area followed by an action, for example: vbot server restart. "
            f"Put target options before the area or after the action (e.g. --port {DEFAULT_PORT}). "
            "Server lifecycle commands run on the machine that owns the server."
        )
    selected, command_end = _select_command(parser, tokens)
    if command_end == len(tokens) and any(
        isinstance(action, argparse._SubParsersAction) for action in selected._actions
    ):
        tokens.append("--help")
    # Let the selected command own validation and defaults. Inserting before its
    # positionals also keeps Extension operation payloads after their own boundary.
    if tokens[command_end : command_end + 1] in (["--help"], ["-h"]):
        command_end += 1
    tokens[command_end:command_end] = root_target
    if isinstance(parser, _CliParser):
        parser._error_help_path = selected.prog
        if isinstance(selected, _CliParser):
            parser._selected_parser = selected
    args = parser.parse_args(tokens)
    if getattr(args, "area", None) == "extensions":
        action = getattr(args, "command", None)
        if action == "set":
            args.rest = ["set", args.field] + ([] if args.value is None else [args.value])
        elif action == "operations":
            args.rest = ["operations"]
        elif action == "run":
            args.rest = [args.operation, *args.rest]
    if root_output is not None and not hasattr(args, "output"):
        args.output = root_output
    explicit_target = getattr(args, "_target_arguments", {})
    if hasattr(args, "address"):
        _take_server_address(parser, args, explicit_target)
    if getattr(args, "url", None) is not None and {"host", "port"} & explicit_target.keys():
        parser.error("the server address already names the host and port; drop --host and --port")
    for clear, value in (
        ("clear_model", "model"),
        ("clear_fallback_models", "fallback_models"),
        ("clear_temperature", "temperature"),
        ("clear_top_p", "top_p"),
        ("clear_thinking_effort", "thinking_effort"),
        ("clear_compaction_policy", "compaction_policy"),
        ("default_workspace", "workspace"),
        ("clear_project", "project"),
        ("clear_default_agent", "default_agent"),
        ("clear_default_model", "default_model"),
        ("clear_default_temperature", "default_temperature"),
        ("clear_default_top_p", "default_top_p"),
        ("clear_default_thinking_effort", "default_thinking_effort"),
    ):
        if getattr(args, clear, False) and getattr(args, value, None) is not None:
            parser.error(
                f"--{clear.replace('_', '-')} cannot be combined with --{value.replace('_', '-')}"
            )
    if getattr(args, "area", None) == "chat":
        _check_chat_arguments(parser, args)
    if getattr(args, "area", None) == "archive" and args.command == "purge":
        _check_archive_purge_selection(parser, args)
    if args.area in {"update", "rollback"}:
        from cli._parser_lifecycle import check_version_arguments

        check_version_arguments(parser, args)
    return args


def _take_server_address(
    parser: argparse.ArgumentParser, args: argparse.Namespace, explicit: Mapping[str, object]
) -> None:
    """Make the ``<url>`` of ``devices pair`` and ``devices logout`` the command's target.

    ``--url`` names the same target; both may be given when they agree. Without
    either, ``VBOT_SERVER_URL`` or ``--host``/``--port`` must name the server.
    """
    from cli._server_target import parse_server_url, selected_server_url

    address = args.address
    del args.address
    if address is None:
        if selected_server_url(args) is None and not {"host", "port"} & explicit.keys():
            parser.error(
                "name the server's address, for example: "
                f"vbot {args._command_path} https://vbot.example.com"
            )
        return
    if (
        args.url is not None
        and parse_server_url(args.url).origin != parse_server_url(address).origin
    ):
        parser.error(f"<url> and --url name different servers; keep only {address}")
    args.url = address
    args._target_arguments = {**explicit, "url": address}


def _take_root_options(
    parser: argparse.ArgumentParser, tokens: list[str]
) -> tuple[str | None, list[str]]:
    output = None
    target: list[str] = []
    while tokens:
        option, separator, value = tokens[0].partition("=")
        if option not in {"--output", "--host", "--port", "--url", "--data-dir"}:
            break
        tokens.pop(0)
        if not separator:
            if not tokens:
                parser.error(f"{option} requires a value")
            value = tokens.pop(0)
        if option == "--output":
            if value not in {"auto", "human", "plain"}:
                parser.error("--output requires auto, human, or plain")
            output = value
        else:
            target.extend([f"{option}={value}"] if separator else [option, value])
    return output, target


def _check_archive_purge_selection(
    parser: argparse.ArgumentParser, args: argparse.Namespace
) -> None:
    """A purge names its entries or selects them with --all, never both.

    The errors never offer an --all command: deleting a whole selection stays the
    caller's explicit choice, so they point to the list and to deleting by id.
    """
    filters = [
        option
        for name in ("kind", "agent", "project")
        if getattr(args, name) is not None
        for option in (f"--{name}", getattr(args, name))
    ]
    # The suggested commands reach the server this one named.
    target = tuple(
        item
        for name, value in getattr(args, "_target_arguments", {}).items()
        for item in (f"--{name.replace('_', '-')}", str(value))
    )
    listing = format_command(("vbot", "archive", "list", *filters, *target))
    by_ids = (
        format_command(("vbot", "archive", "purge", *args.entry_ids, "--yes", *target))
        if args.entry_ids
        else " ".join(("vbot archive purge <entry-id>... --yes", format_command(target))).strip()
    )
    if args.all and args.entry_ids:
        parser.error(
            f"archive purge takes entry ids or --all, not both; to delete the named entries, "
            f"run '{by_ids}'"
        )
    if not args.all and not args.entry_ids:
        shown = "the matching entries" if filters else "the entries"
        parser.error(
            f"archive purge needs entry ids or --all; '{listing}' shows {shown} and their "
            f"ids, then run '{by_ids}'"
        )
    if filters and not args.all:
        parser.error(
            f"{', '.join(filters[::2])} only apply with --all; to delete the named entries, "
            f"run '{by_ids}'"
        )


def _check_chat_arguments(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """Refuse contradicting chat options; decide whether a message-less call opens the TUI.

    Without any message source on an interactive terminal, ``vbot chat`` opens
    the TUI (``args.open_tui``) when its options mean something there: the
    Agent, ``-c``, ``--session`` or ``--project``. Attachments alone are a message.
    """
    if args.ephemeral and args.continue_latest:
        parser.error(
            "--ephemeral starts a new Session, -c continues one; use one of them. "
            "Continue an Ephemeral Session with --session"
        )
    args.open_tui = False
    if args.prompt is not None or args.prompt_file is not None or args.attachments:
        return
    if not _stdin_is_terminal():
        return
    tui_options = not (
        args.json
        or args.workspace
        or args.ephemeral
        or args.model is not None
        or args.thinking_effort is not None
        or args.temperature is not None
        or args.top_p is not None
    )
    if tui_options and sys.stdout is not None and sys.stdout.isatty():
        args.open_tui = True
        return
    parser.error("chat needs a message: pass <prompt>, use --file, or pipe it on stdin")


def _stdin_is_terminal() -> bool:
    """A terminal stdin (or none) cannot supply an omitted message without prompting."""
    stdin = sys.stdin
    return stdin is None or stdin.isatty()


def _select_command(
    parser: argparse.ArgumentParser, tokens: list[str]
) -> tuple[argparse.ArgumentParser, int]:
    """Locate the command through the shared presentation options of its groups."""
    index = 0
    while index < len(tokens):
        commands = next(
            (a for a in parser._actions if isinstance(a, argparse._SubParsersAction)), None
        )
        if commands is None:
            break
        token = tokens[index]
        if token in commands.choices:
            parser = commands.choices[token]
            index += 1
        elif token == "--output" and index + 1 < len(tokens):
            index += 2
        elif token.startswith("--output="):
            index += 1
        else:
            break
    return parser, index


def _normalize_extension_operation(tokens: list[str]) -> None:
    """Route name-first calls through the same opaque argument boundary as ``run``."""
    if not tokens or tokens[0] != "extensions":
        return
    actions = {
        "list",
        "reload",
        "enable",
        "disable",
        "show",
        "set",
        "operations",
        "run",
    }
    positionals: list[int] = []
    index = 1
    while index < len(tokens):
        token = tokens[index]
        option, separator, _value = token.partition("=")
        if option in {"--host", "--port", "--url", "--data-dir", "--output"}:
            index += 1 if separator else 2
            continue
        if token.startswith("-"):
            break
        positionals.append(index)
        if len(positionals) == 2:
            break
        index += 1
    if not positionals or tokens[positionals[0]] in actions:
        return
    action = "show"
    if len(positionals) == 2:
        operation_index = positionals[1]
        operation = tokens[operation_index]
        action = operation if operation in {"set", "operations"} else "run"
        if action != "run":
            del tokens[operation_index]
    tokens.insert(1, action)
