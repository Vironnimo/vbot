"""Signed, stateless delivery of original server-local Assistant output files."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.attachments import sniff_media_type

JsonObject = dict[str, Any]

FILE_URL_PREFIX = "/api/files/"
FILE_SNIFF_BYTES = 65_536
MAX_FILE_TOKEN_LENGTH = 16_384
CHAT_IMAGE_MEDIA_TYPES = frozenset(
    {
        "image/avif",
        "image/bmp",
        "image/gif",
        "image/jpeg",
        "image/png",
        "image/webp",
    }
)
BROWSER_MEDIA_TYPES = CHAT_IMAGE_MEDIA_TYPES | {
    "application/pdf",
    "text/plain",
    "text/html",
    "image/svg+xml",
}
ACTIVE_DOCUMENT_TYPES = {"text/html", "image/svg+xml"}
# Scripts can power self-contained reports without inheriting the app's origin.
# HTTPS assets support styles/fonts/chart libraries; application requests, nested
# documents and form submissions are blocked. No filesystem-relative assets.
DOCUMENT_CSP = (
    "sandbox allow-scripts allow-downloads; default-src 'none'; "
    "script-src https: 'unsafe-inline'; style-src https: 'unsafe-inline'; "
    "img-src https: data: blob:; font-src https: data:; media-src https: data: blob:; "
    "connect-src 'none'; frame-src 'none'; object-src 'none'; "
    "base-uri 'none'; form-action 'none'"
)


@dataclass(frozen=True)
class DeliveredFile:
    """Validated current presentation facts for one original file."""

    path: Path
    media_type: str
    inline: bool

    @property
    def response_headers(self) -> dict[str, str]:
        headers = {
            "Cache-Control": "no-cache",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
        }
        if self.media_type in ACTIVE_DOCUMENT_TYPES:
            headers["Content-Security-Policy"] = DOCUMENT_CSP
        return headers


class FileDelivery:
    """Mint and verify filesystem capability URLs without storing file bytes or IDs."""

    def __init__(self, *, secret: bytes | None = None) -> None:
        self._secret = secret if secret is not None else secrets.token_bytes(32)
        if not self._secret:
            raise ValueError("file delivery secret must not be empty")

    def project_message(self, message: JsonObject) -> JsonObject:
        """Replace recognized Assistant file markers with fresh public Markdown URLs."""
        projected = dict(message)
        image_files = projected.pop("image_files", None)
        if isinstance(image_files, list):
            projected["images"] = self._project_image_files(image_files)
        content = projected.get("content")
        references = projected.pop("output_files", None)
        if (
            projected.get("role") != "assistant"
            or not isinstance(content, str)
            or not isinstance(references, list)
        ):
            return projected

        lines = content.splitlines(keepends=True)
        replacements_by_line: dict[int, list[tuple[int | None, int | None, str]]] = {}
        for reference in references:
            if not isinstance(reference, dict):
                continue
            line_index = reference.get("line_index")
            path_value = reference.get("path")
            start_index = reference.get("start_index")
            end_index = reference.get("end_index")
            if (
                isinstance(line_index, bool)
                or not isinstance(line_index, int)
                or line_index < 0
                or line_index >= len(lines)
                or not isinstance(path_value, str)
            ):
                continue
            presentation = self._presentation_for_path(path_value)
            if presentation is None:
                continue
            if start_index is None and end_index is None:
                span: tuple[int | None, int | None] = (None, None)
            elif (
                isinstance(start_index, int)
                and not isinstance(start_index, bool)
                and isinstance(end_index, int)
                and not isinstance(end_index, bool)
            ):
                span = (start_index, end_index)
            else:
                continue
            token = self._mint_token(presentation.path)
            label = _escape_markdown_label(presentation.path.name)
            markdown = (
                f"![{label}]({FILE_URL_PREFIX}{token})"
                if presentation.media_type in CHAT_IMAGE_MEDIA_TYPES
                else f"[{label}]({FILE_URL_PREFIX}{token})"
            )
            replacements_by_line.setdefault(line_index, []).append(
                (
                    span[0],
                    span[1],
                    markdown,
                )
            )
        for line_index, replacements in replacements_by_line.items():
            lines[line_index] = _apply_line_replacements(lines[line_index], replacements)
        projected["content"] = "".join(lines)
        return projected

    def _project_image_files(self, references: list[Any]) -> list[JsonObject]:
        """Expose original Tool image paths without probing or copying their bytes.

        Missing originals still get URLs: the ordinary 404 lets the UI render
        its unavailable-image placeholder, including after a server restart.
        """
        images: list[JsonObject] = []
        for reference in references:
            if not isinstance(reference, dict):
                continue
            path_value = reference.get("path")
            if not isinstance(path_value, str) or "\0" in path_value:
                continue
            path = Path(path_value)
            if not path.is_absolute():
                continue
            images.append(
                {"url": f"{FILE_URL_PREFIX}{self._mint_token(path)}", "filename": path.name}
            )
        return images

    def resolve_token(self, token: str) -> DeliveredFile | None:
        """Verify one capability and return the original file's current facts."""
        if not token or len(token) > MAX_FILE_TOKEN_LENGTH:
            return None
        payload, separator, signature = token.partition(".")
        if not separator or not payload or not signature:
            return None
        try:
            payload_bytes = payload.encode("ascii")
        except UnicodeEncodeError:
            return None
        expected_signature = _urlsafe_encode(
            hmac.digest(self._secret, payload_bytes, hashlib.sha256)
        )
        if not hmac.compare_digest(signature, expected_signature):
            return None
        try:
            path_text = _urlsafe_decode(payload).decode("utf-8")
            path = Path(path_text)
            if not path.is_absolute():
                return None
            resolved = path.resolve(strict=True)
        except (OSError, RuntimeError, UnicodeDecodeError, ValueError):
            return None
        return self._presentation_for_path(str(resolved))

    def _mint_token(self, path: Path) -> str:
        payload = _urlsafe_encode(str(path).encode("utf-8"))
        signature = _urlsafe_encode(
            hmac.digest(self._secret, payload.encode("ascii"), hashlib.sha256)
        )
        return f"{payload}.{signature}"

    @staticmethod
    def _presentation_for_path(path_value: str) -> DeliveredFile | None:
        try:
            path = Path(path_value)
            if not path.is_absolute():
                return None
            resolved = path.resolve(strict=True)
            if not resolved.is_file():
                return None
            with resolved.open("rb") as file_handle:
                probe = file_handle.read(FILE_SNIFF_BYTES)
        except (OSError, RuntimeError, ValueError):
            return None
        # A bounded probe may split the final UTF-8 character of a large report.
        if len(probe) == FILE_SNIFF_BYTES:
            try:
                probe.decode("utf-8")
            except UnicodeDecodeError as exc:
                if exc.reason == "unexpected end of data":
                    probe = probe[: exc.start]
        media_type = sniff_media_type(probe, resolved.name)
        if media_type == "text/plain" and b"\0" in probe:
            media_type = "application/octet-stream"
        # The storage sniffer intentionally labels all UTF-8 source as plain text.
        # Refine only verified text here; never let a suffix override binary magic.
        if media_type == "text/plain":
            media_type = {
                ".html": "text/html",
                ".htm": "text/html",
                ".svg": "image/svg+xml",
            }.get(resolved.suffix.lower(), media_type)
        return DeliveredFile(
            path=resolved,
            media_type=media_type,
            inline=(
                media_type in BROWSER_MEDIA_TYPES or media_type.startswith(("audio/", "video/"))
            ),
        )


def _urlsafe_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _urlsafe_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.b64decode(value + padding, altchars=b"-_", validate=True)


def _escape_markdown_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")


def _line_ending(value: str) -> str:
    if value.endswith("\r\n"):
        return "\r\n"
    if value.endswith(("\r", "\n")):
        return value[-1]
    return ""


def _apply_line_replacements(
    line: str,
    replacements: list[tuple[int | None, int | None, str]],
) -> str:
    ending = _line_ending(line)
    body = line[: -len(ending)] if ending else line
    legacy = [replacement for replacement in replacements if replacement[0] is None]
    if legacy:
        return legacy[-1][2] + ending

    for start_index, end_index, markdown in sorted(
        replacements,
        key=lambda replacement: int(replacement[0] or 0),
        reverse=True,
    ):
        if (
            start_index is None
            or end_index is None
            or start_index < 0
            or end_index <= start_index
            or end_index > len(body)
        ):
            continue
        body = body[:start_index] + markdown + body[end_index:]
    return body + ending
