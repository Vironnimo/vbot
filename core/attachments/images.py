"""Target-format image conversion shared by Chat and isolated image analysis.

AttachmentStore remains the original-blob storage owner. Chat's block resolver
cannot own this reusable operation: isolated task services are below Chat and
must not import its runtime projection or Model-facing wording. This module owns
conversion, bounded execution and caching without knowing Providers or Sessions.
"""

from __future__ import annotations

import hashlib
import io
from collections import OrderedDict
from typing import Any

from PIL import Image, ImageCms, ImageOps

from core.attachments.attachments import AttachmentError
from core.utils.workers import BoundedWorkerPool

_WORKERS = BoundedWorkerPool(name="image-conversion", max_workers=1)
_CACHE_BYTES = 32 * 1024 * 1024
_PIXEL_LIMIT = 32_000_000
_FORMATS = {"image/png": "PNG", "image/webp": "WEBP", "image/jpeg": "JPEG"}


class ImageConversionError(AttachmentError):
    """Structured failure; callers own user/Model-facing explanations."""

    def __init__(self, reason: str = "conversion_failed") -> None:
        self.reason = reason
        super().__init__(reason)


class ImageConverter:
    """Preserve accepted bytes, otherwise return one compatible image and MIME.

    Cached values are encoded files, never pixels or original-file references.
    Source-content hashes prevent stale results when an Agent edits an original.
    All cache operations and decoding run in the single admitted worker.
    """

    def __init__(self) -> None:
        self._cache: OrderedDict[tuple[bytes, str], bytes] = OrderedDict()
        self._cache_bytes = 0

    async def convert(
        self, data: bytes, media_type: str, supported_types: frozenset[str]
    ) -> tuple[bytes, str]:
        if media_type in supported_types:
            return data, media_type
        target_type = next((kind for kind in _FORMATS if kind in supported_types), None)
        if target_type is None:
            raise ImageConversionError("unsupported_target")
        converted = await _WORKERS.run(self._convert, data, target_type)
        return converted, target_type

    def _convert(self, data: bytes, target_type: str) -> bytes:
        key = (hashlib.sha256(data).digest(), target_type)
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]
        try:
            with Image.open(io.BytesIO(data)) as original:
                if original.width * original.height > _PIXEL_LIMIT:
                    raise ImageConversionError("image_too_large")
                if getattr(original, "n_frames", 1) != 1:
                    raise ImageConversionError("multiple_frames")
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
                if target_type == "image/jpeg":
                    rgba = converted.convert("RGBA")
                    background = Image.new("RGB", rgba.size, "white")
                    background.paste(rgba, mask=rgba.getchannel("A"))
                    converted = background
                elif converted.mode not in {"1", "L", "LA", "P", "RGB", "RGBA", "I;16"}:
                    converted = converted.convert("RGB")
                options: dict[str, Any] = {}
                if profile:
                    options["icc_profile"] = profile
                if target_type == "image/jpeg":
                    options.update(quality=95, subsampling=0)
                elif target_type == "image/webp":
                    options.update(lossless=True, exact=True)
                output = io.BytesIO()
                converted.save(output, format=_FORMATS[target_type], **options)
                result = output.getvalue()
        except (OSError, ValueError, Image.DecompressionBombError, ImageCms.PyCMSError) as exc:
            raise ImageConversionError() from exc
        if len(result) <= _CACHE_BYTES:
            while self._cache_bytes + len(result) > _CACHE_BYTES:
                _, evicted = self._cache.popitem(last=False)
                self._cache_bytes -= len(evicted)
            self._cache[key] = result
            self._cache_bytes += len(result)
        return result
