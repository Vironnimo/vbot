"Preserve complete MCP payloads while moving binary bytes into durable files."

from __future__ import annotations

import base64
import copy
import json
import mimetypes
import re
import uuid
from pathlib import Path
from typing import Any

from core.attachments import AttachmentTooLargeError, AttachmentTypeNotAllowedError
from core.extensions.operations import ExtensionHost
from core.tools.tools import ToolContext, read_media_artifact, run_tool_worker

from .config import atomic_json

RESULT_VIEW_CHARACTERS = 6000
RESULT_PREVIEW_CHARACTERS = 400
RESULT_READ_ENTRIES = 20
RESULT_READ_CHARACTERS = 2000
RESULT_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")

RESULT_MISSING = (
    "Saved MCP result is unavailable. Use an existing result_id returned by this connection."
)

RESULT_DENIED = "This Agent cannot read this saved MCP result."

POINTER_INVALID = "Invalid JSON Pointer. Use a pointer returned by read."

READ_INVALID = "These read options do not apply to the selected value."


class ContentStore:
    def __init__(self, host: ExtensionHost, directory: Path) -> None:
        self.host = host
        self.directory = directory

    async def preserve(
        self, payload: dict[str, Any]
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        artifacts: list[dict[str, Any]] = []
        result = await self._visit(copy.deepcopy(payload), artifacts)
        return result, artifacts

    async def _visit(self, value: Any, artifacts: list[dict[str, Any]]) -> Any:
        if isinstance(value, list):
            return [await self._visit(item, artifacts) for item in value]
        if not isinstance(value, dict):
            return value
        field = self._binary_field(value)
        if field is not None:
            encoded = value[field]
            raw = base64.b64decode(encoded, validate=True)
            media_type = value.get("mimeType", "application/octet-stream")
            suffix = mimetypes.guess_extension(media_type) or ".bin"
            filename = f"mcp-{uuid.uuid4()}{suffix}"
            try:
                record = await run_tool_worker(self.host.store_attachment, filename, raw)
            except (AttachmentTooLargeError, AttachmentTypeNotAllowedError) as error:
                path = self.directory / filename
                await run_tool_worker(self._save, path, raw)
                value["path"] = path.as_posix()
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
        return {key: await self._visit(item, artifacts) for key, item in value.items()}

    @staticmethod
    def _binary_field(value: dict[str, Any]) -> str | None:
        if value.get("type") in {"image", "audio"} and isinstance(value.get("data"), str):
            return "data"
        if "uri" in value and isinstance(value.get("blob"), str):
            return "blob"
        return None

    @staticmethod
    def _save(path: Path, raw: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)

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
        identifier = uuid.uuid4().hex
        path = self.directory / "results" / f"{identifier}.json"
        document = {
            "owner": {"agent_id": context.agent_id, "project_id": context.project_id},
            "connection": connection,
            "source": source,
            "payload": preserved,
        }
        await run_tool_worker(atomic_json, path, document)
        encoded = json.dumps(preserved, ensure_ascii=False, separators=(",", ":"))
        complete = preview is None and len(encoded) <= RESULT_VIEW_CHARACTERS
        view = (
            preserved if complete else self._preview(preview if preview is not None else preserved)
        )
        return {
            "result_id": identifier,
            "result_file": path.as_posix(),
            "complete": complete,
            "value" if complete else "preview": view,
            "read": {"action": "read", "result_id": identifier},
        }, artifacts

    async def load_result(
        self,
        identifier: str,
        context: ToolContext,
        connection: str,
    ) -> dict[str, Any]:
        if not RESULT_ID_PATTERN.fullmatch(identifier):
            raise ValueError(RESULT_MISSING)
        path = self.directory / "results" / f"{identifier}.json"
        try:
            document = await run_tool_worker(self._load_json, path)
        except FileNotFoundError:
            raise ValueError(RESULT_MISSING) from None
        owner = {"agent_id": context.agent_id, "project_id": context.project_id}
        if document["owner"] != owner or document["connection"] != connection:
            raise ValueError(RESULT_DENIED)
        return document

    @staticmethod
    def _load_json(path: Path) -> dict[str, Any]:
        return dict(json.loads(path.read_text(encoding="utf-8")))

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
            response.update(value=value[offset : offset + limit], offset=offset, total=len(value))
            end = min(offset + limit, len(value))
            if end < len(value):
                response["next"] = {**arguments, "offset": end}
            return self._bounded_read(response)
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

    def _bounded_read(self, response: dict[str, Any]) -> dict[str, Any]:
        if len(json.dumps(response, ensure_ascii=False)) <= RESULT_VIEW_CHARACTERS:
            return response
        identifier = response["result_id"]
        return {
            "result_id": identifier,
            "result_file": (self.directory / "results" / f"{identifier}.json").as_posix(),
            "type": response["type"],
            "complete": False,
        }


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
