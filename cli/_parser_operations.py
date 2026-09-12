"""CLI grammar for automation, statistics, Settings, and diagnostics."""

from __future__ import annotations

import argparse

from cli._parser_common import (
    AREA_HELP,
    BOOTSTRAP_HELP,
    BOOTSTRAP_MODES,
    CONFIG_HELP,
    CRON_HELP,
    CRON_STATUSES,
    DEBUG_HELP,
    STATISTICS_HELP,
    STATISTICS_SECTIONS,
    _add_command_parser,
)
from core.utils.config import DEFAULT_HOST


def _add_cron_parsers(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    cron_parser = subparsers.add_parser(
        "cron",
        help=AREA_HELP["cron"],
        description=AREA_HELP["cron"],
    )
    cron_subparsers = cron_parser.add_subparsers(dest="command", required=True)

    _add_command_parser(cron_subparsers, "list", CRON_HELP["list"], example="cron list")
    show = _add_command_parser(
        cron_subparsers,
        "show",
        "Show the complete saved job and prompt",
        example="cron show <job-id>",
    )
    show.add_argument("id", metavar="<job-id>", help="Exact id from cron list")

    create_parser = _add_command_parser(
        cron_subparsers,
        "create",
        CRON_HELP["create"],
        example=(
            'cron create builder@vbot --name "Morning news" '
            '--prompt "Check the news" --cron "0 9 * * *"'
        ),
    )
    create_parser.add_argument(
        "agent", metavar="<agent>", help="Agent that runs the job, as agent or agent@project"
    )
    create_parser.add_argument(
        "--name",
        help="Optional human-readable job name; defaults to the first useful prompt line",
    )
    create_parser.add_argument(
        "--prompt", required=True, help="Prompt text injected when the job fires"
    )
    create_schedule_group = create_parser.add_mutually_exclusive_group(required=True)
    _add_cron_schedule_arguments(create_schedule_group)
    create_parser.add_argument(
        "--repeat",
        type=int,
        metavar="<count>",
        help="Maximum future fires; recurring schedules are unlimited when omitted",
    )
    _add_cron_session_argument(create_parser)

    update_parser = _add_command_parser(
        cron_subparsers,
        "update",
        CRON_HELP["update"],
        example='cron update <job-id> --prompt "Check status and report"',
    )
    update_parser.add_argument("id", metavar="<job-id>", help="Cron job id to update")
    update_parser.add_argument(
        "--agent", metavar="<agent>", help="Agent that runs the job, as agent or agent@project"
    )
    update_parser.add_argument("--name", help="Replace the human-readable job name")
    update_parser.add_argument("--prompt", help="Prompt text injected when the job fires")
    update_schedule_group = update_parser.add_mutually_exclusive_group()
    _add_cron_schedule_arguments(update_schedule_group)
    update_parser.add_argument(
        "--repeat",
        type=int,
        metavar="<count>",
        help="Replace the number of remaining fires",
    )
    session_group = update_parser.add_mutually_exclusive_group()
    session_group.add_argument(
        "--session", metavar="<session-id>", help="Replace the pinned Session"
    )
    session_group.add_argument(
        "--clear-session", action="store_true", help="Create a fresh Session for each future fire"
    )
    update_parser.add_argument(
        "--status", choices=CRON_STATUSES, help="Set the job status directly"
    )

    for command in ("delete", "enable", "disable"):
        command_parser = _add_command_parser(
            cron_subparsers, command, CRON_HELP[command], example=f"cron {command} <job-id>"
        )
        command_parser.add_argument("id", metavar="<job-id>", help=f"Cron job id to {command}")


def _add_cron_schedule_arguments(group: argparse._MutuallyExclusiveGroup) -> None:
    group.add_argument(
        "--cron",
        metavar="<cron-expression>",
        help=(
            "Recurring schedule as exactly five cron fields (minimum one minute), for example "
            '"0 9 * * *"'
        ),
    )
    group.add_argument(
        "--every",
        type=int,
        metavar="<minutes>",
        help="Recurring fixed interval in whole minutes",
    )
    group.add_argument(
        "--at",
        metavar="<iso-datetime>",
        help="One-time schedule as an ISO 8601 datetime",
    )


def _add_cron_session_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--session",
        metavar="<session-id>",
        help="Run in this existing Session; creation defaults to a fresh Session per fire",
    )


def _add_bootstrap_parsers(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    bootstrap_parser = subparsers.add_parser(
        "bootstrap",
        help=AREA_HELP["bootstrap"],
        description=AREA_HELP["bootstrap"],
    )
    commands = bootstrap_parser.add_subparsers(dest="command", required=True)
    _add_command_parser(commands, "list", BOOTSTRAP_HELP["list"], example="bootstrap list")
    show = _add_command_parser(
        commands,
        "show",
        "Show the complete saved job and prompt",
        example="bootstrap show <job-id>",
    )
    show.add_argument("id", metavar="<job-id>", help="Exact id from bootstrap list")
    create = _add_command_parser(
        commands,
        "create",
        BOOTSTRAP_HELP["create"],
        example=(
            'bootstrap create --current-session --name "Verify update" '
            '--prompt "Check status and logs" --mode once'
        ),
    )
    create.add_argument(
        "agent",
        nargs="?",
        metavar="<agent>",
        help="Agent that runs the job, as agent or agent@project",
    )
    create.add_argument(
        "--current-session",
        action="store_true",
        help="Use the current vBot Run Agent and Session; available only inside its Bash command",
    )
    create.add_argument("--name", help="Optional human-readable job name")
    create.add_argument("--prompt", required=True, help="Prompt injected after startup")
    create.add_argument("--mode", required=True, choices=BOOTSTRAP_MODES)
    create.add_argument(
        "--session",
        metavar="<session-id>",
        help="Run in this existing Session; omit to create a fresh Session",
    )
    update = _add_command_parser(
        commands,
        "update",
        BOOTSTRAP_HELP["update"],
        example='bootstrap update <job-id> --prompt "Check status and logs"',
    )
    update.add_argument("id", metavar="<job-id>")
    update.add_argument("--agent", metavar="<agent>")
    update.add_argument("--name")
    update.add_argument("--prompt")
    update.add_argument("--mode", choices=BOOTSTRAP_MODES)
    session_group = update.add_mutually_exclusive_group()
    session_group.add_argument("--session", metavar="<session-id>")
    session_group.add_argument("--clear-session", action="store_true")
    for command in ("delete", "enable", "disable"):
        command_parser = _add_command_parser(
            commands,
            command,
            BOOTSTRAP_HELP[command],
            example=f"bootstrap {command} <job-id>",
        )
        command_parser.add_argument("id", metavar="<job-id>")


def _add_statistics_parsers(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    statistics_parser = subparsers.add_parser(
        "statistics",
        help=AREA_HELP["statistics"],
        description=AREA_HELP["statistics"],
    )
    statistics_subparsers = statistics_parser.add_subparsers(dest="command", required=True)
    for section in STATISTICS_SECTIONS:
        section_parser = _add_command_parser(
            statistics_subparsers,
            section,
            STATISTICS_HELP[section],
            example=f"statistics {section} --since 2026-06-01",
        )
        _add_statistics_window_arguments(section_parser)


def _add_statistics_window_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--since",
        metavar="<iso-datetime>",
        help="Only count activity at or after this ISO 8601 timestamp (server-validated)",
    )
    parser.add_argument(
        "--until",
        metavar="<iso-datetime>",
        help="Only count activity at or before this ISO 8601 timestamp (server-validated)",
    )


def _add_config_parsers(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    config_parser = subparsers.add_parser(
        "config",
        help=AREA_HELP["config"],
        description=AREA_HELP["config"],
    )
    config_subparsers = config_parser.add_subparsers(dest="command")
    config_parser.set_defaults(host=DEFAULT_HOST, port=None, data_dir=None)

    _add_command_parser(
        config_subparsers,
        "effective",
        CONFIG_HELP["effective"],
        example="config effective",
    )

    _add_command_parser(
        config_subparsers,
        "raw",
        CONFIG_HELP["raw"],
        example="config raw",
    )

    list_parser = _add_command_parser(
        config_subparsers,
        "list",
        CONFIG_HELP["list"],
        example="config list web_search",
    )
    list_parser.add_argument(
        "prefix",
        nargs="?",
        help="Optional public path prefix, for example web_search",
    )

    describe_parser = _add_command_parser(
        config_subparsers,
        "describe",
        CONFIG_HELP["describe"],
        example="config describe web_search.provider",
    )
    describe_parser.add_argument("path", metavar="<path>", help="Public Settings path")

    get_parser = _add_command_parser(
        config_subparsers,
        "get",
        CONFIG_HELP["get"],
        example="config get web_search.provider",
    )
    get_parser.add_argument("path", metavar="<path>", help="Public Settings path")
    get_parser.add_argument(
        "--details",
        action="store_true",
        help="Include configured value, source, default, type, and application lifecycle",
    )

    set_parser = _add_command_parser(
        config_subparsers,
        "set",
        CONFIG_HELP["set"],
        example="config set web_search.provider searxng",
    )
    set_parser.add_argument("path", metavar="<path>", help="Public Settings path")
    value_source = set_parser.add_mutually_exclusive_group(required=True)
    value_source.add_argument(
        "value", nargs="?", metavar="<value>", help="JSON value or plain text"
    )
    value_source.add_argument(
        "--stdin", action="store_true", help="Read an exact JSON value from UTF-8 stdin"
    )

    unset_parser = _add_command_parser(
        config_subparsers,
        "unset",
        CONFIG_HELP["unset"],
        example="config unset defaults.agent.temperature",
    )
    unset_parser.add_argument("path", metavar="<path>", help="Public Settings path")

    patch_parser = _add_command_parser(
        config_subparsers,
        "patch",
        CONFIG_HELP["patch"],
        example=(
            "config patch --set web_search.provider searxng "
            "--set web_search.searxng.base_url https://searxng.example"
        ),
    )
    patch_parser.add_argument(
        "--set",
        dest="set_values",
        action="append",
        nargs=2,
        default=[],
        metavar=("<path>", "<value>"),
        help="Set one path; repeat for multiple atomic changes",
    )
    patch_parser.add_argument(
        "--unset",
        dest="unset_paths",
        action="append",
        default=[],
        metavar="<path>",
        help="Unset one path; repeat for multiple atomic changes",
    )


def _add_debug_parsers(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    debug_parser = subparsers.add_parser(
        "debug",
        help=AREA_HELP["debug"],
        description=AREA_HELP["debug"],
    )
    debug_subparsers = debug_parser.add_subparsers(dest="command", required=True)

    _add_command_parser(debug_subparsers, "status", DEBUG_HELP["status"], example="debug status")
    _add_command_parser(debug_subparsers, "traces", DEBUG_HELP["traces"], example="debug traces")

    trace_parser = _add_command_parser(
        debug_subparsers, "trace", DEBUG_HELP["trace"], example="debug trace <trace-id>"
    )
    trace_parser.add_argument("trace_id", metavar="<trace-id>", help="Trace id to show")

    _add_command_parser(debug_subparsers, "clear", DEBUG_HELP["clear"], example="debug clear")

    probe_parser = _add_command_parser(
        debug_subparsers,
        "probe",
        DEBUG_HELP["probe"],
        example="debug probe openai --connection openai:api-key",
    )
    probe_parser.add_argument("provider", metavar="<provider-id>", help="Provider id to probe")
    probe_parser.add_argument(
        "--connection",
        required=True,
        metavar="<provider:connection-id>",
        help="Compositional connection id used for credentials",
    )
