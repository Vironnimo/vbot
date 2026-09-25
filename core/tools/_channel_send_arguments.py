"""Read ``channel_send`` calls written in vBot's schema or another messaging dialect.

Models send chat messages with the fields of messaging Tools they know: OpenClaw's
``message`` Tool (``channel`` as a platform name, ``target``, ``media``,
``attachments``), Hermes' ``send_message`` (``target`` as ``platform:chat:thread``,
``MEDIA:<path>`` inside the text, ``action: list``), and generic chat APIs
(``chat_id``, ``text``, ``files``). This owner maps them onto the canonical fields.

Which Channel a ``channel`` or ``target`` names depends on the calling Agent's
Channels, so both stay unadvertised and reach the handler, which resolves them
against the Agent's configuration and refuses what it cannot read exactly.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

from core.channels.config import ALLOWED_CHANNEL_PLATFORMS
from core.tools._argument_repair import normalize_call_arguments
from core.tools._call_vocabulary import SpellingAliases, is_placeholder, spelling
from core.tools.contracts import ToolContract, ToolContractError

ACTION_FIELD = "action"
CHANNEL_FIELD = "channel"
TARGET_FIELD = "target"

UNADVERTISED_PARAMETERS: dict[str, Any] = {
    ACTION_FIELD: {"type": "string", "enum": ["send", "list"]},
    CHANNEL_FIELD: {"type": "string", "minLength": 1},
    TARGET_FIELD: {"type": "string", "minLength": 1},
}
REFUSAL_PREFIX = "channel_send was not run: "

_CALL_ORDER = (
    "channel_id",
    "platform_target",
    "thread_id",
    "message",
    "file_paths",
    "buttons",
)
_LONG_TEXT = 120
_STAND_INS = {
    "message": "<the message from this call>",
    "buttons": "<the buttons from this call>",
}
_TEMPLATE = re.compile(r"^\s*<[^<>]+>\s*$")
_MEDIA_TOKEN = re.compile(r'MEDIA:(?:"([^"]+)"|(\S+))')
_FILE_OBJECT_KEYS = ("path", "file_path", "filepath", "media", "file", "url", "source", "src")

_FIELD_ALIASES = SpellingAliases(
    {
        "message": ("text", "content", "body", "msg", "message_text", "caption"),
        "platform_target": (
            "chat_id",
            "chat",
            "to",
            "recipient",
            "recipient_id",
            "target_id",
            "conversation_id",
            "peer_id",
            "destination",
        ),
        "thread_id": ("topic_id", "message_thread_id", "thread", "topic"),
        "file_paths": (
            "files",
            "file",
            "file_path",
            "path",
            "paths",
            "attachments",
            "attachment",
            "media",
            "media_path",
            "media_paths",
            "media_url",
            "image",
            "images",
            "image_path",
            "image_paths",
            "document",
            "documents",
            "photo",
            "photos",
        ),
        CHANNEL_FIELD: ("channel_name", "platform"),
    }
)
_ACTION_WORDS = SpellingAliases(
    {
        "send": ("send_message", "message", "post", "reply", "notify", "send_file", "upload"),
        "list": ("list_targets", "targets", "list_channels", "channels", "directory"),
    }
)


def normalize_channel_send_arguments(contract: ToolContract, arguments: Any) -> Any:
    """Return the canonical ``channel_send`` arguments for one Model call."""
    normalized = normalize_call_arguments(
        contract,
        arguments,
        enum_fields=(ACTION_FIELD,),
        field_aliases=_FIELD_ALIASES,
        field_normalizers={ACTION_FIELD: _action_word, "file_paths": _file_list},
        empty_as_omitted=(
            "message",
            "platform_target",
            "thread_id",
            CHANNEL_FIELD,
            TARGET_FIELD,
        ),
    )
    if not isinstance(normalized, dict):
        return normalized
    for name in ("platform_target", "thread_id", CHANNEL_FIELD, TARGET_FIELD):
        item = normalized.get(name)
        if isinstance(item, str) and (is_placeholder(item) or _TEMPLATE.match(item)):
            del normalized[name]
    for name in ("file_paths", "buttons"):
        if normalized.get(name) == []:
            del normalized[name]  # An empty list asks for nothing.
    channel_id = normalized.get("channel_id")
    if (
        isinstance(channel_id, str)
        and spelling(channel_id) in ALLOWED_CHANNEL_PLATFORMS
        and CHANNEL_FIELD not in normalized
    ):
        # A platform name where a Channel id belongs: the handler takes the Channel with
        # that id, else the Agent's only Channel on that platform, else asks.
        normalized[CHANNEL_FIELD] = normalized.pop("channel_id")
    action = normalized.get(ACTION_FIELD)
    if action == "send":
        del normalized[ACTION_FIELD]
    elif action is not None and action != "list":
        raise ToolContractError(
            f'{REFUSAL_PREFIX}action "{action}" is not something channel_send does; it sends '
            "a message or files to a chat. To send, leave action out."
        )
    _read_media_tokens(normalized)
    return normalized


def render_call(arguments: Mapping[str, Any], **overrides: Any) -> str:
    """Render a canonical ``channel_send`` call as compact JSON; None removes a field."""
    call = {**arguments, **overrides}
    ordered: dict[str, Any] = {}
    for name in _CALL_ORDER:
        if call.get(name) is not None:
            ordered[name] = call[name]
    message = ordered.get("message")
    if isinstance(message, str) and len(message) > _LONG_TEXT:
        ordered["message"] = _STAND_INS["message"]
    if "buttons" in ordered and not isinstance(ordered["buttons"], str):
        ordered["buttons"] = _STAND_INS["buttons"]
    return json.dumps(ordered, ensure_ascii=False, separators=(",", ":"))


def refusal(text: str, arguments: Mapping[str, Any], **overrides: Any) -> str:
    """Return a refusal that ends with the corrected call."""
    return f"{REFUSAL_PREFIX}{text} Send: {render_call(arguments, **overrides)}"


def choice(text: str, calls: list[str]) -> str:
    """Return a refusal offering one complete call per reading."""
    return f"{REFUSAL_PREFIX}{text} " + " or ".join(calls)


def _action_word(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    return _ACTION_WORDS.get(value, value)


def _file_list(value: Any) -> Any:
    """One path, a path object (``{"media": ...}``) or a list of either becomes a list."""
    items = value if isinstance(value, list) else [value]
    paths: list[Any] = []
    for item in items:
        if isinstance(item, dict):
            fields = {spelling(key): entry for key, entry in item.items()}
            found = next(
                (fields[key] for key in map(spelling, _FILE_OBJECT_KEYS) if key in fields), None
            )
            paths.append(found if found is not None else item)
        elif not (isinstance(item, str) and not item.strip()) and item is not None:
            paths.append(item)
    return paths


def _read_media_tokens(arguments: dict[str, Any]) -> None:
    """Move ``MEDIA:<path>`` markers out of the text into ``file_paths``."""
    message = arguments.get("message")
    if not isinstance(message, str) or "MEDIA:" not in message:
        return
    found = [quoted or plain for quoted, plain in _MEDIA_TOKEN.findall(message)]
    if not found:
        return
    rest = _MEDIA_TOKEN.sub("", message)
    rest = "\n".join(line.rstrip() for line in rest.splitlines()).strip()
    existing = arguments.get("file_paths")
    paths = list(existing) if isinstance(existing, list) else []
    for path in found:
        if path not in paths:
            paths.append(path)
    arguments["file_paths"] = paths
    if rest:
        arguments["message"] = re.sub(r"\n{3,}", "\n\n", rest)
    else:
        del arguments["message"]


__all__ = [
    "ACTION_FIELD",
    "CHANNEL_FIELD",
    "REFUSAL_PREFIX",
    "TARGET_FIELD",
    "UNADVERTISED_PARAMETERS",
    "choice",
    "normalize_channel_send_arguments",
    "refusal",
    "render_call",
]
