#!/usr/bin/env python
"""Convert suffixless attachment blobs to the canonical extension-bearing layout."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.attachments import AttachmentError, canonical_extension_for_media_type  # noqa: E402
from core.storage.layout import DataDirectoryLayout  # noqa: E402
from core.utils.atomic import atomic_write_text  # noqa: E402

JsonObject = dict[str, Any]


class AttachmentBlobConversionError(Exception):
    """Raised when the existing attachment layout cannot be converted safely."""


@dataclass(frozen=True)
class AttachmentBlobConversionResult:
    converted: int
    already_converted: int


@dataclass(frozen=True)
class _ConversionCandidate:
    sidecar_path: Path
    legacy_blob_path: Path
    typed_blob_path: Path
    sidecar_payload: JsonObject


def convert_attachment_blob_extensions(data_dir: Path) -> AttachmentBlobConversionResult:
    """Convert every sidecar-backed legacy blob under one explicit data directory."""

    attachments_dir = DataDirectoryLayout(data_dir.expanduser().resolve()).attachments
    if not attachments_dir.exists():
        return AttachmentBlobConversionResult(converted=0, already_converted=0)
    if not attachments_dir.is_dir():
        raise AttachmentBlobConversionError(
            f"Attachment path is not a directory: {attachments_dir}"
        )

    candidates: list[_ConversionCandidate] = []
    already_converted = 0
    for sidecar_path in sorted(attachments_dir.glob("*.json")):
        payload = _load_sidecar(sidecar_path)
        attachment_id = _require_string(payload, "id", sidecar_path)
        if sidecar_path.stem != attachment_id:
            raise AttachmentBlobConversionError(
                f"Attachment id does not match sidecar name: {sidecar_path}"
            )

        media_type = _require_string(payload, "media_type", sidecar_path)
        try:
            extension = canonical_extension_for_media_type(media_type)
        except AttachmentError as exc:
            raise AttachmentBlobConversionError(f"Cannot convert {sidecar_path}: {exc}") from exc

        legacy_blob_path = attachments_dir / attachment_id
        typed_blob_path = attachments_dir / f"{attachment_id}{extension}"
        if typed_blob_path.is_file() and not legacy_blob_path.exists():
            already_converted += 1
            continue
        if typed_blob_path.exists():
            raise AttachmentBlobConversionError(
                f"Refusing to overwrite existing typed attachment blob: {typed_blob_path}"
            )
        if not legacy_blob_path.is_file():
            raise AttachmentBlobConversionError(
                f"Legacy attachment blob is missing: {legacy_blob_path}"
            )

        candidates.append(
            _ConversionCandidate(
                sidecar_path=sidecar_path,
                legacy_blob_path=legacy_blob_path,
                typed_blob_path=typed_blob_path,
                sidecar_payload=payload,
            )
        )

    for candidate in candidates:
        _convert_candidate(candidate)

    return AttachmentBlobConversionResult(
        converted=len(candidates),
        already_converted=already_converted,
    )


def _load_sidecar(sidecar_path: Path) -> JsonObject:
    try:
        payload = json.loads(sidecar_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise AttachmentBlobConversionError(
            f"Cannot read attachment sidecar {sidecar_path}: {exc}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise AttachmentBlobConversionError(
            f"Invalid attachment sidecar JSON: {sidecar_path}"
        ) from exc

    if not isinstance(payload, dict):
        raise AttachmentBlobConversionError(
            f"Attachment sidecar must contain a JSON object: {sidecar_path}"
        )
    return payload


def _require_string(payload: JsonObject, key: str, sidecar_path: Path) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise AttachmentBlobConversionError(
            f"Attachment sidecar field '{key}' must be a non-empty string: {sidecar_path}"
        )
    return value


def _convert_candidate(candidate: _ConversionCandidate) -> None:
    candidate.legacy_blob_path.replace(candidate.typed_blob_path)
    updated_payload = {
        **candidate.sidecar_payload,
        "file_path": str(candidate.typed_blob_path),
    }
    serialized = json.dumps(updated_payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    try:
        atomic_write_text(candidate.sidecar_path, serialized)
    except OSError as exc:
        candidate.typed_blob_path.replace(candidate.legacy_blob_path)
        raise AttachmentBlobConversionError(
            f"Cannot update attachment sidecar {candidate.sidecar_path}: {exc}"
        ) from exc


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Rename suffixless vBot attachment blobs to their canonical MIME-derived "
            "extensions and update their sidecars."
        )
    )
    parser.add_argument(
        "data_dir",
        type=Path,
        help="Explicit vBot data directory containing artifacts/attachments/",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        result = convert_attachment_blob_extensions(args.data_dir)
    except AttachmentBlobConversionError as exc:
        print(f"attachment-blob-extensions..... ERROR: {exc}", file=sys.stderr)
        return 1

    print(
        "attachment-blob-extensions..... "
        f"converted={result.converted} already_converted={result.already_converted}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
