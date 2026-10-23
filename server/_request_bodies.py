"""Bounded JSON and multipart ingress shared by the HTTP routes."""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from typing import Any, cast, override

from server._http_dependencies import (
    HTTPException,
    MultiPartException,
    MultipartParseError,
    MultiPartParser,
    Request,
    StarletteUploadFile,
    UploadFile,
)

UPLOAD_READ_CHUNK_SIZE_BYTES = 1_048_576

MULTIPART_BODY_OVERHEAD_ALLOWANCE_BYTES = 65_536

MULTIPART_MAX_FORM_FIELDS = 16

JSON_MEDIA_TYPE = "application/json"

JSON_REQUEST_BODY_MAX_BYTES = 1_048_576


class _UploadTooLargeMultipartError(MultiPartException):  # type: ignore[misc]
    """Abort multipart parsing before an oversized file part is spooled."""


class _SizeLimitedMultiPartParser(MultiPartParser):  # type: ignore[misc]
    """Starlette multipart parser with an exact per-file byte limit."""

    def __init__(
        self,
        *args: Any,
        max_file_size_bytes: int,
        upload_kind: str,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._max_file_size_bytes = max_file_size_bytes
        self._upload_kind = upload_kind
        self._current_file_size_bytes = 0

    @override
    def on_part_begin(self) -> None:
        super().on_part_begin()
        self._current_file_size_bytes = 0

    @override
    def on_part_data(self, data: bytes, start: int, end: int) -> None:
        part_size_bytes = end - start
        if self._current_part.file is not None:
            next_size_bytes = self._current_file_size_bytes + part_size_bytes
            if next_size_bytes > self._max_file_size_bytes:
                raise _UploadTooLargeMultipartError(
                    f"{self._upload_kind} size exceeds limit {self._max_file_size_bytes}"
                )
            self._current_file_size_bytes = next_size_bytes
        super().on_part_data(data, start, end)

    def close_unclaimed_files(self, claimed: UploadFile | None) -> None:
        """Close spools even when cancellation or an incomplete part bypasses FormData."""
        for file in self._files_to_close_on_error:
            if claimed is None or file is not claimed.file:
                file.close()


def _require_json_media_type(request: Request) -> None:
    content_type = request.headers.get("content-type")
    media_type = content_type.partition(";")[0].strip().casefold() if content_type else ""
    if media_type != JSON_MEDIA_TYPE:
        raise HTTPException(
            status_code=415,
            detail="Content-Type must be application/json",
        )


async def _read_json_body_with_limit(request: Request) -> bytes:
    """Read a JSON request body incrementally without allowing unbounded buffering."""
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared_size = int(content_length)
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail="Content-Length must be an integer"
            ) from exc
        if declared_size < 0:
            raise HTTPException(status_code=400, detail="Content-Length must not be negative")
        if declared_size > JSON_REQUEST_BODY_MAX_BYTES:
            raise HTTPException(status_code=413, detail="Request body exceeds size limit")

    chunks: list[bytes] = []
    size_bytes = 0
    async for chunk in request.stream():
        size_bytes += len(chunk)
        if size_bytes > JSON_REQUEST_BODY_MAX_BYTES:
            raise HTTPException(status_code=413, detail="Request body exceeds size limit")
        chunks.append(chunk)
    return b"".join(chunks)


async def _read_json_payload_with_limit(request: Request) -> object:
    body = await _read_json_body_with_limit(request)
    try:
        return json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError("Request body must be valid JSON") from exc


async def _read_upload_file_with_limit(
    file: UploadFile,
    *,
    max_size_bytes: int,
    upload_kind: str,
) -> bytes:
    chunks: list[bytes] = []
    size_bytes = 0
    while True:
        read_size = min(UPLOAD_READ_CHUNK_SIZE_BYTES, max_size_bytes - size_bytes + 1)
        chunk = await file.read(read_size)
        if not chunk:
            return b"".join(chunks)
        size_bytes += len(chunk)
        if size_bytes > max_size_bytes:
            raise HTTPException(
                status_code=413,
                detail=f"{upload_kind} size {size_bytes} exceeds limit {max_size_bytes}",
            )
        chunks.append(chunk)


async def _parse_upload_file_with_limit(
    request: Request,
    *,
    max_size_bytes: int,
    upload_kind: str,
) -> UploadFile:
    content_type = request.headers.get("content-type", "")
    media_type = content_type.partition(";")[0].strip().casefold()
    if media_type != "multipart/form-data":
        raise HTTPException(status_code=422, detail="multipart file field 'file' is required")

    max_body_size_bytes = max_size_bytes + MULTIPART_BODY_OVERHEAD_ALLOWANCE_BYTES
    content_length = _request_content_length(request)
    if content_length is not None and content_length > max_body_size_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"{upload_kind} request body exceeds limit {max_body_size_bytes}",
        )

    parser = _SizeLimitedMultiPartParser(
        request.headers,
        _stream_request_body_with_limit(
            request,
            max_body_size_bytes=max_body_size_bytes,
            upload_kind=upload_kind,
        ),
        max_files=1,
        max_fields=MULTIPART_MAX_FORM_FIELDS,
        max_part_size=MULTIPART_BODY_OVERHEAD_ALLOWANCE_BYTES,
        max_file_size_bytes=max_size_bytes,
        upload_kind=upload_kind,
    )
    claimed: UploadFile | None = None
    try:
        try:
            form = await parser.parse()
        except _UploadTooLargeMultipartError as exc:
            raise HTTPException(status_code=413, detail=exc.message) from exc
        except MultiPartException as exc:
            raise HTTPException(status_code=400, detail=exc.message) from exc
        except MultipartParseError as exc:
            raise HTTPException(status_code=400, detail="Malformed multipart body") from exc

        file = form.get("file")
        if not isinstance(file, StarletteUploadFile):
            raise HTTPException(status_code=422, detail="multipart file field 'file' is required")
        claimed = cast(UploadFile, file)
        return claimed
    finally:
        parser.close_unclaimed_files(claimed)


def _request_content_length(request: Request) -> int | None:
    value = request.headers.get("content-length")
    if value is None:
        return None
    try:
        content_length = int(value)
    except ValueError:
        return None
    return content_length if content_length >= 0 else None


async def _stream_request_body_with_limit(
    request: Request,
    *,
    max_body_size_bytes: int,
    upload_kind: str,
) -> AsyncGenerator[bytes]:
    received_size_bytes = 0
    async for chunk in request.stream():
        received_size_bytes += len(chunk)
        if received_size_bytes > max_body_size_bytes:
            raise _UploadTooLargeMultipartError(
                f"{upload_kind} request body exceeds limit {max_body_size_bytes}"
            )
        yield chunk
