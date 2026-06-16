"""Built-in channel_send tool for proactive outbound channel messaging."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from core.attachments.attachments import _sniff_mime
from core.channels.adapter import FileData
from core.channels.channels import (
    ChannelConfig,
    ChannelConfigError,
    ChannelError,
    ChannelNotFoundError,
)
from core.tools.tools import (
    JsonObject,
    ToolContext,
    ToolDisplay,
    ToolRegistry,
    tool_failure,
    tool_success,
)
from core.utils.logging import get_logger

if TYPE_CHECKING:
    from core.channels.channels import ChannelService
    from core.sessions import ChatSessionManager

_LOGGER = get_logger("tools.channel")

CHANNEL_SEND_TOOL_NAME = "channel_send"
CHANNEL_SEND_TOOL_DESCRIPTION = "Send a proactive outbound message through a configured channel."
_REQUIRED_CHANNEL_SEND_ARGUMENTS = frozenset(("channel_id",))
_OPTIONAL_CHANNEL_SEND_ARGUMENTS = frozenset(("message", "platform_target", "file_paths"))
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
            "description": "Optional outbound message text.",
        },
        "platform_target": {
            "type": "string",
            "description": (
                "Platform-specific target id. If omitted, uses the session metadata "
                "last_reply_target.platform_target value."
            ),
        },
        "file_paths": {
            "type": "array",
            "items": {
                "type": "string",
            },
            "description": (
                "Optional list of file paths to send. Relative paths resolve from the "
                "agent workspace."
            ),
        },
    },
    "required": ["channel_id"],
    "additionalProperties": False,
}


def register_channel_send_tool(
    registry: ToolRegistry,
    channel_service: ChannelService,
    chat_sessions: ChatSessionManager,
    *,
    max_attachment_size_bytes: int,
) -> None:
    """Register the channel_send tool with a vBot tool registry.

    ``max_attachment_size_bytes`` caps the size of any file an agent sends
    outbound, mirroring the limit enforced on inbound attachments and uploads.
    """

    async def handler(context: ToolContext, arguments: JsonObject) -> JsonObject:
        return await _handle_channel_send_tool(
            channel_service,
            chat_sessions,
            context,
            arguments,
            max_attachment_size_bytes=max_attachment_size_bytes,
        )

    registry.register(
        CHANNEL_SEND_TOOL_NAME,
        CHANNEL_SEND_TOOL_DESCRIPTION,
        CHANNEL_SEND_TOOL_PARAMETERS,
        handler,
        display=ToolDisplay(summary_fields=("channel_id", "message")),
    )


async def _handle_channel_send_tool(
    channel_service: ChannelService,
    chat_sessions: ChatSessionManager,
    context: ToolContext,
    arguments: JsonObject,
    *,
    max_attachment_size_bytes: int,
) -> JsonObject:
    unknown_arguments = sorted(set(arguments) - _CHANNEL_SEND_ALLOWED_ARGUMENTS)
    if unknown_arguments:
        names = ", ".join(unknown_arguments)
        return tool_failure("invalid_arguments", f"Unknown argument(s): {names}")

    try:
        channel_id = _required_non_empty_string(
            arguments.get("channel_id"), field_name="channel_id"
        )
        message = _optional_non_empty_string(arguments.get("message"), field_name="message")
        files = _build_file_data(
            arguments.get("file_paths"),
            workspace=context.workspace,
            max_size_bytes=max_attachment_size_bytes,
        )
        if message is None and not files:
            return tool_failure(
                "invalid_arguments",
                "at least one of message or file_paths must be provided",
            )

        channel_config = _channel_config_for_agent(channel_service, channel_id, context.agent_id)
        platform_target = _platform_target_from_arguments_or_context(
            arguments,
            chat_sessions,
            context,
            channel_id,
            channel_config,
        )
        await channel_service.send(channel_id, message, platform_target, files=files or None)
    except ValueError as error:
        return tool_failure("invalid_arguments", str(error))
    except ChannelNotFoundError as error:
        return tool_failure("channel_not_found", str(error))
    except ChannelConfigError as error:
        return tool_failure("invalid_arguments", str(error))
    except ChannelError as error:
        return tool_failure("channel_error", str(error))

    await _record_outbound_message_note(
        channel_service,
        chat_sessions,
        channel_id,
        platform_target,
        sender_agent_id=context.agent_id,
        message=message,
        files=files,
    )
    return tool_success({"channel_id": channel_id, "platform_target": platform_target})


async def _record_outbound_message_note(
    channel_service: ChannelService,
    chat_sessions: ChatSessionManager,
    channel_id: str,
    platform_target: str,
    *,
    sender_agent_id: str,
    message: str | None,
    files: list[FileData],
) -> None:
    try:
        route = channel_service.ensure_outbound_session(channel_id, platform_target)
        # Serialize the outbound-context note against an open tool cycle on the
        # target session. The lock is task-reentrant, so this is safe even when
        # the sending Run targets its own session.
        async with chat_sessions.write_lock(route.agent_id, route.session_id):
            session = chat_sessions.get_or_create(route.agent_id, route.session_id)
            session.add_note(_outbound_message_note(sender_agent_id, message, files))
    except Exception as error:
        # The outbound message already went out; failing to record context into the target
        # Session must not turn a successful send into a tool failure.
        _LOGGER.warning(
            "Could not record channel_send outbound note (channel=%s target=%s): %s",
            channel_id,
            platform_target,
            error,
            exc_info=(type(error), error, error.__traceback__),
        )


def _outbound_message_note(
    sender_agent_id: str,
    message: str | None,
    files: list[FileData],
) -> str:
    parts = [
        f'A message was sent to this chat via the channel_send tool by agent "{sender_agent_id}".'
    ]
    if message is not None:
        parts.append(message)
    if files:
        names = ", ".join(file_data.filename for file_data in files)
        parts.append(f"Attached file(s): {names}")
    return "\n\n".join(parts)


def _platform_target_from_arguments_or_context(
    arguments: JsonObject,
    chat_sessions: ChatSessionManager,
    context: ToolContext,
    channel_id: str,
    channel_config: ChannelConfig,
) -> str:
    platform_target_value = arguments.get("platform_target")
    if platform_target_value is not None:
        return _required_non_empty_string(platform_target_value, field_name="platform_target")

    metadata_platform_target = _platform_target_from_session_metadata(
        chat_sessions,
        context,
        channel_id,
    )
    if metadata_platform_target is not None:
        return metadata_platform_target

    config_platform_target = _platform_target_from_channel_config(channel_config)
    if config_platform_target is not None:
        return config_platform_target

    raise ValueError(
        "platform_target is required when session metadata has no "
        "last_reply_target.platform_target and the channel has no unique allowed_chat_ids target"
    )


def _platform_target_from_session_metadata(
    chat_sessions: ChatSessionManager,
    context: ToolContext,
    channel_id: str,
) -> str | None:
    metadata = chat_sessions.get_metadata(context.agent_id, context.session_id)
    last_reply_target = metadata.get("last_reply_target")
    if not isinstance(last_reply_target, dict):
        return None

    if last_reply_target.get("channel_id") != channel_id:
        return None

    metadata_platform_target = last_reply_target.get("platform_target")
    if metadata_platform_target is None:
        return None

    return _required_non_empty_string(
        metadata_platform_target, field_name="last_reply_target.platform_target"
    )


def _channel_config_for_agent(
    channel_service: ChannelService,
    channel_id: str,
    agent_id: str,
) -> ChannelConfig:
    for config in channel_service.list_channels():
        if config.id != channel_id:
            continue
        if config.agent_id != agent_id:
            raise ChannelConfigError(
                f"Channel {channel_id} belongs to agent {config.agent_id}, not {agent_id}"
            )
        return config
    raise ChannelNotFoundError(f"Channel not found: {channel_id}")


def _platform_target_from_channel_config(channel_config: ChannelConfig) -> str | None:
    if len(channel_config.allowed_chat_ids) != 1:
        return None
    return str(channel_config.allowed_chat_ids[0])


def _required_non_empty_string(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


def _optional_non_empty_string(value: object, *, field_name: str) -> str | None:
    if value is None:
        return None
    return _required_non_empty_string(value, field_name=field_name)


def _build_file_data(value: object, *, workspace: Path, max_size_bytes: int) -> list[FileData]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("file_paths must be an array of strings")

    files: list[FileData] = []
    for index, raw_path in enumerate(value):
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise ValueError(f"file_paths[{index}] must be a non-empty string")

        resolved_path = _resolve_path(raw_path.strip(), workspace=workspace)
        if not resolved_path.is_file():
            raise ValueError(f"file_paths[{index}] is not a file: {raw_path}")

        # Reject oversize files by their on-disk size before reading, so a large
        # file never gets loaded into memory just to be turned away.
        size_bytes = resolved_path.stat().st_size
        if size_bytes > max_size_bytes:
            raise ValueError(
                f"file_paths[{index}] size {size_bytes} exceeds limit {max_size_bytes}: {raw_path}"
            )

        try:
            data = resolved_path.read_bytes()
        except OSError as error:
            raise ValueError(f"cannot read file_paths[{index}] {raw_path}: {error}") from error

        files.append(
            FileData(
                filename=resolved_path.name,
                media_type=_sniff_mime(data, resolved_path.name),
                data=data,
            )
        )

    return files


def _resolve_path(path: str, *, workspace: Path) -> Path:
    resolved = Path(path).expanduser()
    if not resolved.is_absolute():
        resolved = workspace / resolved
    return resolved.resolve()
