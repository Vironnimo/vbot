"""Validated local-file, button, and checklist delivery payloads."""

from __future__ import annotations

import re
import stat
from pathlib import Path
from typing import TYPE_CHECKING, Any

from core.attachments import sniff_media_type
from core.channels import (
    CHECKLIST_ITEM_ID_MAX_BYTES,
    ChecklistItem,
)
from core.channels.adapter import FileData
from core.extensions import InteractionButton
from core.permissions import PermissionBoundary
from core.tools._channel_send_arguments import (
    REFUSAL_PREFIX,
)
from core.tools._path_suggestions import similar_entries
from core.tools.arguments import optional_bool
from core.tools.tools import (
    ToolContext,
)
from core.utils.paths import file_url_path

if TYPE_CHECKING:
    pass


from core.tools._channel_targets import ChannelSendRefusedError

_INTERACTION_BUTTON_ARGUMENTS = frozenset(("label", "data"))
_CHECKLIST_ITEM_ARGUMENTS = frozenset(("id", "label", "checked"))
_WEB_ADDRESS = re.compile(r"^(?:[a-z][a-z0-9+.-]*://|data:|/api/)", re.IGNORECASE)


def _build_file_data(
    value: list[str] | None,
    *,
    context: ToolContext,
    max_size_bytes: int,
) -> list[FileData]:
    if value is None:
        return []

    files: list[FileData] = []
    for raw_path in value:
        path_text = raw_path.strip()
        if path_text.lower().startswith("file://"):
            # None for a file on another computer, which only Windows paths can name.
            local_path = file_url_path(path_text)
        else:
            local_path = None if _WEB_ADDRESS.match(path_text) else path_text
        if local_path is None:
            raise ChannelSendRefusedError(
                f'{REFUSAL_PREFIX}file_paths "{path_text}" is a web address, not a file on this '
                "computer. Send the file's local path, or put the link in message."
            )
        path_text = local_path
        resolved_path = context.resolve_path(path_text, access="read")
        path_text = resolved_path.as_posix()
        try:
            status = resolved_path.stat()
            if stat.S_ISDIR(status.st_mode):
                raise ChannelSendRefusedError(
                    f'{REFUSAL_PREFIX}Nothing was sent. file_paths "{path_text}" is a folder. '
                    "List its files individually in file_paths."
                )
            if not stat.S_ISREG(status.st_mode):
                raise ChannelSendRefusedError(
                    f'{REFUSAL_PREFIX}Nothing was sent. file_paths "{path_text}" is not a '
                    "regular file. Supply the path of a readable local file."
                )
            # Check the size before reading, without loading oversized files into memory.
            if status.st_size > max_size_bytes:
                raise ChannelSendRefusedError(
                    f'{REFUSAL_PREFIX}Nothing was sent. file_paths "{path_text}" is '
                    f"{_size_text(status.st_size)}; the limit is {_size_text(max_size_bytes)}. "
                    "Send a smaller file or put a download link in message."
                )
            data = resolved_path.read_bytes()
        except FileNotFoundError as error:
            raise ChannelSendRefusedError(
                _missing_file(path_text, resolved_path, context.permissions)
            ) from error
        except OSError as error:
            cause = (
                "access was denied"
                if isinstance(error, PermissionError)
                else "the file operation timed out"
                if isinstance(error, TimeoutError)
                else "the operating system could not read the file"
            )
            raise ChannelSendRefusedError(
                f'{REFUSAL_PREFIX}Nothing was sent. Cannot read file_paths "{path_text}": '
                f"{cause}. Check that the file is accessible, or supply a readable local copy."
            ) from error

        files.append(
            FileData(
                filename=resolved_path.name,
                media_type=sniff_media_type(data, resolved_path.name),
                data=data,
            )
        )

    return files


def _missing_file(path_text: str, resolved_path: Path, permissions: PermissionBoundary) -> str:
    text = f'{REFUSAL_PREFIX}Nothing was sent. file_paths "{path_text}" does not exist.'
    similar = similar_entries(resolved_path, permissions=permissions, kind="files", limit=3)
    if similar:
        base = Path(path_text).parent
        names = ", ".join(f'"{(base / entry.name).as_posix()}"' for entry in similar)
        text += f" Files with similar names there: {names}."
    return text + " Supply the intended existing file in file_paths."


def _size_text(size_bytes: int) -> str:
    if size_bytes >= 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    if size_bytes >= 1024:
        return f"{size_bytes / 1024:.0f} KB"
    return f"{size_bytes} bytes"


def _build_buttons(
    value: list[list[dict[str, str]]] | None,
) -> list[list[InteractionButton]] | None:
    """Parse the tool's ``buttons`` payload into neutral inline-keyboard rows.

    Returns ``None`` when omitted. The input schema guarantees non-empty rows of
    buttons with non-empty ``label`` and ``data``; since it leaves button objects
    open, another field raises ``ChannelSendRefusedError`` (mapped to a clean
    ``invalid_arguments`` tool failure). The callback data's byte-length and
    platform support are enforced downstream by the channel service and adapter.
    """
    if value is None:
        return None

    rows: list[list[InteractionButton]] = []
    for row_index, row in enumerate(value):
        buttons: list[InteractionButton] = []
        for button_index, button in enumerate(row):
            unknown_fields = sorted(set(button) - _INTERACTION_BUTTON_ARGUMENTS)
            if unknown_fields:
                names = ", ".join(unknown_fields)
                raise ChannelSendRefusedError(
                    f"{REFUSAL_PREFIX}Nothing was sent. buttons[{row_index}][{button_index}] "
                    f"has unknown field(s): {names}. Each button takes label and data; "
                    "remove the other fields only if their effect is not needed."
                )
            buttons.append(InteractionButton(label=button["label"], data=button["data"]))
        rows.append(buttons)
    return rows


def _build_checklist(value: Any) -> list[ChecklistItem] | None:
    """Parse the tool's ``checklist`` items; None when omitted.

    The input schema guarantees objects with non-empty ``id`` and ``label``; the
    checks here cover what it cannot say: other fields, repeated ids, and ids too
    long for the platform's callback data.
    """
    if value is None:
        return None
    items: list[ChecklistItem] = []
    seen: set[str] = set()
    for index, raw in enumerate(value, start=1):
        unknown_fields = sorted(set(raw) - _CHECKLIST_ITEM_ARGUMENTS)
        if unknown_fields:
            raise ChannelSendRefusedError(
                f"{REFUSAL_PREFIX}checklist item {index} has unknown field(s): "
                f"{', '.join(unknown_fields)}. An item takes id, label and checked."
            )
        item_id = raw["id"]
        size = len(item_id.encode("utf-8"))
        if size > CHECKLIST_ITEM_ID_MAX_BYTES:
            raise ChannelSendRefusedError(
                f'{REFUSAL_PREFIX}checklist item {index} has the id "{item_id}" of {size} bytes; '
                f"an id can have at most {CHECKLIST_ITEM_ID_MAX_BYTES}. Use shorter ids, such as "
                "item numbers you map back to the items."
            )
        if item_id in seen:
            raise ChannelSendRefusedError(
                f'{REFUSAL_PREFIX}the id "{item_id}" appears on more than one checklist item. '
                "Give each item its own id."
            )
        seen.add(item_id)
        checked = optional_bool(raw.get("checked"), field_name="checked", default=False)
        items.append(ChecklistItem(id=item_id, label=raw["label"], checked=checked))
    return items
