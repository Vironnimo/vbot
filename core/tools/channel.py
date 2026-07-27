"""Built-in channel_send tool for outbound channel messaging and files."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from core.attachments.attachments import _sniff_mime
from core.channels.adapter import FileData, RouteFacts
from core.channels.channels import (
    ChannelConfig,
    ChannelConfigError,
    ChannelError,
    ChannelNotFoundError,
)
from core.extensions import InteractionButton
from core.tools.arguments import optional_string, required_string
from core.tools.tools import (
    JsonObject,
    ToolContext,
    ToolDisplay,
    ToolRegistry,
    extract_tool_operation,
    operation_envelope_schema,
    tool_failure,
    tool_success,
)
from core.utils.logging import get_logger

if TYPE_CHECKING:
    from core.channels.channels import ChannelService
    from core.sessions import ChatSessionManager

_LOGGER = get_logger("tools.channel")

CHANNEL_SEND_TOOL_NAME = "channel_send"
CHANNEL_SEND_TOOL_DESCRIPTION = (
    "Send a proactive message or any file through a configured channel. Always use "
    "this tool for channel file delivery, including replies. Put the complete request "
    "inside request with operation set to send."
)
_REQUIRED_CHANNEL_SEND_ARGUMENTS = frozenset(("channel_id",))
_OPTIONAL_CHANNEL_SEND_ARGUMENTS = frozenset(
    ("message", "platform_target", "thread_id", "file_paths", "buttons")
)
_CHANNEL_SEND_ALLOWED_ARGUMENTS = (
    _REQUIRED_CHANNEL_SEND_ARGUMENTS | _OPTIONAL_CHANNEL_SEND_ARGUMENTS
)
_INTERACTION_BUTTON_ARGUMENTS = frozenset(("label", "data"))

CHANNEL_SEND_TOOL_PARAMETERS: JsonObject = {
    "type": "object",
    "description": "Send one message, one or more files, or both.",
    "properties": {
        "channel_id": {
            "type": "string",
            "minLength": 1,
            "description": "Configured channel id to send through.",
        },
        "message": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Outbound message text. At least one of message or file_paths is required."
            ),
        },
        "platform_target": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Platform-specific target id (e.g. a chat or channel id). Omit to "
                "send to wherever this session last replied."
            ),
        },
        "thread_id": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Optional thread/topic inside the target chat (e.g. a Telegram forum "
                "topic id). When the target comes from session metadata, the last "
                "reply's topic is used automatically; ignored on platforms whose "
                "threads are their own targets (Discord)."
            ),
        },
        "file_paths": {
            "type": "array",
            "items": {
                "type": "string",
                "minLength": 1,
            },
            "minItems": 1,
            "description": (
                "File paths to deliver through the channel. Use for every channel file "
                "delivery, including replies. Relative paths resolve from the working "
                "directory."
            ),
        },
        "buttons": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "properties": {
                        "label": {"type": "string", "minLength": 1},
                        "data": {"type": "string", "minLength": 1},
                    },
                    "required": ["label", "data"],
                    "additionalProperties": False,
                },
            },
            "description": (
                "Optional inline-keyboard rows; each button is {label, data}. data is the "
                "callback payload, format '<prefix>:<payload>', max 64 bytes; an extension "
                "registered for '<prefix>' handles taps. The reserved prefix 'run' wakes you "
                "instead: a tap on a 'run:<payload>' button starts a run carrying the tap "
                "context — the tapped button and the message's current button state — so you "
                "can act on it and reply. When sent from an Identity Agent Session, the tap "
                "and later Telegram messages continue in this Session until the user sends "
                "'/new'. Telegram only; cannot be combined with file_paths."
            ),
        },
    },
    "required": ["channel_id"],
    "anyOf": [{"required": ["message"]}, {"required": ["file_paths"]}],
    "not": {"required": ["buttons", "file_paths"]},
    "additionalProperties": False,
}
CHANNEL_SEND_TOOL_PARAMETERS = operation_envelope_schema(
    {"send": CHANNEL_SEND_TOOL_PARAMETERS},
    description="Set request.operation to send and provide the complete request fields.",
)


def _normalize_channel_send_call(arguments: JsonObject) -> JsonObject:
    _, operation_arguments = extract_tool_operation(arguments, ("send",))
    return operation_arguments


def _channel_send_display_summary(arguments: JsonObject) -> str:
    try:
        operation_arguments = _normalize_channel_send_call(arguments)
    except ValueError:
        return ""
    parts: list[str] = []
    for field_name in ("channel_id", "message"):
        value = operation_arguments.get(field_name)
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
    return " · ".join(parts)


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
        result_schema={"type": "object", "required": ["channel_id", "platform_target"]},
        display=ToolDisplay(summary_builder=_channel_send_display_summary),
    )


async def _handle_channel_send_tool(
    channel_service: ChannelService,
    chat_sessions: ChatSessionManager,
    context: ToolContext,
    arguments: JsonObject,
    *,
    max_attachment_size_bytes: int,
) -> JsonObject:
    try:
        arguments = _normalize_channel_send_call(arguments)
    except ValueError as error:
        return tool_failure("invalid_arguments", str(error))
    unknown_arguments = sorted(set(arguments) - _CHANNEL_SEND_ALLOWED_ARGUMENTS)
    if unknown_arguments:
        names = ", ".join(unknown_arguments)
        return tool_failure("invalid_arguments", f"Unknown argument(s): {names}")

    try:
        channel_id = required_string(arguments.get("channel_id"), field_name="channel_id")
        message = optional_string(arguments.get("message"), field_name="message")
        files = _build_file_data(
            arguments.get("file_paths"),
            context=context,
            max_size_bytes=max_attachment_size_bytes,
        )
        buttons = _build_buttons(arguments.get("buttons"))
        if message is None and not files:
            return tool_failure(
                "invalid_arguments",
                "at least one of message or file_paths must be provided",
            )
        if buttons is not None and files:
            return tool_failure(
                "invalid_arguments",
                "buttons cannot be combined with file_paths",
            )

        channel_config = _channel_config_for_agent(channel_service, channel_id, context.agent_id)
        platform_target, thread_id = _send_target_from_arguments_or_context(
            arguments,
            chat_sessions,
            context,
            channel_id,
            channel_config,
        )
        send_options: dict[str, Any] = {
            "files": files or None,
            "thread_id": thread_id,
            "buttons": buttons,
        }
        # Channels route Identity Sessions. A Project Session keeps the legacy raw
        # callback instead of pretending its same-named Session lives in identity storage.
        if _contains_run_button(buttons) and context.project_id is None:
            send_options["run_origin"] = RouteFacts(
                agent_id=context.agent_id,
                session_id=context.session_id,
            )
        await channel_service.send(channel_id, message, platform_target, **send_options)
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
    result: JsonObject = {"channel_id": channel_id, "platform_target": platform_target}
    if thread_id is not None:
        result["thread_id"] = thread_id
    return tool_success(result)


def _contains_run_button(buttons: list[list[InteractionButton]] | None) -> bool:
    return bool(
        buttons and any(button.data.split(":", 1)[0] == "run" for row in buttons for button in row)
    )


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


def _send_target_from_arguments_or_context(
    arguments: JsonObject,
    chat_sessions: ChatSessionManager,
    context: ToolContext,
    channel_id: str,
    channel_config: ChannelConfig,
) -> tuple[str, str | None]:
    """Resolve the (platform_target, thread_id) pair for one send.

    An explicit ``thread_id`` argument always wins. The metadata thread is adopted
    only together with the metadata target — an explicitly targeted send must not
    inherit another conversation's topic.
    """
    explicit_thread_id = optional_string(arguments.get("thread_id"), field_name="thread_id")
    platform_target_value = optional_string(
        arguments.get("platform_target"), field_name="platform_target"
    )
    if platform_target_value is not None:
        return platform_target_value, explicit_thread_id

    metadata_target = _send_target_from_session_metadata(chat_sessions, context, channel_id)
    if metadata_target is not None:
        metadata_platform_target, metadata_thread_id = metadata_target
        return metadata_platform_target, (
            explicit_thread_id if explicit_thread_id is not None else metadata_thread_id
        )

    config_platform_target = _platform_target_from_channel_config(channel_config)
    if config_platform_target is not None:
        return config_platform_target, explicit_thread_id

    raise ValueError(
        "platform_target is required when session metadata has no "
        "last_reply_target.platform_target and the channel has no unique allowed_chat_ids target"
    )


def _send_target_from_session_metadata(
    chat_sessions: ChatSessionManager,
    context: ToolContext,
    channel_id: str,
) -> tuple[str, str | None] | None:
    metadata = chat_sessions.get_metadata(context.agent_id, context.session_id)
    last_reply_target = metadata.get("last_reply_target")
    if not isinstance(last_reply_target, dict):
        return None

    if last_reply_target.get("channel_id") != channel_id:
        return None

    metadata_platform_target = last_reply_target.get("platform_target")
    if metadata_platform_target is None:
        return None

    platform_target = required_string(
        metadata_platform_target, field_name="last_reply_target.platform_target"
    )
    thread_id_value = last_reply_target.get("thread_id")
    thread_id = optional_string(thread_id_value, field_name="last_reply_target.thread_id")
    return platform_target, thread_id


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


def _build_file_data(
    value: object,
    *,
    context: ToolContext,
    max_size_bytes: int,
) -> list[FileData]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("file_paths must be an array of strings")

    files: list[FileData] = []
    for index, raw_path in enumerate(value):
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise ValueError(f"file_paths[{index}] must be a non-empty string")

        resolved_path = context.resolve_path(raw_path.strip())
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


def _build_buttons(value: object) -> list[list[InteractionButton]] | None:
    """Parse the tool's ``buttons`` payload into neutral inline-keyboard rows.

    Returns ``None`` when omitted. Raises ``ValueError`` (mapped to a clean
    ``invalid_arguments`` tool failure) on a malformed structure; the callback
    data's byte-length and platform support are enforced downstream by the
    channel service and adapter.
    """
    if value is None:
        return None
    if not isinstance(value, list):
        raise ValueError("buttons must be an array of button rows")

    rows: list[list[InteractionButton]] = []
    for row_index, row in enumerate(value):
        if not isinstance(row, list):
            raise ValueError(f"buttons[{row_index}] must be an array of buttons")
        buttons: list[InteractionButton] = []
        for button_index, button in enumerate(row):
            if not isinstance(button, dict):
                raise ValueError(f"buttons[{row_index}][{button_index}] must be an object")
            unknown_fields = sorted(set(button) - _INTERACTION_BUTTON_ARGUMENTS)
            if unknown_fields:
                names = ", ".join(unknown_fields)
                raise ValueError(
                    f"buttons[{row_index}][{button_index}] has unknown field(s): {names}"
                )
            label = button.get("label")
            data = button.get("data")
            if not isinstance(label, str) or not label:
                raise ValueError(
                    f"buttons[{row_index}][{button_index}].label must be a non-empty string"
                )
            if not isinstance(data, str) or not data:
                raise ValueError(
                    f"buttons[{row_index}][{button_index}].data must be a non-empty string"
                )
            buttons.append(InteractionButton(label=label, data=data))
        rows.append(buttons)
    return rows or None
