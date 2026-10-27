"""Neutral Channel message notes and Run-output projections."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from core.attachments import AttachmentTooLargeError, AttachmentTypeNotAllowedError
from core.channels.adapter import (
    ConversationFacts,
    RunButtonBinding,
    parse_bound_run_callback_data,
)
from core.channels.checklist import CHECKLIST_DONE_DATA, checklist_changes, is_checklist_button
from core.chat.messages import MessageSender
from core.extensions import InteractionButton
from core.runs import is_silent_answer
from core.sessions import CHANNEL_MESSAGE_NOTE_PREFIX

if TYPE_CHECKING:
    from core.extensions.interactions import InteractionEvent

_FAILED_REPLY = "Sorry, I couldn't complete that request. Please try again."
_CANCELLED_REPLY = "Sorry, this request was cancelled before completion."
_INTERRUPTED_REPLY = "Sorry, this request was interrupted before it could finish."
_EMPTY_ASSISTANT_REPLY = "I finished processing your message, but no reply text was produced."
_COMPACTED_REPLY = "Context compacted."

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

    Without a checklist it calls out the tapped button and lists every current
    button's label and callback data as quoted records in row order, so the agent
    reads the whole keyboard state from the note alone. A checklist keyboard
    (``core.channels.checklist``) is reported as the items whose state changed
    since sending, by their ids, so the agent applies only what the user changed.
    """
    changes = checklist_changes(event.buttons)
    if changes is not None and event.data == CHECKLIST_DONE_DATA:
        lines = ["The checklist you sent to this chat was submitted."]
    else:
        lines = [
            "A button on your message was tapped.",
            "Tapped button: "
            + _quote_interaction_data(label=_tapped_button_label(event), data=event.data),
        ]
    if conversation.kind == "group":
        lines.append(
            "Tapped by: "
            f"[{_sanitize_sender_tag_part(conversation.user_display_name or conversation.user_id)}"
            f"|{_sanitize_sender_tag_part(conversation.user_id)}"
            f"|{conversation.sender_role or 'member'}]"
        )
    if changes is None:
        lines.append("Current buttons on the message (top to bottom, left to right):")
        lines.extend(
            "- " + _quote_interaction_data(label=button.label, data=button.data)
            for row in event.buttons
            for button in row
        )
        lines.append("Carry out the tapped action, then confirm in this chat.")
        return "\n".join(lines)

    changed = len(changes.checked) + len(changes.unchecked)
    if changed:
        lines.append("Checklist items changed since you sent it:")
        lines.extend(
            "- checked: " + _quote_interaction_data(label=item.label, id=item.id)
            for item in changes.checked
        )
        lines.extend(
            "- unchecked: " + _quote_interaction_data(label=item.label, id=item.id)
            for item in changes.unchecked
        )
        lines.append(f"Unchanged items: {changes.unchanged}.")
    else:
        lines.append(f"No checklist item changed since you sent it ({changes.unchanged} items).")
    others = [button for row in event.buttons for button in row if not is_checklist_button(button)]
    if others:
        lines.append("Other buttons on the message:")
        lines.extend(
            "- " + _quote_interaction_data(label=button.label, data=button.data)
            for button in others
        )
    if event.data != CHECKLIST_DONE_DATA:
        lines.append("Carry out the tapped action, then confirm in this chat.")
    else:
        lines.append(
            "Act on these changes, then confirm in this chat."
            if changed
            else "Confirm in this chat."
        )
    return "\n".join(lines)


def _quote_interaction_data(**values: str) -> str:
    """Keep message-authored labels and values inside one quoted data record."""
    return json.dumps(values, ensure_ascii=False).replace("<", "\\u003c").replace(">", "\\u003e")


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


def _interaction_retry_keyboard(event: InteractionEvent) -> dict[str, Any]:
    """Keep the accepted tap's wire keyboard for a usable retry after interruption."""
    return {
        "buttons": [
            [{"label": button.label, "data": button.data} for button in row]
            for row in event.buttons
        ]
    }


def _retry_buttons(
    keyboard: dict[str, Any] | None, binding_id: str | None
) -> list[list[InteractionButton]] | None:
    """Read known keyboard fields while leaving its durable open payload intact."""
    if not isinstance(keyboard, dict) or not isinstance(rows := keyboard.get("buttons"), list):
        return None
    buttons: list[list[InteractionButton]] = []
    has_run_button = False
    for row in rows:
        if not isinstance(row, list) or not row:
            return None
        converted = []
        for button in row:
            if not isinstance(button, dict):
                return None
            label, data = button.get("label"), button.get("data")
            if not isinstance(label, str) or not isinstance(data, str) or not label or not data:
                return None
            if data.startswith("run:"):
                parsed = parse_bound_run_callback_data(data)
                if binding_id is not None and (parsed is None or parsed[0] != binding_id):
                    return None
                has_run_button = True
            converted.append(InteractionButton(label=label, data=data))
        buttons.append(converted)
    return buttons if has_run_button else None


def _sanitize_sender_tag_part(value: str) -> str:
    sanitized = value.translate(_SENDER_TAG_UNSAFE_CHARACTERS).strip()
    return sanitized or "unknown"


def _sender_tag(sender: MessageSender) -> str:
    return (
        f"[{_sanitize_sender_tag_part(sender.display_name)}"
        f"|{_sanitize_sender_tag_part(sender.id)}|{sender.role}]"
    )


class RunReply:
    """The one reply a Run's chat gets, from the Run's Assistant output and outcome.

    A live relay feeds the Run's events; a reply resumed after its engine ended
    feeds the entries the Run left in its Session history. Both settle on the
    same text.
    """

    def __init__(self) -> None:
        self._assistant_text: str | None = None
        # The latest output while it is a complete answer: a Run stopped after it
        # (for example during post-answer Compaction) still delivers it.
        self._final_answer: str | None = None
        self._interrupted_segments: list[str] = []
        self._compaction_completed = False

    def observe_output(self, message: object) -> None:
        """Take one Assistant message, as an output event or a history entry carries it."""
        if not isinstance(message, Mapping):
            return
        interrupted = message.get("interrupted") is True
        extracted = _message_text(
            message, preserve_whitespace=interrupted or bool(self._interrupted_segments)
        )
        if extracted is not None:
            if interrupted or self._interrupted_segments:
                self._interrupted_segments.append(extracted)
            else:
                self._assistant_text = extracted
        is_answer = not interrupted and not message.get("tool_calls")
        self._final_answer = extracted if is_answer and not self._interrupted_segments else None

    def observe_input(self) -> None:
        """Take a user input the Run received after it started (a steered message).

        An answer given before it no longer answers the Run's latest input.
        """
        self._final_answer = None

    def observe_compaction(self) -> None:
        self._compaction_completed = True

    def settle(self, status: str, reason: str | None = None) -> str | None:
        """Return the reply for the Run's terminal ``status`` and cancellation ``reason``.

        None when the Run's answer is the silent ending: the chat gets nothing.
        """
        reply = self._reply(status, reason)
        return None if is_silent_answer(reply) else reply

    def _reply(self, status: str, reason: str | None) -> str:
        if status == "completed":
            return (
                self._partial_answer()
                or self._assistant_text
                or (_COMPACTED_REPLY if self._compaction_completed else None)
                or _EMPTY_ASSISTANT_REPLY
            )
        if status == "failed":
            # A failure after the complete answer (for example while recording the
            # Run's end) does not take back the answer the chat is owed.
            return self._final_answer or _FAILED_REPLY
        if status == "cancelled" and reason != "shutdown":
            return self._final_answer or _CANCELLED_REPLY
        # Interrupted, or stopped because vBot shut down: what the Run kept.
        return self._partial_answer() or self._assistant_text or _INTERRUPTED_REPLY

    def _partial_answer(self) -> str | None:
        # These are consecutive fragments of one visible answer across internal
        # Model boundaries. Preserve their bytes instead of inventing separators;
        # the continuation Model owns any required whitespace or Markdown break.
        return "".join(self._interrupted_segments) if self._interrupted_segments else None


def _message_text(message: Mapping[str, object], *, preserve_whitespace: bool) -> str | None:
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        return None
    return content if preserve_whitespace else content.strip()


def _media_failure_reply(error: Exception) -> str:
    """Map a media-ingest failure to user-facing reply text without leaking internals."""
    if isinstance(error, AttachmentTypeNotAllowedError):
        return _UNSUPPORTED_FILE_REPLY
    if isinstance(error, AttachmentTooLargeError):
        return _FILE_TOO_LARGE_REPLY
    if getattr(error, "retryable", False) is True:
        return _MEDIA_DOWNLOAD_FAILED_REPLY
    return _MEDIA_FAILED_REPLY
