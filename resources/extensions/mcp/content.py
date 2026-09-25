"""Present MCP payloads: binary bytes become Attachments, large results stay readable.

In a Session the Model reads a view of the payload (``_views.py``): text as a
verbatim body, other fields compact. A view too large to show at once shows
its start; the complete payload is attached to its Tool Result as a result
payload and read selectively through the ``read`` action. Outside a Session
(management and CLI calls) nothing could read a saved result later, so the
complete payload is returned inline.
"""

from __future__ import annotations

import base64
import copy
import json
import mimetypes
import re
from pathlib import Path
from typing import Any

from core.attachments import AttachmentTooLargeError, AttachmentTypeNotAllowedError
from core.extensions.operations import ExtensionHost
from core.tools.tools import ToolContext, read_media_artifact, run_tool_worker
from core.utils.ids import is_safe_id, new_id

from ._views import Part, body_parts, compact, error_text, join_parts, payload_view, view_size

RESULT_VIEW_CHARACTERS = 6000
RESULT_PREVIEW_CHARACTERS = 400
RESULT_READ_ENTRIES = 20
RESULT_READ_CHARACTERS = 4000
# The start of a large text result, and the ends of a long error report, shown before a read.
RESULT_TEXT_CHARACTERS = 3000
ERROR_HEAD_CHARACTERS = 1000
ERROR_TAIL_CHARACTERS = 2000
# A field of a large result shown beside its text start; larger ones are read.
RESULT_FIELD_CHARACTERS = 500

RESULT_MISSING = (
    "read was not run: that saved MCP result is not available in this conversation. Use a "
    "result_id that this connection returned here. To get the data again, repeat the "
    "original call only if it changes nothing."
)

RESULT_DENIED = (
    "read was not run: that saved result belongs to the MCP connection {connection}. "
    "Read it with mcp_{connection}."
)

POINTER_INVALID = (
    "read was not run: pointer {pointer} does not select a value; {reason}. "
    "See what it holds with {call}."
)

READ_INVALID = (
    "read was not run: {options} apply to {applies}, but {selection} is {kind}. Send {call}."
)

READ_TOO_LARGE = (
    "This selection is too large to show. Read a deeper pointer, fewer fields, or a smaller limit."
)


class _PointerError(ValueError):
    def __init__(self, reached: str, reason: str) -> None:
        super().__init__(reason)
        self.reached = reached
        self.reason = reason


class ContentStore:
    def __init__(self, host: ExtensionHost) -> None:
        self.host = host

    async def preserve(
        self, payload: dict[str, Any]
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        artifacts: list[dict[str, Any]] = []
        result = copy.deepcopy(payload)
        # Only protocol-owned content positions contain media. Application JSON,
        # schemas and metadata can use the same keys with unrelated meanings.
        for block in result.get("content", []):
            await self._visit(block, artifacts)
        for resource in result.get("contents", []):
            await self._visit(resource, artifacts, resource=True)
        for message in result.get("messages", []):
            content = message.get("content", [])
            for block in content if isinstance(content, list) else [content]:
                await self._visit(block, artifacts)
        return result, artifacts

    async def _visit(
        self, value: Any, artifacts: list[dict[str, Any]], *, resource: bool = False
    ) -> None:
        if not isinstance(value, dict):
            return
        if not resource and value.get("type") == "resource":
            await self._visit(value.get("resource"), artifacts, resource=True)
            return
        field = self._binary_field(value, resource=resource)
        if field is not None:
            encoded = value[field]
            raw = base64.b64decode(encoded, validate=True)
            media_type = value.get("mimeType", "application/octet-stream")
            suffix = mimetypes.guess_extension(media_type) or ".bin"
            filename = f"{new_id('mcp')}{suffix}"
            try:
                record = await run_tool_worker(self.host.store_attachment, filename, raw)
            except (AttachmentTooLargeError, AttachmentTypeNotAllowedError) as error:
                # vBot keeps no unmanaged copy: the bytes are omitted and the
                # marker says why.
                value["content_omitted"] = True
                value["media_delivery_error"] = str(error)
            else:
                value["path"] = Path(record.file_path).as_posix()
                value["attachment_id"] = record.id
                if value.get("type") in {"image", "audio"}:
                    artifacts.append(
                        read_media_artifact(
                            attachment_id=record.id,
                            filename=record.filename,
                            media_type=record.media_type,
                        )
                    )
            value.pop(field)
            value["size_bytes"] = len(raw)

    @staticmethod
    def _binary_field(value: dict[str, Any], *, resource: bool) -> str | None:
        if (
            not resource
            and value.get("type") in {"image", "audio"}
            and isinstance(value.get("data"), str)
        ):
            return "data"
        if resource and "uri" in value and isinstance(value.get("blob"), str):
            return "blob"
        return None

    async def present(
        self,
        payload: dict[str, Any],
        context: ToolContext,
        connection: str,
        *,
        source: str | None = None,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Return the Model's view of a payload, or the complete payload outside a Session."""
        preserved, artifacts = await self.preserve(payload)
        if not context.result_payloads_available:
            return {"complete": True, "value": preserved}, artifacts
        view = payload_view(preserved)
        if view_size(view) <= RESULT_VIEW_CHARACTERS:
            return view, artifacts
        identifier = self.attach(preserved, context, connection, source)
        rendered = body_parts(preserved)
        if rendered is None:
            return {
                "result_id": identifier,
                "complete": False,
                "preview": self._preview(preserved),
                "read": {"action": "read", "result_id": identifier},
            }, artifacts
        return self._text_start(identifier, view, rendered[0]), artifacts

    async def error_report(
        self,
        payload: dict[str, Any],
        context: ToolContext,
        connection: str,
        *,
        source: str | None = None,
    ) -> tuple[str, list[dict[str, Any]]]:
        """Return a failed call's own report; a long report's middle is left to ``read``."""
        preserved, artifacts = await self.preserve(payload)
        text = error_text(payload_view(preserved))
        shown = ERROR_HEAD_CHARACTERS + ERROR_TAIL_CHARACTERS
        if not context.result_payloads_available or len(text) <= shown + 200:
            return text, artifacts
        identifier = self.attach(preserved, context, connection, source)
        call: dict[str, Any] = {"action": "read", "result_id": identifier}
        rendered = body_parts(preserved)
        parts = rendered[0] if rendered is not None else []
        if len(parts) == 1 and parts[0].pointer is not None and not parts[0].prefix:
            call.update(pointer=parts[0].pointer, offset=ERROR_HEAD_CHARACTERS)
        omitted = len(text) - shown
        return (
            f"{text[:ERROR_HEAD_CHARACTERS]}\n[... {omitted} characters omitted; read the "
            f"complete report with {compact(call)} ...]\n{text[-ERROR_TAIL_CHARACTERS:]}"
        ), artifacts

    @staticmethod
    def attach(
        payload: dict[str, Any],
        context: ToolContext,
        connection: str,
        source: str | None = None,
    ) -> str:
        """Keep a complete payload with its Tool Result for ``read``; return its id."""
        return context.attach_result_payload(
            {"connection": connection, "source": source, "payload": payload}
        )

    @staticmethod
    def _text_start(identifier: str, view: dict[str, Any], parts: list[Part]) -> dict[str, Any]:
        """Show the start of a large text result and the reads that continue it."""
        read = {"action": "read", "result_id": identifier}
        result: dict[str, Any] = {"result_id": identifier}
        large = []
        for key, value in view.items():
            if key == "content":
                continue
            if len(compact(value)) <= RESULT_FIELD_CHARACTERS:
                result[key] = value
            else:
                large.append("/" + pointer_part(key))
        text, continuation, following = _text_prefix(parts, RESULT_TEXT_CHARACTERS)
        notes = []
        if continuation is not None:
            notes.append(
                f"This shows the first {len(text)} of {len(view['content'])} characters. "
                f"Continue with {compact({**read, **continuation})}."
            )
            if following is not None:
                notes.append(f"The next part starts at pointer {following}.")
        if large:
            notes.append(
                f"Too large to show here: {', '.join(large)}; read it with "
                f"{compact({**read, 'pointer': large[0]})}."
            )
        result["note"] = " ".join(notes)
        result["content"] = text
        return result

    async def load_result(
        self,
        identifier: str,
        context: ToolContext,
        connection: str,
    ) -> dict[str, Any]:
        load = self.host.load_result_payload
        if not is_safe_id(identifier) or load is None:
            raise ValueError(RESULT_MISSING)
        document = await load(context, identifier)
        if not isinstance(document, dict):
            raise ValueError(RESULT_MISSING)
        if document.get("connection") != connection:
            raise ValueError(RESULT_DENIED.format(connection=document.get("connection")))
        return document

    @staticmethod
    def _preview(value: Any) -> Any:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        if len(encoded) <= RESULT_VIEW_CHARACTERS:
            return value
        if isinstance(value, dict):
            outline = {
                key: {"pointer": "/" + pointer_part(key), "type": json_kind(item)}
                for key, item in list(value.items())[:RESULT_READ_ENTRIES]
            }
            if len(json.dumps(outline, ensure_ascii=False)) <= RESULT_VIEW_CHARACTERS:
                return outline
        return {"type": json_kind(value), "characters": len(encoded)}

    def read_result(self, document: dict[str, Any], arguments: dict[str, Any]) -> dict[str, Any]:
        identifier = arguments["result_id"]
        pointer = arguments.get("pointer", "")
        try:
            value = select_pointer(document["payload"], pointer)
        except _PointerError as error:
            call = {"action": "read", "result_id": identifier}
            if error.reached:
                call["pointer"] = error.reached
            raise ValueError(
                POINTER_INVALID.format(pointer=pointer, reason=error.reason, call=compact(call))
            ) from None
        fields = arguments.get("fields")
        offset = arguments.get("offset", 0)
        default_limit = RESULT_READ_CHARACTERS if isinstance(value, str) else RESULT_READ_ENTRIES
        limit = arguments.get("limit", default_limit)
        if fields is not None and not isinstance(value, (dict, list)):
            raise ValueError(_inapplicable(arguments, value, ("fields",), "objects and lists"))
        response: dict[str, Any] = {"result_id": identifier, "pointer": pointer}
        if isinstance(value, str):
            limit = min(limit, RESULT_READ_CHARACTERS)
            while True:
                page = {**response, "offset": offset, "total": len(value)}
                end = min(offset + limit, len(value))
                if end < len(value):
                    page["next"] = {**arguments, "offset": end}
                # The Model reads the text verbatim as the body below these fields.
                page["content"] = value[offset : offset + limit]
                # Escaped characters can outgrow the view; a shorter page still advances.
                if limit == 1 or _fits(page):
                    return self._bounded_read(page)
                limit //= 2
        response["type"] = json_kind(value)
        if not isinstance(value, (dict, list)):
            if offset or "limit" in arguments:
                raise ValueError(
                    _inapplicable(arguments, value, ("offset", "limit"), "text, objects and lists")
                )
            response["value"] = value
            return self._bounded_read(response)
        candidates = list(value.items()) if isinstance(value, dict) else list(enumerate(value))
        if fields is not None and isinstance(value, dict):
            candidates = [(key, item) for key, item in candidates if key in fields]
        entries: list[dict[str, Any]] = []
        for key, item in candidates[offset : offset + limit]:
            item_pointer = pointer + "/" + pointer_part(str(key))
            if fields is not None and isinstance(item, dict) and isinstance(value, list):
                item = {field: item[field] for field in fields if field in item}
            encoded = json.dumps(item, ensure_ascii=False, separators=(",", ":"))
            entry: dict[str, Any] = {"pointer": item_pointer}
            if len(encoded) > RESULT_PREVIEW_CHARACTERS:
                entry.update(
                    complete=False,
                    type=json_kind(item),
                    preview=encoded[:RESULT_PREVIEW_CHARACTERS],
                    read={"action": "read", "result_id": identifier, "pointer": item_pointer},
                )
                if fields is not None and isinstance(value, list) and isinstance(item, dict):
                    entry["read"]["fields"] = fields
            else:
                entry.update(complete=True, value=item)
            if (
                entries
                and len(json.dumps(entries + [entry], ensure_ascii=False)) > RESULT_VIEW_CHARACTERS
            ):
                break
            entries.append(entry)
        response.update(entries=entries, offset=offset, total=len(candidates))
        end = min(offset + len(entries), len(candidates))
        if end < len(candidates):
            response["next"] = {**arguments, "offset": end}
        return self._bounded_read(response)

    @staticmethod
    def _bounded_read(response: dict[str, Any]) -> dict[str, Any]:
        if _fits(response):
            return response
        return {
            "result_id": response["result_id"],
            "type": response.get("type", "string"),
            "complete": False,
            "guidance": READ_TOO_LARGE,
        }


def _fits(response: dict[str, Any]) -> bool:
    return len(json.dumps(response, ensure_ascii=False)) <= RESULT_VIEW_CHARACTERS


def _inapplicable(
    arguments: dict[str, Any], value: Any, options: tuple[str, ...], applies: str
) -> str:
    supplied = [option for option in options if option in arguments]
    corrected = {key: item for key, item in arguments.items() if key not in supplied}
    kind = json_kind(value)
    return READ_INVALID.format(
        options=" and ".join(supplied),
        applies=applies,
        selection=arguments.get("pointer") or "the root",
        kind=f"an {kind}" if kind[:1] in "aeiou" else f"a {kind}",
        call=compact(corrected),
    )


def _text_prefix(parts: list[Part], budget: int) -> tuple[str, dict[str, Any] | None, str | None]:
    """Return the body start within budget, the read that continues it, and the next part."""
    rendered = join_parts(parts)
    if len(rendered) <= budget:
        return rendered, None, None
    separator = "\n\n" if any("\n" in part.rendered for part in parts) else "\n"
    shown: list[str] = []
    used = 0
    for index, part in enumerate(parts):
        gap = len(separator) if shown else 0
        if used + gap + len(part.rendered) <= budget:
            shown.append(part.rendered)
            used += gap + len(part.rendered)
            continue
        room = budget - used - gap - len(part.prefix)
        if part.pointer is not None and room > 0:
            shown.append(part.prefix + part.text[:room])
            continuation: dict[str, Any] = {"pointer": part.pointer, "offset": room}
        else:
            continuation = {"pointer": part.item}
        following = parts[index + 1].item if index + 1 < len(parts) else None
        return separator.join(shown), continuation, following
    return rendered, None, None


def pointer_part(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def select_pointer(value: Any, pointer: str) -> Any:
    if pointer == "":
        return value
    if not pointer.startswith("/") or re.search(r"~(?![01])", pointer):
        raise _PointerError("", "a pointer starts with / and writes ~ as ~0 and / in a name as ~1")
    reached = ""
    for part in pointer[1:].split("/"):
        key = part.replace("~1", "/").replace("~0", "~")
        place = reached or "the root"
        if isinstance(value, dict):
            if key not in value:
                raise _PointerError(reached, f"{place} has no field {key[:60]}")
            value = value[key]
        elif isinstance(value, list):
            if not re.fullmatch(r"0|[1-9][0-9]*", key) or int(key) >= len(value):
                raise _PointerError(reached, f"{place} is a list of {len(value)} entries")
            value = value[int(key)]
        else:
            raise _PointerError(reached, f"{place} is a {json_kind(value)} without parts")
        reached += "/" + part
    return value


def json_kind(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    if isinstance(value, str):
        return "string"
    return "number"
