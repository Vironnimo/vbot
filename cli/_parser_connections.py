"""CLI grammar for Channels, Providers, Models, and task-model bindings."""

from __future__ import annotations

import argparse

from cli._parser_common import (
    AREA_HELP,
    CHANNEL_DM_SCOPES,
    CHANNEL_HELP,
    CHANNEL_PLATFORMS,
    CHANNEL_RESPONSE_MODES,
    MODEL_HELP,
    MODEL_TASK_TYPES,
    PROVIDER_HELP,
    TASK_MODEL_HELP,
    TASK_TYPES,
    _add_command_parser,
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

    identity_parser = _add_command_parser(
        channel_subparsers,
        "identity",
        CHANNEL_HELP["identity"],
        example="channel identity tg-main --user 12345",
    )
    identity_parser.add_argument("id", metavar="<channel-id>", help="Channel config id")
    identity_parser.add_argument(
        "--user",
        metavar="<user-id>",
        help="Set this previously seen platform user as the Channel account's own identity",
    )

    access_parser = _add_command_parser(
        channel_subparsers,
        "access",
        CHANNEL_HELP["access"],
        example="channel access tg-main --group -100123",
    )
    access_parser.add_argument("id", metavar="<channel-id>", help="Channel config id")
    access_parser.add_argument(
        "--group",
        required=True,
        dest="access_scope_id",
        metavar="<group-id>",
        help="Group access-scope id",
    )

    for command in ("grant-admin", "revoke-admin"):
        admin_parser = _add_command_parser(
            channel_subparsers,
            command,
            CHANNEL_HELP[command],
            example=f"channel {command} tg-main --group -100123 --user 12345",
        )
        admin_parser.add_argument("id", metavar="<channel-id>", help="Channel config id")
        admin_parser.add_argument(
            "--group",
            required=True,
            dest="access_scope_id",
            metavar="<group-id>",
            help="Group access-scope id",
        )
        admin_parser.add_argument(
            "--user",
            required=True,
            dest="user_id",
            metavar="<user-id>",
            help="Stable platform user id",
        )


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
        "--observe-unaddressed",
        choices=("true", "false"),
        default="false" if include_defaults else None,
        help="Let the agent observe group messages it does not answer",
    )


def _add_provider_parsers(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    provider_parser = subparsers.add_parser(
        "provider",
        help=AREA_HELP["provider"],
        description="Inspect and configure vBot provider connections",
    )
    provider_subparsers = provider_parser.add_subparsers(dest="command", required=True)

    _add_command_parser(provider_subparsers, "list", PROVIDER_HELP["list"], example="provider list")

    _add_command_parser(
        provider_subparsers,
        "custom-list",
        PROVIDER_HELP["custom-list"],
        example="provider custom-list",
    )
    custom_save_parser = _add_command_parser(
        provider_subparsers,
        "custom-save",
        PROVIDER_HELP["custom-save"],
        example=(
            'provider custom-save local-ai --name "Local AI" '
            "--base-url http://127.0.0.1:8080/v1 --auth none --model chat-model"
        ),
    )
    custom_save_parser.add_argument(
        "provider",
        metavar="<provider-id>",
        help="Lowercase, hyphen-separated Custom Provider id",
    )
    custom_save_parser.add_argument("--name", required=True, help="Display name")
    custom_save_parser.add_argument(
        "--adapter",
        default="openai_compatible",
        choices=("openai_compatible",),
        help="Provider wire adapter",
    )
    custom_save_parser.add_argument(
        "--base-url",
        required=True,
        help="OpenAI-compatible API base URL",
    )
    custom_save_parser.add_argument(
        "--auth",
        choices=("api_key", "none"),
        default="api_key",
        help="Connection authentication type",
    )
    key_source = custom_save_parser.add_mutually_exclusive_group()
    key_source.add_argument("--api-key", help="Optional API key; prefer --api-key-stdin")
    key_source.add_argument(
        "--api-key-stdin", action="store_true", help="Read the optional API key from UTF-8 stdin"
    )
    custom_save_parser.add_argument(
        "--models-endpoint",
        help="Optional discovery path such as /models",
    )
    custom_save_parser.add_argument(
        "--model",
        action="append",
        default=[],
        metavar="<model-id>",
        help="Manual chat Model id; repeat to add several",
    )
    custom_delete_parser = _add_command_parser(
        provider_subparsers,
        "custom-delete",
        PROVIDER_HELP["custom-delete"],
        example="provider custom-delete local-ai",
    )
    custom_delete_parser.add_argument(
        "provider",
        metavar="<provider-id>",
        help="Custom Provider id to delete",
    )

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

    usage_history_parser = _add_command_parser(
        provider_subparsers,
        "usage-history",
        PROVIDER_HELP["usage-history"],
        example="provider usage-history --since 2026-08-01T00:00:00Z",
    )
    usage_history_parser.add_argument("--since", help="ISO 8601 lower bound (inclusive)")
    usage_history_parser.add_argument("--until", help="ISO 8601 upper bound (inclusive)")

    usage_history_clear_parser = _add_command_parser(
        provider_subparsers,
        "usage-history-clear",
        PROVIDER_HELP["usage-history-clear"],
        example="provider usage-history-clear --yes",
    )
    usage_history_clear_parser.add_argument("--yes", action="store_true", help="Confirm deletion")

    set_key_parser = _add_command_parser(
        provider_subparsers,
        "set-key",
        PROVIDER_HELP["set-key"],
        example="provider set-key openai --stdin --refresh-models",
    )
    set_key_parser.description = (
        "Write an API key to the target data-dir .env through the server RPC contract. "
        "Example: vbot provider set-key openai --stdin --refresh-models"
    )
    set_key_parser.add_argument(
        "provider", metavar="<provider-id>", help="Provider id to configure"
    )
    key_source = set_key_parser.add_mutually_exclusive_group(required=True)
    key_source.add_argument(
        "value",
        nargs="?",
        metavar="<api-key>",
        help="API key; prefer --stdin to keep it out of shell arguments",
    )
    key_source.add_argument(
        "--stdin", action="store_true", help="Read the API key from UTF-8 stdin"
    )
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
        "Process-environment credentials are not touched. Example: vbot provider unset-key openai"
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
            f"Example: vbot provider {command} ollama"
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
        choices=MODEL_TASK_TYPES,
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
    show_parser = _add_command_parser(
        model_subparsers,
        "show",
        MODEL_HELP["show"],
        example="model show openrouter/microsoft/mai-voice-2",
    )
    show_parser.add_argument(
        "model",
        metavar="<provider>/<model-id>",
        help="Exact Model id from model list",
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

    status_parser = _add_command_parser(
        task_model_subparsers,
        "status",
        TASK_MODEL_HELP["status"],
        example="task-model status text_to_speech",
    )
    status_parser.add_argument("task_type", metavar="<task-type>", choices=TASK_TYPES)

    options_parser = _add_command_parser(
        task_model_subparsers,
        "options",
        TASK_MODEL_HELP["options"],
        example="task-model options text_to_speech openai/gpt-4o-mini-tts::api-key",
    )
    options_parser.add_argument("task_type", metavar="<task-type>", choices=TASK_TYPES)
    options_parser.add_argument(
        "target",
        nargs="?",
        metavar="<target-id>",
        help="Target id; omitted uses the currently configured binding",
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
    option_group = set_parser.add_mutually_exclusive_group()
    option_group.add_argument(
        "--options",
        dest="options_json",
        metavar="<json>",
        help='Task options as a JSON object, for example \'{"voice": "alloy"}\'',
    )
    option_group.add_argument(
        "--option",
        dest="option_pairs",
        nargs=2,
        action="append",
        default=[],
        metavar=("<name>", "<value>"),
        help="Set one option without JSON quoting; repeat for multiple options",
    )
    option_group.add_argument(
        "--options-stdin",
        action="store_true",
        help="Read the complete UTF-8 JSON option object from stdin",
    )

    set_option_parser = _add_command_parser(
        task_model_subparsers,
        "set-option",
        TASK_MODEL_HELP["set-option"],
        example=("task-model set-option text_to_speech voice en-us-harper:mai-voice-2"),
    )
    set_option_parser.add_argument("task_type", metavar="<task-type>", choices=TASK_TYPES)
    set_option_parser.add_argument("name", metavar="<name>")
    set_option_parser.add_argument("value", nargs="?", metavar="<value>")
    set_option_parser.add_argument(
        "--stdin",
        action="store_true",
        help="Read the UTF-8 option value from stdin (useful for JSON objects)",
    )

    unset_option_parser = _add_command_parser(
        task_model_subparsers,
        "unset-option",
        TASK_MODEL_HELP["unset-option"],
        example="task-model unset-option text_to_speech speed",
    )
    unset_option_parser.add_argument("task_type", metavar="<task-type>", choices=TASK_TYPES)
    unset_option_parser.add_argument("name", metavar="<name>")

    clear_parser = _add_command_parser(
        task_model_subparsers,
        "clear",
        TASK_MODEL_HELP["clear"],
        example="task-model clear image_generation",
    )
    clear_parser.add_argument("task_type", metavar="<task-type>", choices=TASK_TYPES)
