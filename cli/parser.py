"""Public CLI parser entrypoint; area builders own their command grammar."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

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


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse vBot CLI arguments without prompting for input."""

    parser = argparse.ArgumentParser(
        description="Manage vBot from the command line",
        epilog=(
            "Start with vbot <area> --help, then vbot <area> <command> --help. "
            "Use vbot config list [prefix] to discover Settings and vbot config describe <path> "
            "to inspect a setting before changing it. Append target options after the command; "
            "keep the same target on follow-up calls. Read mutation results for saved state "
            "and pending work; exit 0 alone does not prove runtime readiness."
        ),
    )
    subparsers = parser.add_subparsers(dest="area", required=True)
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
    tokens = list(sys.argv[1:] if argv is None else argv)
    operation_args = _extension_operation_args(tokens)
    args = operation_args if operation_args is not None else parser.parse_args(tokens)
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


def _extension_operation_args(tokens: list[str]) -> argparse.Namespace | None:
    if not tokens or tokens[0] != "extensions":
        return None
    target_parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    _add_target_arguments(target_parser)
    target, remaining = target_parser.parse_known_args(tokens[1:])
    if len(remaining) < 2 or remaining[0] in {"list", "reload", "enable", "disable"}:
        return None
    if remaining[1] == "set":
        return None
    return argparse.Namespace(
        area="extensions",
        selector=remaining[0],
        rest=remaining[1:],
        stdin=False,
        host=target.host,
        port=target.port,
        data_dir=target.data_dir,
    )
