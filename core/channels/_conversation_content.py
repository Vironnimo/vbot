"""Neutral Channel message notes and Run-output projections."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from core.attachments import AttachmentTooLargeError, AttachmentTypeNotAllowedError
from core.channels.adapter import (
    ConversationFacts,
    RunButtonBinding,
    parse_bound_run_callback_data,
)
from core.chat.messages import MessageSender
from core.sessions import CHANNEL_MESSAGE_NOTE_PREFIX

if TYPE_CHECKING:
    from core.extensions.interactions import InteractionEvent
    from core.runs import RunEvent

_UNSUPPORTED_FILE_REPLY = "Sorry, this file type isn't supported yet."


_FILE_TOO_LARGE_REPLY = "Sorry, this file is too large to process."


_MEDIA_FAILED_REPLY = "Sorry, I couldn't process the attached file. Please try again."


_MEDIA_DOWNLOAD_FAILED_REPLY = (
    "Sorry, the messaging platform couldn't download the attached file after several "
    "attempts. Please resend it."
)


_SENDER_TAG_UNSAFE_CHARACTERS = str.maketrans("", "", "[]|\r\n")


def _format_observed_message(conversation: ConversationFacts, text: str) -> str:
    display_name = _sanitize_sender_tag_part(conversation.user_display_name or conversation.user_id)
    sender_id = _sanitize_sender_tag_part(conversation.user_id)
    role = conversation.sender_role or "member"
    return f"{CHANNEL_MESSAGE_NOTE_PREFIX}[{display_name}|{sender_id}|{role}]: {text}"


def _format_interaction_note(conversation: ConversationFacts, event: InteractionEvent) -> str:
    """Render the neutral kernel note for a run-triggering button tap.

    Content-agnostic: it calls out the tapped button and then lists every current
    button's label and callback data verbatim in row order, so the agent can read
    the whole keyboard state — e.g. which items a skill marked ✅ — from the note
    alone, with no server-side store. Any glyph or id convention inside the labels
    or data is the skill's interpretation, never the engine's.
    """
    lines = [
        "A channel button was tapped and is asking you to act on it.",
        f'Tapped button: "{_tapped_button_label(event)}" ({event.data})',
    ]
    if conversation.kind == "group":
        lines.append(
            "Tapped by: "
            f"[{_sanitize_sender_tag_part(conversation.user_display_name or conversation.user_id)}"
            f"|{_sanitize_sender_tag_part(conversation.user_id)}"
            f"|{conversation.sender_role or 'member'}]"
        )
    lines.append("Current buttons on the message (top to bottom, left to right):")
    lines.extend(f'- "{button.label}" ({button.data})' for row in event.buttons for button in row)
    lines.append("Act on the current button state, then confirm in this chat.")
    return "\n".join(lines)


def _restore_bound_interaction_event(
    binding: RunButtonBinding,
    event: InteractionEvent,
    tapped_index: int,
) -> InteractionEvent | None:
    """Hide the private binding envelope and restore the agent-authored ``run:*`` data."""
    if tapped_index >= len(binding.original_button_data):
        return None

    restored_rows = []
    for row in event.buttons:
        restored_row = []
        for button in row:
            parsed = parse_bound_run_callback_data(button.data)
            if parsed is None or parsed[0] != binding.id:
                restored_row.append(button)
                continue
            button_index = parsed[1]
            if button_index >= len(binding.original_button_data):
                return None
            restored_row.append(replace(button, data=binding.original_button_data[button_index]))
        restored_rows.append(tuple(restored_row))

    return replace(
        event,
        data=binding.original_button_data[tapped_index],
        buttons=tuple(restored_rows),
    )


def _tapped_button_label(event: InteractionEvent) -> str:
    """The label of the button whose data was tapped, or the raw data as fallback."""
    for row in event.buttons:
        for button in row:
            if button.data == event.data:
                return button.label
    return event.data


def _sanitize_sender_tag_part(value: str) -> str:
    sanitized = value.translate(_SENDER_TAG_UNSAFE_CHARACTERS).strip()
    return sanitized or "unknown"


def _sender_tag(sender: MessageSender) -> str:
    return (
        f"[{_sanitize_sender_tag_part(sender.display_name)}"
        f"|{_sanitize_sender_tag_part(sender.id)}|{sender.role}]"
    )


def _extract_assistant_output(event: RunEvent, *, preserve_whitespace: bool = False) -> str | None:
    payload = event.payload
    if not isinstance(payload, dict):
        return None

    message = payload.get("message")
    if not isinstance(message, dict):
        return None

    content = message.get("content")
    if not isinstance(content, str):
        return None

    if not content.strip():
        return None
    return content if preserve_whitespace else content.strip()


def _assistant_output_interrupted(event: RunEvent) -> bool:
    message = event.payload.get("message")
    return isinstance(message, dict) and message.get("interrupted") is True


def _combined_interrupted_output(segments: list[str]) -> str | None:
    # These are consecutive fragments of one visible answer across internal
    # Model boundaries. Preserve their bytes instead of inventing separators;
    # the continuation Model owns any required whitespace or Markdown break.
    return "".join(segments) if segments else None


def _media_failure_reply(error: Exception) -> str:
    """Map a media-ingest failure to user-facing reply text without leaking internals."""
    if isinstance(error, AttachmentTypeNotAllowedError):
        return _UNSUPPORTED_FILE_REPLY
    if isinstance(error, AttachmentTooLargeError):
        return _FILE_TOO_LARGE_REPLY
    if getattr(error, "retryable", False) is True:
        return _MEDIA_DOWNLOAD_FAILED_REPLY
    return _MEDIA_FAILED_REPLY
