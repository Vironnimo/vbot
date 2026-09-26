"""Image URLs and data: URLs for analyze_image: download, check and store.

Web addresses go through the guarded public transport of web_fetch
(``_public_http.fetch_public``), so private and local network addresses stay
unreachable. Downloads run concurrently and are stored as attachments only after
every requested image arrived as an image; a failed call leaves nothing behind.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, unquote_to_bytes, urlparse

from core.attachments import AttachmentError, sniff_media_type
from core.tools._public_http import PublicFetchError, fetch_public

_IMAGE_ACCEPT = "image/avif,image/webp,image/apng,image/*,*/*;q=0.8"
_MARKDOWN_LINK = re.compile(r"!?\[[^\]]*\]\((\S+?)\)")
_WRAPPERS = {("<", ">"), ('"', '"'), ("'", "'"), ("`", "`")}
# "https://", and copies that lost or doubled a slash ("https:/host").
_WEB_SCHEME = re.compile(r"(https?):/*(?=[^/])", re.IGNORECASE)
_DATA_URL = re.compile(r"data:([^,]*),(.*)", re.IGNORECASE | re.DOTALL)
_DATA_URL_SUBJECT = "The data: URL"
_FORMATS = "PNG, JPEG, GIF or WebP"


class ImageDownloadError(Exception):
    """A requested image address did not yield an image; the message says why."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        attempts_made: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.attempts_made = attempts_made


@dataclass(frozen=True)
class _Image:
    data: bytes
    filename: str


def image_address(text: str) -> str | None:
    """Return the web or data: address an image item means, or None for a local path.

    Copies wrapped in <>, quotes, backticks or a Markdown link, and web schemes that
    lost a slash, still count as the address. A schemeless text stays a local path.
    """
    candidate = text.strip()
    link = _MARKDOWN_LINK.fullmatch(candidate)
    if link is not None:
        candidate = link[1]
    while len(candidate) >= 2 and (candidate[0], candidate[-1]) in _WRAPPERS:
        candidate = candidate[1:-1].strip()
    if candidate[:5].casefold() == "data:":
        return candidate
    scheme = _WEB_SCHEME.match(candidate)
    if scheme is not None:
        return f"{scheme[1].lower()}://{candidate[scheme.end() :]}"
    return None


def _size_label(size: int) -> str:
    megabytes, remainder = divmod(size, 1024 * 1024)
    return f"{megabytes} MB" if megabytes and not remainder else f"{size:,} bytes"


def _too_large(max_bytes: int) -> ImageDownloadError:
    return ImageDownloadError(
        "image_too_large",
        f"{_DATA_URL_SUBJECT} holds more than {_size_label(max_bytes)} of data; an image can "
        f"be at most {_size_label(max_bytes)}. Pass a smaller image.",
    )


def _decode_data_url(address: str, max_bytes: int) -> tuple[bytes, str]:
    """Return the bytes and declared media type of a base64 or percent-encoded data: URL."""
    match = _DATA_URL.fullmatch(address)
    if match is None:
        raise ImageDownloadError(
            "invalid_arguments",
            f"{_DATA_URL_SUBJECT} is incomplete: it needs the form "
            "data:image/png;base64,<data>, with a comma before the data.",
        )
    parameters = [part.strip().casefold() for part in match[1].split(";")]
    declared, payload = parameters[0], match[2]
    if "base64" in parameters[1:]:
        text = "".join(unquote(payload).split())
        if len(text) > max_bytes * 4 // 3 + 4:
            raise _too_large(max_bytes)
        altchars = b"-_" if ("-" in text or "_" in text) else None
        try:
            data = base64.b64decode(text + "=" * (-len(text) % 4), altchars, validate=True)
        except (binascii.Error, ValueError) as error:
            raise ImageDownloadError(
                "invalid_arguments",
                f"{_DATA_URL_SUBJECT} has damaged base64 data; it may have been cut off. Pass "
                "the complete data: URL, or the image file's path.",
            ) from error
    else:
        if len(payload) > max_bytes * 3:
            raise _too_large(max_bytes)
        data = unquote_to_bytes(payload)
    if len(data) > max_bytes:
        raise _too_large(max_bytes)
    return data, declared


def _looks_like(data: bytes, *markers: bytes) -> bool:
    head = data[:512].lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    return any(head.startswith(marker) or marker in head[:300] for marker in markers)


def _not_an_image(url: str | None, declared: str, sniffed: str, data: bytes) -> str:
    """Say what arrived instead of an image and what to pass instead."""
    returned = f"{url} returned" if url else f"{_DATA_URL_SUBJECT} holds"
    if not data:
        if url:
            return f"{url} returned an empty response, not an image. Try another source."
        return f"{_DATA_URL_SUBJECT} holds no data."
    if "svg" in declared or _looks_like(data, b"<svg"):
        subject = f"{url} is" if url else f"{_DATA_URL_SUBJECT} holds"
        return (
            f"{subject} an SVG drawing, which cannot be analyzed. Pass a {_FORMATS} image instead."
        )
    if "html" in declared or _looks_like(data, b"<!doctype html", b"<html"):
        if not url:
            return f"{_DATA_URL_SUBJECT} holds a web page, not an image."
        return (
            f"{url} returned a web page, not an image. Pass the address of the image file "
            "itself, not of a page that shows it; the site may also show an access check "
            "instead of the image."
        )
    kind = declared or sniffed
    return f"{returned} {kind} content, not an image. Pass an image file such as {_FORMATS}."


def _checked(data: bytes, declared: str, url: str | None, filename: str) -> _Image:
    sniffed = sniff_media_type(data, filename)
    if not sniffed.startswith("image/"):
        raise ImageDownloadError("not_an_image", _not_an_image(url, declared, sniffed, data))
    return _Image(data, filename)


def _filename(url: str) -> str:
    name = unquote(urlparse(url).path.rsplit("/", 1)[-1]).strip()
    return name or "image"


async def _download(address: str, max_bytes: int) -> _Image:
    if address[:5].casefold() == "data:":
        data, declared = _decode_data_url(address, max_bytes)
        return _checked(data, declared, None, "image")
    try:
        response = await fetch_public(
            address, accept=_IMAGE_ACCEPT, max_bytes=max_bytes, what="image"
        )
    except PublicFetchError as error:
        raise ImageDownloadError(
            error.code, str(error), retryable=error.retryable, attempts_made=error.attempts_made
        ) from error
    declared = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    return _checked(response.content, declared, address, _filename(response.url))


async def _attempt(address: str, max_bytes: int) -> _Image | ImageDownloadError:
    try:
        return await _download(address, max_bytes)
    except ImageDownloadError as error:
        return error


async def download_images(
    addresses: dict[int, str], *, field: str, attachment_store: Any, several: bool
) -> dict[int, tuple[Path, str]]:
    """Download the images at ``addresses`` (keyed by argument index) and store them.

    Returns each stored file with its display name, taken from the address. Every
    failure is reported together, each prefixed with its argument index when the
    call names several images. Nothing is stored unless every image arrived.
    """
    if not addresses:
        return {}
    max_bytes = attachment_store.max_size_bytes
    outcomes = await asyncio.gather(
        *(_attempt(address, max_bytes) for address in addresses.values())
    )
    failures = [
        (index, outcome)
        for index, outcome in zip(addresses, outcomes, strict=True)
        if isinstance(outcome, ImageDownloadError)
    ]
    if failures:
        raise _combined(failures, field, several)
    images = [outcome for outcome in outcomes if isinstance(outcome, _Image)]
    stored: dict[int, tuple[Path, str]] = {}
    for index, image in zip(addresses, images, strict=True):
        try:
            record = await attachment_store.store_async(image.filename, image.data)
        except AttachmentError as error:
            prefix = f"{field}[{index}]: " if several else ""
            raise ImageDownloadError(
                "storage_error",
                f"{prefix}The downloaded image could not be saved ({error}). Check available "
                "disk space and retry.",
            ) from error
        stored[index] = (Path(record.file_path), record.filename)
    return stored


def _combined(
    failures: list[tuple[int, ImageDownloadError]], field: str, several: bool
) -> ImageDownloadError:
    if not several:
        return failures[0][1]
    codes = {error.code for _, error in failures}
    return ImageDownloadError(
        codes.pop() if len(codes) == 1 else "image_download_failed",
        "\n".join(f"{field}[{index}]: {error}" for index, error in failures),
        retryable=all(error.retryable for _, error in failures),
        attempts_made=failures[0][1].attempts_made if len(failures) == 1 else None,
    )
