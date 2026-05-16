"""Built-in channel_send tool for proactive outbound channel messaging."""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.channels.channels import ChannelConfigError, ChannelError, ChannelNotFoundError
from core.tools.tools import JsonObject, ToolContext, ToolRegistry, tool_failure, tool_success

if TYPE_CHECKING:
    from core.channels.channels import ChannelService
    from core.chat.chat import ChatSessionManager

CHANNEL_SEND_TOOL_NAME = "channel_send"
CHANNEL_SEND_TOOL_DESCRIPTION = "Send a proactive outbound message through a configured channel."
_REQUIRED_CHANNEL_SEND_ARGUMENTS = frozenset(("channel_id", "message"))
_OPTIONAL_CHANNEL_SEND_ARGUMENTS = frozenset(("platform_target",))
_CHANNEL_SEND_ALLOWED_ARGUMENTS = (
    _REQUIRED_CHANNEL_SEND_ARGUMENTS | _OPTIONAL_CHANNEL_SEND_ARGUMENTS
)

CHANNEL_SEND_TOOL_PARAMETERS: JsonObject = {
    "type": "object",
    "properties": {
        "channel_id": {
            "type": "string",
            "description": "Configured channel id to send through.",
        },
        "message": {
            "type": "string",
            "description": "Outbound message text.",
        },
        "platform_target": {
            "type": "string",
            "description": (
                "Platform-specific target id. If omitted, uses the session metadata "
                "last_reply_target.platform_target value."
            ),
        },
    },
    "required": ["channel_id", "message"],
    "additionalProperties": False,
}


def register_channel_send_tool(
    registry: ToolRegistry,
    channel_service: ChannelService,
    chat_sessions: ChatSessionManager,
) -> None:
    """Register the channel_send tool with a vBot tool registry."""

    async def handler(context: ToolContext, arguments: JsonObject) -> JsonObject:
        return await _handle_channel_send_tool(channel_service, chat_sessions, context, arguments)

    registry.register(
        CHANNEL_SEND_TOOL_NAME,
        CHANNEL_SEND_TOOL_DESCRIPTION,
        CHANNEL_SEND_TOOL_PARAMETERS,
        handler,
    )


async def _handle_channel_send_tool(
    channel_service: ChannelService,
    chat_sessions: ChatSessionManager,
    context: ToolContext,
    arguments: JsonObject,
) -> JsonObject:
    unknown_arguments = sorted(set(arguments) - _CHANNEL_SEND_ALLOWED_ARGUMENTS)
    if unknown_arguments:
        names = ", ".join(unknown_arguments)
        return tool_failure("invalid_arguments", f"Unknown argument(s): {names}")

    try:
        channel_id = _required_non_empty_string(
            arguments.get("channel_id"), field_name="channel_id"
        )
        message = _required_non_empty_string(arguments.get("message"), field_name="message")
        platform_target = _platform_target_from_arguments_or_context(
            arguments,
            channel_service,
            chat_sessions,
            context,
            channel_id,
        )
        await channel_service.send(channel_id, message, platform_target)
    except ValueError as error:
        return tool_failure("invalid_arguments", str(error))
    except ChannelNotFoundError as error:
        return tool_failure("channel_not_found", str(error))
    except ChannelConfigError as error:
        return tool_failure("invalid_arguments", str(error))
    except ChannelError as error:
        return tool_failure("channel_error", str(error))

    return tool_success({"channel_id": channel_id, "platform_target": platform_target})


def _platform_target_from_arguments_or_context(
    arguments: JsonObject,
    channel_service: ChannelService,
    chat_sessions: ChatSessionManager,
    context: ToolContext,
    channel_id: str,
) -> str:
    platform_target_value = arguments.get("platform_target")
    if platform_target_value is not None:
        return _required_non_empty_string(platform_target_value, field_name="platform_target")

    metadata_platform_target = _platform_target_from_session_metadata(chat_sessions, context)
    if metadata_platform_target is not None:
        return metadata_platform_target

    config_platform_target = _platform_target_from_channel_config(channel_service, channel_id)
    if config_platform_target is not None:
        return config_platform_target

    raise ValueError(
        "platform_target is required when session metadata has no "
        "last_reply_target.platform_target and the channel has no unique allowed_chat_ids target"
    )


def _platform_target_from_session_metadata(
    chat_sessions: ChatSessionManager,
    context: ToolContext,
) -> str | None:
    metadata = chat_sessions.get_metadata(context.agent_id, context.session_id)
    last_reply_target = metadata.get("last_reply_target")
    if not isinstance(last_reply_target, dict):
        return None

    metadata_platform_target = last_reply_target.get("platform_target")
    if metadata_platform_target is None:
        return None

    return _required_non_empty_string(metadata_platform_target, field_name="last_reply_target.platform_target")


def _platform_target_from_channel_config(
    channel_service: ChannelService,
    channel_id: str,
) -> str | None:
    for config in channel_service.list_channels():
        if config.id != channel_id:
            continue
        if len(config.allowed_chat_ids) != 1:
            return None
        return str(config.allowed_chat_ids[0])
    return None


def _required_non_empty_string(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()
