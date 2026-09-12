"""CLI grammar for Agents, Projects, Sessions, and Session-store maintenance."""

from __future__ import annotations

import argparse

from cli._parser_common import (
    AGENT_HELP,
    AREA_HELP,
    PROJECT_HELP,
    SESSION_HELP,
    SESSION_STORE_HELP,
    SESSION_STORE_SNAPSHOT_HELP,
    THINKING_EFFORTS,
    _add_command_parser,
    _json_object_argument,
)
from core.memory import MEMORY_PROMPT_MODES
from core.settings import PROJECT_SOURCE_FORMATS


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

    reorder_parser = _add_command_parser(
        agent_subparsers,
        "reorder",
        AGENT_HELP["reorder"],
        example="agent reorder researcher assistant coder",
    )
    reorder_parser.add_argument(
        "ids",
        nargs="+",
        metavar="<agent-id>",
        help=(
            "Agent ids in the desired order; agents not listed keep their "
            "relative order after the listed ones"
        ),
    )


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
    parser.add_argument(
        "--fallback-models",
        action="append",
        help="Ordered fallback model as <provider>/<model-id>; repeat for priority order",
    )
    if include_name:
        parser.add_argument(
            "--clear-model", action="store_true", help="Clear the primary model override"
        )
        parser.add_argument(
            "--clear-fallback-models", action="store_true", help="Clear the fallback chain"
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
    parser.add_argument(
        "--tool-access-mode",
        choices=("all", "selected", "none"),
        help="Replace the complete Tool policy; repeat selections and denials to preserve them",
    )
    parser.add_argument(
        "--tool-allow",
        nargs="*",
        help="Requires --tool-access-mode selected; omitted or empty selects no direct Tools",
    )
    parser.add_argument(
        "--tool-deny",
        nargs="*",
        help="Requires --tool-access-mode; omitted or empty clears denials in the replacement",
    )
    parser.add_argument(
        "--allowed-skills",
        nargs="*",
        help=(
            "Replace allowed shared/global/bundled Skills; private and active "
            "Project Skills remain allowed"
        ),
    )
    parser.add_argument(
        "--subagent-allow",
        nargs="*",
        metavar="<agent-id>",
        help="Replace additional delegation targets; empty permits self-delegation only",
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
        choices=(
            "model",
            "temperature",
            "thinking_effort",
            "compaction_policy",
            "tool_access",
        ),
        help="Override field",
    )
    set_override_parser.add_argument(
        "value",
        metavar="<value>",
        help="Field value; compaction_policy and tool_access take a JSON object",
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
        choices=(
            "model",
            "temperature",
            "thinking_effort",
            "compaction_policy",
            "tool_access",
        ),
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

    _add_command_parser(
        project_subparsers,
        "detect",
        PROJECT_HELP["detect"],
        example="project detect C:/Development/projects/vBot",
    ).add_argument(
        "cwd",
        nargs="?",
        metavar="<path>",
        help="Directory on the server; omission inspects the server working directory",
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
        "agent", metavar="<agent>", help="Agent whose sessions to list, as agent or agent@project"
    )

    list_parser.add_argument(
        "--limit",
        type=int,
        default=100,
        help="Page size (default: 100; server validates the range)",
    )
    list_parser.add_argument(
        "--cursor", type=_json_object_argument, help="Continuation JSON returned as next_cursor"
    )
    list_parser.add_argument(
        "--all", action="store_true", help="Fetch every page; output may be large"
    )
    create_parser = _add_command_parser(
        session_subparsers,
        "create",
        SESSION_HELP["create"],
        example="session create orchestrator@vbot --make-current",
    )
    create_parser.add_argument(
        "agent", metavar="<agent>", help="Agent to create a session for, as agent or agent@project"
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
        "agent", metavar="<agent>", help="Agent owning the session, as agent or agent@project"
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
        example="session set-compaction-policy assistant <session-id> --clear",
    )
    policy_parser.add_argument("agent", metavar="<agent>", help="Agent address")
    policy_parser.add_argument("session", metavar="<session-id>", help="Session id")
    policy_group = policy_parser.add_mutually_exclusive_group(required=True)
    policy_group.add_argument(
        "--policy",
        type=_json_object_argument,
        metavar="<json-object>",
        help=(
            "Complete Session Policy with enabled, trigger, and strategy; "
            "inspect session list first"
        ),
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


def _add_session_store_parsers(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    session_store_parser = subparsers.add_parser(
        "session-store",
        help=AREA_HELP["session-store"],
        description=AREA_HELP["session-store"],
    )
    commands = session_store_parser.add_subparsers(dest="command", required=True)

    _add_command_parser(
        commands,
        "status",
        SESSION_STORE_HELP["status"],
        example="session-store status",
    )

    snapshot_parser = commands.add_parser(
        "snapshot",
        help=SESSION_STORE_HELP["snapshot"],
        description=SESSION_STORE_HELP["snapshot"],
    )
    snapshot_commands = snapshot_parser.add_subparsers(dest="snapshot_command", required=True)
    _add_command_parser(
        snapshot_commands,
        "list",
        SESSION_STORE_SNAPSHOT_HELP["list"],
        example="session-store snapshot list",
    )
    create_parser = _add_command_parser(
        snapshot_commands,
        "create",
        SESSION_STORE_SNAPSHOT_HELP["create"],
        example="session-store snapshot create --reason manual",
    )
    create_parser.add_argument(
        "--reason",
        choices=("manual", "rpc", "update", "recovery"),
        default="manual",
        help="Reason recorded in the snapshot manifest",
    )
    verify_parser = _add_command_parser(
        snapshot_commands,
        "verify",
        SESSION_STORE_SNAPSHOT_HELP["verify"],
        example="session-store snapshot verify <snapshot-id>",
    )
    verify_parser.add_argument("snapshot_id", metavar="<snapshot-id>")
    restore_parser = _add_command_parser(
        snapshot_commands,
        "restore",
        SESSION_STORE_SNAPSHOT_HELP["restore"],
        example="session-store snapshot restore <snapshot-id> --yes",
    )
    restore_parser.add_argument("snapshot_id", metavar="<snapshot-id>")
    restore_parser.add_argument(
        "--yes",
        action="store_true",
        help="Confirm restore while the exact server target is stopped",
    )

    incident_parser = commands.add_parser(
        "incident",
        help=SESSION_STORE_HELP["incident"],
        description=SESSION_STORE_HELP["incident"],
    )
    incident_commands = incident_parser.add_subparsers(dest="incident_command", required=True)
    acknowledge_parser = _add_command_parser(
        incident_commands,
        "acknowledge",
        "Acknowledge one durable Session-store recovery incident",
        example="session-store incident acknowledge <incident-id>",
    )
    acknowledge_parser.add_argument("incident_id", metavar="<incident-id>")
