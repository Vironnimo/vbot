"""Argparse tree for the vBot CLI.

Owns area/command parser construction, shared target options, and all
user-facing help text. Command dispatch and output live in cli/main.py.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from core.memory import MEMORY_PROMPT_MODES
from core.model_tasks import SUPPORTED_TASK_TYPES
from server.main import DEFAULT_HOST

SERVER_COMMANDS = ("start", "stop", "restart", "status")
THINKING_EFFORTS = ("", "none", "minimal", "low", "medium", "high", "xhigh", "max")
CHANNEL_PLATFORMS = ("discord", "telegram")
CHANNEL_DM_SCOPES = (
    "per_conversation",
    "main",
    "per_peer",
    "per_account_channel_peer",
)
CRON_STATUSES = ("active", "paused", "completed")
TASK_TYPES = tuple(sorted(SUPPORTED_TASK_TYPES))
AREA_HELP = {
    "server": "Start, stop, restart, and inspect the local server",
    "update": "Update the installation from git, refresh deps/WebUI, and restart",
    "agent": "Inspect and manage agent configs",
    "project": "Inspect and manage projects and their scanned teams",
    "session": "Inspect and manage agent chat sessions",
    "channel": "Inspect and manage channel configs",
    "tool": "Inspect public tool catalog",
    "prompt": "Inspect and manage prompt fragments",
    "log": "Inspect parsed server logs",
    "provider": "Inspect and configure provider connections",
    "model": "Inspect and refresh model catalogs",
    "task-model": "Inspect and manage specialized task-model bindings",
    "skill": "Inspect skill availability and diagnostics",
    "extensions": "Inspect and toggle loaded extensions",
    "cron": "Inspect and manage scheduled cron jobs",
    "config": "Inspect and update raw settings",
    "debug": "Inspect debug mode state and stored traces",
    "doctor": "Run local configuration health checks",
}
SERVER_HELP = {
    "start": "Start the local vBot server",
    "stop": "Stop the local vBot server",
    "restart": "Restart the local vBot server",
    "status": "Show local server status",
}
AGENT_HELP = {
    "list": "List configured agents",
    "show": "Show one agent config",
    "create": "Create an agent config",
    "update": "Update an agent config",
    "delete": "Delete an agent config",
}
PROJECT_HELP = {
    "add": "Add a project from a repo directory and show its scan preview",
    "list": "List configured projects",
    "show": "Show one project's config, team, and scan report",
    "set": "Update one project's config",
    "rm": "Remove a project, archiving its anchor",
}
SESSION_HELP = {
    "list": "List one agent's chat sessions",
    "create": "Create a new chat session for one agent",
    "delete": "Delete (archive) one agent's chat session",
    "link-channel": "Link a session to a channel conversation for outbound replies",
}
CHANNEL_HELP = {
    "add": "Create a channel config",
    "list": "List channel configs",
    "remove": "Delete a channel config",
    "update": "Update a channel config",
    "enable": "Enable a channel listener",
    "disable": "Disable a channel listener",
    "status": "Show one channel listener status",
}
PROMPT_HELP = {
    "list": "List editable prompt fragments",
    "update": "Replace one prompt fragment",
    "reset": "Reset one prompt fragment to bundled default",
    "preview": "Render one agent's complete system prompt",
}
LOG_HELP = {
    "list": "List available daily log files",
    "read": "Read parsed entries from one daily log file",
}
PROVIDER_HELP = {
    "list": "List provider connections and usability",
    "status": "Show one provider or connection status",
    "set-key": "Set an API-key provider credential",
    "unset-key": "Remove an API-key provider credential",
    "connect": "Start the OAuth device flow for one provider connection",
    "disconnect": "Remove the stored OAuth token for one provider connection",
    "connect-status": "Show OAuth connection and device-flow state",
}
MODEL_HELP = {
    "list": "List available models",
    "refresh": "Refresh model catalogs",
}
TASK_MODEL_HELP = {
    "list": "List configured task-model bindings",
    "targets": "List available targets for one task type",
    "options": "Show the option schema for one task-type target",
    "set": "Bind one task type to a target",
    "clear": "Remove one task-type binding",
}
CRON_HELP = {
    "list": "List scheduled cron jobs",
    "create": "Create a cron job for one agent",
    "update": "Update a cron job",
    "delete": "Delete a cron job",
    "enable": "Enable a cron job",
    "disable": "Disable a cron job",
}
CONFIG_HELP = {
    "get": "Show one raw settings key",
    "set": "Set one raw settings key",
}
DEBUG_HELP = {
    "status": "Show debug mode state and trace count",
    "traces": "List stored debug trace metadata",
    "trace": "Show one stored debug trace as JSON",
    "clear": "Delete all stored debug traces",
    "probe": "Fetch one provider's models endpoint and preview the response",
}
DOCTOR_HELP = {
    "settings": "Validate the target data-dir settings.json",
    "config": "Validate all user-editable JSON config files in the target data-dir",
}
TOOL_HELP = {"list": "List public registered tools"}
SKILL_HELP = {"list": "List skills and diagnostics"}
EXTENSIONS_HELP = {
    "list": "List loaded, failed, and disabled extensions",
    "enable": "Enable a disabled extension (restart-applied)",
    "disable": "Disable an extension (restart-applied)",
}


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse vBot CLI arguments without prompting for input."""

    parser = argparse.ArgumentParser(description="Manage vBot from the command line")
    subparsers = parser.add_subparsers(dest="area", required=True)
    _add_server_parsers(subparsers)
    _add_update_parsers(subparsers)
    _add_agent_parsers(subparsers)
    _add_project_parsers(subparsers)
    _add_session_parsers(subparsers)
    _add_channel_parsers(subparsers)
    _add_tool_parsers(subparsers)
    _add_prompt_parsers(subparsers)
    _add_log_parsers(subparsers)
    _add_provider_parsers(subparsers)
    _add_model_parsers(subparsers)
    _add_task_model_parsers(subparsers)
    _add_skill_parsers(subparsers)
    _add_extensions_parsers(subparsers)
    _add_cron_parsers(subparsers)
    _add_config_parsers(subparsers)
    _add_debug_parsers(subparsers)
    _add_doctor_parsers(subparsers)
    return parser.parse_args(argv)


def _add_target_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int)
    parser.add_argument("--data-dir")


def _add_command_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
    command: str,
    help_text: str,
    *,
    example: str | None = None,
) -> argparse.ArgumentParser:
    description = help_text if example is None else f"{help_text}. Example: {example}"
    command_parser = subparsers.add_parser(command, help=help_text, description=description)
    _add_target_arguments(command_parser)
    return command_parser


def _add_server_parsers(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    server_parser = subparsers.add_parser(
        "server",
        help=AREA_HELP["server"],
        description=AREA_HELP["server"],
    )
    server_subparsers = server_parser.add_subparsers(dest="command", required=True)
    for command in SERVER_COMMANDS:
        _add_command_parser(server_subparsers, command, SERVER_HELP[command])


def _add_agent_parsers(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    agent_parser = subparsers.add_parser(
        "agent",
        help=AREA_HELP["agent"],
        description=AREA_HELP["agent"],
    )
    agent_subparsers = agent_parser.add_subparsers(dest="command", required=True)

    _add_command_parser(agent_subparsers, "list", AGENT_HELP["list"], example="agent list")

    show_parser = _add_command_parser(
        agent_subparsers, "show", AGENT_HELP["show"], example="agent show assistant"
    )
    show_parser.add_argument("id", metavar="<agent-id>", help="Agent id to show")

    create_parser = _add_command_parser(
        agent_subparsers,
        "create",
        AGENT_HELP["create"],
        example='agent create coder "Coding Agent" --model openrouter/anthropic/claude-sonnet-4',
    )
    create_parser.add_argument("id", metavar="<agent-id>", help="Id for the new agent")
    create_parser.add_argument("name", metavar="<name>", help="Display name for the new agent")
    _add_agent_change_arguments(create_parser, include_name=False, include_session=False)

    update_parser = _add_command_parser(
        agent_subparsers,
        "update",
        AGENT_HELP["update"],
        example="agent update assistant --thinking-effort high",
    )
    update_parser.add_argument("id", metavar="<agent-id>", help="Agent id to update")
    _add_agent_change_arguments(update_parser, include_name=True, include_session=True)

    delete_parser = _add_command_parser(
        agent_subparsers, "delete", AGENT_HELP["delete"], example="agent delete coder"
    )
    delete_parser.add_argument("id", metavar="<agent-id>", help="Agent id to delete")


def _add_agent_change_arguments(
    parser: argparse.ArgumentParser,
    *,
    include_name: bool,
    include_session: bool,
) -> None:
    if include_name:
        parser.add_argument("--name", help="New display name")
    parser.add_argument("--model", help="Primary model as <provider>/<model-id>")
    parser.add_argument("--fallback-model", help="Fallback model as <provider>/<model-id>")
    parser.add_argument("--temperature", type=float, help="Sampling temperature (0.0-2.0)")
    parser.add_argument(
        "--clear-temperature",
        action="store_true",
        help="Clear the temperature override and inherit the default",
    )
    parser.add_argument(
        "--thinking-effort",
        choices=THINKING_EFFORTS,
        help="Reasoning effort; empty string means provider default",
    )
    parser.add_argument(
        "--clear-thinking-effort",
        action="store_true",
        help="Clear the thinking-effort override and inherit the default",
    )
    parser.add_argument(
        "--memory-prompt-mode",
        choices=MEMORY_PROMPT_MODES,
        help="Which workspace memory files become prompt-visible",
    )
    parser.add_argument(
        "--custom-system-prompt",
        choices=("true", "false"),
        help="Enable or disable the agent's own editable prompt fragments",
    )
    parser.add_argument("--allowed-tools", nargs="*", help="Replace the full tool allowlist")
    parser.add_argument("--allowed-skills", nargs="*", help="Replace the full skill allowlist")
    if include_session:
        parser.add_argument("--current-session-id", help="Switch the agent's current session")


def _add_project_parsers(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    project_parser = subparsers.add_parser(
        "project",
        help=AREA_HELP["project"],
        description=AREA_HELP["project"],
    )
    project_subparsers = project_parser.add_subparsers(dest="command", required=True)

    add_parser = _add_command_parser(
        project_subparsers,
        "add",
        PROJECT_HELP["add"],
        example="project add ./my-repo --name vbot --default-agent orchestrator",
    )
    add_parser.add_argument(
        "cwd", metavar="<path>", help="Repo directory the project's tools resolve paths against"
    )
    add_parser.add_argument("--name", metavar="<display-name>", help="Project display name")
    add_parser.add_argument("--default-agent", metavar="<agent-id>", help="Project default agent")
    add_parser.add_argument(
        "--default-model",
        metavar="<provider/model-id>",
        help="Project default model as <provider>/<model-id>",
    )
    add_parser.add_argument(
        "--default-temperature",
        type=float,
        metavar="<0.0-2.0>",
        help="Project default sampling temperature (0.0-2.0)",
    )
    add_parser.add_argument(
        "--clear-default-temperature",
        action="store_true",
        help="Clear the project default temperature (fall through to the global default)",
    )
    add_parser.add_argument(
        "--default-thinking-effort",
        choices=THINKING_EFFORTS,
        help="Project default reasoning effort; empty string means provider default",
    )
    add_parser.add_argument(
        "--clear-default-thinking-effort",
        action="store_true",
        help="Clear the project default thinking effort (fall through to the global default)",
    )
    add_parser.add_argument(
        "--auto-load",
        nargs="*",
        metavar="<file>",
        help="Repo files auto-loaded into project agent prompts",
    )

    _add_command_parser(project_subparsers, "list", PROJECT_HELP["list"], example="project list")

    show_parser = _add_command_parser(
        project_subparsers, "show", PROJECT_HELP["show"], example="project show vbot"
    )
    show_parser.add_argument("id", metavar="<project-id>", help="Project id to show")

    set_parser = _add_command_parser(
        project_subparsers,
        "set",
        PROJECT_HELP["set"],
        example="project set vbot --default-agent builder",
    )
    set_parser.add_argument("id", metavar="<project-id>", help="Project id to update")
    set_parser.add_argument(
        "--cwd", metavar="<path>", help="Re-point the repo directory of the project"
    )
    set_parser.add_argument("--name", metavar="<display-name>", help="New project display name")
    set_parser.add_argument(
        "--default-agent", metavar="<agent-id>", help="New project default agent"
    )
    set_parser.add_argument(
        "--default-model",
        metavar="<provider/model-id>",
        help="New project default model as <provider>/<model-id>",
    )
    set_parser.add_argument(
        "--default-temperature",
        type=float,
        metavar="<0.0-2.0>",
        help="New project default sampling temperature (0.0-2.0)",
    )
    set_parser.add_argument(
        "--clear-default-temperature",
        action="store_true",
        help="Clear the project default temperature (fall through to the global default)",
    )
    set_parser.add_argument(
        "--default-thinking-effort",
        choices=THINKING_EFFORTS,
        help="New project default reasoning effort; empty string means provider default",
    )
    set_parser.add_argument(
        "--clear-default-thinking-effort",
        action="store_true",
        help="Clear the project default thinking effort (fall through to the global default)",
    )
    set_parser.add_argument(
        "--auto-load",
        nargs="*",
        metavar="<file>",
        help="Replace the full auto-load file list",
    )

    rm_parser = _add_command_parser(
        project_subparsers, "rm", PROJECT_HELP["rm"], example="project rm vbot"
    )
    rm_parser.add_argument("id", metavar="<project-id>", help="Project id to remove")


def _add_session_parsers(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    session_parser = subparsers.add_parser(
        "session",
        help=AREA_HELP["session"],
        description=AREA_HELP["session"],
    )
    session_subparsers = session_parser.add_subparsers(dest="command", required=True)

    list_parser = _add_command_parser(
        session_subparsers, "list", SESSION_HELP["list"], example="session list orchestrator@vbot"
    )
    list_parser.add_argument(
        "agent", metavar="<agent>", help="Agent whose sessions to list, as agent or agent@projekt"
    )

    create_parser = _add_command_parser(
        session_subparsers,
        "create",
        SESSION_HELP["create"],
        example="session create orchestrator@vbot --make-current",
    )
    create_parser.add_argument(
        "agent", metavar="<agent>", help="Agent to create a session for, as agent or agent@projekt"
    )
    create_parser.add_argument(
        "--id", metavar="<session-id>", help="Explicit session id; omitted means server-generated"
    )
    create_parser.add_argument(
        "--make-current",
        action="store_true",
        help="Switch the agent's current session to the new session",
    )

    delete_parser = _add_command_parser(
        session_subparsers,
        "delete",
        SESSION_HELP["delete"],
        example="session delete assistant <session-id> --yes",
    )
    delete_parser.add_argument(
        "agent", metavar="<agent>", help="Agent owning the session, as agent or agent@projekt"
    )
    delete_parser.add_argument("session", metavar="<session-id>", help="Session id to delete")
    delete_parser.add_argument(
        "--yes",
        action="store_true",
        help="Confirm deletion; the session is archived (recoverable), not erased",
    )

    link_parser = _add_command_parser(
        session_subparsers,
        "link-channel",
        SESSION_HELP["link-channel"],
        example="session link-channel assistant <session-id> --channel tg-main --conversation 99",
    )
    link_parser.add_argument("agent", metavar="<agent-id>", help="Agent owning the session")
    link_parser.add_argument("session", metavar="<session-id>", help="Session id to link")
    link_parser.add_argument(
        "--channel", required=True, metavar="<channel-id>", help="Channel config id to link"
    )
    link_parser.add_argument(
        "--conversation",
        required=True,
        metavar="<platform-conv-id>",
        help="Platform conversation id, for example a Telegram chat id",
    )


def _add_channel_parsers(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    channel_parser = subparsers.add_parser(
        "channel",
        help=AREA_HELP["channel"],
        description=AREA_HELP["channel"],
    )
    channel_subparsers = channel_parser.add_subparsers(dest="command", required=True)

    add_parser = _add_command_parser(
        channel_subparsers,
        "add",
        CHANNEL_HELP["add"],
        example=(
            "channel add tg-main --platform telegram --agent assistant "
            "--token-env TELEGRAM_BOT_TOKEN_MAIN"
        ),
    )
    add_parser.add_argument("id", metavar="<channel-id>", help="Id for the new channel")
    add_parser.add_argument("--platform", required=True, choices=CHANNEL_PLATFORMS)
    add_parser.add_argument(
        "--agent", required=True, metavar="<agent-id>", help="Agent that handles channel messages"
    )
    add_parser.add_argument(
        "--token-env",
        required=True,
        metavar="<env-var>",
        help="Environment variable holding the bot token",
    )
    add_parser.add_argument("--dm-scope", default="per_conversation", choices=CHANNEL_DM_SCOPES)
    add_parser.add_argument(
        "--allow",
        type=str,
        nargs="*",
        default=[],
        metavar="<chat-id>",
        help="Allowed chat ids; empty denies all inbound chats",
    )

    _add_command_parser(channel_subparsers, "list", CHANNEL_HELP["list"], example="channel list")

    remove_parser = _add_command_parser(
        channel_subparsers, "remove", CHANNEL_HELP["remove"], example="channel remove tg-main"
    )
    remove_parser.add_argument("id", metavar="<channel-id>", help="Channel id to remove")

    update_parser = _add_command_parser(
        channel_subparsers,
        "update",
        CHANNEL_HELP["update"],
        example="channel update tg-main --agent coder",
    )
    update_parser.add_argument("id", metavar="<channel-id>", help="Channel id to update")
    update_parser.add_argument("--platform", choices=CHANNEL_PLATFORMS)
    update_parser.add_argument("--agent", metavar="<agent-id>")
    update_parser.add_argument("--token-env", metavar="<env-var>")
    update_parser.add_argument("--dm-scope", choices=CHANNEL_DM_SCOPES)
    update_parser.add_argument(
        "--allow",
        type=str,
        nargs="*",
        metavar="<chat-id>",
        help="Replace the full allowed chat-id list",
    )
    update_parser.add_argument("--enabled", choices=("true", "false"))

    for command in ("enable", "disable", "status"):
        command_parser = _add_command_parser(
            channel_subparsers,
            command,
            CHANNEL_HELP[command],
            example=f"channel {command} tg-main",
        )
        command_parser.add_argument("id", metavar="<channel-id>", help=f"Channel id to {command}")


def _add_tool_parsers(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    tool_parser = subparsers.add_parser(
        "tool",
        help=AREA_HELP["tool"],
        description=AREA_HELP["tool"],
    )
    tool_subparsers = tool_parser.add_subparsers(dest="command", required=True)
    _add_command_parser(tool_subparsers, "list", TOOL_HELP["list"], example="tool list")


def _add_extensions_parsers(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    extensions_parser = subparsers.add_parser(
        "extensions",
        help=AREA_HELP["extensions"],
        description=AREA_HELP["extensions"],
    )
    extensions_subparsers = extensions_parser.add_subparsers(dest="command", required=True)
    _add_command_parser(
        extensions_subparsers, "list", EXTENSIONS_HELP["list"], example="extensions list"
    )

    for command in ("enable", "disable"):
        command_parser = _add_command_parser(
            extensions_subparsers,
            command,
            EXTENSIONS_HELP[command],
            example=f"extensions {command} guard_bash",
        )
        command_parser.add_argument(
            "name", metavar="<extension-name>", help=f"Extension name to {command}"
        )


def _add_prompt_parsers(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    prompt_parser = subparsers.add_parser(
        "prompt",
        help=AREA_HELP["prompt"],
        description=AREA_HELP["prompt"],
    )
    prompt_subparsers = prompt_parser.add_subparsers(dest="command", required=True)

    _add_command_parser(prompt_subparsers, "list", PROMPT_HELP["list"], example="prompt list")

    update_parser = _add_command_parser(
        prompt_subparsers,
        "update",
        PROMPT_HELP["update"],
        example="prompt update identity --file identity.md",
    )
    update_parser.add_argument("name", metavar="<fragment-name>", help="Prompt fragment to update")
    content_group = update_parser.add_mutually_exclusive_group(required=True)
    content_group.add_argument("--content", help="New fragment content as inline text")
    content_group.add_argument(
        "--file", dest="content_file", metavar="<path>", help="Read fragment content from a file"
    )

    reset_parser = _add_command_parser(
        prompt_subparsers, "reset", PROMPT_HELP["reset"], example="prompt reset identity"
    )
    reset_parser.add_argument("name", metavar="<fragment-name>", help="Prompt fragment to reset")

    preview_parser = _add_command_parser(
        prompt_subparsers, "preview", PROMPT_HELP["preview"], example="prompt preview assistant"
    )
    preview_parser.add_argument(
        "agent",
        metavar="<agent>",
        help="Agent whose system prompt to render, as agent or agent@projekt",
    )


def _add_log_parsers(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    log_parser = subparsers.add_parser(
        "log",
        help=AREA_HELP["log"],
        description=AREA_HELP["log"],
    )
    log_subparsers = log_parser.add_subparsers(dest="command", required=True)
    _add_command_parser(log_subparsers, "list", LOG_HELP["list"], example="log list")
    read_parser = _add_command_parser(
        log_subparsers, "read", LOG_HELP["read"], example="log read 2026-06-11.log"
    )
    read_parser.add_argument("file", metavar="<log-file>", help="Daily log file name to read")


def _add_provider_parsers(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    provider_parser = subparsers.add_parser(
        "provider",
        help=AREA_HELP["provider"],
        description="Inspect and configure vBot provider connections",
    )
    provider_subparsers = provider_parser.add_subparsers(dest="command", required=True)

    _add_command_parser(provider_subparsers, "list", PROVIDER_HELP["list"], example="provider list")

    status_parser = _add_command_parser(
        provider_subparsers, "status", PROVIDER_HELP["status"], example="provider status openai"
    )
    status_parser.add_argument("provider", metavar="<provider-id>", help="Provider id to inspect")
    status_parser.add_argument(
        "--connection",
        metavar="<provider:connection-id>",
        help="Narrow to one compositional connection id, for example openai:api-key",
    )

    set_key_parser = _add_command_parser(
        provider_subparsers,
        "set-key",
        PROVIDER_HELP["set-key"],
        example="provider set-key openai sk-... --refresh-models",
    )
    set_key_parser.description = (
        "Write an API key to the target data-dir .env through the server RPC contract. "
        "Example: provider set-key openai sk-... --refresh-models"
    )
    set_key_parser.add_argument(
        "provider", metavar="<provider-id>", help="Provider id to configure"
    )
    set_key_parser.add_argument("value", metavar="<api-key>", help="API key value to persist")
    set_key_parser.add_argument(
        "--connection",
        metavar="<provider:connection-id>",
        help="Required when the provider has multiple API-key connections",
    )
    set_key_parser.add_argument(
        "--account",
        metavar="<account-id>",
        help="Named credential slot on the connection (default: default)",
    )
    set_key_parser.add_argument(
        "--refresh-models",
        action="store_true",
        help="Refresh this provider's model catalog after setting the key",
    )

    unset_key_parser = _add_command_parser(
        provider_subparsers,
        "unset-key",
        PROVIDER_HELP["unset-key"],
        example="provider unset-key openai",
    )
    unset_key_parser.description = (
        "Remove an API key from the target data-dir .env through the server RPC contract. "
        "Process-environment credentials are not touched. Example: provider unset-key openai"
    )
    unset_key_parser.add_argument("provider", metavar="<provider-id>", help="Provider id to clear")
    unset_key_parser.add_argument(
        "--connection",
        metavar="<provider:connection-id>",
        help="Required when the provider has multiple API-key connections",
    )
    unset_key_parser.add_argument(
        "--account",
        metavar="<account-id>",
        help="Named credential slot on the connection (default: default)",
    )

    for command in ("connect", "disconnect", "connect-status"):
        command_parser = _add_command_parser(
            provider_subparsers,
            command,
            PROVIDER_HELP[command],
            example=f"provider {command} openai --connection openai:subscription",
        )
        command_parser.add_argument(
            "provider", metavar="<provider-id>", help="Provider id of the OAuth connection"
        )
        command_parser.add_argument(
            "--connection",
            required=True,
            metavar="<provider:connection-id>",
            help="Compositional OAuth connection id, for example openai:subscription",
        )
        command_parser.add_argument(
            "--account",
            metavar="<account-id>",
            help="Named credential slot on the connection (default: default)",
        )


def _add_model_parsers(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    model_parser = subparsers.add_parser(
        "model",
        help=AREA_HELP["model"],
        description=AREA_HELP["model"],
    )
    model_subparsers = model_parser.add_subparsers(dest="command", required=True)
    _add_command_parser(model_subparsers, "list", MODEL_HELP["list"], example="model list")
    refresh_parser = _add_command_parser(
        model_subparsers, "refresh", MODEL_HELP["refresh"], example="model refresh openrouter"
    )
    refresh_parser.add_argument(
        "provider",
        nargs="?",
        metavar="<provider-id>",
        help="Refresh only this provider; omitted means all refreshable providers",
    )


def _add_task_model_parsers(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    task_model_parser = subparsers.add_parser(
        "task-model",
        help=AREA_HELP["task-model"],
        description=AREA_HELP["task-model"],
    )
    task_model_subparsers = task_model_parser.add_subparsers(dest="command", required=True)

    _add_command_parser(
        task_model_subparsers, "list", TASK_MODEL_HELP["list"], example="task-model list"
    )

    targets_parser = _add_command_parser(
        task_model_subparsers,
        "targets",
        TASK_MODEL_HELP["targets"],
        example="task-model targets speech_to_text",
    )
    targets_parser.add_argument("task_type", metavar="<task-type>", choices=TASK_TYPES)

    options_parser = _add_command_parser(
        task_model_subparsers,
        "options",
        TASK_MODEL_HELP["options"],
        example="task-model options text_to_speech openai/gpt-4o-mini-tts::api-key",
    )
    options_parser.add_argument("task_type", metavar="<task-type>", choices=TASK_TYPES)
    options_parser.add_argument(
        "target",
        metavar="<target-id>",
        help="Target id as <provider>/<model>::<connection> or local/<id>",
    )

    set_parser = _add_command_parser(
        task_model_subparsers,
        "set",
        TASK_MODEL_HELP["set"],
        example="task-model set text_embedding openai/text-embedding-3-small::api-key",
    )
    set_parser.add_argument("task_type", metavar="<task-type>", choices=TASK_TYPES)
    set_parser.add_argument(
        "target",
        metavar="<target-id>",
        help="Target id as <provider>/<model>::<connection> or local/<id>",
    )
    set_parser.add_argument(
        "--options",
        dest="options_json",
        metavar="<json>",
        help='Task options as a JSON object, for example \'{"voice": "alloy"}\'',
    )

    clear_parser = _add_command_parser(
        task_model_subparsers,
        "clear",
        TASK_MODEL_HELP["clear"],
        example="task-model clear image_generation",
    )
    clear_parser.add_argument("task_type", metavar="<task-type>", choices=TASK_TYPES)


def _add_skill_parsers(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    skill_parser = subparsers.add_parser(
        "skill",
        help=AREA_HELP["skill"],
        description=AREA_HELP["skill"],
    )
    skill_subparsers = skill_parser.add_subparsers(dest="command", required=True)
    _add_command_parser(skill_subparsers, "list", SKILL_HELP["list"], example="skill list")


def _add_cron_parsers(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    cron_parser = subparsers.add_parser(
        "cron",
        help=AREA_HELP["cron"],
        description=AREA_HELP["cron"],
    )
    cron_subparsers = cron_parser.add_subparsers(dest="command", required=True)

    _add_command_parser(cron_subparsers, "list", CRON_HELP["list"], example="cron list")

    create_parser = _add_command_parser(
        cron_subparsers,
        "create",
        CRON_HELP["create"],
        example='cron create builder@vbot --prompt "Check the news" --cron "0 9 * * *"',
    )
    create_parser.add_argument(
        "agent", metavar="<agent>", help="Agent that runs the job, as agent or agent@projekt"
    )
    create_parser.add_argument(
        "--prompt", required=True, help="Prompt text injected when the job fires"
    )
    create_schedule_group = create_parser.add_mutually_exclusive_group(required=True)
    _add_cron_schedule_arguments(create_schedule_group)
    _add_cron_optional_arguments(create_parser)

    update_parser = _add_command_parser(
        cron_subparsers,
        "update",
        CRON_HELP["update"],
        example="cron update <job-id> --status paused",
    )
    update_parser.add_argument("id", metavar="<job-id>", help="Cron job id to update")
    update_parser.add_argument(
        "--agent", metavar="<agent>", help="Agent that runs the job, as agent or agent@projekt"
    )
    update_parser.add_argument("--prompt", help="Prompt text injected when the job fires")
    update_schedule_group = update_parser.add_mutually_exclusive_group()
    _add_cron_schedule_arguments(update_schedule_group)
    _add_cron_optional_arguments(update_parser)
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
        help='Recurring schedule as a cron expression, for example "0 9 * * *"',
    )
    group.add_argument(
        "--at",
        metavar="<iso-datetime>",
        help="One-time schedule as an ISO 8601 datetime",
    )


def _add_cron_optional_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--timezone",
        metavar="<iana-timezone>",
        help="IANA timezone for the schedule, for example Europe/Berlin",
    )
    parser.add_argument(
        "--session",
        metavar="<session-id>",
        help="Run in this fixed session instead of a job-managed session",
    )


def _add_config_parsers(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    config_parser = subparsers.add_parser(
        "config",
        help=AREA_HELP["config"],
        description=AREA_HELP["config"],
    )
    config_subparsers = config_parser.add_subparsers(dest="command")
    _add_target_arguments(config_parser)

    get_parser = _add_command_parser(
        config_subparsers, "get", CONFIG_HELP["get"], example="config get recall"
    )
    get_parser.add_argument("key", metavar="<key>", help="Top-level settings key to show")

    set_parser = _add_command_parser(
        config_subparsers, "set", CONFIG_HELP["set"], example="config set port 8500"
    )
    set_parser.add_argument("key", metavar="<key>", help="Top-level settings key to set")
    set_parser.add_argument(
        "value", metavar="<value>", help="New value; parsed as JSON, falling back to plain text"
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


def _add_update_parsers(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    update_parser = subparsers.add_parser(
        "update",
        help=AREA_HELP["update"],
        description=f"{AREA_HELP['update']}. Example: vbot update",
    )
    _add_target_arguments(update_parser)
    local_changes = update_parser.add_mutually_exclusive_group()
    local_changes.add_argument(
        "--discard",
        action="store_true",
        help="Discard local changes to tracked files before updating",
    )
    local_changes.add_argument(
        "--stash",
        action="store_true",
        help="Stash local changes, update, then reapply them",
    )
    update_parser.add_argument(
        "--no-restart",
        action="store_true",
        help="Update the code without restarting the server afterward",
    )


def _add_doctor_parsers(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    doctor_parser = subparsers.add_parser(
        "doctor",
        help=AREA_HELP["doctor"],
        description=AREA_HELP["doctor"],
    )
    doctor_subparsers = doctor_parser.add_subparsers(dest="command", required=True)
    for command in ("settings", "config"):
        doctor_command_parser = doctor_subparsers.add_parser(
            command,
            help=DOCTOR_HELP[command],
            description=DOCTOR_HELP[command],
        )
        doctor_command_parser.add_argument(
            "--data-dir",
            help=(
                "Target vBot data directory; defaults to VBOT_DATA_DIR, worktree marker, or ~/.vbot"
            ),
        )
