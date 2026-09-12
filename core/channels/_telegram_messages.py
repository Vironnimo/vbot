"""Telegram message formatting and value parsing."""

from __future__ import annotations

import re
from typing import Any, TypeGuard

from core.channels.config import ChannelConfigError

TELEGRAM_MESSAGE_LIMIT = 4096


def split_telegram_message(message: str, max_chars: int = TELEGRAM_MESSAGE_LIMIT) -> list[str]:
    """Split one message into Telegram-size chunks measured in UTF-16 code units.

    Telegram counts its length limits in UTF-16 code units, not Unicode code points, so an
    astral-plane character (most emoji) counts as two. Splitting on Python's code-point
    slicing would let an emoji-heavy chunk exceed the wire limit and fail with BadRequest,
    so chunk boundaries are placed by UTF-16 length and never inside a character.
    """
    if max_chars <= 0:
        raise ValueError("max_chars must be positive")
    if not message:
        return []

    chunks: list[str] = []
    chunk_start = 0
    chunk_units = 0
    for index, character in enumerate(message):
        units = _utf16_units(character)
        if chunk_units + units > max_chars and index > chunk_start:
            chunks.append(message[chunk_start:index])
            chunk_start = index
            chunk_units = 0
        chunk_units += units
    chunks.append(message[chunk_start:])
    return chunks


def _utf16_units(character: str) -> int:
    # Astral-plane characters encode as a UTF-16 surrogate pair (2 units); everything in
    # the Basic Multilingual Plane is a single unit. Telegram counts in these units.
    return 2 if ord(character) > 0xFFFF else 1


def _utf16_length(text: str) -> int:
    return sum(_utf16_units(character) for character in text)


def _normalize_optional_message(message: str | None) -> str | None:
    if message is None:
        return None
    if not isinstance(message, str) or not message.strip():
        raise ChannelConfigError("message must be a non-empty string when provided")
    return message.strip()


def _parse_start_command(text: str) -> str | None:
    """Return the /start deep-link payload ("" when bare), or None for other text."""
    stripped = text.strip()
    if stripped == "/start":
        return ""
    if stripped.startswith("/start "):
        return stripped[len("/start ") :].strip()
    return None


def _extract_message_text(update: Any) -> str | None:
    message = getattr(update, "effective_message", None)
    text = getattr(message, "text", None)
    if not isinstance(text, str):
        return None
    if not text.strip():
        return None
    return text


def _user_display_name(user: Any) -> str | None:
    # full_name is derived from first_name (Bot-API-mandatory) + optional last_name;
    # username is optional and unset for many accounts.
    full_name = getattr(user, "full_name", None)
    if isinstance(full_name, str) and full_name.strip():
        return full_name.strip()

    username = getattr(user, "username", None)
    if isinstance(username, str) and username.strip():
        return username.strip()
    return None


def _telegram_bot_display_name(bot_user: Any) -> str | None:
    """Return the human-visible Telegram name without falling back to @username."""
    full_name = getattr(bot_user, "full_name", None)
    if isinstance(full_name, str) and full_name.strip():
        return full_name.strip()

    name_parts = [
        value.strip()
        for value in (
            getattr(bot_user, "first_name", None),
            getattr(bot_user, "last_name", None),
        )
        if isinstance(value, str) and value.strip()
    ]
    return " ".join(name_parts) or None


def _compile_display_name_pattern(display_name: str) -> re.Pattern[str]:
    """Compile an exact, whitespace-tolerant display-name address pattern."""
    name_parts = re.split(r"\s+", display_name.strip())
    escaped_name = r"\s+".join(re.escape(part) for part in name_parts)
    prefix = r"(?<!\w)" if _is_word_character(name_parts[0][0]) else ""
    suffix = r"(?!\w)" if _is_word_character(name_parts[-1][-1]) else ""
    return re.compile(f"{prefix}{escaped_name}{suffix}", re.IGNORECASE)


def _is_word_character(value: str) -> bool:
    return re.fullmatch(r"\w", value) is not None


def _render_structured_message(message: Any) -> str | None:
    """Render a location/contact/poll payload as bracketed text for the model.

    These message types carry structured data instead of downloadable media; a compact
    text rendering keeps their content usable in the conversation without a separate
    content-block type.
    """
    venue = getattr(message, "venue", None)
    if venue is not None:
        return _render_venue(venue)
    location = getattr(message, "location", None)
    if location is not None:
        return _render_location(location)
    contact = getattr(message, "contact", None)
    if contact is not None:
        return _render_contact(contact)
    poll = getattr(message, "poll", None)
    if poll is not None:
        return _render_poll(poll)
    return None


def _location_coordinates(location: Any) -> str | None:
    latitude = getattr(location, "latitude", None)
    longitude = getattr(location, "longitude", None)
    if not isinstance(latitude, (int, float)) or not isinstance(longitude, (int, float)):
        return None
    return f"latitude {latitude}, longitude {longitude}"


def _render_location(location: Any) -> str | None:
    coordinates = _location_coordinates(location)
    if coordinates is None:
        return None
    return f"[location shared] {coordinates}"


def _render_venue(venue: Any) -> str | None:
    details = ", ".join(
        value.strip()
        for value in (getattr(venue, "title", None), getattr(venue, "address", None))
        if isinstance(value, str) and value.strip()
    )
    coordinates = _location_coordinates(getattr(venue, "location", None))
    if details and coordinates:
        return f"[location shared] {details} ({coordinates})"
    if details:
        return f"[location shared] {details}"
    if coordinates:
        return f"[location shared] {coordinates}"
    return None


def _render_contact(contact: Any) -> str | None:
    name = " ".join(
        value.strip()
        for value in (getattr(contact, "first_name", None), getattr(contact, "last_name", None))
        if isinstance(value, str) and value.strip()
    )
    phone_raw = getattr(contact, "phone_number", None)
    phone = phone_raw.strip() if isinstance(phone_raw, str) and phone_raw.strip() else None
    if not name and phone is None:
        return None
    if phone is None:
        return f"[contact shared] {name}"
    if not name:
        return f"[contact shared] phone: {phone}"
    return f"[contact shared] {name}, phone: {phone}"


def _render_poll(poll: Any) -> str | None:
    question = getattr(poll, "question", None)
    if not isinstance(question, str) or not question.strip():
        return None
    lines = [f"[poll] {question.strip()}"]
    for option in getattr(poll, "options", ()) or ():
        text = getattr(option, "text", None)
        if isinstance(text, str) and text.strip():
            lines.append(f"- {text.strip()}")
    return "\n".join(lines)


def _extract_caption(message: Any) -> str | None:
    caption = getattr(message, "caption", None)
    if not isinstance(caption, str):
        return None
    caption = caption.strip()
    return caption or None


def _telegram_message_has_media(message: Any) -> bool:
    photos = getattr(message, "photo", None)
    if isinstance(photos, (list, tuple)) and bool(photos):
        return True
    return any(
        getattr(message, attribute_name, None) is not None
        for attribute_name in (
            "document",
            "voice",
            "audio",
            "video",
            "video_note",
            "animation",
        )
    )


def _telegram_message_author(message: Any) -> tuple[str, str | None] | None:
    user = getattr(message, "from_user", None)
    user_id = getattr(user, "id", None)
    if _is_integer(user_id):
        return str(user_id), _user_display_name(user)

    sender_chat = getattr(message, "sender_chat", None)
    sender_chat_id = getattr(sender_chat, "id", None)
    if not _is_integer(sender_chat_id):
        return None
    display_name = next(
        (
            value.strip()
            for value in (
                getattr(sender_chat, "title", None),
                getattr(sender_chat, "username", None),
            )
            if isinstance(value, str) and value.strip()
        ),
        None,
    )
    return str(sender_chat_id), display_name


def _default_photo_filename(file_unique_id: object) -> str:
    if isinstance(file_unique_id, str) and file_unique_id.strip():
        return f"telegram-photo-{file_unique_id.strip()}.jpg"
    return "telegram-photo.jpg"


def _default_document_filename(document: Any) -> str:
    filename = getattr(document, "file_name", None)
    if isinstance(filename, str) and filename.strip():
        return filename.strip()

    file_unique_id = getattr(document, "file_unique_id", None)
    if isinstance(file_unique_id, str) and file_unique_id.strip():
        return f"telegram-document-{file_unique_id.strip()}"
    return "telegram-document"


def _media_filename(media_object: Any, prefix: str, extension: str) -> str:
    filename = getattr(media_object, "file_name", None)
    if isinstance(filename, str) and filename.strip():
        return filename.strip()

    file_unique_id = getattr(media_object, "file_unique_id", None)
    if isinstance(file_unique_id, str) and file_unique_id.strip():
        return f"{prefix}-{file_unique_id.strip()}{extension}"
    return f"{prefix}{extension}"


def _default_voice_filename(voice: Any) -> str:
    return _media_filename(voice, "telegram-voice", ".ogg")


def _default_audio_filename(audio: Any) -> str:
    return _media_filename(audio, "telegram-audio", "")


def _default_video_filename(video: Any) -> str:
    return _media_filename(video, "telegram-video", ".mp4")


def _default_video_note_filename(video_note: Any) -> str:
    return _media_filename(video_note, "telegram-video-note", ".mp4")


def _default_animation_filename(animation: Any) -> str:
    # Telegram converts GIFs to MP4 animations; a real GIF still sniffs as image/gif
    # in the attachment store regardless of this fallback extension.
    return _media_filename(animation, "telegram-animation", ".mp4")


def _is_image_media_type(media_type: str) -> bool:
    return isinstance(media_type, str) and media_type.startswith("image/")


def _parse_platform_target(platform_target: str) -> int:
    try:
        chat_id = int(platform_target)
    except (TypeError, ValueError) as error:
        raise ChannelConfigError("platform_target must be an integer chat id") from error
    return chat_id


def _parse_thread_id(thread_id: str | None) -> int | None:
    if thread_id is None:
        return None
    try:
        return int(thread_id)
    except (TypeError, ValueError) as error:
        raise ChannelConfigError("thread_id must be an integer Telegram topic id") from error


def _is_integer(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def _parse_message_id(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(value)
    except ValueError:
        return None
    return parsed if parsed >= 0 else None
