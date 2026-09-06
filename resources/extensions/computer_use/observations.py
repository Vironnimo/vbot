"""Bounded observations with retained originals and exact image-space targeting."""

from __future__ import annotations

import base64
import binascii
import io
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PIL import Image, UnidentifiedImageError

from core.tools import ToolContext
from core.utils.ids import write_id_file

from .driver import ComputerUseError

MAX_BYTES = 32 * 1024 * 1024
MAX_PIXELS = 40_000_000
OVERVIEW_EDGE = 1600
OVERVIEW_PIXELS = 1_500_000
MAX_TEXT = 24_000


@dataclass
class Observation:
    target: tuple[Any, ...]
    elements: dict[str, str] = field(default_factory=dict)
    original: Path | None = None
    view_id: str | None = None
    # Mapping displayed pixels to the driver's original screenshot coordinates.
    origin: tuple[int, int] = (0, 0)
    source_size: tuple[int, int] = (0, 0)
    display_size: tuple[int, int] = (0, 0)

    def point(self, view_id: str, x: int, y: int, *, edge: bool = False) -> tuple[int, int]:
        if not self.view_id or view_id != self.view_id:
            raise ComputerUseError(
                "This view is stale. Capture the target again or zoom the current view.",
                "stale_view",
            )
        width, height = self.display_size
        if not (0 <= x < width + int(edge) and 0 <= y < height + int(edge)):
            raise ComputerUseError(
                "Coordinates are outside this image. Use coordinates from the returned image.",
                "invalid_coordinates",
            )
        source_width, source_height = self.source_size
        return (
            self.origin[0] + min(round(x * source_width / width), source_width - (not edge)),
            self.origin[1] + min(round(y * source_height / height), source_height - (not edge)),
        )

    def token(self, reference: str) -> str:
        token = self.elements.get(reference)
        if token is None and reference in self.elements.values():
            token = reference
        if token is None:
            raise ComputerUseError(
                "Capture this target again before sending input.", "capture_required"
            )
        return token


def _image(raw: bytes) -> Image.Image:
    try:
        if len(raw) > MAX_BYTES:
            raise ValueError()
        image = Image.open(io.BytesIO(raw))
        if image.format != "PNG" or image.width * image.height > MAX_PIXELS:
            raise ValueError()
        image.load()
        return image
    except (ValueError, OSError, UnidentifiedImageError, Image.DecompressionBombError) as exc:
        raise ComputerUseError(
            "The image is invalid or exceeds the supported size. Capture a smaller target."
        ) from exc


def _directory(context: ToolContext) -> Path:
    directory = context.data_root / "tmp" / "computer-use"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def screenshot_bytes(payload: dict[str, Any], expected_path: Path | None = None) -> bytes | None:
    encoded = payload.pop("screenshot_png_b64", None)
    # Paths supplied by the driver are never trusted as local file read authority.
    payload.pop("screenshot_file_path", None)
    if encoded is not None:
        try:
            if not isinstance(encoded, str) or len(encoded) > (MAX_BYTES * 4 // 3 + 4):
                raise ValueError()
            return base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ComputerUseError(
                "The image is invalid or exceeds the supported size. Capture a smaller target."
            ) from exc
    if expected_path is not None and expected_path.is_file():
        if expected_path.stat().st_size > MAX_BYTES:
            raise ComputerUseError(
                "The image is invalid or exceeds the supported size. Capture a smaller target."
            )
        return expected_path.read_bytes()
    return None


def _present(
    context: ToolContext,
    observation: Observation,
    image: Image.Image,
    resolution: str,
) -> dict[str, Any]:
    source_width, source_height = image.size
    factor = 1.0
    if resolution == "auto":
        factor = min(
            1.0,
            OVERVIEW_EDGE / max(image.size),
            math.sqrt(OVERVIEW_PIXELS / (source_width * source_height)),
        )
    display_size = (max(1, round(source_width * factor)), max(1, round(source_height * factor)))
    if display_size != image.size:
        image = image.resize(display_size, Image.Resampling.LANCZOS)
    stream = io.BytesIO()
    image.save(stream, format="PNG", compress_level=1)
    raw = stream.getvalue()
    if len(raw) > MAX_BYTES:
        raise ComputerUseError(
            "The image is invalid or exceeds the supported size. Capture a smaller target."
        )
    path = write_id_file(_directory(context), "view", ".png", raw)
    observation.view_id = path.stem
    observation.source_size = (source_width, source_height)
    observation.display_size = display_size
    context.result_media.append(
        {
            "path": path.as_posix(),
            "filename": path.name,
            "media_type": "image/png",
            "base64": base64.b64encode(raw).decode("ascii"),
        }
    )
    original = observation.original or path
    context.presentation_images.append({"path": original.as_posix(), "filename": original.name})
    result: dict[str, Any] = {
        "view_id": observation.view_id,
        "image_width": image.width,
        "image_height": image.height,
        "source_width": source_width,
        "source_height": source_height,
        "original": original.as_posix(),
        "screenshot": path.as_posix(),
    }
    if factor < 1:
        result["image_note"] = (
            "The image was reduced for overview. Use zoom or resolution original for small details."
        )
    return result


def capture(
    context: ToolContext,
    target: tuple[Any, ...],
    payload: dict[str, Any],
    *,
    mode: str = "som",
    resolution: str = "auto",
    expected_path: Path | None = None,
) -> tuple[Observation, dict[str, Any]]:
    payload = dict(payload)
    raw = screenshot_bytes(payload, expected_path)
    for key in ("_note", "screenshot_width", "screenshot_height"):
        payload.pop(key, None)
    observation = Observation(target)
    payload.pop("screenshot", None)
    result: dict[str, Any] = {}
    if mode != "ax":
        if raw is None:
            raise ComputerUseError(
                "The image is invalid or exceeds the supported size. Capture a smaller target."
            )
        image = _image(raw)
        path = write_id_file(_directory(context), "orig", ".png", raw)
        observation.original = path
        result.update(_present(context, observation, image, resolution))
    elements = payload.get("elements")
    if isinstance(elements, list):
        observation.elements = {
            str(item["element_index"]): item["element_token"]
            for item in elements
            if isinstance(item, dict)
            and type(item.get("element_index")) is int
            and isinstance(item.get("element_token"), str)
        }
        payload["elements"] = [
            {key: value for key, value in item.items() if key not in {"frame", "bounds"}}
            for item in elements
            if isinstance(item, dict)
        ]
        payload.pop("tree_markdown", None)
    if mode == "vision":
        payload.pop("elements", None)
        payload.pop("tree_markdown", None)
    elif mode == "ax" and not payload:
        raise ComputerUseError(
            "No window elements were returned. Capture with mode vision to inspect the pixels."
        )
    result.update(bounded(context, payload))
    return observation, result


def bounded(context: ToolContext, payload: dict[str, Any]) -> dict[str, Any]:
    serialized = json.dumps(payload, ensure_ascii=False)
    if len(serialized) <= MAX_TEXT:
        return payload
    path = write_id_file(_directory(context), "state", ".json", serialized.encode("utf-8"))
    # Preserve exact target ids and the complete structured file; never truncate JSON text.
    result: dict[str, Any] = {
        key: payload[key] for key in ("target_id", "tab_id", "pid", "window_id") if key in payload
    }
    result.update({"truncated": True, "state_file": path.as_posix()})
    for key, value in payload.items():
        if key in result:
            continue
        if isinstance(value, list):
            value = value[:40]
        elif isinstance(value, str):
            value = value[:2000]
        if (
            len(json.dumps(result, ensure_ascii=False)) + len(json.dumps(value, ensure_ascii=False))
            < MAX_TEXT
        ):
            result[key] = value
    return result


def zoom(
    context: ToolContext,
    observation: Observation,
    view_id: str,
    x: int,
    y: int,
    x2: int,
    y2: int,
) -> tuple[Observation, dict[str, Any]]:
    left, top = observation.point(view_id, x, y)
    right, bottom = observation.point(view_id, x2, y2, edge=True)
    if right <= left or bottom <= top or observation.original is None:
        raise ComputerUseError(
            "Coordinates are outside this image. Use coordinates from the returned image."
        )
    original = _image(observation.original.read_bytes())
    crop = original.crop((left, top, right, bottom))
    zoomed = Observation(observation.target, observation.elements.copy(), observation.original)
    zoomed.origin = (left, top)
    return zoomed, _present(context, zoomed, crop, "original")
