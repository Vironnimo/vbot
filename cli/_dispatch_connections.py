"""CLI argument translation for Channels, Providers, Models, and Extensions."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from typing import Any

from cli._input import (
    _read_stdin_utf8,
)
from cli.extensions_management import extensions_operation
from cli.provider_management import (
    provider_connect,
    provider_connect_status,
    provider_custom_delete,
    provider_custom_list,
    provider_custom_save,
    provider_disconnect,
    provider_set_enabled,
    provider_unset_key,
    provider_usage_history,
    provider_usage_history_clear,
)
from cli.server_management import CommandResult, ServerInstance
from cli.task_model_management import (
    task_model_clear,
    task_model_list,
    task_model_options,
    task_model_set,
    task_model_set_option,
    task_model_status,
    task_model_targets,
    task_model_unset_option,
)


def dispatch_channel_command(
    args: argparse.Namespace,
    instance: ServerInstance,
    *,
    add_channel: Callable[
        [
            ServerInstance,
            str,
            str,
            str,
            str,
            str,
            Sequence[str],
            str,
            Sequence[str],
            bool,
            str | None,
        ],
        CommandResult,
    ],
    list_channels: Callable[[ServerInstance], CommandResult],
    remove_channel: Callable[[ServerInstance, str], CommandResult],
    update_channel: Callable[[ServerInstance, str, dict[str, Any]], CommandResult],
    enable_channel: Callable[[ServerInstance, str], CommandResult],
    disable_channel: Callable[[ServerInstance, str], CommandResult],
    channel_status_fn: Callable[[ServerInstance, str], CommandResult],
    set_channel_token: Callable[[ServerInstance, str, str], CommandResult],
    channel_identity_fn: Callable[[ServerInstance, str, str | None], CommandResult],
    channel_access_fn: Callable[[ServerInstance, str, str], CommandResult],
    grant_channel_admin_fn: Callable[[ServerInstance, str, str, str], CommandResult],
    revoke_channel_admin_fn: Callable[[ServerInstance, str, str, str], CommandResult],
) -> CommandResult:
    """Dispatch one parsed channel command against the server RPC client."""

    if args.command == "add":
        token: str | None = None
        if args.token_stdin:
            try:
                token = _read_stdin_utf8()
            except (OSError, UnicodeError) as exc:
                return CommandResult(
                    ok=False,
                    message=f"cannot read --token-stdin value as UTF-8: {exc}",
                    instance=instance,
                )
        return add_channel(
            instance,
            args.id,
            args.platform,
            args.agent,
            args.token_env,
            args.dm_scope,
            args.allow,
            args.response_mode,
            args.mention_patterns,
            args.observe_unaddressed == "true",
            token,
        )
    if args.command == "list":
        return list_channels(instance)
    if args.command == "remove":
        return remove_channel(instance, args.id)
    if args.command == "update":
        return update_channel(instance, args.id, _channel_changes_from_args(args))
    if args.command == "enable":
        return enable_channel(instance, args.id)
    if args.command == "disable":
        return disable_channel(instance, args.id)
    if args.command == "status":
        return channel_status_fn(instance, args.id)
    if args.command == "set-token":
        try:
            token = _read_stdin_utf8()
        except (OSError, UnicodeError) as exc:
            return CommandResult(
                ok=False,
                message=f"cannot read --stdin token as UTF-8: {exc}",
                instance=instance,
            )
        return set_channel_token(instance, args.id, token)
    if args.command == "identity":
        return channel_identity_fn(instance, args.id, args.user)
    if args.command == "access":
        return channel_access_fn(instance, args.id, args.access_scope_id)
    if args.command == "grant-admin":
        return grant_channel_admin_fn(
            instance,
            args.id,
            args.access_scope_id,
            args.user_id,
        )
    if args.command == "revoke-admin":
        return revoke_channel_admin_fn(
            instance,
            args.id,
            args.access_scope_id,
            args.user_id,
        )
    raise ValueError(f"Unsupported channel command: {args.command}")


def _channel_changes_from_args(args: argparse.Namespace) -> dict[str, Any]:
    changes: dict[str, Any] = {}
    if args.platform is not None:
        changes["platform"] = args.platform
    if args.agent is not None:
        changes["agent_id"] = args.agent
    if args.token_env is not None:
        changes["token_env_var"] = args.token_env
    if args.dm_scope is not None:
        changes["dm_scope"] = args.dm_scope
    if args.allow is not None:
        changes["allowed_chat_ids"] = list(args.allow)
    if args.enabled is not None:
        changes["enabled"] = args.enabled == "true"
    if args.response_mode is not None:
        changes["response_mode"] = args.response_mode
    if args.mention_patterns is not None:
        changes["mention_patterns"] = list(args.mention_patterns)
    if args.observe_unaddressed is not None:
        changes["observe_unaddressed"] = args.observe_unaddressed == "true"
    return changes


def dispatch_provider_command(
    args: argparse.Namespace,
    instance: ServerInstance,
    *,
    list_providers: Callable[[ServerInstance], CommandResult],
    provider_status_fn: Callable[[ServerInstance, str, str | None], CommandResult],
    provider_usage_fn: Callable[[ServerInstance, Sequence[str] | None], CommandResult],
    set_provider_key: Callable[
        [ServerInstance, str, str, str | None, bool, str | None], CommandResult
    ],
    unset_provider_key_fn: Callable[
        [ServerInstance, str, str | None, str | None], CommandResult
    ] = provider_unset_key,
    connect_provider_fn: Callable[
        [ServerInstance, str, str, str | None], CommandResult
    ] = provider_connect,
    disconnect_provider_fn: Callable[
        [ServerInstance, str, str, str | None], CommandResult
    ] = provider_disconnect,
    connect_status_fn: Callable[
        [ServerInstance, str, str, str | None], CommandResult
    ] = provider_connect_status,
    set_enabled_fn: Callable[
        [ServerInstance, str, bool, str | None], CommandResult
    ] = provider_set_enabled,
    custom_list_fn: Callable[[ServerInstance], CommandResult] = provider_custom_list,
    custom_save_fn: Callable[..., CommandResult] = provider_custom_save,
    custom_delete_fn: Callable[[ServerInstance, str], CommandResult] = provider_custom_delete,
    usage_history_fn: Callable[
        [ServerInstance, str | None, str | None], CommandResult
    ] = provider_usage_history,
    usage_history_clear_fn: Callable[
        [ServerInstance, bool], CommandResult
    ] = provider_usage_history_clear,
) -> CommandResult:
    """Dispatch one parsed provider command against the server RPC client."""

    if args.command == "list":
        return list_providers(instance)
    if args.command == "custom-list":
        return custom_list_fn(instance)
    if args.command == "custom-save":
        try:
            api_key = _read_stdin_utf8() if args.api_key_stdin else args.api_key
        except (OSError, UnicodeError):
            return CommandResult(
                ok=False, message="cannot read API key from UTF-8 stdin", instance=instance
            )
        return custom_save_fn(
            instance,
            args.provider,
            name=args.name,
            adapter=args.adapter,
            base_url=args.base_url,
            auth=args.auth,
            api_key=api_key,
            models_endpoint=args.models_endpoint,
            model_ids=args.model,
        )
    if args.command == "custom-delete":
        return custom_delete_fn(instance, args.provider)
    if args.command == "status":
        return provider_status_fn(instance, args.provider, args.connection)
    if args.command == "usage":
        return provider_usage_fn(instance, args.connection)
    if args.command == "usage-history":
        return usage_history_fn(instance, args.since, args.until)
    if args.command == "usage-history-clear":
        return usage_history_clear_fn(instance, args.yes)
    if args.command in ("enable", "disable"):
        return set_enabled_fn(instance, args.provider, args.command == "enable", args.connection)
    if args.command == "set-key":
        try:
            value = _read_stdin_utf8() if args.stdin else args.value
        except (OSError, UnicodeError):
            return CommandResult(
                ok=False, message="cannot read API key from UTF-8 stdin", instance=instance
            )
        return set_provider_key(
            instance,
            args.provider,
            value,
            args.connection,
            args.refresh_models,
            args.account,
        )
    if args.command == "unset-key":
        return unset_provider_key_fn(instance, args.provider, args.connection, args.account)
    if args.command == "connect":
        return connect_provider_fn(instance, args.provider, args.connection, args.account)
    if args.command == "disconnect":
        return disconnect_provider_fn(instance, args.provider, args.connection, args.account)
    if args.command == "connect-status":
        return connect_status_fn(instance, args.provider, args.connection, args.account)
    raise ValueError(f"Unsupported provider command: {args.command}")


def dispatch_model_command(
    args: argparse.Namespace,
    instance: ServerInstance,
    *,
    list_models_fn: Callable[[ServerInstance, dict[str, Any]], CommandResult],
    show_model_fn: Callable[[ServerInstance, str], CommandResult],
    refresh_models_fn: Callable[[ServerInstance, str | None], CommandResult],
) -> CommandResult:
    """Dispatch one parsed model command against the server RPC client."""

    if args.command == "list":
        return list_models_fn(instance, _model_filters_from_args(args))
    if args.command == "show":
        return show_model_fn(instance, args.model)
    if args.command == "refresh":
        return refresh_models_fn(instance, args.provider)
    raise ValueError(f"Unsupported model command: {args.command}")


def _model_filters_from_args(args: argparse.Namespace) -> dict[str, Any]:
    filters: dict[str, Any] = {}
    for argument, rpc_field in (
        ("provider_id", "provider_id"),
        ("capability", "capabilities"),
        ("task", "tasks"),
        ("input_modality", "input_modalities"),
        ("output_modality", "output_modalities"),
        ("min_context_window", "min_context_window"),
    ):
        value = getattr(args, argument, None)
        if value is not None:
            filters[rpc_field] = value
    return filters


def dispatch_task_model_command(
    args: argparse.Namespace,
    instance: ServerInstance,
    *,
    list_bindings_fn: Callable[[ServerInstance], CommandResult] = task_model_list,
    list_targets_fn: Callable[[ServerInstance, str], CommandResult] = task_model_targets,
    show_options_fn: Callable[
        [ServerInstance, str, str | None], CommandResult
    ] = task_model_options,
    set_binding_fn: Callable[
        [ServerInstance, str, str, str | None, Sequence[Sequence[str]]], CommandResult
    ] = task_model_set,
    set_option_fn: Callable[[ServerInstance, str, str, str], CommandResult] = task_model_set_option,
    unset_option_fn: Callable[[ServerInstance, str, str], CommandResult] = task_model_unset_option,
    clear_binding_fn: Callable[[ServerInstance, str], CommandResult] = task_model_clear,
    status_fn: Callable[[ServerInstance, str], CommandResult] = task_model_status,
) -> CommandResult:
    """Dispatch one parsed task-model command against the server RPC client."""

    if args.command == "list":
        return list_bindings_fn(instance)
    if args.command == "status":
        return status_fn(instance, args.task_type)
    if args.command == "targets":
        return list_targets_fn(instance, args.task_type)
    if args.command == "options":
        return show_options_fn(instance, args.task_type, args.target)
    if args.command == "set":
        options_json = args.options_json
        if args.options_stdin:
            try:
                options_json = _read_stdin_utf8()
            except UnicodeError as exc:
                return CommandResult(
                    ok=False,
                    message=f"cannot read --options-stdin value as UTF-8: {exc}",
                    instance=instance,
                )
        return set_binding_fn(
            instance,
            args.task_type,
            args.target,
            options_json,
            args.option_pairs,
        )
    if args.command == "set-option":
        if args.stdin and args.value is not None:
            return CommandResult(
                ok=False,
                message="task-model set-option accepts either <value> or --stdin, not both",
                instance=instance,
            )
        if args.stdin:
            try:
                value = _read_stdin_utf8()
            except UnicodeError as exc:
                return CommandResult(
                    ok=False,
                    message=f"cannot read --stdin value as UTF-8: {exc}",
                    instance=instance,
                )
        else:
            value = args.value
        if value is None:
            return CommandResult(
                ok=False,
                message="task-model set-option requires <value> or --stdin",
                instance=instance,
            )
        return set_option_fn(instance, args.task_type, args.name, value)
    if args.command == "unset-option":
        return unset_option_fn(instance, args.task_type, args.name)
    if args.command == "clear":
        return clear_binding_fn(instance, args.task_type)
    raise ValueError(f"Unsupported task-model command: {args.command}")


def dispatch_extensions_command(
    args: argparse.Namespace,
    instance: ServerInstance,
    *,
    list_extensions_fn: Callable[[ServerInstance], CommandResult],
    reload_extensions_fn: Callable[[ServerInstance], CommandResult],
    enable_extension_fn: Callable[[ServerInstance, str], CommandResult],
    disable_extension_fn: Callable[[ServerInstance, str], CommandResult],
    show_extension_fn: Callable[[ServerInstance, str], CommandResult],
    set_extension_fn: Callable[[ServerInstance, str, str, str], CommandResult],
) -> CommandResult:
    """Route one name-first extensions command against the server RPC client.

    Grammar: ``list`` | ``reload`` | ``enable|disable <name>`` | ``<name>`` (show
    settings) | ``<name> set <field> <value>`` (write one setting). The selector is
    either a reserved verb or an extension name; a name is inspected or configured.
    """

    selector = args.selector
    rest = list(args.rest)

    if selector == "list":
        if rest:
            return _extensions_usage(instance, "extensions list takes no arguments")
        return list_extensions_fn(instance)

    if selector == "reload":
        if rest:
            return _extensions_usage(instance, "extensions reload takes no arguments")
        return reload_extensions_fn(instance)

    if selector in ("enable", "disable"):
        if len(rest) != 1:
            return _extensions_usage(instance, f"usage: extensions {selector} <name>")
        toggle = enable_extension_fn if selector == "enable" else disable_extension_fn
        return toggle(instance, rest[0])

    # Otherwise the selector is an extension name: inspect or configure it.
    name = selector
    if not rest:
        return show_extension_fn(instance, name)
    if rest[0] == "set":
        return _dispatch_extensions_set(args, instance, name, rest, set_extension_fn)
    return extensions_operation(instance, name, rest)


def _dispatch_extensions_set(
    args: argparse.Namespace,
    instance: ServerInstance,
    name: str,
    rest: list[str],
    set_extension_fn: Callable[[ServerInstance, str, str, str], CommandResult],
) -> CommandResult:
    """Parse ``<name> set <field> <value>`` (or ``--stdin``) and delegate."""

    if args.stdin:
        if len(rest) != 2:
            return _extensions_usage(instance, f"usage: extensions {name} set <field> --stdin")
        field = rest[1]
        try:
            value = _read_stdin_utf8()
        except (OSError, UnicodeError) as exc:
            return CommandResult(
                ok=False,
                message=f"cannot read --stdin value as UTF-8: {exc}",
                instance=instance,
            )
    else:
        if len(rest) != 3:
            return _extensions_usage(
                instance, f"usage: extensions {name} set <field> <value>  (or --stdin)"
            )
        field = rest[1]
        value = rest[2]
    return set_extension_fn(instance, name, field, value)


def _extensions_usage(instance: ServerInstance, message: str) -> CommandResult:
    return CommandResult(ok=False, message=message, instance=instance)
