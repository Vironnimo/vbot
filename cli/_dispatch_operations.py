"""CLI argument translation for content, automation, Settings, and diagnostics."""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable, Sequence
from typing import Any

from cli._input import (
    _optional_content_from_args,
    _prompt_content_from_args,
    _read_stdin_utf8,
)
from cli.bootstrap_management import (
    bootstrap_create,
    bootstrap_delete,
    bootstrap_disable,
    bootstrap_enable,
    bootstrap_list,
    bootstrap_show,
    bootstrap_update,
)
from cli.config_management import coerce_config_value
from cli.cron_management import (
    cron_create,
    cron_delete,
    cron_disable,
    cron_enable,
    cron_list,
    cron_show,
    cron_update,
)
from cli.debug_management import (
    debug_model_probe,
    debug_status,
    debug_trace_clear,
    debug_trace_list,
    debug_trace_show,
)
from cli.log_management import log_read
from cli.memory_management import memory_add, memory_list, memory_remove, memory_replace
from cli.prompt_management import (
    prompt_create,
    prompt_remove,
    prompt_reset_layout,
    prompt_set_layout,
    prompt_show,
)
from cli.server_management import CommandResult, ServerInstance
from cli.skill_management import (
    skill_create,
    skill_delete,
    skill_inspect,
    skill_inventory,
    skill_read,
    skill_remove_file,
    skill_set_disabled,
    skill_share,
    skill_unshare,
    skill_update,
    skill_write_file,
)
from cli.statistics_management import statistics_report


def _statistics_report_adapter(
    instance: ServerInstance, section: str, since: str | None, until: str | None
) -> CommandResult:
    """Adapt the keyword-only statistics report call to a positional signature.

    ``statistics_report`` takes ``since``/``until`` as keyword-only options; the
    dispatcher and its injected test double use a uniform positional signature,
    so this thin adapter bridges the two.
    """

    return statistics_report(instance, section, since=since, until=until)


def dispatch_tool_command(
    args: argparse.Namespace,
    instance: ServerInstance,
    *,
    list_tools_fn: Callable[[ServerInstance], CommandResult],
) -> CommandResult:
    """Dispatch one parsed tool command against the server RPC client."""

    if args.command == "list":
        return list_tools_fn(instance)
    raise ValueError(f"Unsupported tool command: {args.command}")


def dispatch_prompt_command(
    args: argparse.Namespace,
    instance: ServerInstance,
    *,
    list_prompts_fn: Callable[[ServerInstance, str], CommandResult],
    update_prompt_fn: Callable[[ServerInstance, str, str, str], CommandResult],
    reset_prompt_fn: Callable[[ServerInstance, str, str], CommandResult],
    preview_prompt_fn: Callable[[ServerInstance, str, str], CommandResult],
    create_prompt_fn: Callable[
        [ServerInstance, str, str | None, int | None, str], CommandResult
    ] = prompt_create,
    remove_prompt_fn: Callable[[ServerInstance, str, str], CommandResult] = prompt_remove,
    set_prompt_layout_fn: Callable[
        [ServerInstance, list[object], str], CommandResult
    ] = prompt_set_layout,
    reset_prompt_layout_fn: Callable[[ServerInstance, str], CommandResult] = prompt_reset_layout,
) -> CommandResult:
    """Dispatch one parsed prompt command against the server RPC client."""

    if args.command == "list":
        return list_prompts_fn(instance, args.scope)
    if args.command == "show":
        return prompt_show(instance, args.block_id, args.scope)
    if args.command == "update":
        try:
            content = _prompt_content_from_args(args)
        except (OSError, ValueError) as exc:
            return CommandResult(
                ok=False,
                message=f"cannot read prompt content file: {exc}",
                instance=instance,
            )
        return update_prompt_fn(instance, args.block_id, content, args.scope)
    if args.command == "reset":
        return reset_prompt_fn(instance, args.block_id, args.scope)
    if args.command == "create":
        try:
            create_content = _optional_content_from_args(args)
        except OSError as exc:
            return CommandResult(
                ok=False,
                message=f"cannot read prompt content file: {exc}",
                instance=instance,
            )
        return create_prompt_fn(instance, args.slug, create_content, args.position, args.scope)
    if args.command == "remove":
        return remove_prompt_fn(instance, args.block_id, args.scope)
    if args.command == "set-layout":
        return set_prompt_layout_fn(instance, args.layout_json, args.scope)
    if args.command == "reset-layout":
        return reset_prompt_layout_fn(instance, args.scope)
    if args.command == "preview":
        return preview_prompt_fn(instance, args.agent, args.scope)
    raise ValueError(f"Unsupported prompt command: {args.command}")


def dispatch_log_command(
    args: argparse.Namespace,
    instance: ServerInstance,
    *,
    list_logs_fn: Callable[[ServerInstance], CommandResult],
    read_log_fn: Callable[[ServerInstance, str], CommandResult],
) -> CommandResult:
    """Dispatch one parsed log command against the server RPC client."""

    if args.command == "list":
        return list_logs_fn(instance)
    if args.command == "read":
        if args.limit < 0:
            return CommandResult(
                ok=False, message="--limit must be zero or positive", instance=instance
            )
        if args.limit != 100 or args.level is not None:
            return log_read(instance, args.file, limit=args.limit, level=args.level)
        return read_log_fn(instance, args.file)
    raise ValueError(f"Unsupported log command: {args.command}")


def dispatch_skill_command(
    args: argparse.Namespace,
    instance: ServerInstance,
    *,
    list_skills_fn: Callable[[ServerInstance], CommandResult],
    read_skills_fn: Callable[[ServerInstance, str], CommandResult] = skill_read,
    create_skill_fn: Callable[
        [ServerInstance, str, str, str, str | None], CommandResult
    ] = skill_create,
    update_skill_fn: Callable[
        [ServerInstance, str, str, str, str | None], CommandResult
    ] = skill_update,
    delete_skill_fn: Callable[[ServerInstance, str, str, bool], CommandResult] = skill_delete,
    write_skill_file_fn: Callable[
        [ServerInstance, str, str, str, str], CommandResult
    ] = skill_write_file,
    remove_skill_file_fn: Callable[
        [ServerInstance, str, str, str, bool], CommandResult
    ] = skill_remove_file,
    inventory_skills_fn: Callable[[ServerInstance], CommandResult] = skill_inventory,
    set_skill_disabled_fn: Callable[
        [ServerInstance, str, bool], CommandResult
    ] = skill_set_disabled,
    share_skill_fn: Callable[
        [ServerInstance, str, str, Sequence[str]], CommandResult
    ] = skill_share,
    unshare_skill_fn: Callable[[ServerInstance, str, str], CommandResult] = skill_unshare,
) -> CommandResult:
    """Dispatch one parsed skill command against the server RPC client."""

    if args.command == "list":
        return list_skills_fn(instance)
    if args.command == "inventory":
        return inventory_skills_fn(instance)
    if args.command == "inspect":
        return skill_inspect(instance, args.id)
    if args.command in {"disable", "enable"}:
        return set_skill_disabled_fn(instance, args.name, args.command == "disable")
    if args.command == "share":
        return share_skill_fn(instance, args.agent, args.name, args.receivers)
    if args.command == "unshare":
        return unshare_skill_fn(instance, args.agent, args.name)
    if args.command == "read":
        if args.name is not None:
            return skill_read(instance, args.scope, args.name)
        return read_skills_fn(instance, args.scope)
    if args.command in {"create", "update", "write-file"}:
        try:
            content = _prompt_content_from_args(args)
        except (OSError, ValueError) as exc:
            return CommandResult(
                ok=False,
                message=f"cannot read skill content file: {exc}",
                instance=instance,
            )
        if args.command == "create":
            return create_skill_fn(instance, args.scope, args.name, content, args.source)
        if args.command == "update":
            return update_skill_fn(instance, args.scope, args.name, content, args.source)
        return write_skill_file_fn(instance, args.scope, args.name, args.path, content)
    if args.command == "delete":
        return delete_skill_fn(instance, args.scope, args.name, args.yes)
    if args.command == "remove-file":
        return remove_skill_file_fn(instance, args.scope, args.name, args.path, args.yes)
    raise ValueError(f"Unsupported skill command: {args.command}")


def dispatch_memory_command(
    args: argparse.Namespace,
    instance: ServerInstance,
) -> CommandResult:
    """Dispatch one parsed memory command against the server RPC client."""

    try:
        content = _optional_content_from_args(args)
    except (OSError, ValueError) as exc:
        return CommandResult(
            ok=False,
            message=f"cannot read memory entry file: {exc}",
            instance=instance,
        )
    if args.command == "list":
        return memory_list(instance, args.agent)
    if args.command == "add":
        return memory_add(instance, args.agent, args.scope, content or "")
    if args.command == "replace":
        return memory_replace(instance, args.agent, args.scope, args.entry_id, content or "")
    if args.command == "remove":
        return memory_remove(instance, args.agent, args.scope, args.entry_id, args.yes)
    raise ValueError(f"Unsupported memory command: {args.command}")


def dispatch_cron_command(
    args: argparse.Namespace,
    instance: ServerInstance,
    *,
    create_cron_fn: Callable[[ServerInstance, dict[str, Any]], CommandResult] = cron_create,
    list_cron_fn: Callable[[ServerInstance], CommandResult] = cron_list,
    update_cron_fn: Callable[[ServerInstance, str, dict[str, Any]], CommandResult] = cron_update,
    delete_cron_fn: Callable[[ServerInstance, str], CommandResult] = cron_delete,
    enable_cron_fn: Callable[[ServerInstance, str], CommandResult] = cron_enable,
    disable_cron_fn: Callable[[ServerInstance, str], CommandResult] = cron_disable,
) -> CommandResult:
    """Dispatch one parsed cron command against the server RPC client."""

    if args.command == "list":
        return list_cron_fn(instance)
    if args.command == "show":
        return cron_show(instance, args.id)
    if args.command == "create":
        return create_cron_fn(instance, _cron_create_fields_from_args(args))
    if args.command == "update":
        return update_cron_fn(instance, args.id, _cron_changes_from_args(args))
    if args.command == "delete":
        return delete_cron_fn(instance, args.id)
    if args.command == "enable":
        return enable_cron_fn(instance, args.id)
    if args.command == "disable":
        return disable_cron_fn(instance, args.id)
    raise ValueError(f"Unsupported cron command: {args.command}")


def dispatch_bootstrap_command(
    args: argparse.Namespace,
    instance: ServerInstance,
    *,
    create_fn: Callable[..., CommandResult] = bootstrap_create,
    list_fn: Callable[[ServerInstance], CommandResult] = bootstrap_list,
    update_fn: Callable[[ServerInstance, str, dict[str, Any]], CommandResult] = bootstrap_update,
    delete_fn: Callable[[ServerInstance, str], CommandResult] = bootstrap_delete,
    enable_fn: Callable[[ServerInstance, str], CommandResult] = bootstrap_enable,
    disable_fn: Callable[[ServerInstance, str], CommandResult] = bootstrap_disable,
) -> CommandResult:
    if args.command == "list":
        return list_fn(instance)
    if args.command == "show":
        return bootstrap_show(instance, args.id)
    if args.command == "create":
        if args.current_session and (args.agent is not None or args.session is not None):
            return CommandResult(
                ok=False,
                message="--current-session cannot be combined with <agent> or --session",
                instance=instance,
            )
        if not args.current_session and args.agent is None:
            return CommandResult(
                ok=False,
                message="provide <agent> or use --current-session",
                instance=instance,
            )
        fields: dict[str, Any] = {"prompt": args.prompt, "mode": args.mode}
        if args.agent is not None:
            fields["agent_id"] = args.agent
        if args.name is not None:
            fields["name"] = args.name
        if args.session is not None:
            fields["session_id"] = args.session
        return create_fn(instance, fields, current_session=args.current_session)
    if args.command == "update":
        changes: dict[str, Any] = {}
        for argument, field in (
            (args.agent, "agent_id"),
            (args.name, "name"),
            (args.prompt, "prompt"),
            (args.mode, "mode"),
            (args.session, "session_id"),
        ):
            if argument is not None:
                changes[field] = argument
        if args.clear_session:
            changes["session_id"] = None
        return update_fn(instance, args.id, changes)
    if args.command == "delete":
        return delete_fn(instance, args.id)
    if args.command == "enable":
        return enable_fn(instance, args.id)
    if args.command == "disable":
        return disable_fn(instance, args.id)
    raise ValueError(f"Unsupported Bootstrap command: {args.command}")


def _cron_create_fields_from_args(args: argparse.Namespace) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "agent_id": args.agent,
        "prompt": args.prompt,
    }
    if args.name is not None:
        fields["name"] = args.name
    if args.cron is not None:
        fields["schedule_type"] = "cron"
        fields["cron_expression"] = args.cron
    elif args.every is not None:
        fields["schedule_type"] = "interval"
        fields["interval_seconds"] = args.every * 60
    else:
        fields["schedule_type"] = "once"
        fields["run_at"] = args.at
    if args.repeat is not None:
        fields["repeat"] = args.repeat
    if args.session is not None:
        fields["session_id"] = args.session
    return fields


def _cron_changes_from_args(args: argparse.Namespace) -> dict[str, Any]:
    changes: dict[str, Any] = {}
    if args.agent is not None:
        changes["agent_id"] = args.agent
    if args.name is not None:
        changes["name"] = args.name
    if args.prompt is not None:
        changes["prompt"] = args.prompt
    if args.cron is not None:
        changes["schedule_type"] = "cron"
        changes["cron_expression"] = args.cron
    elif args.every is not None:
        changes["schedule_type"] = "interval"
        changes["interval_seconds"] = args.every * 60
    elif args.at is not None:
        changes["schedule_type"] = "once"
        changes["run_at"] = args.at
    if args.repeat is not None:
        changes["repeat"] = args.repeat
    if args.session is not None:
        changes["session_id"] = args.session
    if args.clear_session:
        changes["session_id"] = None
    if args.status is not None:
        changes["status"] = args.status
    return changes


def dispatch_statistics_command(
    args: argparse.Namespace,
    instance: ServerInstance,
    *,
    statistics_report_fn: Callable[
        [ServerInstance, str, str | None, str | None], CommandResult
    ] = _statistics_report_adapter,
) -> CommandResult:
    """Dispatch one parsed statistics command against the server RPC client.

    The subcommand name is the report section; ``--since``/``--until`` pass
    through verbatim (``None`` when the flag was omitted) so the server owns
    their validation.
    """

    return statistics_report_fn(instance, args.command, args.since, args.until)


def dispatch_debug_command(
    args: argparse.Namespace,
    instance: ServerInstance,
    *,
    debug_status_fn: Callable[[ServerInstance], CommandResult] = debug_status,
    trace_list_fn: Callable[[ServerInstance], CommandResult] = debug_trace_list,
    trace_show_fn: Callable[[ServerInstance, str], CommandResult] = debug_trace_show,
    trace_clear_fn: Callable[[ServerInstance], CommandResult] = debug_trace_clear,
    model_probe_fn: Callable[[ServerInstance, str, str], CommandResult] = debug_model_probe,
) -> CommandResult:
    """Dispatch one parsed debug command against the server RPC client."""

    if args.command == "status":
        return debug_status_fn(instance)
    if args.command == "traces":
        return trace_list_fn(instance)
    if args.command == "trace":
        return trace_show_fn(instance, args.trace_id)
    if args.command == "clear":
        return trace_clear_fn(instance)
    if args.command == "probe":
        return model_probe_fn(instance, args.provider, args.connection)
    raise ValueError(f"Unsupported debug command: {args.command}")


def dispatch_config_command(
    args: argparse.Namespace,
    instance: ServerInstance,
    *,
    raw_config_fn: Callable[[ServerInstance], CommandResult],
    list_config_fn: Callable[[ServerInstance, str | None], CommandResult],
    describe_config_fn: Callable[[ServerInstance, str], CommandResult],
    effective_config_fn: Callable[[ServerInstance], CommandResult],
    get_config_fn: Callable[[ServerInstance, str], CommandResult],
    set_config_fn: Callable[[ServerInstance, str, Any], CommandResult],
    unset_config_fn: Callable[[ServerInstance, str], CommandResult],
    patch_config_fn: Callable[[ServerInstance, list[dict[str, Any]]], CommandResult],
) -> CommandResult:
    """Dispatch one parsed config command against the server RPC client."""

    if args.command is None:
        return list_config_fn(instance, None)
    if args.command == "list":
        return list_config_fn(instance, args.prefix)
    if args.command == "describe":
        return describe_config_fn(instance, args.path)
    if args.command == "effective":
        return effective_config_fn(instance)
    if args.command == "raw":
        return raw_config_fn(instance)
    if args.command == "get":
        if args.details:
            return describe_config_fn(instance, args.path)
        return get_config_fn(instance, args.path)
    if args.command == "set":
        if args.stdin:
            try:
                coerced = json.loads(_read_stdin_utf8())
            except (OSError, ValueError):
                return CommandResult(
                    ok=False,
                    message="--stdin requires one valid UTF-8 JSON value; no setting was changed",
                    instance=instance,
                )
        else:
            coerced = coerce_config_value(args.value)
        return set_config_fn(instance, args.path, coerced)
    if args.command == "unset":
        return unset_config_fn(instance, args.path)
    if args.command == "patch":
        operations = [
            {"op": "set", "path": path, "value": coerce_config_value(value)}
            for path, value in args.set_values
        ]
        operations.extend({"op": "unset", "path": path} for path in args.unset_paths)
        if not operations:
            return CommandResult(
                ok=False,
                message="config patch requires at least one --set or --unset operation",
                instance=instance,
            )
        return patch_config_fn(instance, operations)
    raise ValueError(f"Unsupported config command: {args.command}")
