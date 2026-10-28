"""Channel selection and explicit or default destination resolution."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from core.channels import (
    ALLOWED_CHANNEL_PLATFORMS,
    ChannelConfig,
    ChannelNotFoundError,
    ChecklistItem,
)
from core.channels.adapter import FileData
from core.extensions import InteractionButton
from core.sessions import SessionAddress
from core.tools._channel_send_arguments import (
    ACTION_FIELD,
    CHANNEL_FIELD,
    REFUSAL_PREFIX,
    TARGET_FIELD,
    choice,
    refusal,
    render_call,
)
from core.tools.arguments import optional_string, required_string
from core.tools.call_syntax import spelling
from core.tools.contracts import ToolContractError
from core.tools.tools import (
    JsonObject,
    ToolContext,
    tool_success,
)

if TYPE_CHECKING:
    from core.channels import ChannelService
    from core.sessions import ChatSessionManager


_LISTED_CHATS = 10
_SEND_FIELDS = ("message", "file_paths", "buttons", "checklist", "done_label")


class ChannelSendRefusedError(ToolContractError):
    """A ``channel_send`` call was refused before anything was sent; the message names the fix."""


@dataclass(frozen=True)
class _PreparedChannelSend:
    channel_id: str
    channel_config: ChannelConfig
    message: str | None
    files: list[FileData]
    buttons: list[list[InteractionButton]] | None
    checklist: list[ChecklistItem] | None
    requested_platform_target: str | None
    requested_thread_id: str | None
    call: JsonObject


_PLATFORM_NAMES = {
    "telegram": "Telegram",
    "discord": "Discord",
    "slack": "Slack",
    "mattermost": "Mattermost",
    "whatsapp": "WhatsApp",
    "email": "Email",
}


def _platform_name(platform: str) -> str:
    return _PLATFORM_NAMES.get(platform, platform.title())


def _owned_channels(channel_service: ChannelService, agent_id: str) -> list[ChannelConfig]:
    return sorted(
        (
            config
            for config in channel_service.list_channels()
            if config.enabled and config.agent_id == agent_id
        ),
        key=lambda config: (config.platform, config.id),
    )


def _resolve_channel(
    channel_service: ChannelService, agent_id: str, arguments: JsonObject
) -> tuple[ChannelConfig, str | None, str | None]:
    """Return the Channel a call names, with the chat and thread a ``target`` carries."""
    owned = _owned_channels(channel_service, agent_id)
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
        resolved.append(_owned_channel(owned, explicit, arguments, agent_id))
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
                "Name the Channel and the chat separately.",
                _canonical(arguments),
                channel_id=owned[0].id if len(owned) == 1 else "<channel id>",
                platform_target="<chat id>",
            )
        )
    return head, parts[1], parts[2] if len(parts) == 3 and parts[2] else None


def _owned_channel(
    owned: list[ChannelConfig], channel_id: str, arguments: JsonObject, agent_id: str
) -> ChannelConfig:
    """The Agent's enabled Channel with exactly this id; ids are never matched loosely."""
    for config in owned:
        if config.id == channel_id:
            return config
    if not owned:
        raise ChannelNotFoundError(f"Agent {agent_id} has no enabled Channel to send through.")
    raise ChannelSendRefusedError(
        choice(
            f'you have no Channel "{channel_id}"; your Channels are {_channel_listing(owned)}:',
            [_call_for(arguments, config, None, None) for config in owned],
        )
    )


def _channel_listing(owned: list[ChannelConfig]) -> str:
    return ", ".join(f"{config.id} ({_platform_name(config.platform)})" for config in owned)


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
            f"you have {what}; your Channels are {_channel_listing(owned)}:",
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


async def _list_targets(
    channel_service: ChannelService,
    chat_sessions: ChatSessionManager,
    context: ToolContext,
    arguments: JsonObject,
) -> JsonObject:
    """Show the Agent's Channels, this conversation's chat and the allowed chats."""
    sends = sorted(name for name in _SEND_FIELDS if name in arguments)
    if sends:
        raise ChannelSendRefusedError(
            f"{REFUSAL_PREFIX}action list only shows where messages can go, but the call also "
            f"has {', '.join(sends)}. To send, leave action out: "
            f"{render_call(_canonical(arguments))}"
        )
    owned = _owned_channels(channel_service, context.agent_id)
    metadata = await chat_sessions.get_metadata_async(
        SessionAddress(
            project_id=context.project_id,
            agent_id=context.agent_id,
            session_id=context.session_id,
        )
    )
    blocks: list[str] = []
    for config in owned:
        lines = [f"{config.id} ({_platform_name(config.platform)})"]
        current = _send_target_from_run(context, config.id) or _send_target_from_session_metadata(
            metadata, config.id
        )
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


async def _resolve_send_target(
    chat_sessions: ChatSessionManager,
    context: ToolContext,
    prepared: _PreparedChannelSend,
) -> tuple[str, str | None]:
    """Resolve the (platform_target, thread_id) pair for one send.

    An explicit target never inherits a thread. Otherwise prefer the admitted
    Run's destination, which later ingress cannot change, before falling back to
    the Session's last destination or the Channel's sole allowed chat.
    """
    requested_thread_id = prepared.requested_thread_id
    if prepared.requested_platform_target is not None:
        return prepared.requested_platform_target, requested_thread_id

    run_target = _send_target_from_run(context, prepared.channel_id)
    if run_target is not None:
        platform_target, thread_id = run_target
        return (
            platform_target,
            requested_thread_id if requested_thread_id is not None else thread_id,
        )

    address = SessionAddress(
        project_id=context.project_id, agent_id=context.agent_id, session_id=context.session_id
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


def _send_target_from_run(context: ToolContext, channel_id: str) -> tuple[str, str | None] | None:
    surface = context.reply_surface
    if surface is None or surface.channel_id != channel_id or surface.platform_target is None:
        return None
    return surface.platform_target, surface.thread_id


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


def _platform_target_from_channel_config(channel_config: ChannelConfig) -> str | None:
    if len(channel_config.allowed_chat_ids) != 1:
        return None
    return str(channel_config.allowed_chat_ids[0])
