"""Public CLI parser entrypoint; area builders own their command grammar."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from difflib import get_close_matches
from typing import NoReturn

from cli._parser_agents import (
    _add_agent_parsers,
    _add_project_parsers,
    _add_session_parsers,
    _add_session_store_parsers,
)
from cli._parser_common import (
    _add_target_arguments,
)
from cli._parser_connections import (
    _add_channel_parsers,
    _add_model_parsers,
    _add_provider_parsers,
    _add_task_model_parsers,
)
from cli._parser_content import (
    _add_extensions_parsers,
    _add_log_parsers,
    _add_memory_parsers,
    _add_prompt_parsers,
    _add_skill_parsers,
    _add_tool_parsers,
)
from cli._parser_lifecycle import (
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
    _add_statistics_parsers,
)
from cli._progress import status_line

AREA_ALIASES = {
    "agents": "agent",
    "projects": "project",
    "sessions": "session",
    "channels": "channel",
    "tools": "tool",
    "prompts": "prompt",
    "logs": "log",
    "providers": "provider",
    "models": "model",
    "skills": "skill",
    "task-models": "task-model",
    "extension": "extensions",
}


class _CliParser(argparse.ArgumentParser):
    """Give discovery and syntax errors the same command-oriented vocabulary."""

    def __init__(self, *args, **kwargs) -> None:
        kwargs.setdefault("allow_abbrev", False)
        super().__init__(*args, **kwargs)
        self._error_help_path = self.prog
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

    def add_subparsers(self, **kwargs):
        kwargs.setdefault("metavar", "<command>")
        kwargs.setdefault("title", "commands")
        return super().add_subparsers(**kwargs)

    def error(self, message: str) -> NoReturn:
        help_path = getattr(self, "_error_help_path", self.prog)
        self.exit(
            2, f"{status_line('error', message, stream=sys.stderr)}\nHelp: {help_path} --help\n"
        )

    def _check_value(self, action, value) -> None:
        if isinstance(action, argparse._SubParsersAction) and value not in action.choices:
            matches = get_close_matches(value, action.choices, n=1)
            hint = f" Did you mean '{matches[0]}'?" if matches else ""
            raise argparse.ArgumentError(action, f"unknown command '{value}'.{hint}")
        super()._check_value(action, value)


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
            "to inspect a setting before changing it. Append target options after the command; "
            "keep the same target on follow-up calls. Read mutation results for saved state "
            "and pending work; exit 0 alone does not prove runtime readiness."
        ),
    )
    subparsers = parser.add_subparsers(dest="area", required=True, title="areas", metavar="<area>")
    _add_server_parsers(subparsers)
    _add_desktop_parsers(subparsers)
    _add_home_parser(subparsers)
    _add_update_parsers(subparsers)
    _add_uninstall_parser(subparsers)
    _add_autostart_parsers(subparsers)
    _add_agent_parsers(subparsers)
    _add_project_parsers(subparsers)
    _add_session_parsers(subparsers)
    _add_session_store_parsers(subparsers)
    _add_channel_parsers(subparsers)
    _add_tool_parsers(subparsers)
    _add_prompt_parsers(subparsers)
    _add_log_parsers(subparsers)
    _add_provider_parsers(subparsers)
    _add_model_parsers(subparsers)
    _add_task_model_parsers(subparsers)
    _add_skill_parsers(subparsers)
    _add_memory_parsers(subparsers)
    _add_extensions_parsers(subparsers)
    _add_cron_parsers(subparsers)
    _add_bootstrap_parsers(subparsers)
    _add_statistics_parsers(subparsers)
    _add_config_parsers(subparsers)
    _add_debug_parsers(subparsers)
    _add_doctor_parsers(subparsers)
    help_parser = subparsers.add_parser("help", help="Show help for any command path")
    help_parser.add_argument("path", nargs="*")
    return parser


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse vBot CLI arguments without prompting for input."""
    parser = build_parser()
    tokens = list(sys.argv[1:] if argv is None else argv)
    root_output = None
    while tokens and (tokens[0] == "--output" or tokens[0].startswith("--output=")):
        option = tokens.pop(0)
        if option == "--output":
            if not tokens:
                parser.error("--output requires auto, human, or plain")
            root_output = tokens.pop(0)
        else:
            root_output = option.split("=", 1)[1]
        if root_output not in {"auto", "human", "plain"}:
            parser.error("--output requires auto, human, or plain")
    if tokens and tokens[0] == "help":
        tokens = [*tokens[1:], "--help"]
    if tokens and tokens[0] in AREA_ALIASES:
        tokens[0] = AREA_ALIASES[tokens[0]]
    if tokens and tokens[0] in {"--server", "--restart", "--start", "--stop"}:
        parser.error(
            "Use an area followed by an action, for example: vbot server restart. "
            "Put options after the action (e.g. --port 8420). "
            "Server lifecycle commands run on the machine that owns the server."
        )
    operation_args = _extension_operation_args(tokens, parser)
    if operation_args is None and _is_command_group(parser, tokens):
        tokens.append("--help")
    selected = parser
    for token in tokens:
        commands = next(
            (a for a in selected._actions if isinstance(a, argparse._SubParsersAction)), None
        )
        if commands is None or token not in commands.choices:
            break
        selected = commands.choices[token]
    if isinstance(parser, _CliParser):
        parser._error_help_path = selected.prog
    args = operation_args if operation_args is not None else parser.parse_args(tokens)
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
    for clear, value in (
        ("clear_model", "model"),
        ("clear_fallback_models", "fallback_models"),
        ("clear_temperature", "temperature"),
        ("clear_thinking_effort", "thinking_effort"),
        ("clear_compaction_policy", "compaction_policy"),
        ("default_workspace", "workspace"),
        ("clear_project", "project"),
        ("clear_default_agent", "default_agent"),
        ("clear_default_model", "default_model"),
        ("clear_default_temperature", "default_temperature"),
        ("clear_default_thinking_effort", "default_thinking_effort"),
    ):
        if getattr(args, clear, False) and getattr(args, value, None) is not None:
            parser.error(
                f"--{clear.replace('_', '-')} cannot be combined with --{value.replace('_', '-')}"
            )
    return args


def _is_command_group(parser: argparse.ArgumentParser, tokens: list[str]) -> bool:
    """A bare group is a discovery request, never an implicit mutation."""
    for token in tokens:
        commands = next(
            (a for a in parser._actions if isinstance(a, argparse._SubParsersAction)), None
        )
        if commands is None or token not in commands.choices:
            return False
        parser = commands.choices[token]
    return any(isinstance(a, argparse._SubParsersAction) for a in parser._actions)


def _extension_operation_args(
    tokens: list[str],
    parser: argparse.ArgumentParser,
) -> argparse.Namespace | None:
    """Keep name-first Extension calls, including opaque operation arguments, compatible."""
    if not tokens or tokens[0] != "extensions":
        return None
    target_parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    _add_target_arguments(target_parser)
    target, remaining = target_parser.parse_known_args(tokens[1:])
    if (
        not remaining
        or remaining[0].startswith("-")
        or remaining[0]
        in {
            "list",
            "reload",
            "enable",
            "disable",
            "show",
            "set",
            "operations",
            "run",
        }
    ):
        return None
    if len(remaining) == 1 or remaining[1] in {"set", "--help", "-h"}:
        action = "set" if len(remaining) > 1 and remaining[1] == "set" else "show"
        arguments = remaining[2:] if action == "set" else remaining[1:]
        selection = ["--host", target.host]
        if target.port is not None:
            selection.extend(["--port", str(target.port)])
        if target.data_dir is not None:
            selection.extend(["--data-dir", target.data_dir])
        return parser.parse_args(["extensions", action, remaining[0], *arguments, *selection])
    return argparse.Namespace(
        area="extensions",
        selector=remaining[0],
        rest=remaining[1:],
        stdin=False,
        host=target.host,
        port=target.port,
        data_dir=target.data_dir,
        _command_path="extensions run",
    )
