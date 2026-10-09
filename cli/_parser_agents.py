"""CLI grammar for Agents, Projects, Sessions, the archive, chat, and data-store maintenance."""

from __future__ import annotations

import argparse

from cli._parser_common import (
    AGENT_HELP,
    ARCHIVE_HELP,
    AREA_HELP,
    DATA_STORE_CONFIG_BACKUP_HELP,
    DATA_STORE_HELP,
    DATA_STORE_SNAPSHOT_HELP,
    PERMANENT_DELETE_HELP,
    PROJECT_HELP,
    SESSION_HELP,
    THINKING_EFFORTS,
    _add_command_parser,
    _add_target_arguments,
    _json_array_argument,
    _json_object_argument,
)
from core.memory import MEMORY_PROMPT_MODES
from core.providers.reasoning import THINKING_EFFORT_ORDER
from core.sessions import ARCHIVE_KINDS

# The Identity Agent a fresh installation creates first.
DEFAULT_CHAT_AGENT = "main"


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
    delete_parser.add_argument("--permanent", action="store_true", help=PERMANENT_DELETE_HELP)
    delete_parser.add_argument(
        "--yes", action="store_true", help="Confirm a permanent deletion (with --permanent)"
    )

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
    parser.add_argument(
        "--temperature",
        type=float,
        help="Sampling temperature (0.0-2.0); unset leaves it to the Provider",
    )
    parser.add_argument(
        "--clear-temperature",
        action="store_true",
        help="Clear the temperature override and inherit the default",
    )
    parser.add_argument(
        "--top-p",
        type=float,
        help="Nucleus sampling top_p (0.0-1.0); unset leaves it to the Provider",
    )
    parser.add_argument(
        "--clear-top-p",
        action="store_true",
        help="Clear the top_p override and inherit the default",
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
        "--librarian",
        choices=("true", "false"),
        help=(
            "Whether Librarian passes curate the agent's own skills (default true); "
            "librarian.enabled still switches scheduled passes off for every agent"
        ),
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
        "--tools-on-demand",
        choices=("on", "off"),
        help=(
            "Replace the On-demand Tools setting: on keeps only the always-loaded Tools in "
            "the Tool list and lets the agent load the others when it needs them"
        ),
    )
    parser.add_argument(
        "--always-loaded",
        nargs="*",
        metavar="<tool>",
        help=(
            "Requires --tools-on-demand; the Tools that stay in the Tool list (empty for "
            "none); omitted keeps the default set"
        ),
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
        "--excluded-skills",
        nargs="*",
        help=(
            "Replace Skills turned off for this Agent: withheld from --allowed-skills "
            "(also from *) and from its private Skills; active Project Skills stay "
            "allowed; empty clears the exclusions"
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
            help=(
                "Set the default Project: new Sessions of the Agent work in it; "
                "existing Sessions keep their Project"
            ),
        )
        parser.add_argument(
            "--clear-project",
            action="store_true",
            help=(
                "Clear the default Project: new Sessions work in the Agent's Workspace; "
                "existing Sessions keep their Project"
            ),
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
        "--default-top-p",
        type=float,
        metavar="<0.0-1.0>",
        help="Project default nucleus sampling top_p (0.0-1.0)",
    )
    add_parser.add_argument(
        "--clear-default-top-p",
        action="store_true",
        help="Clear the project default top_p (fall through to the global default)",
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
        "--sources",
        type=_json_array_argument,
        help="Ordered source selections as JSON [{id, enabled}]",
    )
    add_parser.add_argument(
        "--model-mappings",
        type=_json_object_argument,
        help="Model wish to vBot Model mapping as JSON",
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
        "--default-top-p",
        type=float,
        metavar="<0.0-1.0>",
        help="New project default nucleus sampling top_p (0.0-1.0)",
    )
    set_parser.add_argument(
        "--clear-default-top-p",
        action="store_true",
        help="Clear the project default top_p (fall through to the global default)",
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
        "--sources",
        type=_json_array_argument,
        help="Ordered source selections as JSON [{id, enabled}]",
    )
    set_parser.add_argument(
        "--model-mappings",
        type=_json_object_argument,
        help="Model wish to vBot Model mapping as JSON",
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
        example="project override set vbot builder model openrouter/openai/gpt-5",
    )
    set_override_parser.add_argument("id", metavar="<project-id>", help="Project id")
    set_override_parser.add_argument("agent", metavar="<agent-id>", help="Team agent id")
    set_override_parser.add_argument(
        "field",
        choices=(
            "model",
            "temperature",
            "top_p",
            "thinking_effort",
            "compaction_policy",
            "tool_access",
            "tool_loading",
        ),
        help="Override field",
    )
    set_override_parser.add_argument(
        "value",
        metavar="<value>",
        help="Field value; compaction_policy, tool_access and tool_loading take a JSON object",
    )

    clear_override_parser = _add_command_parser(
        project_subparsers,
        "clear-override",
        PROJECT_HELP["clear-override"],
        example="project override clear vbot builder model",
    )
    clear_override_parser.add_argument("id", metavar="<project-id>", help="Project id")
    clear_override_parser.add_argument("agent", metavar="<agent-id>", help="Team agent id")
    clear_override_parser.add_argument(
        "field",
        choices=(
            "model",
            "temperature",
            "top_p",
            "thinking_effort",
            "compaction_policy",
            "tool_access",
            "tool_loading",
        ),
        help="Override field to clear",
    )

    rm_parser = _add_command_parser(
        project_subparsers, "rm", PROJECT_HELP["rm"], example="project remove vbot"
    )
    rm_parser.add_argument("id", metavar="<project-id>", help="Project id to remove")
    rm_parser.add_argument(
        "--copy-rooted-agent-files",
        action="store_true",
        help=(
            "Copy SOUL.md, USER.md, and MEMORY.md from custom Workspaces before "
            "Identity Agents with this default Project are reset to their default Workspace"
        ),
    )
    rm_parser.add_argument("--permanent", action="store_true", help=PERMANENT_DELETE_HELP)
    rm_parser.add_argument(
        "--yes", action="store_true", help="Confirm a permanent deletion (with --permanent)"
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
        help="Confirm deletion; without --permanent the session moves to the archive",
    )
    delete_parser.add_argument("--permanent", action="store_true", help=PERMANENT_DELETE_HELP)

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
        example="session policy set assistant <session-id> --clear",
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
        example="session channel link assistant <session-id> --channel tg-main --conversation 99",
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


def _add_archive_parsers(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    archive_parser = subparsers.add_parser(
        "archive",
        help=AREA_HELP["archive"],
        description=(
            f"{AREA_HELP['archive']}. Deleting an Agent, Project or Session moves it into "
            "the archive as one archive entry (arc_...), which is restored or deleted "
            "permanently as a unit."
        ),
    )
    archive_subparsers = archive_parser.add_subparsers(dest="command", required=True)

    list_parser = _add_command_parser(
        archive_subparsers, "list", ARCHIVE_HELP["list"], example="archive list --kind agent"
    )
    _add_archive_filter_arguments(list_parser, selects="List")
    list_parser.add_argument(
        "--limit",
        type=int,
        default=50,
        help="Page size (default: 50; the server accepts 1 to 200)",
    )
    list_parser.add_argument(
        "--cursor",
        type=_json_object_argument,
        metavar="<json-object>",
        help="Continuation JSON a previous list printed for its next page",
    )
    list_parser.add_argument(
        "--all", action="store_true", help="Fetch every page and print every matching entry"
    )

    show_parser = _add_command_parser(
        archive_subparsers,
        "show",
        ARCHIVE_HELP["show"],
        example="archive show arc_7k2m9q4xw1ab",
    )
    show_parser.add_argument("entry_id", metavar="<entry-id>", help="Archive entry id (arc_...)")

    restore_parser = _add_command_parser(
        archive_subparsers,
        "restore",
        ARCHIVE_HELP["restore"],
        example="archive restore arc_7k2m9q4xw1ab --as coder-2",
    )
    restore_parser.add_argument("entry_id", metavar="<entry-id>", help="Archive entry id (arc_...)")
    restore_parser.add_argument(
        "--as",
        dest="target_id",
        metavar="<new-id>",
        help="Restore under this new Agent, Project or Session id when the original is in use",
    )

    purge_parser = _add_command_parser(
        archive_subparsers,
        "purge",
        ARCHIVE_HELP["purge"],
        example="archive purge arc_7k2m9q4xw1ab --yes",
    )
    purge_parser.add_argument(
        "entry_ids",
        nargs="*",
        metavar="<entry-id>",
        help="Archive entry ids to delete; or use --all",
    )
    purge_parser.add_argument(
        "--all",
        action="store_true",
        help="Delete every archive entry, or with filters every matching one",
    )
    _add_archive_filter_arguments(purge_parser, selects="With --all, delete")
    purge_parser.add_argument(
        "--yes",
        action="store_true",
        help="Confirm the permanent deletion; it cannot be undone",
    )


def _add_archive_filter_arguments(parser: argparse.ArgumentParser, *, selects: str) -> None:
    parser.add_argument(
        "--kind", choices=ARCHIVE_KINDS, help=f"{selects} only entries of this kind"
    )
    parser.add_argument(
        "--agent",
        metavar="<agent-id>",
        help=(
            f"{selects} only entries of this Agent: an Identity Agent id, or agent@project "
            "for a Project Agent"
        ),
    )
    parser.add_argument(
        "--project",
        metavar="<project-id>",
        help=f"{selects} only entries of this Project, including the Sessions of its Agents",
    )


def _add_chat_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    chat_parser = subparsers.add_parser(
        "chat",
        help=AREA_HELP["chat"],
        description=(
            f"{AREA_HELP['chat']}. Without -c or --session the message starts a new "
            "Session; the server creates it only for a message it accepts. "
            "--model, --thinking-effort, --temperature and --top-p are saved on the "
            "Session and apply to its later messages too. The answer goes to stdout; "
            "Tool calls and retries are reported on stderr. "
            'Example: vbot chat --agent coder@vbot -c "Continue with the next step"'
        ),
    )
    chat_parser.add_argument(
        "prompt",
        nargs="?",
        metavar="<prompt>",
        help="Message to send; - or omitted reads it from piped stdin",
    )
    chat_parser.add_argument(
        "--agent",
        default=DEFAULT_CHAT_AGENT,
        metavar="<agent>",
        help=f"Agent as agent or agent@project (default: {DEFAULT_CHAT_AGENT})",
    )
    session_choice = chat_parser.add_mutually_exclusive_group()
    session_choice.add_argument(
        "-c",
        "--continue",
        dest="continue_latest",
        action="store_true",
        help=(
            "Continue the Agent's most recently active conversation Session "
            "(a new Session when it has none)"
        ),
    )
    session_choice.add_argument(
        "--session", metavar="<session-id>", help="Continue this Session of the Agent"
    )
    session_choice.add_argument(
        "--project",
        dest="working_project",
        metavar="<project-id>",
        help=(
            "Start the new Session in this Project; it keeps working there "
            "(default: the Agent's default Project)"
        ),
    )
    session_choice.add_argument(
        "--workspace",
        action="store_true",
        help="Start the new Session in the Agent's Workspace instead of its default Project",
    )
    chat_parser.add_argument(
        "--model",
        metavar="<provider/model-id>",
        help="Model for this Session, as <provider>/<model-id>",
    )
    chat_parser.add_argument(
        "--thinking-effort",
        choices=THINKING_EFFORT_ORDER,
        help="Reasoning effort for this Session",
    )
    chat_parser.add_argument(
        "--temperature",
        type=float,
        metavar="<0.0-2.0>",
        help="Sampling temperature for this Session",
    )
    chat_parser.add_argument(
        "--top-p",
        type=float,
        metavar="<0.0-1.0>",
        help="Nucleus sampling top_p for this Session",
    )
    chat_parser.add_argument(
        "--json",
        action="store_true",
        help="Print one JSON object with the answer, Tool calls and Usage instead of text",
    )
    _add_target_arguments(chat_parser)


def _add_data_store_parsers(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    data_store_parser = subparsers.add_parser(
        "data-store",
        help=AREA_HELP["data-store"],
        description=AREA_HELP["data-store"],
    )
    commands = data_store_parser.add_subparsers(dest="command", required=True)

    _add_command_parser(
        commands,
        "status",
        DATA_STORE_HELP["status"],
        example="data-store status",
    )

    snapshot_parser = commands.add_parser(
        "snapshot",
        help=DATA_STORE_HELP["snapshot"],
        description=DATA_STORE_HELP["snapshot"],
    )
    snapshot_commands = snapshot_parser.add_subparsers(dest="snapshot_command", required=True)
    _add_command_parser(
        snapshot_commands,
        "list",
        DATA_STORE_SNAPSHOT_HELP["list"],
        example="data-store snapshot list",
    )
    create_parser = _add_command_parser(
        snapshot_commands,
        "create",
        DATA_STORE_SNAPSHOT_HELP["create"],
        example="data-store snapshot create --reason manual",
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
        DATA_STORE_SNAPSHOT_HELP["verify"],
        example="data-store snapshot verify <snapshot-id>",
    )
    verify_parser.add_argument("snapshot_id", metavar="<snapshot-id>")
    restore_parser = _add_command_parser(
        snapshot_commands,
        "restore",
        DATA_STORE_SNAPSHOT_HELP["restore"],
        example="data-store snapshot restore <snapshot-id> --yes",
    )
    restore_parser.add_argument("snapshot_id", metavar="<snapshot-id>")
    restore_parser.add_argument(
        "--database",
        action="append",
        default=[],
        metavar="<name>",
        help="Restore only this database (repeatable); default: every database in the snapshot",
    )
    restore_parser.add_argument(
        "--documents",
        action="store_true",
        help=(
            "Restore the snapshot's JSON documents (settings, Agents, Channels, Projects, "
            "jobs, tokens) as one set; without --database, only the documents are restored"
        ),
    )
    restore_parser.add_argument(
        "--all",
        action="store_true",
        help=(
            "Restore the complete snapshot: every database, the JSON documents, and move "
            "databases registered after the snapshot to quarantine"
        ),
    )
    restore_parser.add_argument(
        "--yes",
        action="store_true",
        help="Confirm restore while the exact server target is stopped",
    )

    config_backup_parser = commands.add_parser(
        "config-backup",
        help=DATA_STORE_HELP["config-backup"],
        description=DATA_STORE_HELP["config-backup"],
    )
    config_backup_commands = config_backup_parser.add_subparsers(
        dest="config_backup_command", required=True
    )
    _add_command_parser(
        config_backup_commands,
        "list",
        DATA_STORE_CONFIG_BACKUP_HELP["list"],
        example="data-store config-backup list",
    )
    show_parser = _add_command_parser(
        config_backup_commands,
        "show",
        DATA_STORE_CONFIG_BACKUP_HELP["show"],
        example="data-store config-backup show <backup-id>",
    )
    show_parser.add_argument("backup_id", metavar="<backup-id>")
    config_restore_parser = _add_command_parser(
        config_backup_commands,
        "restore",
        DATA_STORE_CONFIG_BACKUP_HELP["restore"],
        example="data-store config-backup restore <backup-id> --file settings.json --yes",
    )
    config_restore_parser.add_argument("backup_id", metavar="<backup-id>")
    config_restore_parser.add_argument(
        "--file",
        action="append",
        default=[],
        metavar="<path>",
        help="Restore this file, as `show` names it (repeatable)",
    )
    config_restore_parser.add_argument(
        "--all",
        action="store_true",
        help=(
            "Restore every file of the backup; files created after it and files whose "
            "folder no longer exists or is a link stay as they are"
        ),
    )
    config_restore_parser.add_argument(
        "--yes",
        action="store_true",
        help="Confirm restore while the exact server target is stopped",
    )

    incident_parser = commands.add_parser(
        "incident",
        help=DATA_STORE_HELP["incident"],
        description=DATA_STORE_HELP["incident"],
    )
    incident_commands = incident_parser.add_subparsers(dest="incident_command", required=True)
    acknowledge_parser = _add_command_parser(
        incident_commands,
        "acknowledge",
        "Acknowledge one durable database recovery incident",
        example="data-store incident acknowledge <incident-id>",
    )
    acknowledge_parser.add_argument("incident_id", metavar="<incident-id>")

    unregister_parser = _add_command_parser(
        commands,
        "unregister",
        DATA_STORE_HELP["unregister"],
        example="data-store unregister ext.<extension>.<name> --yes",
    )
    unregister_parser.add_argument(
        "name",
        metavar="<name>",
        help="Registered Extension database name, ext.<extension>.<name>",
    )
    unregister_parser.add_argument(
        "--yes",
        action="store_true",
        help="Confirm moving the database files to quarantine and dropping the registration",
    )
