"""Attachment storage public API."""

from importlib import import_module
from typing import TYPE_CHECKING, Any

from core.attachments.attachments import (
    AttachmentError,
    AttachmentNotFoundError,
    AttachmentRecord,
    AttachmentStore,
    AttachmentTooLargeError,
    AttachmentTypeNotAllowedError,
    canonical_extension_for_media_type,
    sniff_media_type,
    validate_attachment_metadata_file,
)

if TYPE_CHECKING:
    from core.attachments.images import ImageTarget


__all__ = [
    "AttachmentError",
    "AttachmentNotFoundError",
    "AttachmentRecord",
    "AttachmentStore",
    "AttachmentTooLargeError",
    "AttachmentTypeNotAllowedError",
    "ImageTarget",
    "canonical_extension_for_media_type",
    "sniff_media_type",
    "validate_attachment_metadata_file",
]


def __getattr__(name: str) -> Any:
    if name != "ImageTarget":
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module("core.attachments.images"), name)
    globals()[name] = value
    return value
