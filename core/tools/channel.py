"""Built-in channel_send tool for outbound channel messaging and files."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit
from urllib.request import url2pathname

from core.attachments.attachments import _sniff_mime
from core.channels import (
    ALLOWED_CHANNEL_PLATFORMS,
    ChannelConfig,
    ChannelConfigError,
    ChannelError,
    ChannelNotFoundError,
)
from core.channels.adapter import FileData, RouteFacts
from core.extensions import InteractionButton
from core.sessions import SessionAddress
from core.tools._call_vocabulary import spelling
from core.tools._channel_send_arguments import (
    ACTION_FIELD,
    CHANNEL_FIELD,
    REFUSAL_PREFIX,
    TARGET_FIELD,
    UNADVERTISED_PARAMETERS,
    choice,
    normalize_channel_send_arguments,
    refusal,
    render_call,
)
from core.tools._path_suggestions import similar_entries
from core.tools.arguments import optional_string, required_string
from core.tools.contracts import ToolContract, compile_tool_contract
from core.tools.tools import (
    JsonObject,
    ToolContext,
    ToolDefinitionProfile,
    ToolDefinitionProfileContext,
    ToolDisplay,
    ToolDisplayPart,
    ToolRegistry,
    run_tool_worker,
    tool_failure,
    tool_success,
)
from core.utils.logging import get_logger

if TYPE_CHECKING:
    from core.channels import ChannelService
    from core.sessions import ChatSessionManager

_LOGGER = get_logger("tools.channel")

CHANNEL_SEND_TOOL_NAME = "channel_send"
CHANNEL_SEND_TOOL_DESCRIPTION = (
    "Send a message or files to a chat through your messaging Channels. Your final reply "
    "already reaches the chat you are answering; use this Tool for files, including with a "
    "reply, and to write to a chat on your own."
)
_INTERACTION_BUTTON_ARGUMENTS = frozenset(("label", "data"))
_LISTED_CHATS = 10
_WEB_ADDRESS = re.compile(r"^(?:[a-z][a-z0-9+.-]*://|data:|/api/)", re.IGNORECASE)


class ChannelSendRefusedError(ValueError):
    """A ``channel_send`` call was refused before anything was sent; the message names the fix."""


@dataclass(frozen=True)
class _PreparedChannelSend:
    channel_id: str
    channel_config: ChannelConfig
    message: str | None
    files: list[FileData]
    buttons: list[list[InteractionButton]] | None
    requested_platform_target: str | None
    requested_thread_id: str | None
    call: JsonObject


CHANNEL_SEND_TOOL_PARAMETERS: JsonObject = {
    "type": "object",
    "properties": {
        "channel_id": {
            "type": "string",
            "minLength": 1,
            "description": "Channel to send through; optional if you have only one.",
        },
        "message": {
            "type": "string",
            "minLength": 1,
            "description": "Text to send, alone or with the files.",
        },
        "platform_target": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Chat id on the platform. Leave out for this conversation's chat on that "
                "Channel, if any, else the Channel's only allowed chat."
            ),
        },
        "thread_id": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Thread or topic id in that chat. Leave out for this conversation's thread, if any."
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
                "Local files to send, such as images or documents. Relative paths start in the "
                "working directory."
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
                        "label": {
                            "type": "string",
                            "minLength": 1,
                            "description": "Text shown on the button.",
                        },
                        "data": {
                            "type": "string",
                            "minLength": 1,
                            "description": (
                                "Callback payload as '<prefix>:<payload>'; max 64 UTF-8 bytes."
                            ),
                        },
                    },
                    "required": ["label", "data"],
                },
            },
            "description": (
                "Telegram inline-keyboard rows. Use 'run:<payload>' to wake this Agent; other "
                "prefixes require a registered interaction handler. Cannot be combined with "
                "file_paths."
            ),
        },
    },
}

# The fields each platform's adapter delivers; a profile shows the union of its Channels'.
_CHANNEL_PROFILE_FIELDS: dict[str, tuple[str, ...]] = {
    "telegram": (
        "channel_id",
        "message",
        "platform_target",
        "thread_id",
        "file_paths",
        "buttons",
    ),
    "discord": ("channel_id", "message", "platform_target", "file_paths"),
    "slack": ("channel_id", "message", "platform_target", "thread_id", "file_paths"),
    "mattermost": ("channel_id", "message", "platform_target", "thread_id", "file_paths"),
    "whatsapp": ("channel_id", "message", "platform_target", "file_paths"),
}
_PLATFORM_NAMES = {
    "telegram": "Telegram",
    "discord": "Discord",
    "slack": "Slack",
    "mattermost": "Mattermost",
    "whatsapp": "WhatsApp",
}


@cache
def _repair_contract() -> ToolContract:
    return compile_tool_contract(
        name=CHANNEL_SEND_TOOL_NAME,
        input_schema={
            **CHANNEL_SEND_TOOL_PARAMETERS,
            "properties": {
                **CHANNEL_SEND_TOOL_PARAMETERS["properties"],
                **UNADVERTISED_PARAMETERS,
            },
        },
        require_closed_input=False,
    )


def _normalize_channel_send_arguments(arguments: Any) -> Any:
    return normalize_channel_send_arguments(_repair_contract(), arguments)


def _platform_name(platform: str) -> str:
    return _PLATFORM_NAMES.get(platform, platform.title())


def _channel_send_profile_parameters(configs: list[ChannelConfig]) -> JsonObject:
    canonical_properties = CHANNEL_SEND_TOOL_PARAMETERS["properties"]
    if not isinstance(canonical_properties, dict):
        raise ValueError("channel_send canonical properties must be an object")
    visible_fields = {
        field_name
        for config in configs
        for field_name in _CHANNEL_PROFILE_FIELDS.get(config.platform, ())
    }
    properties = {
        name: copy.deepcopy(schema)
        for name, schema in canonical_properties.items()
        if name in visible_fields
    }
    properties["channel_id"]["enum"] = sorted(config.id for config in configs)
    return {"type": "object", "properties": properties}


def _channel_send_profile_description(configs: list[ChannelConfig]) -> str:
    available = ", ".join(
        f"{config.id} ({_platform_name(config.platform)})"
        for config in sorted(configs, key=lambda item: (item.platform, item.id))
    )
    return f"{CHANNEL_SEND_TOOL_DESCRIPTION} Channels: {available}."


def _channel_send_definition_profile(configs: list[ChannelConfig]) -> ToolDefinitionProfile:
    signature = json.dumps(
        [(config.platform, config.id) for config in configs],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return ToolDefinitionProfile(
        key=f"channels:{hashlib.sha256(signature).hexdigest()}",
        description=_channel_send_profile_description(configs),
        parameters=_channel_send_profile_parameters(configs),
    )


def _owned_channels(channel_service: ChannelService, agent_id: str) -> list[ChannelConfig]:
    return sorted(
        (
            config
            for config in channel_service.list_channels()
            if config.enabled and config.agent_id == agent_id
        ),
        key=lambda config: (config.platform, config.id),
    )


def _channel_send_profile_resolver(channel_service: ChannelService):
    def resolve(
        context: ToolDefinitionProfileContext,
    ) -> ToolDefinitionProfile | None:
        configs = _owned_channels(channel_service, context.agent_id)
        if not configs:
            return None
        return _channel_send_definition_profile(configs)

    return resolve


def _channel_send_display_parts(raw_arguments: JsonObject) -> tuple[ToolDisplayPart, ...]:
    # Persisted calls keep the Model's own spelling; label what the call meant.
    try:
        arguments = _normalize_channel_send_arguments(raw_arguments)
    except ValueError:
        arguments = raw_arguments
    if not isinstance(arguments, dict):
        return ()
    parts: list[ToolDisplayPart] = []
    channel_id = arguments.get("channel_id") or arguments.get(CHANNEL_FIELD)
    if isinstance(channel_id, str) and channel_id.strip():
        parts.append(ToolDisplayPart(channel_id.strip(), kind="identifier", truncate="middle"))
    message = arguments.get("message")
    if isinstance(message, str) and message.strip():
        parts.append(ToolDisplayPart(message.strip()))
    return tuple(parts)


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
        open_input_schema=True,
        unadvertised_parameters=UNADVERTISED_PARAMETERS,
        argument_normalizer=_normalize_channel_send_arguments,
        result_schema={"type": "object"},
        display=ToolDisplay(parts_builder=_channel_send_display_parts),
        definition_profile_resolver=_channel_send_profile_resolver(channel_service),
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
        if arguments.get(ACTION_FIELD) == "list":
            return await _list_targets(channel_service, chat_sessions, context, arguments)
        prepared = await run_tool_worker(
            _prepare_channel_send,
            channel_service,
            context=context,
            arguments=arguments,
            max_size_bytes=max_attachment_size_bytes,
        )
        platform_target, thread_id = await _resolve_send_target(chat_sessions, context, prepared)
        send_options: dict[str, Any] = {
            "files": prepared.files or None,
            "thread_id": thread_id,
            "buttons": prepared.buttons,
        }
        # Channels route Identity Sessions. A Project Session keeps the legacy raw
        # callback instead of pretending its same-named Session lives in identity storage.
        if _contains_run_button(prepared.buttons) and context.project_id is None:
            send_options["run_origin"] = RouteFacts(
                agent_id=context.agent_id,
                session_id=context.session_id,
            )
        await channel_service.send(
            prepared.channel_id,
            prepared.message,
            platform_target,
            **send_options,
        )
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
        prepared.channel_id,
        platform_target,
        sender_agent_id=context.agent_id,
        message=prepared.message,
        files=prepared.files,
    )
    result: JsonObject = {
        "channel_id": prepared.channel_id,
        "platform_target": platform_target,
    }
    if thread_id is not None:
        result["thread_id"] = thread_id
    return tool_success(result)


def _prepare_channel_send(
    channel_service: ChannelService,
    *,
    context: ToolContext,
    arguments: JsonObject,
    max_size_bytes: int,
) -> _PreparedChannelSend:
    channel_config, target_chat, target_thread = _resolve_channel(
        channel_service, context.agent_id, arguments
    )
    requested_platform_target = _one_value(
        arguments, "platform_target", target_chat, channel_config
    )
    requested_thread_id = _one_value(arguments, "thread_id", target_thread, channel_config)
    call: JsonObject = {
        name: arguments[name] for name in ("message", "file_paths", "buttons") if name in arguments
    }
    call["channel_id"] = channel_config.id
    if requested_platform_target is not None:
        call["platform_target"] = requested_platform_target
    if requested_thread_id is not None:
        call["thread_id"] = requested_thread_id
    _validate_platform_arguments(call, channel_config)

    message = optional_string(arguments.get("message"), field_name="message")
    files = _build_file_data(
        arguments.get("file_paths"),
        context=context,
        max_size_bytes=max_size_bytes,
    )
    buttons = _build_buttons(arguments.get("buttons"))
    if message is None and not files:
        raise ChannelSendRefusedError(
            refusal(
                'it needs "message", "file_paths", or both.',
                call,
                message="<text to send>",
            )
        )
    if buttons is not None and files:
        without_buttons = render_call(call, buttons=None)
        buttons_only = render_call(call, file_paths=None, message=call.get("message", "<text>"))
        raise ChannelSendRefusedError(
            f"{REFUSAL_PREFIX}buttons cannot go with files in one message. Send the files "
            f"first, then the buttons, in two calls: {without_buttons} then {buttons_only}"
        )
    return _PreparedChannelSend(
        channel_id=channel_config.id,
        channel_config=channel_config,
        message=message,
        files=files,
        buttons=buttons,
        requested_platform_target=requested_platform_target,
        requested_thread_id=requested_thread_id,
        call=call,
    )


def _resolve_channel(
    channel_service: ChannelService, agent_id: str, arguments: JsonObject
) -> tuple[ChannelConfig, str | None, str | None]:
    """Return the Channel a call names, with the chat and thread a ``target`` carries."""
    configs = channel_service.list_channels()
    owned = sorted(
        (config for config in configs if config.enabled and config.agent_id == agent_id),
        key=lambda config: (config.platform, config.id),
    )
    chat: str | None = None
    thread: str | None = None
    references: list[str] = []
    target = arguments.get(TARGET_FIELD)
    if isinstance(target, str):
        prefix, chat, thread = _split_target(target, owned, arguments)
        if prefix is not None:
            references.append(prefix)
    reference = arguments.get(CHANNEL_FIELD)
    if isinstance(reference, str):
        references.append(reference)
    resolved: list[ChannelConfig] = []
    explicit = arguments.get("channel_id")
    if isinstance(explicit, str):
        resolved.append(_channel_config_for_agent(configs, explicit, agent_id))
    resolved.extend(_channel_by_reference(owned, item, arguments) for item in references)
    distinct = list({config.id: config for config in resolved}.values())
    if len(distinct) > 1:
        raise ChannelSendRefusedError(
            choice(
                "the call names different Channels:",
                [_call_for(arguments, config, chat, thread) for config in distinct],
            )
        )
    if distinct:
        return distinct[0], chat, thread
    if len(owned) == 1:
        return owned[0], chat, thread
    if not owned:
        raise ChannelNotFoundError(f"Agent {agent_id} has no enabled Channel to send through.")
    raise ChannelSendRefusedError(
        choice(
            'it needs "channel_id"; you have several Channels:',
            [_call_for(arguments, config, chat, thread) for config in owned],
        )
    )


def _split_target(
    target: str, owned: list[ChannelConfig], arguments: JsonObject
) -> tuple[str | None, str | None, str | None]:
    """Read ``platform``, ``channel:chat`` or ``platform:chat:thread``, or a plain chat id."""
    parts = [part.strip() for part in target.split(":")]
    head = parts[0]
    names_channel = head in {config.id for config in owned} or (
        spelling(head) in ALLOWED_CHANNEL_PLATFORMS
    )
    if len(parts) == 1:
        return (head, None, None) if names_channel else (None, head, None)
    if not names_channel or len(parts) > 3 or not parts[1]:
        ids = ", ".join(config.id for config in owned)
        raise ChannelSendRefusedError(
            refusal(
                f'"target" "{target}" is not "channel:chat" with one of your Channels ({ids}). '
                "Name the Channel and the chat separately:",
                _canonical(arguments),
                channel_id=owned[0].id if len(owned) == 1 else "<channel id>",
                platform_target="<chat id>",
            )
        )
    return head, parts[1], parts[2] if len(parts) == 3 and parts[2] else None


def _channel_by_reference(
    owned: list[ChannelConfig], reference: str, arguments: JsonObject
) -> ChannelConfig:
    """An exact Channel id of the Agent, or its only Channel on a named platform."""
    for config in owned:
        if config.id == reference:
            return config
    platform = spelling(reference)
    matches = [config for config in owned if config.platform == platform]
    if len(matches) == 1:
        return matches[0]
    listing = ", ".join(f"{config.id} ({_platform_name(config.platform)})" for config in owned)
    if matches:
        raise ChannelSendRefusedError(
            choice(
                f"you have several {_platform_name(platform)} Channels:",
                [_call_for(arguments, config, None, None) for config in matches],
            )
        )
    what = (
        f"no {_platform_name(platform)} Channel"
        if platform in ALLOWED_CHANNEL_PLATFORMS
        else f'no Channel "{reference}"'
    )
    raise ChannelSendRefusedError(
        choice(
            f"you have {what}; your Channels are {listing}:",
            [_call_for(arguments, config, None, None) for config in owned],
        )
    )


def _one_value(
    arguments: JsonObject, name: str, from_target: str | None, config: ChannelConfig
) -> str | None:
    """The explicit field and the part of ``target`` must agree."""
    explicit = optional_string(arguments.get(name), field_name=name)
    if explicit is not None and from_target is not None and explicit != from_target:
        raise ChannelSendRefusedError(
            choice(
                f'"{name}" "{explicit}" and "target" name different values:',
                [
                    render_call(_canonical(arguments), channel_id=config.id, **{name: value})
                    for value in (explicit, from_target)
                ],
            )
        )
    return explicit if explicit is not None else from_target


def _canonical(arguments: JsonObject) -> JsonObject:
    return {
        key: value
        for key, value in arguments.items()
        if key not in (ACTION_FIELD, CHANNEL_FIELD, TARGET_FIELD)
    }


def _call_for(
    arguments: JsonObject, config: ChannelConfig, chat: str | None, thread: str | None
) -> str:
    call = _canonical(arguments)
    call["channel_id"] = config.id
    if chat is not None and "platform_target" not in call:
        call["platform_target"] = chat
    if thread is not None and "thread_id" not in call:
        call["thread_id"] = thread
    return render_call(call)


def _validate_platform_arguments(call: JsonObject, channel_config: ChannelConfig) -> None:
    allowed_arguments = frozenset(_CHANNEL_PROFILE_FIELDS.get(channel_config.platform, ()))
    unsupported = sorted(name for name in call if name not in allowed_arguments)
    if unsupported:
        names = " and ".join(unsupported)
        verb = "does" if len(unsupported) == 1 else "do"
        raise ChannelSendRefusedError(
            refusal(
                f"{names} {verb} not work on the {_platform_name(channel_config.platform)} "
                f"Channel {channel_config.id}. The call below sends without "
                f"{'it' if len(unsupported) == 1 else 'them'}; send it only if that is meant.",
                call,
                **dict.fromkeys(unsupported),
            )
        )


def _contains_run_button(buttons: list[list[InteractionButton]] | None) -> bool:
    return bool(
        buttons and any(button.data.split(":", 1)[0] == "run" for row in buttons for button in row)
    )


async def _list_targets(
    channel_service: ChannelService,
    chat_sessions: ChatSessionManager,
    context: ToolContext,
    arguments: JsonObject,
) -> JsonObject:
    """Show the Agent's Channels, this conversation's chat and the allowed chats."""
    sends = sorted(name for name in ("message", "file_paths", "buttons") if name in arguments)
    if sends:
        raise ChannelSendRefusedError(
            f"{REFUSAL_PREFIX}action list only shows where messages can go, but the call also "
            f"has {', '.join(sends)}. To send, leave action out: "
            f"{render_call(_canonical(arguments))}"
        )
    owned = _owned_channels(channel_service, context.agent_id)
    metadata = await chat_sessions.get_metadata_async(
        SessionAddress(project_id=None, agent_id=context.agent_id, session_id=context.session_id)
    )
    blocks: list[str] = []
    for config in owned:
        lines = [f"{config.id} ({_platform_name(config.platform)})"]
        current = _send_target_from_session_metadata(metadata, config.id)
        if current is not None:
            chat, thread = current
            lines.append(
                f"  this conversation's chat: {chat}" + (f", thread {thread}" if thread else "")
            )
        lines.append("  " + _allowed_text(config))
        blocks.append("\n".join(lines))
    data: JsonObject = {"channels": len(owned)}
    if blocks:
        data["content"] = "\n\n".join(blocks)
    return tool_success(data)


def _allowed_text(config: ChannelConfig) -> str:
    chats = [str(item) for item in config.allowed_chat_ids]
    if not chats:
        return "allowed chats: none yet"
    shown = ", ".join(chats[:_LISTED_CHATS])
    more = f", and {len(chats) - _LISTED_CHATS} more" if len(chats) > _LISTED_CHATS else ""
    return f"allowed chats: {shown}{more}"


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
        route = await channel_service.ensure_outbound_session(channel_id, platform_target)
        # Serialize the outbound-context note against an open tool cycle on the
        # target session. The lock is task-reentrant, so this is safe even when
        # the sending Run targets its own session.
        target = SessionAddress(
            project_id=None, agent_id=route.agent_id, session_id=route.session_id
        )
        note = _outbound_message_note(sender_agent_id, message, files)
        async with chat_sessions.write_lock(target):
            await chat_sessions.run_async(_append_session_note, chat_sessions, target, note)
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


def _append_session_note(
    chat_sessions: ChatSessionManager, target: SessionAddress, note: str
) -> None:
    chat_sessions.get_or_create(target).add_note(note)


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


async def _resolve_send_target(
    chat_sessions: ChatSessionManager,
    context: ToolContext,
    prepared: _PreparedChannelSend,
) -> tuple[str, str | None]:
    """Resolve the (platform_target, thread_id) pair for one send.

    An explicit ``thread_id`` argument always wins. The metadata thread is adopted
    only together with the metadata target: an explicitly targeted send must not
    inherit another conversation's topic. The calling Session's metadata is read,
    on the Session database's pool, only when no target was requested.
    """
    requested_thread_id = prepared.requested_thread_id
    if prepared.requested_platform_target is not None:
        return prepared.requested_platform_target, requested_thread_id

    address = SessionAddress(
        project_id=None, agent_id=context.agent_id, session_id=context.session_id
    )
    metadata = await chat_sessions.get_metadata_async(address)
    metadata_target = _send_target_from_session_metadata(metadata, prepared.channel_id)
    if metadata_target is not None:
        metadata_platform_target, metadata_thread_id = metadata_target
        return metadata_platform_target, (
            requested_thread_id if requested_thread_id is not None else metadata_thread_id
        )

    config_platform_target = _platform_target_from_channel_config(prepared.channel_config)
    if config_platform_target is not None:
        return config_platform_target, requested_thread_id

    raise ChannelSendRefusedError(_missing_target(prepared))


def _missing_target(prepared: _PreparedChannelSend) -> str:
    """Name the chats a send without a target could mean, one call each."""
    config = prepared.channel_config
    chats = [str(item) for item in config.allowed_chat_ids]
    why = (
        f"this conversation is not with a chat on {config.id}, and the Channel allows no "
        "chats yet, so there is no chat to send to by default. Give the chat's id on "
        f"{_platform_name(config.platform)}:"
        if not chats
        else f"this conversation is not with a chat on {config.id}, and the Channel allows "
        f"{len(chats)} chats, so it is not clear which one is meant. Choose one:"
    )
    if not chats:
        return refusal(why.removesuffix(":") + ".", prepared.call, platform_target="<chat id>")
    calls = [render_call(prepared.call, platform_target=chat) for chat in chats[:_LISTED_CHATS]]
    return choice(why, calls)


def _send_target_from_session_metadata(
    metadata: JsonObject,
    channel_id: str,
) -> tuple[str, str | None] | None:
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
    configs: list[ChannelConfig],
    channel_id: str,
    agent_id: str,
) -> ChannelConfig:
    for config in configs:
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
    if not value:
        raise ValueError("file_paths must contain at least one file path")

    files: list[FileData] = []
    for index, raw_path in enumerate(value):
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise ValueError(f"file_paths[{index}] must be a non-empty string")
        path_text = raw_path.strip()
        if path_text.lower().startswith("file://"):
            path_text = url2pathname(urlsplit(path_text).path)
        elif _WEB_ADDRESS.match(path_text):
            raise ChannelSendRefusedError(
                f'{REFUSAL_PREFIX}file_paths "{path_text}" is a web address, not a file on this '
                "computer. Send the file's local path, or put the link in message."
            )
        resolved_path = context.resolve_path(path_text)
        if resolved_path.is_dir():
            raise ChannelSendRefusedError(
                f'{REFUSAL_PREFIX}file_paths "{path_text}" is a folder; list the files in it '
                "one by one."
            )
        if not resolved_path.is_file():
            raise ChannelSendRefusedError(_missing_file(path_text, resolved_path))

        # Reject oversize files by their on-disk size before reading, so a large
        # file never gets loaded into memory just to be turned away.
        size_bytes = resolved_path.stat().st_size
        if size_bytes > max_size_bytes:
            raise ChannelSendRefusedError(
                f'{REFUSAL_PREFIX}file_paths "{path_text}" is {_size_text(size_bytes)}; files '
                f"sent through a Channel may be at most {_size_text(max_size_bytes)}."
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


def _missing_file(path_text: str, resolved_path: Path) -> str:
    text = f'{REFUSAL_PREFIX}file_paths "{path_text}" does not exist ({resolved_path}).'
    similar = similar_entries(resolved_path, kind="files", limit=3)
    if similar:
        base = Path(path_text).parent
        names = ", ".join(f'"{(base / entry.name).as_posix()}"' for entry in similar)
        text += f" Files with similar names there: {names}."
    return text


def _size_text(size_bytes: int) -> str:
    if size_bytes >= 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    if size_bytes >= 1024:
        return f"{size_bytes / 1024:.0f} KB"
    return f"{size_bytes} bytes"


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
    if not value:
        raise ValueError("buttons must contain at least one button row")

    rows: list[list[InteractionButton]] = []
    for row_index, row in enumerate(value):
        if not isinstance(row, list):
            raise ValueError(f"buttons[{row_index}] must be an array of buttons")
        if not row:
            raise ValueError(f"buttons[{row_index}] must contain at least one button")
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
