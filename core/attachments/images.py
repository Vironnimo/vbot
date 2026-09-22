"""Bounded image validation and preparation, independent of Providers and Sessions.

Callers supply accepted MIME types and known encoded-file byte ceilings. Original
files are never rewritten; results explain changed copies and any loss of detail.
"""

from __future__ import annotations

import hashlib
import io
import math
import struct
import warnings
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from PIL import Image, ImageCms, ImageOps
from pillow_heif import register_heif_opener

from core.attachments.attachments import AttachmentError
from core.utils.workers import BoundedWorkerPool

# Never silently substitute an embedded thumbnail for the main image.
register_heif_opener(thumbnails=False)

_WORKERS = BoundedWorkerPool(name="image-conversion", max_workers=1)
_CACHE_BYTES = 32 * 1024 * 1024
_VALIDATION_CACHE_SIZE = 256
_PIXEL_LIMIT = 32_000_000
_FORMATS = {"image/png": "PNG", "image/webp": "WEBP", "image/jpeg": "JPEG"}
_SOURCE_FORMATS = {
    **_FORMATS,
    "image/gif": "GIF",
    "image/bmp": "BMP",
    "image/tiff": "TIFF",
    "image/avif": "AVIF",
    "image/heic": "HEIF",
    "image/heif": "HEIF",
}
_DECODE_ERRORS = (OSError, ValueError, SyntaxError, EOFError, struct.error)


class ImageConversionError(AttachmentError):
    """A classified preparation failure with actionable recovery."""

    def __init__(
        self,
        reason: str = "conversion_failed",
        *,
        max_bytes: int | None = None,
        pixels: int | None = None,
    ) -> None:
        self.reason = reason
        self.max_bytes = max_bytes
        self.pixels = pixels
        self.max_pixels = _PIXEL_LIMIT
        messages = {
            "invalid_image": (
                "The image data is damaged or unreadable. Obtain a fresh copy or open and "
                "export the original in an image editor before trying again."
            ),
            "image_too_large": (
                f"The image exceeds the decoding limit of {_PIXEL_LIMIT:,} pixels "
                "across its frames or pages. Export a smaller image or crop the relevant "
                "area before trying again; changing the file format alone will not help."
            ),
            "multiple_frames": (
                "The image contains multiple frames or pages and needs conversion or "
                "size reduction. Export the relevant frames or pages as separate PNG or "
                "JPEG files; no frame or page has been discarded automatically."
            ),
            "unsupported_target": (
                "This destination cannot receive the image in an available output format. "
                "Use a destination that accepts PNG, WebP, or JPEG images."
            ),
            "output_too_large": (
                f"The image could not be prepared within the {max_bytes} byte limit. "
                "Export a smaller image or crop the relevant area before trying again."
            ),
            "conversion_failed": (
                "The image could not be encoded in a supported format. Open the original "
                "in an image editor and export a standard PNG or JPEG copy."
            ),
        }
        super().__init__(messages[reason])


@dataclass(frozen=True)
class PreparedImage:
    """Encoded pixels and facts needed to explain a changed copy."""

    data: bytes
    media_type: str
    original_size: tuple[int, int]
    size: tuple[int, int]
    reencoded: bool = False
    lossy: bool = False

    @property
    def note(self) -> str | None:
        if not self.reencoded:
            return None
        details = [f"a converted copy is shown as {self.media_type}"]
        if self.size != self.original_size:
            details.append(
                f"resized from {self.original_size[0]}x{self.original_size[1]} "
                f"to {self.size[0]}x{self.size[1]} pixels to fit the byte limit"
            )
        if self.lossy:
            details.append("lossy compression was used, so fine details may be reduced")
        details.append("the original file is unchanged")
        return "; ".join(details)


@dataclass(frozen=True)
class _ImageInfo:
    size: tuple[int, int]
    frames: int
    pixels: int


class _EncodingTooLargeError(Exception):
    def __init__(self, size: int) -> None:
        self.size = size


class _OutputBuffer(io.BytesIO):
    """Bound retained bytes while counting sequential PNG/WebP/JPEG output."""

    def __init__(self, max_bytes: int | None) -> None:
        super().__init__()
        self.max_bytes = max_bytes
        self.encoded_size = 0

    def write(self, data: Any) -> int:
        self.encoded_size += len(data)
        if self.max_bytes is not None and self.encoded_size > self.max_bytes:
            return len(data)
        return super().write(data)


class ImageConverter:
    """Validate every image; preserve accepted bytes or prepare a bounded copy.

    Validation caches retain only hashes and dimensions. Converted-output caches
    are byte-bounded and include destination formats and byte ceiling in the key.
    All inspection, decoding, encoding and cache work uses one bounded worker.
    """

    def __init__(self) -> None:
        self._cache: OrderedDict[tuple[Any, ...], PreparedImage] = OrderedDict()
        self._cache_bytes = 0
        self._validated: OrderedDict[tuple[bytes, str], _ImageInfo] = OrderedDict()

    async def convert(
        self,
        data: bytes,
        media_type: str,
        supported_types: frozenset[str],
        *,
        max_output_bytes: int | None = None,
    ) -> PreparedImage:
        if max_output_bytes is not None and max_output_bytes <= 0:
            raise ValueError("max_output_bytes must be positive")
        return await _WORKERS.run(
            self._prepare, data, media_type, supported_types, max_output_bytes
        )

    def _prepare(
        self,
        data: bytes,
        media_type: str,
        supported_types: frozenset[str],
        max_bytes: int | None,
    ) -> PreparedImage:
        digest = hashlib.sha256(data).digest()
        info = self._validate(data, media_type, digest)
        if media_type in supported_types and (max_bytes is None or len(data) <= max_bytes):
            return PreparedImage(data, media_type, info.size, info.size)
        targets = tuple(kind for kind in _FORMATS if kind in supported_types)
        if not targets:
            raise ImageConversionError("unsupported_target")
        if info.frames != 1:
            raise ImageConversionError("multiple_frames")
        key = (digest, media_type, targets, max_bytes)
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]
        try:
            with Image.open(io.BytesIO(data)) as original:
                converted = ImageOps.exif_transpose(original)
                profile = original.info.get("icc_profile")
                if converted.mode == "CMYK" and profile:
                    profiled = ImageCms.profileToProfile(
                        converted,
                        io.BytesIO(profile),
                        ImageCms.createProfile("sRGB"),
                        outputMode="RGB",
                    )
                    assert profiled is not None
                    converted = profiled
                    profile = converted.info.get("icc_profile")
                result = self._fit(converted, targets, max_bytes, profile)
        except (*_DECODE_ERRORS, ImageCms.PyCMSError) as exc:
            raise ImageConversionError() from exc
        if len(result.data) <= _CACHE_BYTES:
            while self._cache_bytes + len(result.data) > _CACHE_BYTES:
                _, evicted = self._cache.popitem(last=False)
                self._cache_bytes -= len(evicted.data)
            self._cache[key] = result
            self._cache_bytes += len(result.data)
        return result

    def _validate(self, data: bytes, media_type: str, digest: bytes) -> _ImageInfo:
        key = (digest, media_type)
        if key in self._validated:
            self._validated.move_to_end(key)
            info = self._validated[key]
            if info.pixels > _PIXEL_LIMIT:
                raise ImageConversionError("image_too_large", pixels=info.pixels)
            return info
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(data)) as image:
                    if image.format != _SOURCE_FORMATS.get(media_type):
                        raise ImageConversionError("invalid_image")
                    if image.width * image.height > _PIXEL_LIMIT:
                        raise ImageConversionError(
                            "image_too_large", pixels=image.width * image.height
                        )
                    image.verify()
                with Image.open(io.BytesIO(data)) as image:
                    size = image.size
                    frames = getattr(image, "n_frames", 1)
                    pixels = 0
                    for frame in range(frames):
                        image.seek(frame)
                        pixels += image.width * image.height
                        if pixels > _PIXEL_LIMIT:
                            raise ImageConversionError("image_too_large", pixels=pixels)
                        image.load()
                    info = _ImageInfo(size, frames, pixels)
        except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
            raise ImageConversionError("image_too_large") from exc
        except _DECODE_ERRORS as exc:
            raise ImageConversionError("invalid_image") from exc
        self._validated[key] = info
        while len(self._validated) > _VALIDATION_CACHE_SIZE:
            self._validated.popitem(last=False)
        return info

    @staticmethod
    def _fit(
        original: Image.Image,
        targets: tuple[str, ...],
        max_bytes: int | None,
        profile: bytes | None,
    ) -> PreparedImage:
        image = original
        # Full-size lossless output precedes JPEG. Only a concrete byte ceiling
        # permits lower quality or resampling, always from the original pixels.
        for attempt in range(9):
            smallest_overflow: int | None = None
            for target in targets:
                qualities = (95, 85, 75) if target == "image/jpeg" and max_bytes else (95,)
                for quality in qualities:
                    try:
                        data = _encode(image, target, quality, profile, max_bytes)
                    except _EncodingTooLargeError as exc:
                        smallest_overflow = min(smallest_overflow or exc.size, exc.size)
                        continue
                    return PreparedImage(
                        data,
                        target,
                        original.size,
                        image.size,
                        reencoded=True,
                        lossy=target == "image/jpeg",
                    )
            if max_bytes is None or image.size == (1, 1) or attempt == 8:
                break
            factor = min(0.85, math.sqrt(max_bytes / (smallest_overflow or max_bytes)) * 0.9)
            size = (max(1, int(image.width * factor)), max(1, int(image.height * factor)))
            image = original.resize(size, Image.Resampling.LANCZOS)
        raise ImageConversionError("output_too_large", max_bytes=max_bytes)


def _encode(
    image: Image.Image,
    media_type: str,
    quality: int,
    profile: bytes | None,
    max_bytes: int | None,
) -> bytes:
    options: dict[str, Any] = {}
    if profile:
        options["icc_profile"] = profile
    if media_type == "image/jpeg":
        rgba = image.convert("RGBA")
        background = Image.new("RGB", rgba.size, "white")
        background.paste(rgba, mask=rgba.getchannel("A"))
        image = background
        options.update(quality=quality, subsampling=0, optimize=True)
    elif image.mode not in {"1", "L", "LA", "P", "RGB", "RGBA", "I;16"}:
        image = image.convert("RGB")
    if media_type == "image/webp":
        options.update(lossless=True, exact=True)
    with _OutputBuffer(max_bytes) as output:
        image.save(output, format=_FORMATS[media_type], **options)
        if max_bytes is not None and output.encoded_size > max_bytes:
            raise _EncodingTooLargeError(output.encoded_size)
        return output.getvalue()
