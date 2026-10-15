"""Attachment and Skill archive HTTP transfer routes."""

from __future__ import annotations

import logging
from typing import Any

from core.attachments.attachments import (
    AttachmentError,
    AttachmentNotFoundError,
    AttachmentTooLargeError,
    AttachmentTypeNotAllowedError,
)
from core.skills import SKILL_ARCHIVE_MAX_BYTES
from server._http_dependencies import FastAPIType, FileResponse, HTTPException, Request
from server._request_bodies import _parse_upload_file_with_limit, _read_upload_file_with_limit
from server.rpc.errors import RpcError
from server.rpc.skill_methods import install_skill_upload

JsonObject = dict[str, Any]


def register_upload_routes(app: FastAPIType) -> None:
    @app.post("/api/skills/install")
    async def upload_skill(request: Request) -> JsonObject:
        params: JsonObject = dict(request.query_params)
        if set(params) - {"scope", "path", "replace", "dry_run", "expected_sha256"}:
            raise HTTPException(status_code=422, detail="Unsupported Skill upload parameters")
        for key in ("replace", "dry_run"):
            if key in params:
                if params[key] not in {"true", "false"}:
                    raise HTTPException(status_code=422, detail=f"{key} must be true or false")
                params[key] = params[key] == "true"
        file = await _parse_upload_file_with_limit(
            request, max_size_bytes=SKILL_ARCHIVE_MAX_BYTES, upload_kind="Skill archive"
        )
        try:
            data = await _read_upload_file_with_limit(
                file, max_size_bytes=SKILL_ARCHIVE_MAX_BYTES, upload_kind="Skill archive"
            )
            return await install_skill_upload(
                request.app.state, params, data=data, filename=file.filename or "upload.skill"
            )
        except RpcError as error:
            raise HTTPException(status_code=400, detail=error.message) from error
        finally:
            await file.close()

    @app.post("/api/upload")
    async def upload_attachment(request: Request) -> JsonObject:
        attachment_store = request.app.state.runtime.attachment_store
        file = await _parse_upload_file_with_limit(
            request,
            max_size_bytes=attachment_store.max_size_bytes,
            upload_kind="Attachment",
        )
        filename = file.filename or "upload"
        try:
            data = await _read_upload_file_with_limit(
                file,
                max_size_bytes=attachment_store.max_size_bytes,
                upload_kind="Attachment",
            )
            record = await attachment_store.store_async(filename, data)
        except AttachmentTooLargeError as exc:
            raise HTTPException(status_code=413, detail=str(exc)) from exc
        except AttachmentTypeNotAllowedError as exc:
            raise HTTPException(status_code=415, detail=str(exc)) from exc
        finally:
            await file.close()

        return {
            "attachment_id": record.id,
            "filename": record.filename,
            "media_type": record.media_type,
            "size_bytes": record.size_bytes,
        }

    @app.get("/api/attachments/{attachment_id}")
    async def get_attachment(request: Request, attachment_id: str) -> FileResponse:
        attachment_store = request.app.state.runtime.attachment_store
        try:
            record = attachment_store.get(attachment_id)
        except AttachmentNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except AttachmentError as exc:
            # Unreadable stored metadata is an expected storage failure: report the
            # attachment as unavailable without exposing its internal path.
            logging.getLogger("vbot.server.app").warning(
                "Attachment is unavailable (attachment=%s): %s", attachment_id, exc
            )
            raise HTTPException(status_code=404, detail="Attachment is unavailable") from exc
        return FileResponse(
            record.file_path,
            media_type=record.media_type,
            filename=record.filename,
            content_disposition_type="inline",
        )
