"""Computer Use's native wire values, shared by the Extension and Desktop.

This module has no kernel dependency: the Desktop imports only the native
Computer Use target and these explicit value conversions.
"""

from __future__ import annotations

import base64
import io
from dataclasses import asdict
from typing import Any

from PIL import Image

from .target import AppInfo, Display, TargetError, WindowInfo

# Leave space for the JSON envelope below the connection's 16 MiB limit.
MAX_PNG_BYTES = 11 * 1024 * 1024
MAX_CAPTURE_PIXELS = 40_000_000


def app_data(app: AppInfo) -> dict[str, Any]:
    return {
        "name": app.name,
        "keys": sorted(app.keys),
        "running": app.running,
        "launchable": app.launchable,
    }


def app_value(data: dict[str, Any]) -> AppInfo:
    return AppInfo(
        str(data["name"]),
        frozenset(str(key) for key in data["keys"]),
        bool(data["running"]),
        bool(data["launchable"]),
    )


def display_data(display: Display) -> dict[str, Any]:
    return asdict(display)


def display_value(data: dict[str, Any]) -> Display:
    return Display(
        str(data["id"]),
        str(data["name"]),
        int(data["left"]),
        int(data["top"]),
        int(data["width"]),
        int(data["height"]),
        bool(data["primary"]),
        int(data["scale_percent"]),
    )


def window_data(window: WindowInfo | None) -> dict[str, Any] | None:
    if window is None:
        return None
    return {**asdict(window), "app": app_data(window.app)}


def window_value(data: dict[str, Any] | None) -> WindowInfo | None:
    if data is None:
        return None
    return WindowInfo(
        int(data["handle"]),
        str(data["title"]),
        app_value(data["app"]),
        int(data["left"]),
        int(data["top"]),
        int(data["right"]),
        int(data["bottom"]),
        bool(data["elevated"]),
        int(data.get("owner", 0)),
    )


def image_data(image: Image.Image) -> dict[str, Any]:
    if image.width * image.height > MAX_CAPTURE_PIXELS:
        raise TargetError("The display is too large to capture.", "computer_use_unavailable")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", compress_level=4)
    content = buffer.getvalue()
    if len(content) > MAX_PNG_BYTES:
        raise TargetError(
            "The desktop screenshot is too large to transfer. Reduce the display resolution "
            "before trying again.",
            "computer_use_unavailable",
        )
    return {"png": base64.b64encode(content).decode("ascii")}


def image_value(data: dict[str, Any]) -> Image.Image:
    encoded = data.get("png")
    if not isinstance(encoded, str) or len(encoded) > (MAX_PNG_BYTES + 2) // 3 * 4:
        raise TargetError("The computer returned an invalid screenshot.")
    try:
        content = base64.b64decode(encoded, validate=True)
        with Image.open(io.BytesIO(content)) as image:
            if image.width * image.height > MAX_CAPTURE_PIXELS or image.format != "PNG":
                raise ValueError("Invalid screenshot dimensions or format")
            return image.convert("RGB")
    except (ValueError, OSError) as error:
        raise TargetError("The computer returned an invalid screenshot.") from error
