"""Validated local-file, button, and checklist delivery payloads."""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from core.attachments import sniff_media_type
from core.channels import (
    CHECKLIST_ITEM_ID_MAX_BYTES,
    ChecklistItem,
)
from core.channels.adapter import FileData
from core.extensions import InteractionButton
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
    for index, raw_path in enumerate(value):
        if not raw_path.strip():
            raise ChannelSendRefusedError(f"file_paths[{index}] must be a non-empty string")
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
            raise ChannelSendRefusedError(
                f"cannot read file_paths[{index}] {raw_path}: {error}"
            ) from error

        files.append(
            FileData(
                filename=resolved_path.name,
                media_type=sniff_media_type(data, resolved_path.name),
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
                    f"buttons[{row_index}][{button_index}] has unknown field(s): {names}"
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
