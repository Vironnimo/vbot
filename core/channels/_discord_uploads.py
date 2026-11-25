"""Size-bounded Discord image uploads after an explicit payload rejection."""

from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import PurePath
from typing import Any

from core.attachments import canonical_extension_for_media_type
from core.attachments.images import ImageConversionError, ImageConverter, ImageTarget
from core.channels.adapter import FileData

_DEFAULT_FILE_LIMIT = 20 * 1024 * 1024
# Create Message also limits the entire multipart request to 25 MiB. Leave
# room for boundaries, filenames, JSON and the message that accompanies a file.
_REQUEST_FILE_BUDGET = 25 * 1024 * 1024 - 64 * 1024
_OUTPUTS = frozenset({"image/png", "image/webp", "image/jpeg"})


@dataclass(frozen=True)
class _UploadFit:
    advertised: int
    threshold: int
    budget: int
    expires: float


class DiscordUploads:
    def __init__(self) -> None:
        self._converter = ImageConverter()
        self._fits: OrderedDict[int, _UploadFit] = OrderedDict()

    @staticmethod
    def _destination(target: Any) -> tuple[int, int]:
        guild = getattr(target, "guild", None)
        limit = getattr(guild, "filesize_limit", None)
        if type(limit) is not int or limit <= 0:
            limit = _DEFAULT_FILE_LIMIT
        return (guild or target).id, min(limit, _REQUEST_FILE_BUDGET)

    def learned_budget(self, target: Any, size: int) -> int | None:
        key, advertised = self._destination(target)
        fit = self._fits.get(key)
        if fit is None:
            return None
        if fit.expires <= time.monotonic() or fit.advertised != advertised:
            del self._fits[key]
            return None
        self._fits.move_to_end(key)
        return fit.budget if size >= fit.threshold else None

    def fallback_budget(self, target: Any, rejected_size: int) -> int:
        _, advertised = self._destination(target)
        return advertised if rejected_size > advertised else max(1, rejected_size // 2)

    def remember(self, target: Any, rejected_size: int, budget: int) -> None:
        key, advertised = self._destination(target)
        # A 413 by itself gives no exact byte count. Use the advertised ceiling
        # if it explains the refusal; otherwise only fit files at least as large
        # as the one actually rejected, and probe again after an hour.
        threshold = advertised + 1 if rejected_size > advertised else rejected_size
        self._fits[key] = _UploadFit(advertised, threshold, budget, time.monotonic() + 3600)
        self._fits.move_to_end(key)
        if len(self._fits) > 256:
            self._fits.popitem(last=False)

    async def fit(self, file: FileData, budget: int | None) -> FileData:
        if budget is None or len(file.data) <= budget or not file.media_type.startswith("image/"):
            return file
        try:
            image = await self._converter.convert(
                file.data, file.media_type, ImageTarget(_OUTPUTS, max_output_bytes=budget)
            )
        except ImageConversionError:
            # Includes animated/multipage and unreadable images. Retain every
            # frame and never silently drop a member of an attachment batch.
            return file
        name = file.filename
        if image.media_type != file.media_type:
            extension = canonical_extension_for_media_type(image.media_type)
            stem = PurePath(name).stem or "image"
            name = f"{stem}{extension}"
        return FileData(filename=name, media_type=image.media_type, data=image.data)
