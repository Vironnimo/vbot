"""Argument dialects and local image paths for the image and media generation Tools."""

from __future__ import annotations

import json
import os
import re
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from core.tools._argument_repair import normalize_call_arguments
from core.tools._call_vocabulary import SpellingAliases
from core.tools._path_suggestions import corrected_paths
from core.tools.contracts import ToolContract
from core.tools.search import display_search_path
from core.tools.tools import ToolContext

# Names other image Tools and Agents use for analyze_image fields.
ANALYZE_FIELD_ALIASES = SpellingAliases(
    {
        "images": (
            "image",
            "image_path",
            "image_paths",
            "image_file",
            "image_files",
            "image_url",
            "image_urls",
            "path",
            "paths",
            "file",
            "files",
            "file_path",
            "file_paths",
            "url",
            "urls",
        ),
        "prompt": ("question", "query", "instruction", "instructions", "task"),
    }
)

_SOURCE_IMAGE_ALIASES = (
    "source_image",
    "input_image",
    "input_images",
    "reference_image",
    "reference_images",
    "image_path",
    "image_paths",
)
_OUTPUT_DIR_ALIASES = ("output_directory", "out_dir", "save_dir", "output_folder")

# Names other image generators use for image_generation fields.
GENERATION_FIELD_ALIASES = SpellingAliases(
    {
        "source_images": _SOURCE_IMAGE_ALIASES,
        "output_dir": _OUTPUT_DIR_ALIASES,
    }
)

# Names other video generators use for generate_video fields.
VIDEO_FIELD_ALIASES = SpellingAliases(
    {
        "first_frame": ("start_frame", "start_image", "first_frame_image"),
        "last_frame": ("end_frame", "end_image", "last_frame_image"),
        "output_dir": _OUTPUT_DIR_ALIASES,
    }
)

# Names other music generators use for generate_music fields.
MUSIC_FIELD_ALIASES = SpellingAliases(
    {
        "source_images": _SOURCE_IMAGE_ALIASES,
        "output_dir": _OUTPUT_DIR_ALIASES,
    }
)

# Keys an image item object may use for its one path or address.
_ITEM_KEYS = frozenset(
    {"path", "file", "file_path", "filepath", "url", "image_url", "image", "src"}
)
# Keys that describe an item without naming another image.
_ITEM_DECORATION = frozenset({"type", "detail", "alt"})
_IMAGE_SUFFIXES = frozenset(
    {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".heic", ".heif"}
)
_FOLDER_EXAMPLES = 6
# A suggestion must share most of the missing file's name, not only its extension.
_MIN_STEM_RATIO = 0.6
_WINDOWS_DRIVE_PATH = re.compile(r"/[A-Za-z]:[/\\]")


class UnusableImageError(ValueError):
    """A requested image cannot be used; ``code`` is the Tool error code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _item(value: Any) -> Any:
    """Unwrap {"path": ...}, {"url": ...} and {"type": "image_url", "image_url": {...}}."""
    if not isinstance(value, dict):
        return value
    named = [key for key in value if key not in _ITEM_DECORATION]
    if len(named) != 1 or named[0].casefold() not in _ITEM_KEYS:
        return value
    return _item(value[named[0]])


def image_list(value: Any) -> Any:
    """Return an images value as a list of path strings where the intent is exact."""
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("["):
            try:
                parsed = json.loads(text)
            except ValueError:
                parsed = None
            if isinstance(parsed, list):
                return [_item(item) for item in parsed]
        return [text] if text else value
    if isinstance(value, dict):
        return [_item(value)]
    if isinstance(value, list):
        return [_item(item) for item in value]
    return value


def normalize_analyze_image_arguments(contract: ToolContract, arguments: Any) -> Any:
    """Return canonical analyze_image arguments for calls written in other dialects."""
    return normalize_call_arguments(
        contract,
        arguments,
        field_aliases=ANALYZE_FIELD_ALIASES,
        field_normalizers={"images": image_list},
    )


def normalize_image_generation_arguments(contract: ToolContract, arguments: Any) -> Any:
    """Return canonical image_generation arguments for calls written in other dialects."""
    return normalize_call_arguments(
        contract,
        arguments,
        field_aliases=GENERATION_FIELD_ALIASES,
        field_normalizers={"source_images": image_list},
        empty_as_omitted=("output_dir", "aspect_ratio", "resolution"),
    )


def normalize_generate_video_arguments(contract: ToolContract, arguments: Any) -> Any:
    """Return canonical generate_video arguments for calls written in other dialects."""
    return normalize_call_arguments(
        contract,
        arguments,
        field_aliases=VIDEO_FIELD_ALIASES,
        empty_as_omitted=(
            "output_dir",
            "first_frame",
            "last_frame",
            "resolution",
            "aspect_ratio",
            "size",
        ),
    )


def normalize_generate_music_arguments(contract: ToolContract, arguments: Any) -> Any:
    """Return canonical generate_music arguments for calls written in other dialects."""
    return normalize_call_arguments(
        contract,
        arguments,
        field_aliases=MUSIC_FIELD_ALIASES,
        field_normalizers={"source_images": image_list},
        empty_as_omitted=("output_dir",),
    )


def _local_path_text(text: str, field: str) -> str:
    """Turn a file: URL into a path; refuse web and data addresses with the reason."""
    lowered = text.casefold()
    if lowered.startswith(("http://", "https://")):
        raise UnusableImageError(
            "invalid_arguments",
            f"{field} must be local image files; web addresses such as {text} cannot be "
            "opened. Save the image to a file first, then pass that file's path.",
        )
    if lowered.startswith("data:"):
        raise UnusableImageError(
            "invalid_arguments",
            f"{field} must be local image files; data: URLs cannot be opened. Save the "
            "image to a file first, then pass that file's path.",
        )
    if not lowered.startswith("file:"):
        return text
    parts = urlsplit(text)
    path = unquote(parts.path)
    if parts.netloc and parts.netloc.casefold() != "localhost":
        path = f"//{parts.netloc}{path}"
    elif os.name == "nt" and _WINDOWS_DRIVE_PATH.match(path):
        path = path[1:]
    return path


def _call(field: str, paths: list[str], single: bool) -> str:
    """Render the corrected argument as the Agent would write it."""
    return json.dumps({field: paths[0] if single else paths}, ensure_ascii=False)


def _folder_problem(
    folder: Path, label: str, field: str, cwd: Path, single: bool
) -> UnusableImageError:
    try:
        images = sorted(
            entry
            for entry in folder.iterdir()
            if entry.suffix.casefold() in _IMAGE_SUFFIXES and entry.is_file()
        )[:_FOLDER_EXAMPLES]
    except OSError:
        images = []
    if not images:
        return UnusableImageError(
            "image_read_error", f"{label} is a folder with no image files, not an image."
        )
    example = _call(field, [display_search_path(image, cwd=cwd) for image in images], single)
    files = "an image file" if single else "image files"
    return UnusableImageError(
        "image_read_error",
        f"{label} is a folder, not an image. Pass {files} from it, for example {example}.",
    )


def _similar_images(missing: Path, cwd: Path) -> list[Path]:
    """Existing files that the missing image most likely means, best first."""
    stem = missing.stem.casefold()
    return [
        item
        for item in corrected_paths(missing, cwd)
        if item.is_file()
        and SequenceMatcher(None, stem, item.stem.casefold()).ratio() >= _MIN_STEM_RATIO
    ]


def resolve_local_image(context: ToolContext, raw_path: Any, field: str) -> Path:
    """Resolve one requested image, such as a video frame, like ``resolve_local_images``."""
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise UnusableImageError("invalid_arguments", f"{field} must be a local image path.")
    return resolve_local_images(context, [raw_path], field, single=True)[0]


def resolve_local_images(
    context: ToolContext, raw_paths: Any, field: str, *, single: bool = False
) -> list[Path]:
    """Resolve requested images against the working directory.

    Missing files fail together, each with similar existing files and, when every
    missing file has one, the corrected call. Nothing is substituted.
    """
    if not isinstance(raw_paths, list):
        raise UnusableImageError(
            "invalid_arguments", f"{field} must be a list of local image paths."
        )
    if not raw_paths:
        raise UnusableImageError(
            "invalid_arguments", f"{field} is empty; pass at least one local image path."
        )
    cwd = context.resolve_path(".")
    requested: list[tuple[str, Path]] = []
    for index, raw in enumerate(raw_paths):
        if not isinstance(raw, str) or not raw.strip():
            raise UnusableImageError(
                "invalid_arguments", f"{field}[{index}] must be a non-empty path."
            )
        text = _local_path_text(raw.strip(), field)
        requested.append((raw.strip(), context.resolve_path(text)))
    context.presentation_images.extend(
        {"path": str(path), "filename": path.name} for _, path in requested
    )
    missing: list[tuple[int, str, list[Path]]] = []
    for index, (raw, path) in enumerate(requested):
        if path.is_dir():
            raise _folder_problem(path, raw, field, cwd, single)
        if not path.exists():
            similar = _similar_images(path, cwd)
            missing.append((index, raw, similar))
    if missing:
        raise UnusableImageError(
            "image_not_found", _missing_message(requested, missing, field, cwd, single)
        )
    return [path for _, path in requested]


def _missing_message(
    requested: list[tuple[str, Path]],
    missing: list[tuple[int, str, list[Path]]],
    field: str,
    cwd: Path,
    single: bool,
) -> str:
    lines = []
    for _, raw, similar in missing:
        if similar:
            names = ", ".join(display_search_path(item, cwd=cwd) for item in similar)
            lines.append(f"No image at {raw} (similar: {names}).")
        else:
            lines.append(f"No image at {raw}, and no similar file is beside it.")
    if all(similar for _, _, similar in missing):
        corrected = [raw for raw, _ in requested]
        for index, _, similar in missing:
            corrected[index] = display_search_path(similar[0], cwd=cwd)
        call = _call(field, corrected, single)
        meant = "that file" if len(missing) == 1 else "those files"
        lines.append(f"If you meant {meant}, pass {call}.")
    return "\n".join(lines)
