"""Argparse tree for the vBot CLI.

Owns area/command parser construction, shared target options, and all
user-facing help text. Command dispatch and output live in cli/main.py.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence

from core.channels import (
    ALLOWED_CHANNEL_DM_SCOPES,
    ALLOWED_CHANNEL_PLATFORMS,
    ALLOWED_CHANNEL_RESPONSE_MODES,
)
from core.memory import MEMORY_PROMPT_MODES
from core.model_tasks import SUPPORTED_TASK_TYPES
from core.providers.reasoning import THINKING_EFFORT_ORDER
from core.settings import PROJECT_SOURCE_FORMATS
from core.utils.config import DEFAULT_HOST

SERVER_COMMANDS = ("start", "stop", "restart", "status")
# Empty string = provider default; the rest is the canonical effort ladder.
THINKING_EFFORTS = ("", *THINKING_EFFORT_ORDER)
# Argparse choices need a deterministic order; the canonical sets are unordered.
CHANNEL_PLATFORMS = tuple(sorted(ALLOWED_CHANNEL_PLATFORMS))
CHANNEL_DM_SCOPES = tuple(sorted(ALLOWED_CHANNEL_DM_SCOPES))
CHANNEL_RESPONSE_MODES = tuple(sorted(ALLOWED_CHANNEL_RESPONSE_MODES))
CRON_STATUSES = ("active", "paused")
STATISTICS_SECTIONS = ("overview", "usage", "runs", "errors", "tools", "skills")
TASK_TYPES = tuple(sorted(SUPPORTED_TASK_TYPES))
AREA_HELP = {
    "server": "Start, stop, restart, and inspect the local server",
    "desktop": "Open the desktop window pointed at a local or remote server",
    "home": "Show the application and data directories",
    "update": "Update the installation from git, refresh deps/WebUI, and restart",
    "uninstall": "Remove the application, its data, or both with explicit confirmation",
    "autostart": "Enable, disable, or inspect OS autostart for the server",
    "agent": "Inspect and manage agent configs",
    "project": "Inspect and manage projects and their scanned teams",
    "session": "Inspect and manage agent chat sessions",
    "channel": "Inspect and manage channel configs",
    "tool": "Inspect public tool catalog",
    "prompt": "Inspect and manage System Prompt blocks",
    "log": "Inspect parsed server logs",
    "provider": "Inspect and configure provider connections",
    "model": "Inspect and refresh model catalogs",
    "task-model": "Inspect and manage specialized task-model bindings",
    "skill": "Inspect skill availability and diagnostics",
    "extensions": "Inspect and toggle loaded extensions",
    "cron": "Inspect and manage scheduled cron jobs",
    "statistics": "Inspect usage statistics computed from persisted sessions",
    "config": "Inspect and update public Settings paths",
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
    "rename": "Change an Identity Agent id and retarget live references",
    "delete": "Delete an agent config",
}
PROJECT_HELP = {
    "add": "Add a project from a repo directory and show its scan preview",
    "list": "List configured projects",
    "show": "Show one project's config, team, and scan report",
    "set": "Update one project's config",
    "set-override": "Set one project-team agent override",
    "clear-override": "Clear one project-team agent override",
    "rm": "Remove a project, archiving its anchor",
}
SESSION_HELP = {
    "list": "List one agent's chat sessions",
    "create": "Create a new chat session for one agent",
    "delete": "Delete (archive) one agent's chat session",
    "fork": "Fork a session, optionally to another agent",
    "rename": "Set or clear a session's display title",
    "set-compaction-policy": "Set or clear a Session Policy override",
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
    "set-token": "Set or rotate a channel bot token from stdin",
}
PROMPT_HELP = {
    "list": "List System Prompt blocks",
    "update": "Replace one editable prompt block's text",
    "reset": "Reset one editable prompt block to its inherited default",
    "create": "Create a custom user prompt block",
    "remove": "Remove a custom user prompt block",
    "set-layout": "Replace a scope's prompt block order and enabled states",
    "reset-layout": "Reset a scope's prompt layout to the bundled default",
    "preview": "Render one agent's complete system prompt",
}
LOG_HELP = {
    "list": "List available daily log files",
    "read": "Read parsed entries from one daily log file",
}
PROVIDER_HELP = {
    "list": "List provider connections and usability",
    "status": "Show one provider or connection status",
    "usage": "Show live Provider subscription usage and reset windows",
    "set-key": "Set an API-key provider credential",
    "unset-key": "Remove an API-key provider credential",
    "enable": "Enable a provider connection (local providers start disabled)",
    "disable": "Disable a provider connection (no probes, no listed models)",
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
STATISTICS_HELP = {
    "overview": "Show the overview section: agents, sessions, runs, and message totals",
    "usage": "Show the usage section: token totals and per-provider/model breakdowns",
    "runs": "Show the runs section: counts, status rates, and durations",
    "errors": "Show the errors section: totals and breakdowns by kind, provider, and agent",
    "tools": "Show the tools section: call counts and per-tool success rates",
    "skills": "Show Skill offers, activations, and evidence-backed offer conversion",
}
CONFIG_HELP = {
    "list": "List public Settings paths and metadata",
    "describe": "Describe one public Settings path",
    "effective": "Show all normalized public Settings values",
    "raw": "Show the internal settings.json document for diagnostics",
    "get": "Show one effective public Settings value",
    "set": "Set one public Settings path",
    "unset": "Remove one configured Settings override",
    "patch": "Apply multiple Settings changes atomically",
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
AUTOSTART_HELP = {
    "enable": "Register OS autostart and start the server now",
    "disable": "Remove the OS autostart entry",
    "status": "Show whether OS autostart is registered",
}
TOOL_HELP = {"list": "List public registered tools"}
SKILL_HELP = {
    "list": "List effective skills and diagnostics",
    "read": "Read editable skills in a global or private agent scope",
    "create": "Create a skill in a global or private agent scope",
    "update": "Replace a skill's SKILL.md in an editable scope",
    "delete": "Delete a skill from an editable scope",
    "write-file": "Write one supporting file inside an editable skill",
    "remove-file": "Remove one supporting file from an editable skill",
}
EXTENSIONS_HELP = {
    "list": "List loaded, failed, and disabled extensions",
    "reload": "Reload all extensions from disk (applies code changes, applied live)",
    "enable": "Enable a disabled extension (applied live)",
    "disable": "Disable an extension (applied live)",
    "show": "Show one extension's settings (schema, current values, secret set-state)",
    "set": "Set one extension setting (secret -> .env, other fields -> live config)",
}


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse vBot CLI arguments without prompting for input."""

    parser = argparse.ArgumentParser(description="Manage vBot from the command line")
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
    _add_statistics_parsers(subparsers)
    _add_config_parsers(subparsers)
    _add_debug_parsers(subparsers)
    _add_doctor_parsers(subparsers)
    return parser.parse_args(argv)


def _add_target_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int)
    parser.add_argument("--data-dir")


def _json_object_argument(raw: str) -> dict[str, object]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise argparse.ArgumentTypeError(f"invalid JSON: {exc.msg}") from exc
    if not isinstance(value, dict):
        raise argparse.ArgumentTypeError("value must be a JSON object")
    return value


def _json_array_argument(raw: str) -> list[object]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise argparse.ArgumentTypeError(f"invalid JSON: {exc.msg}") from exc
    if not isinstance(value, list):
        raise argparse.ArgumentTypeError("value must be a JSON array")
    return value


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
        command_parser = _add_command_parser(server_subparsers, command, SERVER_HELP[command])
        if command == "restart":
            command_parser.add_argument(
                "--service-name",
                help=(
                    "systemd user unit to restart when the install is unit-managed (default: vbot)"
                ),
            )


def _add_desktop_parsers(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    desktop_parser = subparsers.add_parser(
        "desktop",
        help=AREA_HELP["desktop"],
        description=(
            f"{AREA_HELP['desktop']}. Example: vbot desktop --host 192.168.1.50 --port 8420"
        ),
    )
    desktop_parser.add_argument(
        "--host",
        metavar="<host>",
        help="Server host to open; omitted auto-connects to the last-used server",
    )
    desktop_parser.add_argument(
        "--port",
        type=int,
        metavar="<port>",
        help="Server port to open; omitted auto-connects to the last-used server",
    )


def _add_home_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    home_parser = subparsers.add_parser(
        "home",
        help=AREA_HELP["home"],
        description=f"{AREA_HELP['home']}. Example: vbot home",
    )
    home_parser.add_argument(
        "--data-dir",
        help="Target data directory; defaults to VBOT_DATA_DIR, worktree marker, or ~/.vbot",
    )


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
    _add_agent_change_arguments(
        create_parser,
        include_name=False,
        include_session=False,
        include_location=False,
    )

    update_parser = _add_command_parser(
        agent_subparsers,
        "update",
        AGENT_HELP["update"],
        example="agent update assistant --thinking-effort high",
    )
    update_parser.add_argument("id", metavar="<agent-id>", help="Agent id to update")
    _add_agent_change_arguments(
        update_parser,
        include_name=True,
        include_session=True,
        include_location=True,
    )

    rename_parser = _add_command_parser(
        agent_subparsers,
        "rename",
        AGENT_HELP["rename"],
        example="agent rename assistant researcher",
    )
    rename_parser.add_argument("id", metavar="<agent-id>", help="Current Identity Agent id")
    rename_parser.add_argument(
        "new_id",
        metavar="<new-agent-id>",
        help="New Identity Agent id",
    )

    delete_parser = _add_command_parser(
        agent_subparsers, "delete", AGENT_HELP["delete"], example="agent delete coder"
    )
    delete_parser.add_argument("id", metavar="<agent-id>", help="Agent id to delete")


def _add_agent_change_arguments(
    parser: argparse.ArgumentParser,
    *,
    include_name: bool,
    include_session: bool,
    include_location: bool,
) -> None:
    if include_name:
        parser.add_argument("--name", help="New display name")
    parser.add_argument("--model", help="Primary model as <provider>/<model-id>")
    parser.add_argument("--fallback-model", help="Fallback model as <provider>/<model-id>")
    if include_name:
        parser.add_argument(
            "--clear-model", action="store_true", help="Clear the primary model override"
        )
        parser.add_argument(
            "--clear-fallback-model", action="store_true", help="Clear the fallback model"
        )
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
    parser.add_argument(
        "--subagent-allow",
        nargs="*",
        metavar="<agent-id>",
        help="Replace the agents this agent may delegate to; empty denies all",
    )
    parser.add_argument(
        "--compaction-policy",
        type=_json_object_argument,
        metavar="<json-object>",
        help="Replace the full Agent Policy override as JSON",
    )
    if include_name:
        parser.add_argument(
            "--clear-compaction-policy",
            action="store_true",
            help="Clear the Agent Policy override and inherit the global policy",
        )
    if include_location:
        parser.add_argument(
            "--workspace",
            help="Move the identity and Memory home to this absolute path",
        )
        parser.add_argument(
            "--default-workspace",
            action="store_true",
            help="Move the identity and Memory home back to the agent's default Workspace",
        )
        parser.add_argument(
            "--copy-workspace-files",
            action="store_true",
            help="Copy SOUL.md, USER.md, and MEMORY.md when changing Workspace",
        )
        parser.add_argument(
            "--project",
            metavar="<project-id>",
            help="Select the Project used for relative file and shell work",
        )
        parser.add_argument(
            "--clear-project",
            action="store_true",
            help="Clear the selected Project without changing Workspace or Memory",
        )
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
        "--format",
        choices=PROJECT_SOURCE_FORMATS,
        help=(
            "Source format the project's agents and skills come from "
            "(.opencode/ or .claude/); omitted: auto-detected from the repo, "
            "defaulting to opencode when both or neither are present"
        ),
    )
    add_parser.add_argument(
        "--auto-load",
        nargs="*",
        metavar="<file>",
        help="Repo files auto-loaded into project agent prompts",
    )
    _add_project_capability_arguments(add_parser)

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
        "--clear-default-agent",
        action="store_true",
        help="Clear the project default agent",
    )
    set_parser.add_argument(
        "--default-model",
        metavar="<provider/model-id>",
        help="New project default model as <provider>/<model-id>",
    )
    set_parser.add_argument(
        "--clear-default-model",
        action="store_true",
        help="Clear the project default model (fall through to the global default)",
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
        "--format",
        choices=PROJECT_SOURCE_FORMATS,
        help=(
            "Switch the project's source format; team and skills re-derive "
            "from the new format's directories on the next show/run"
        ),
    )
    set_parser.add_argument(
        "--auto-load",
        nargs="*",
        metavar="<file>",
        help="Replace the full auto-load file list",
    )
    _add_project_capability_arguments(set_parser)

    set_override_parser = _add_command_parser(
        project_subparsers,
        "set-override",
        PROJECT_HELP["set-override"],
        example="project set-override vbot builder model openrouter/openai/gpt-5",
    )
    set_override_parser.add_argument("id", metavar="<project-id>", help="Project id")
    set_override_parser.add_argument("agent", metavar="<agent-id>", help="Team agent id")
    set_override_parser.add_argument(
        "field",
        choices=("model", "temperature", "thinking_effort", "compaction_policy"),
        help="Override field",
    )
    set_override_parser.add_argument(
        "value", metavar="<value>", help="Field value; compaction_policy takes a JSON object"
    )

    clear_override_parser = _add_command_parser(
        project_subparsers,
        "clear-override",
        PROJECT_HELP["clear-override"],
        example="project clear-override vbot builder model",
    )
    clear_override_parser.add_argument("id", metavar="<project-id>", help="Project id")
    clear_override_parser.add_argument("agent", metavar="<agent-id>", help="Team agent id")
    clear_override_parser.add_argument(
        "field",
        choices=("model", "temperature", "thinking_effort", "compaction_policy"),
        help="Override field to clear",
    )

    rm_parser = _add_command_parser(
        project_subparsers, "rm", PROJECT_HELP["rm"], example="project rm vbot"
    )
    rm_parser.add_argument("id", metavar="<project-id>", help="Project id to remove")
    rm_parser.add_argument(
        "--copy-rooted-agent-files",
        action="store_true",
        help=(
            "Copy SOUL.md, USER.md, and MEMORY.md from custom Workspaces before rooted "
            "Identity Agents are reset to their default Workspace"
        ),
    )


def _add_project_capability_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--allowed-tools", nargs="*", help="Replace the Project-wide tool allowlist"
    )
    parser.add_argument(
        "--enabled-bundled-skills",
        nargs="*",
        metavar="<skill>",
        help="Replace the bundled Skill allowlist; empty disables all bundled Skills",
    )
    parser.add_argument(
        "--enabled-global-skills",
        nargs="*",
        metavar="<skill>",
        help="Replace the global Skill allowlist; empty disables all global Skills",
    )
    parser.add_argument(
        "--disabled-project-skills",
        nargs="*",
        metavar="<skill>",
        help="Replace the denylist for Skills discovered in this Project",
    )


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

    fork_parser = _add_command_parser(
        session_subparsers,
        "fork",
        SESSION_HELP["fork"],
        example="session fork assistant <session-id> --target-agent reviewer@vbot",
    )
    fork_parser.add_argument("agent", metavar="<agent>", help="Source agent address")
    fork_parser.add_argument("session", metavar="<session-id>", help="Source session id")
    fork_parser.add_argument(
        "--target-agent",
        metavar="<agent>",
        help="Destination agent address; omitted forks within the source agent",
    )

    rename_parser = _add_command_parser(
        session_subparsers,
        "rename",
        SESSION_HELP["rename"],
        example='session rename assistant <session-id> --title "Research notes"',
    )
    rename_parser.add_argument("agent", metavar="<agent>", help="Agent address")
    rename_parser.add_argument("session", metavar="<session-id>", help="Session id")
    rename_group = rename_parser.add_mutually_exclusive_group(required=True)
    rename_group.add_argument("--title", help="New display title")
    rename_group.add_argument(
        "--clear-title", action="store_true", help="Clear the title and restore automatic display"
    )

    policy_parser = _add_command_parser(
        session_subparsers,
        "set-compaction-policy",
        SESSION_HELP["set-compaction-policy"],
        example='session set-compaction-policy assistant <session-id> --policy "{}"',
    )
    policy_parser.add_argument("agent", metavar="<agent>", help="Agent address")
    policy_parser.add_argument("session", metavar="<session-id>", help="Session id")
    policy_group = policy_parser.add_mutually_exclusive_group(required=True)
    policy_group.add_argument(
        "--policy", type=_json_object_argument, metavar="<json-object>", help="Session Policy"
    )
    policy_group.add_argument(
        "--clear", action="store_true", help="Clear the override and resume live inheritance"
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
        example=("channel add tg-main --platform telegram --agent assistant --token-stdin"),
    )
    add_parser.add_argument("id", metavar="<channel-id>", help="Id for the new channel")
    add_parser.add_argument("--platform", required=True, choices=CHANNEL_PLATFORMS)
    add_parser.add_argument(
        "--agent", required=True, metavar="<agent-id>", help="Agent that handles channel messages"
    )
    token_group = add_parser.add_mutually_exclusive_group(required=True)
    token_group.add_argument(
        "--token-env",
        metavar="<env-var>",
        help="Existing environment variable holding the bot token",
    )
    token_group.add_argument(
        "--token-stdin",
        action="store_true",
        help="Read and manage the bot token from UTF-8 stdin",
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
    _add_channel_policy_arguments(add_parser, include_defaults=True)

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
    _add_channel_policy_arguments(update_parser, include_defaults=False)

    set_token_parser = _add_command_parser(
        channel_subparsers,
        "set-token",
        CHANNEL_HELP["set-token"],
        example="channel set-token tg-main --stdin",
    )
    set_token_parser.add_argument("id", metavar="<channel-id>", help="Channel id to update")
    set_token_parser.add_argument(
        "--stdin",
        action="store_true",
        required=True,
        help="Read the new bot token from UTF-8 stdin",
    )

    for command in ("enable", "disable", "status"):
        command_parser = _add_command_parser(
            channel_subparsers,
            command,
            CHANNEL_HELP[command],
            example=f"channel {command} tg-main",
        )
        command_parser.add_argument("id", metavar="<channel-id>", help=f"Channel id to {command}")


def _add_channel_policy_arguments(
    parser: argparse.ArgumentParser, *, include_defaults: bool
) -> None:
    parser.add_argument(
        "--response-mode",
        choices=CHANNEL_RESPONSE_MODES,
        default="mention" if include_defaults else None,
        help="When group messages trigger a response",
    )
    parser.add_argument(
        "--mention-pattern",
        dest="mention_patterns",
        nargs="*",
        default=[] if include_defaults else None,
        metavar="<pattern>",
        help="Replace explicit mention patterns",
    )
    parser.add_argument(
        "--owner-user",
        dest="owner_user_ids",
        nargs="*",
        default=[] if include_defaults else None,
        metavar="<user-id>",
        help="Replace platform user ids treated as the owner",
    )
    parser.add_argument(
        "--observe-unaddressed",
        choices=("true", "false"),
        default="false" if include_defaults else None,
        help="Let the agent observe group messages it does not answer",
    )


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
    # The extension name is dynamic, so this area is name-first with routing in
    # dispatch_extensions_command rather than a fixed sub-command set:
    #   extensions list
    #   extensions reload
    #   extensions enable|disable <name>
    #   extensions <name>                    -> show that extension's settings
    #   extensions <name> set <field> <value>-> write one setting (schema-routed)
    extensions_parser = subparsers.add_parser(
        "extensions",
        help=AREA_HELP["extensions"],
        description=(
            "Inspect and configure loaded extensions. "
            "'extensions list' lists all; 'extensions reload' rebuilds the whole "
            "extension layer from disk (applies code changes live); 'extensions "
            "<name>' shows one extension's settings; 'extensions <name> set <field> "
            "<value>' writes one setting (a secret field is stored in .env, other "
            "fields go to live config, both applied without a restart); 'extensions "
            "enable|disable <name>' toggles an extension (applied live)."
        ),
    )
    _add_target_arguments(extensions_parser)
    extensions_parser.add_argument(
        "selector",
        metavar="<list|reload|enable|disable|extension-name>",
        help="'list', 'reload', 'enable', 'disable', or an extension name to inspect/configure",
    )
    extensions_parser.add_argument(
        "rest",
        nargs="*",
        metavar="args",
        help="'<name>' for enable/disable, or 'set <field> <value>' for a name selector",
    )
    extensions_parser.add_argument(
        "--stdin",
        action="store_true",
        help="read the value for 'set <field>' from stdin (keeps a secret out of shell history)",
    )


def _add_prompt_parsers(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    prompt_parser = subparsers.add_parser(
        "prompt",
        help=AREA_HELP["prompt"],
        description=AREA_HELP["prompt"],
    )
    prompt_subparsers = prompt_parser.add_subparsers(dest="command", required=True)

    list_parser = _add_command_parser(
        prompt_subparsers, "list", PROMPT_HELP["list"], example="prompt list"
    )
    _add_prompt_scope_argument(list_parser)

    update_parser = _add_command_parser(
        prompt_subparsers,
        "update",
        PROMPT_HELP["update"],
        example="prompt update core:tools --file tools.md",
    )
    update_parser.add_argument(
        "block_id", metavar="<block-id>", help="Editable prompt block id, for example core:tools"
    )
    content_group = update_parser.add_mutually_exclusive_group(required=True)
    content_group.add_argument("--content", help="New block content as inline text")
    content_group.add_argument(
        "--file", dest="content_file", metavar="<path>", help="Read block content from a file"
    )
    _add_prompt_scope_argument(update_parser)

    reset_parser = _add_command_parser(
        prompt_subparsers, "reset", PROMPT_HELP["reset"], example="prompt reset core:tools"
    )
    reset_parser.add_argument(
        "block_id", metavar="<block-id>", help="Editable prompt block id, for example core:tools"
    )
    _add_prompt_scope_argument(reset_parser)

    create_parser = _add_command_parser(
        prompt_subparsers,
        "create",
        PROMPT_HELP["create"],
        example='prompt create project-rules --content "Follow the repo rules."',
    )
    create_parser.add_argument("slug", metavar="<slug>", help="Slug for the new user:<slug> block")
    create_content = create_parser.add_mutually_exclusive_group()
    create_content.add_argument("--content", help="Initial block content as inline text")
    create_content.add_argument(
        "--file", dest="content_file", metavar="<path>", help="Read initial content from a file"
    )
    create_parser.add_argument(
        "--position", type=int, metavar="<index>", help="0-based layout insertion position"
    )
    _add_prompt_scope_argument(create_parser)

    remove_parser = _add_command_parser(
        prompt_subparsers,
        "remove",
        PROMPT_HELP["remove"],
        example="prompt remove user:project-rules",
    )
    remove_parser.add_argument("block_id", metavar="<block-id>", help="Custom user: block id")
    _add_prompt_scope_argument(remove_parser)

    layout_parser = _add_command_parser(
        prompt_subparsers,
        "set-layout",
        PROMPT_HELP["set-layout"],
        example='prompt set-layout --layout-json \'[{"id":"core:tools","enabled":true}]\'',
    )
    layout_parser.add_argument(
        "--layout-json",
        required=True,
        type=_json_array_argument,
        metavar="<json-array>",
        help="Ordered [{id, enabled}] layout",
    )
    _add_prompt_scope_argument(layout_parser)

    reset_layout_parser = _add_command_parser(
        prompt_subparsers,
        "reset-layout",
        PROMPT_HELP["reset-layout"],
        example="prompt reset-layout --scope agent:assistant",
    )
    _add_prompt_scope_argument(reset_layout_parser)

    preview_parser = _add_command_parser(
        prompt_subparsers, "preview", PROMPT_HELP["preview"], example="prompt preview assistant"
    )
    preview_parser.add_argument(
        "agent",
        metavar="<agent>",
        help="Agent whose system prompt to render, as agent or agent@projekt",
    )
    _add_prompt_scope_argument(preview_parser)


def _add_prompt_scope_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--scope",
        default="default",
        metavar="<default|agent:id>",
        help="Editable prompt scope (default: default)",
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

    usage_parser = _add_command_parser(
        provider_subparsers,
        "usage",
        PROVIDER_HELP["usage"],
        example="provider usage --connection openai:subscription",
    )
    usage_parser.add_argument(
        "--connection",
        action="append",
        metavar="<provider:connection-id>",
        help="Probe only this connection; repeat to select multiple connections",
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

    for command in ("enable", "disable"):
        toggle_parser = _add_command_parser(
            provider_subparsers,
            command,
            PROVIDER_HELP[command],
            example=f"provider {command} ollama",
        )
        toggle_parser.description = (
            f"{PROVIDER_HELP[command]} "
            "Keyless local connections (e.g. Ollama) are disabled until enabled here; "
            "a disabled connection is never probed and offers no models. "
            f"Example: provider {command} ollama"
        )
        toggle_parser.add_argument(
            "provider", metavar="<provider-id>", help="Provider id to toggle"
        )
        toggle_parser.add_argument(
            "--connection",
            metavar="<provider:connection-id>",
            help="Required when the provider has multiple connections, e.g. ollama:local",
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
    list_parser = _add_command_parser(
        model_subparsers,
        "list",
        MODEL_HELP["list"],
        example="model list --task chat",
    )
    list_parser.add_argument("--provider", dest="provider_id", help="Filter by Provider id")
    list_parser.add_argument(
        "--capability",
        action="append",
        help="Require a capability; repeat for multiple requirements",
    )
    list_parser.add_argument(
        "--task",
        action="append",
        help="Require a task type; repeat for multiple requirements",
    )
    list_parser.add_argument(
        "--input-modality",
        action="append",
        help="Require an input modality; repeat for multiple requirements",
    )
    list_parser.add_argument(
        "--output-modality",
        action="append",
        help="Require an output modality; repeat for multiple requirements",
    )
    list_parser.add_argument(
        "--min-context-window",
        type=int,
        metavar="<tokens>",
        help="Require at least this many context tokens",
    )
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

    read_parser = _add_command_parser(
        skill_subparsers, "read", SKILL_HELP["read"], example="skill read --scope global"
    )
    _add_skill_scope_argument(read_parser)

    for command in ("create", "update"):
        command_parser = _add_command_parser(
            skill_subparsers,
            command,
            SKILL_HELP[command],
            example=f"skill {command} librarian --scope agent:assistant --file SKILL.md",
        )
        command_parser.add_argument("name", metavar="<skill-name>", help="Skill directory name")
        content_group = command_parser.add_mutually_exclusive_group(required=True)
        content_group.add_argument("--content", help="Complete SKILL.md content as inline text")
        content_group.add_argument(
            "--file", dest="content_file", metavar="<path>", help="Read SKILL.md from a file"
        )
        command_parser.add_argument("--source", help="Optional provenance label")
        _add_skill_scope_argument(command_parser)

    delete_parser = _add_command_parser(
        skill_subparsers,
        "delete",
        SKILL_HELP["delete"],
        example="skill delete librarian --scope global --yes",
    )
    delete_parser.add_argument("name", metavar="<skill-name>", help="Skill directory name")
    delete_parser.add_argument("--yes", action="store_true", help="Confirm deletion")
    _add_skill_scope_argument(delete_parser)

    write_parser = _add_command_parser(
        skill_subparsers,
        "write-file",
        SKILL_HELP["write-file"],
        example="skill write-file librarian references/schema.md --scope global --file schema.md",
    )
    write_parser.add_argument("name", metavar="<skill-name>", help="Skill directory name")
    write_parser.add_argument("path", metavar="<relative-path>", help="Path inside the Skill")
    write_content = write_parser.add_mutually_exclusive_group(required=True)
    write_content.add_argument("--content", help="File content as inline text")
    write_content.add_argument(
        "--file", dest="content_file", metavar="<path>", help="Read content from a file"
    )
    _add_skill_scope_argument(write_parser)

    remove_file_parser = _add_command_parser(
        skill_subparsers,
        "remove-file",
        SKILL_HELP["remove-file"],
        example="skill remove-file librarian references/schema.md --scope global --yes",
    )
    remove_file_parser.add_argument("name", metavar="<skill-name>", help="Skill directory name")
    remove_file_parser.add_argument("path", metavar="<relative-path>", help="Path inside the Skill")
    remove_file_parser.add_argument("--yes", action="store_true", help="Confirm removal")
    _add_skill_scope_argument(remove_file_parser)


def _add_skill_scope_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--scope",
        required=True,
        metavar="<global|agent:id>",
        help="Editable global or private Identity Agent Skill scope",
    )


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
        example=(
            'cron create builder@vbot --name "Morning news" '
            '--prompt "Check the news" --cron "0 9 * * *"'
        ),
    )
    create_parser.add_argument(
        "agent", metavar="<agent>", help="Agent that runs the job, as agent or agent@projekt"
    )
    create_parser.add_argument(
        "--name", required=True, help="Human-readable job name; it does not need to be unique"
    )
    create_parser.add_argument(
        "--prompt", required=True, help="Prompt text injected when the job fires"
    )
    create_schedule_group = create_parser.add_mutually_exclusive_group(required=True)
    _add_cron_schedule_arguments(create_schedule_group)
    _add_cron_session_argument(create_parser)

    update_parser = _add_command_parser(
        cron_subparsers,
        "update",
        CRON_HELP["update"],
        example='cron update <job-id> --prompt "Check status and report"',
    )
    update_parser.add_argument("id", metavar="<job-id>", help="Cron job id to update")
    update_parser.add_argument(
        "--agent", metavar="<agent>", help="Agent that runs the job, as agent or agent@projekt"
    )
    update_parser.add_argument("--name", help="Replace the human-readable job name")
    update_parser.add_argument("--prompt", help="Prompt text injected when the job fires")
    update_schedule_group = update_parser.add_mutually_exclusive_group()
    _add_cron_schedule_arguments(update_schedule_group)
    _add_cron_session_argument(update_parser)
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
        "--at",
        metavar="<iso-datetime>",
        help="One-time schedule as an ISO 8601 datetime",
    )


def _add_cron_session_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--session",
        metavar="<session-id>",
        help="Run in this existing Session; omit to create a fresh Session for every fire",
    )


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
    _add_target_arguments(config_parser)

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
    set_parser.add_argument(
        "value", metavar="<value>", help="New value; parsed as JSON, falling back to plain text"
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
    update_parser.add_argument(
        "--service-name",
        help="systemd user unit to restart when the install is unit-managed (default: vbot)",
    )


def _add_uninstall_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    uninstall_parser = subparsers.add_parser(
        "uninstall",
        help=AREA_HELP["uninstall"],
        description=f"{AREA_HELP['uninstall']}. Example: vbot uninstall",
    )
    modes = uninstall_parser.add_mutually_exclusive_group()
    modes.add_argument(
        "--app-only",
        action="store_const",
        const="app-only",
        dest="uninstall_mode",
        help="Remove the application and Autostart while preserving data",
    )
    modes.add_argument(
        "--data-only",
        action="store_const",
        const="data-only",
        dest="uninstall_mode",
        help="Delete the data directory while preserving the application",
    )
    modes.add_argument(
        "--all",
        action="store_const",
        const="all",
        dest="uninstall_mode",
        help="Remove both the application and its data directory",
    )
    uninstall_parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip the confirmation prompt (a removal mode is still required without a TTY)",
    )
    uninstall_parser.add_argument(
        "--host", help="Server host; defaults to the recorded installation target"
    )
    uninstall_parser.add_argument(
        "--port", type=int, help="Server port; defaults to the recorded installation target"
    )
    uninstall_parser.add_argument("--data-dir", help="Exact data directory to keep or delete")
    uninstall_parser.add_argument(
        "--task-name", help="Windows Task Scheduler task name (default: vBot)"
    )
    uninstall_parser.add_argument(
        "--service-name", help="Linux systemd user unit name without .service (default: vbot)"
    )


def _add_autostart_parsers(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    autostart_parser = subparsers.add_parser(
        "autostart",
        help=AREA_HELP["autostart"],
        description=AREA_HELP["autostart"],
    )
    autostart_subparsers = autostart_parser.add_subparsers(dest="command", required=True)
    for command in ("enable", "disable", "status"):
        command_parser = _add_command_parser(
            autostart_subparsers, command, AUTOSTART_HELP[command], example=f"autostart {command}"
        )
        command_parser.add_argument(
            "--task-name", help="Windows Task Scheduler task name (default: vBot)"
        )
        command_parser.add_argument(
            "--service-name", help="systemd user unit name without .service (default: vbot)"
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
