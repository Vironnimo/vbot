"""CLI grammar for automation, statistics, Settings, and diagnostics."""

from __future__ import annotations

import argparse

from cli._parser_common import (
    AREA_HELP,
    BOOTSTRAP_HELP,
    BOOTSTRAP_MODES,
    CONFIG_HELP,
    CRON_DELIVERY_KINDS,
    CRON_HELP,
    CRON_STATUSES,
    DEBUG_HELP,
    DEVICES_HELP,
    PERFORMANCE_HELP,
    STATISTICS_HELP,
    STATISTICS_SECTIONS,
    THINKING_EFFORTS,
    _add_command_parser,
    _add_text_file_argument,
    server_url_argument,
)
from cli.device_pairing import PAIR_WAIT_SECONDS
from core.performance import (
    DEFAULT_HEAP_TOP,
    DEFAULT_RECORDING_SECONDS,
    HISTORY_RETENTION_DAYS,
    MAX_HEAP_TOP,
    MAX_LABEL_LENGTH,
    MAX_RECORDING_SECONDS,
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
    create_prompt = create_parser.add_mutually_exclusive_group(required=True)
    create_prompt.add_argument("--prompt", help="Prompt text injected when the job fires")
    _add_text_file_argument(create_prompt, content="prompt text", dest="prompt_file")
    create_schedule_group = create_parser.add_mutually_exclusive_group(required=True)
    _add_cron_schedule_arguments(create_schedule_group)
    _add_cron_event_time_argument(create_parser)
    create_parser.add_argument(
        "--repeat",
        type=int,
        metavar="<count>",
        help="Maximum future fires; recurring schedules are unlimited when omitted",
    )
    _add_cron_session_argument(create_parser)
    _add_cron_execution_arguments(create_parser, update=False)
    _add_cron_delivery_arguments(create_parser, update=False)
    _add_cron_email_filter_arguments(create_parser, update=False)
    create_parser.add_argument("--watch", metavar="<command-or-url>", help=_CRON_WATCH_HELP)
    create_parser.add_argument(
        "--watch-runs",
        choices=_CRON_WATCH_RUNS,
        help="When a job with --watch runs: on_change only when the output changed, always at "
        "every due time with the output and the change. Omit for on_change",
    )

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
    update_prompt = update_parser.add_mutually_exclusive_group()
    update_prompt.add_argument("--prompt", help="Prompt text injected when the job fires")
    _add_text_file_argument(update_prompt, content="prompt text", dest="prompt_file")
    update_schedule_group = update_parser.add_mutually_exclusive_group()
    _add_cron_schedule_arguments(update_schedule_group)
    _add_cron_event_time_argument(update_parser)
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
    _add_cron_execution_arguments(update_parser, update=True)
    _add_cron_delivery_arguments(update_parser, update=True)
    _add_cron_email_filter_arguments(update_parser, update=True)
    watch_group = update_parser.add_mutually_exclusive_group()
    watch_group.add_argument(
        "--watch",
        metavar="<command-or-url>",
        help=f"{_CRON_WATCH_HELP.removesuffix('. Omit for none')}. Omit to keep the current one",
    )
    watch_group.add_argument(
        "--clear-watch",
        action="store_true",
        help="Remove the watch, so every due time starts a Run",
    )
    update_parser.add_argument(
        "--watch-runs",
        choices=_CRON_WATCH_RUNS,
        help="When the watched job runs: on_change only when the output changed, always at "
        "every due time with the output and the change. Omit to keep the current mode",
    )

    for command in ("delete", "enable", "disable"):
        command_parser = _add_command_parser(
            cron_subparsers, command, CRON_HELP[command], example=f"cron {command} <job-id>"
        )
        command_parser.add_argument("id", metavar="<job-id>", help=f"Cron job id to {command}")

    secret_parser = _add_command_parser(
        cron_subparsers, "secret", CRON_HELP["secret"], example="cron secret <job-id>"
    )
    secret_parser.epilog = (
        "Prints the job's webhook URL, the secret and how a request proves it. Exit 0 when "
        "shown or replaced; 1 when the job has no webhook or is not found."
    )
    secret_parser.add_argument("id", metavar="<job-id>", help="Exact id of a webhook job")
    secret_parser.add_argument(
        "--rotate",
        action="store_true",
        help=(
            "Replace the secret; requests that send the old one are refused from then on. "
            "Omit to show the current secret"
        ),
    )


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
    group.add_argument(
        "--event",
        metavar="<event-id>",
        help="Run once at every occurrence of this calendar event",
    )
    group.add_argument(
        "--webhook",
        action="store_true",
        help=(
            "Run whenever a request reaches the job's webhook URL; each Run gets the request's "
            "headers and body. The output shows the URL and the secret requests send"
        ),
    )
    group.add_argument(
        "--email",
        metavar="<channel-id>",
        help=(
            "Run whenever this email Channel of the Agent receives a mail; each Run gets the "
            "mail. Narrow it with --email-from and --email-subject; vbot channel list shows "
            "the Channels"
        ),
    )


def _add_cron_email_filter_arguments(parser: argparse.ArgumentParser, *, update: bool) -> None:
    """Which mails start the Runs of an --email job."""
    keep = "Omit to keep the current filter" if update else None
    senders = parser.add_mutually_exclusive_group() if update else parser
    senders.add_argument(
        "--email-from",
        action="append",
        metavar="<address-or-@domain>",
        help=(
            "For --email jobs: start Runs only for mails from this address, or from any "
            "address of a domain such as @example.com; repeat for several. "
            + (keep or "Omit for any sender")
        ),
    )
    subject = parser.add_mutually_exclusive_group() if update else parser
    subject.add_argument(
        "--email-subject",
        metavar="<text>",
        help=(
            "For --email jobs: start Runs only for mails whose subject contains this text, "
            "ignoring case. " + (keep or "Omit for any subject")
        ),
    )
    if update:
        senders.add_argument(
            "--clear-email-from", action="store_true", help="Start Runs for mails from any sender"
        )
        subject.add_argument(
            "--clear-email-subject", action="store_true", help="Start Runs for any subject"
        )


_CRON_WATCH_HELP = (
    "Before each Run, run this command or read this page, and start the Run only when its "
    "output changed since the last check (see --watch-runs); the Run gets the change. An "
    "http:// or https:// "
    "address is read as a page; anything else runs in the host shell (PowerShell on Windows, "
    "bash elsewhere). Only for --cron and --every jobs. Omit for none"
)
_CRON_WATCH_RUNS = ("on_change", "always")


def _add_cron_execution_arguments(parser: argparse.ArgumentParser, *, update: bool) -> None:
    """Explicit working context and Run-only overrides, shared by create and update."""
    project = parser.add_mutually_exclusive_group()
    project.add_argument(
        "--working-project",
        metavar="<project-id>",
        help="Fix the Project for new Sessions of an Identity Agent. "
        + (
            "Omit to keep the choice"
            if update
            else "Omit to fix the Agent's current default Project"
        ),
    )
    project.add_argument(
        "--no-project",
        action="store_true",
        help="Run new Sessions of an Identity Agent in its Workspace; the choice stays fixed",
    )
    model = parser.add_mutually_exclusive_group()
    model.add_argument(
        "--model",
        metavar="<provider/model>",
        help="Model for this job's Runs only. "
        + ("Omit to keep the override" if update else "Omit to inherit the Session or Agent Model"),
    )
    if update:
        model.add_argument(
            "--clear-model", action="store_true", help="Inherit the Session or Agent Model again"
        )
    thinking = parser.add_mutually_exclusive_group()
    thinking.add_argument(
        "--thinking-effort",
        choices=(*(effort for effort in THINKING_EFFORTS if effort), "default"),
        help="Reasoning for this job's Runs only; default uses the Model's default. "
        + ("Omit to keep the override" if update else "Omit to inherit Session or Agent Reasoning"),
    )
    if update:
        thinking.add_argument(
            "--clear-thinking-effort",
            action="store_true",
            help="Inherit Session or Agent Reasoning again",
        )


def _add_cron_delivery_arguments(parser: argparse.ArgumentParser, *, update: bool) -> None:
    omit = "Omit to keep the current one" if update else "Omit for none"
    parser.add_argument(
        "--deliver",
        choices=CRON_DELIVERY_KINDS,
        help=(
            "Where each Run's final answer goes: app (the Run's Session shows as unread in vBot "
            "and the user gets a notification), chat (a Channel chat named by --channel and "
            f"--chat) or none (the answer stays in the Run's Session). {omit}"
        ),
    )
    parser.add_argument(
        "--channel",
        metavar="<channel-id>",
        help="For chat delivery: the Channel, by its id from vbot channel list",
    )
    parser.add_argument(
        "--chat",
        metavar="<chat-id>",
        help="For chat delivery: the chat on that Channel, such as one of its allowed_chat_ids",
    )
    parser.add_argument(
        "--thread",
        metavar="<thread-id>",
        help="For chat delivery: a thread or topic in that chat. Omit to post in the chat itself",
    )


def _add_cron_event_time_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--event-time",
        metavar="<event-time>",
        help=(
            "When a job bound to a calendar event runs at each occurrence: start or end, "
            "optionally + or - minutes (m), hours (h) or days (d) up to 31 days, for example "
            '"start - 30m"; defaults to start'
        ),
    )


def _add_cron_session_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--session",
        metavar="<session-id>",
        help="Run in this existing Session; creation defaults to a fresh Session per fire",
    )


def _add_devices_parsers(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    devices_parser = subparsers.add_parser(
        "devices",
        help=AREA_HELP["devices"],
        description=(
            f"{AREA_HELP['devices']}. Devices sign in only in the access mode "
            "'With login (internet)'; see vbot application access show."
        ),
    )
    commands = devices_parser.add_subparsers(dest="command", required=True)
    _add_command_parser(commands, "list", DEVICES_HELP["list"], example="devices list")
    approve = _add_command_parser(
        commands, "approve", DEVICES_HELP["approve"], example="devices approve K7QM-4XPA"
    )
    approve.add_argument("code", metavar="<code>", help="The code the new device shows")
    approve.add_argument(
        "--yes", action="store_true", help="Approve without showing the device and asking"
    )
    deny = _add_command_parser(
        commands, "deny", DEVICES_HELP["deny"], example="devices deny K7QM-4XPA"
    )
    deny.add_argument("code", metavar="<code>", help="The code the device shows")
    rename = _add_command_parser(
        commands, "rename", DEVICES_HELP["rename"], example='devices rename <id> "Work laptop"'
    )
    rename.add_argument("id", metavar="<device-id>", help="Exact id from devices list")
    rename.add_argument("name", metavar="<name>")
    revoke = _add_command_parser(
        commands, "revoke", DEVICES_HELP["revoke"], example="devices revoke <id>"
    )
    revoke.add_argument("id", metavar="<device-id>", help="Exact id from devices list")
    pair = _add_command_parser(
        commands, "pair", DEVICES_HELP["pair"], example="devices pair https://vbot.example.com"
    )
    pair.epilog = (
        "Shows a code that the user approves in the WebUI of a signed-in device (Settings > "
        "Access & devices > Add device) or on the server's computer with vbot devices approve "
        "<code>. In a terminal the command waits until the code is approved, denied or expired; "
        f"otherwise it waits {int(PAIR_WAIT_SECONDS)} seconds and then reports that the code "
        "still waits: run the same command again to keep waiting for that code. Afterwards, "
        "commands reach the server with --url and its address. Exit 0 when signed in, already "
        "signed in, no sign-in needed or still waiting; 1 when denied, expired or refused."
    )
    _add_server_address(pair, "The server's https:// address, as the browser opens it")
    logout = _add_command_parser(
        commands,
        "logout",
        DEVICES_HELP["logout"],
        example="devices logout https://vbot.example.com",
    )
    logout.epilog = (
        "Deletes the stored sign-in also when the server cannot be reached; the Device then "
        "stays listed there until it is revoked in the WebUI or unused for 90 days."
    )
    _add_server_address(logout, "The server's address, as vbot devices pair got it")


def _add_server_address(parser: argparse.ArgumentParser, help_text: str) -> None:
    """The positional server address of ``devices pair`` and ``devices logout``.

    The address is their whole target: the target options stay accepted (``--url``
    names the same server) but leave the help, so that it shows one way.
    """
    for action in parser._actions:
        if action.dest in {"host", "port", "url", "data_dir"}:
            action.help = argparse.SUPPRESS
    parser.add_argument(
        "address",
        nargs="?",
        type=server_url_argument,
        metavar="<url>",
        help=f"{help_text}. Omit to use VBOT_SERVER_URL",
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
    create_prompt = create.add_mutually_exclusive_group(required=True)
    create_prompt.add_argument("--prompt", help="Prompt injected after startup")
    _add_text_file_argument(create_prompt, content="prompt text", dest="prompt_file")
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
    update_prompt = update.add_mutually_exclusive_group()
    update_prompt.add_argument("--prompt", help="Replace the prompt injected after startup")
    _add_text_file_argument(update_prompt, content="prompt text", dest="prompt_file")
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
            example=f"statistics {section} --since 2026-06-01T00:00:00Z",
        )
        _add_statistics_window_arguments(section_parser)


def _add_statistics_window_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--since",
        metavar="<iso-datetime>",
        help=(
            "Count activity at or after this ISO 8601 time with an explicit offset, "
            "rounded down to a full hour"
        ),
    )
    parser.add_argument(
        "--until",
        metavar="<iso-datetime>",
        help=(
            "Count activity before this ISO 8601 time with an explicit offset, "
            "rounded up to a full hour"
        ),
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


def _add_performance_parsers(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    performance_parser = subparsers.add_parser(
        "performance",
        help=AREA_HELP["performance"],
        description=f"{AREA_HELP['performance']}. Alias: vbot perf",
    )
    performance_subparsers = performance_parser.add_subparsers(dest="command", required=True)

    _add_command_parser(
        performance_subparsers,
        "status",
        PERFORMANCE_HELP["status"],
        example="performance status",
    )
    start_parser = _add_command_parser(
        performance_subparsers,
        "record-start",
        PERFORMANCE_HELP["record-start"],
        example="performance record-start --label baseline --max-seconds 120",
    )
    start_parser.add_argument(
        "--label",
        metavar="<text>",
        help=f"Short label stored with the recording (at most {MAX_LABEL_LENGTH} characters)",
    )
    start_parser.add_argument(
        "--max-seconds",
        type=int,
        default=DEFAULT_RECORDING_SECONDS,
        metavar="<seconds>",
        help=(
            "Stop automatically after this many seconds "
            f"(1-{MAX_RECORDING_SECONDS}, default: {DEFAULT_RECORDING_SECONDS})"
        ),
    )
    _add_command_parser(
        performance_subparsers,
        "record-stop",
        PERFORMANCE_HELP["record-stop"],
        example="performance record-stop",
    )
    recordings_parser = _add_command_parser(
        performance_subparsers,
        "recordings",
        PERFORMANCE_HELP["recordings"],
        example="performance recordings --limit 5",
    )
    recordings_parser.add_argument(
        "--limit",
        type=int,
        default=20,
        metavar="<count>",
        help="Maximum recordings to list (1-100, default: 20)",
    )
    heap_parser = _add_command_parser(
        performance_subparsers,
        "heap",
        PERFORMANCE_HELP["heap"],
        example="performance heap --top 30",
    )
    heap_parser.add_argument(
        "--top",
        type=int,
        default=DEFAULT_HEAP_TOP,
        metavar="<count>",
        help=f"Rows per list (1-{MAX_HEAP_TOP}, default: {DEFAULT_HEAP_TOP})",
    )
    history_parser = _add_command_parser(
        performance_subparsers,
        "history",
        PERFORMANCE_HELP["history"],
        example='performance history --at "2026-09-28 16:40"',
    )
    history_parser.add_argument(
        "--hours",
        type=int,
        default=24,
        metavar="<hours>",
        help=f"How far back to show windows (1-{HISTORY_RETENTION_DAYS * 24}, default: 24)",
    )
    history_parser.add_argument(
        "--metric",
        action="append",
        default=[],
        metavar="<name>",
        help="Show this metric, counter or gauge per window instead (repeatable)",
    )
    history_parser.add_argument(
        "--at",
        metavar="<time>",
        help="Show the full windows containing this time (as in log lines: local time)",
    )
