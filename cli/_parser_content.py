"""CLI grammar for Tools, Extensions, prompts, logs, Skills, and Memory."""

from __future__ import annotations

import argparse

from cli._parser_common import (
    AREA_HELP,
    LOG_HELP,
    MEMORY_HELP,
    PROMPT_HELP,
    SKILL_HELP,
    TOOL_HELP,
    _add_command_parser,
    _add_target_arguments,
    _json_array_argument,
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
            "enable|disable <name>' toggles an extension (applied live). "
            "Use 'extensions <name> operations' to discover managed operations, "
            "then 'extensions <name> <operation> --help' for its arguments."
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
    show = _add_command_parser(
        prompt_subparsers,
        "show",
        "Read one complete prompt block before editing",
        example="prompt show core:tools",
    )
    show.add_argument("block_id", metavar="<block-id>", help="Exact block id from prompt list")
    _add_prompt_scope_argument(show)

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
        help="Agent whose system prompt to render, as agent or agent@project",
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
    read_parser.add_argument("file", metavar="<log-file>", help="Daily log file name from log list")
    read_parser.add_argument(
        "--limit",
        type=int,
        default=100,
        help="Print the newest N matching entries (default: 100; 0 prints all)",
    )
    read_parser.add_argument(
        "--level",
        choices=("debug", "info", "warn", "error", "critical", "unknown"),
        help="Show only this exact log level",
    )


def _add_skill_parsers(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    skill_parser = subparsers.add_parser(
        "skill",
        help=AREA_HELP["skill"],
        description=AREA_HELP["skill"],
    )
    skill_subparsers = skill_parser.add_subparsers(dest="command", required=True)
    _add_command_parser(skill_subparsers, "list", SKILL_HELP["list"], example="skill list")

    _add_command_parser(
        skill_subparsers, "inventory", SKILL_HELP["inventory"], example="skill inventory"
    )

    for command in ("disable", "enable"):
        command_parser = _add_command_parser(
            skill_subparsers,
            command,
            SKILL_HELP[command],
            example=f"skill {command} librarian",
        )
        command_parser.add_argument("name", metavar="<skill-name>", help="Skill name to toggle")

    share_parser = _add_command_parser(
        skill_subparsers,
        "share",
        SKILL_HELP["share"],
        example="skill share assistant librarian --to researcher --to coder",
    )
    share_parser.add_argument("agent", metavar="<agent-id>", help="Owning Identity Agent id")
    share_parser.add_argument("name", metavar="<skill-name>", help="Private Skill name to share")
    share_parser.add_argument(
        "--to",
        dest="receivers",
        action="append",
        required=True,
        metavar="<receiver-agent-id>",
        help="Receiver agent id; repeat to share with several agents",
    )

    unshare_parser = _add_command_parser(
        skill_subparsers,
        "unshare",
        SKILL_HELP["unshare"],
        example="skill unshare assistant librarian",
    )
    unshare_parser.add_argument("agent", metavar="<agent-id>", help="Owning Identity Agent id")
    unshare_parser.add_argument("name", metavar="<skill-name>", help="Shared Skill name")

    read_parser = _add_command_parser(
        skill_subparsers, "read", SKILL_HELP["read"], example="skill read --scope global"
    )
    _add_skill_scope_argument(read_parser)
    read_parser.add_argument(
        "name",
        nargs="?",
        metavar="<skill-name>",
        help="Read only this Skill; omission reads the whole editable scope",
    )
    inspect = _add_command_parser(
        skill_subparsers,
        "inspect",
        "Read one exact source package from the inventory",
        example="skill inspect <inventory-id>",
    )
    inspect.add_argument(
        "id",
        metavar="<inventory-id>",
        help="Exact id from skill inventory, including read-only sources",
    )

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


def _add_memory_parsers(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    memory_parser = subparsers.add_parser(
        "memory",
        help=AREA_HELP["memory"],
        description=AREA_HELP["memory"],
    )
    memory_subparsers = memory_parser.add_subparsers(dest="command", required=True)

    list_parser = _add_command_parser(
        memory_subparsers, "list", MEMORY_HELP["list"], example="memory list assistant"
    )
    _add_memory_agent_argument(list_parser)

    add_parser = _add_command_parser(
        memory_subparsers,
        "add",
        MEMORY_HELP["add"],
        example='memory add assistant --scope user --content "Prefers concise answers"',
    )
    _add_memory_agent_argument(add_parser)
    _add_memory_content_arguments(add_parser)

    replace_parser = _add_command_parser(
        memory_subparsers,
        "replace",
        MEMORY_HELP["replace"],
        example='memory replace assistant 3 --content "Updated preference"',
    )
    _add_memory_agent_argument(replace_parser)
    replace_parser.add_argument("entry_id", type=int, metavar="<entry-id>")
    _add_memory_content_arguments(replace_parser)

    remove_parser = _add_command_parser(
        memory_subparsers,
        "remove",
        MEMORY_HELP["remove"],
        example="memory remove assistant 3 --yes",
    )
    _add_memory_agent_argument(remove_parser)
    remove_parser.add_argument("entry_id", type=int, metavar="<entry-id>")
    remove_parser.add_argument("--yes", action="store_true", help="Confirm removal")


def _add_memory_agent_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("agent", metavar="<agent-id>", help="Identity Agent id")
    parser.add_argument(
        "--scope",
        choices=("agent", "user"),
        default="agent",
        help="Memory file to operate on (default: agent)",
    )


def _add_memory_content_arguments(parser: argparse.ArgumentParser) -> None:
    content_group = parser.add_mutually_exclusive_group(required=True)
    content_group.add_argument("--content", help="Entry text as inline content")
    content_group.add_argument(
        "--file", dest="content_file", metavar="<path>", help="Read entry text from a file"
    )
