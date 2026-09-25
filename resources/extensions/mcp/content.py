"""Present MCP payloads: binary bytes become Attachments, large results stay readable.

A result too large to show at once is attached to its Tool Result as a result
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

RESULT_VIEW_CHARACTERS = 6000
RESULT_PREVIEW_CHARACTERS = 400
RESULT_READ_ENTRIES = 20
RESULT_READ_CHARACTERS = 2000

RESULT_MISSING = (
    "Saved MCP result is unavailable. Use an existing result_id returned by this connection."
)

RESULT_DENIED = "This Agent cannot read this saved MCP result."

POINTER_INVALID = "Invalid JSON Pointer. Use a pointer returned by read."

READ_INVALID = "These read options do not apply to the selected value."

READ_TOO_LARGE = (
    "This selection is too large to show. Read a deeper pointer, fewer fields, or a smaller limit."
)


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
        preview: Any = None,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        preserved, artifacts = await self.preserve(payload)
        encoded = json.dumps(preserved, ensure_ascii=False, separators=(",", ":"))
        if not context.result_payloads_available or (
            preview is None and len(encoded) <= RESULT_VIEW_CHARACTERS
        ):
            return {"complete": True, "value": preserved}, artifacts
        identifier = context.attach_result_payload(
            {"connection": connection, "source": source, "payload": preserved}
        )
        return {
            "result_id": identifier,
            "complete": False,
            "preview": self._preview(preview if preview is not None else preserved),
            "read": {"action": "read", "result_id": identifier},
        }, artifacts

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
            raise ValueError(RESULT_DENIED)
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
        value = select_pointer(document["payload"], pointer)
        fields = arguments.get("fields")
        offset = arguments.get("offset", 0)
        default_limit = RESULT_READ_CHARACTERS if isinstance(value, str) else RESULT_READ_ENTRIES
        limit = arguments.get("limit", default_limit)
        if fields is not None and not isinstance(value, (dict, list)):
            raise ValueError(READ_INVALID)
        response: dict[str, Any] = {
            "result_id": identifier,
            "pointer": pointer,
            "type": json_kind(value),
        }
        if isinstance(value, str):
            limit = min(limit, RESULT_READ_CHARACTERS)
            while True:
                page = {**response, "value": value[offset : offset + limit]}
                page.update(offset=offset, total=len(value))
                end = min(offset + limit, len(value))
                if end < len(value):
                    page["next"] = {**arguments, "offset": end}
                # Escaped characters can outgrow the view; a shorter page still advances.
                if limit == 1 or _fits(page):
                    return self._bounded_read(page)
                limit //= 2
        if not isinstance(value, (dict, list)):
            if offset or "limit" in arguments:
                raise ValueError(READ_INVALID)
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
            "type": response["type"],
            "complete": False,
            "guidance": READ_TOO_LARGE,
        }


def _fits(response: dict[str, Any]) -> bool:
    return len(json.dumps(response, ensure_ascii=False)) <= RESULT_VIEW_CHARACTERS


def pointer_part(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def select_pointer(value: Any, pointer: str) -> Any:
    if pointer == "":
        return value
    if not pointer.startswith("/") or re.search(r"~(?![01])", pointer):
        raise ValueError(POINTER_INVALID)
    for part in pointer[1:].split("/"):
        key = part.replace("~1", "/").replace("~0", "~")
        try:
            if isinstance(value, dict):
                value = value[key]
            elif isinstance(value, list) and re.fullmatch(r"0|[1-9][0-9]*", key):
                value = value[int(key)]
            else:
                raise ValueError(POINTER_INVALID)
        except (KeyError, IndexError):
            raise ValueError(POINTER_INVALID) from None
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
