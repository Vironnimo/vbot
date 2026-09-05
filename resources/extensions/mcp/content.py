"""Preserve complete MCP payloads while moving binary bytes into durable files."""

from __future__ import annotations

import base64
import copy
import mimetypes
import uuid
from pathlib import Path
from typing import Any

from core.attachments import AttachmentTooLargeError, AttachmentTypeNotAllowedError
from core.extensions.operations import ExtensionHost
from core.tools.tools import read_media_artifact, run_tool_worker


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
