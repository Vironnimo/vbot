"""Tests for the explicit attachment blob extension converter."""

from __future__ import annotations

import json
import shutil
from importlib import import_module
from pathlib import Path

import pytest

from core.attachments import AttachmentStore
from core.storage.layout import DataDirectoryLayout

_CONVERTER = import_module("scripts.converters.attachment_blob_extensions")
AttachmentBlobConversionError = _CONVERTER.AttachmentBlobConversionError
convert_attachment_blob_extensions = _CONVERTER.convert_attachment_blob_extensions


def test_converter_renames_legacy_blob_and_updates_sidecar(tmp_path: Path) -> None:
    store = AttachmentStore(tmp_path)
    record = store.store("photo.jpg", b"\xff\xd8\xff\x00\x10")
    typed_blob_path = Path(record.file_path)
    legacy_blob_path = typed_blob_path.with_suffix("")
    typed_blob_path.replace(legacy_blob_path)
    sidecar_path = DataDirectoryLayout(tmp_path).attachments / f"{record.id}.json"
    payload = json.loads(sidecar_path.read_text(encoding="utf-8"))
    payload["file_path"] = str(legacy_blob_path)
    sidecar_path.write_text(json.dumps(payload), encoding="utf-8")

    result = convert_attachment_blob_extensions(tmp_path)

    assert result.converted == 1
    assert result.already_converted == 0
    assert not legacy_blob_path.exists()
    assert typed_blob_path.read_bytes() == b"\xff\xd8\xff\x00\x10"
    converted_payload = json.loads(sidecar_path.read_text(encoding="utf-8"))
    assert converted_payload["file_path"] == str(typed_blob_path)
    assert store.get(record.id).file_path == str(typed_blob_path)


def test_converter_is_idempotent_for_typed_blobs(tmp_path: Path) -> None:
    store = AttachmentStore(tmp_path)
    store.store("notes.txt", b"already converted")

    result = convert_attachment_blob_extensions(tmp_path)

    assert result.converted == 0
    assert result.already_converted == 1


def test_converter_preflights_conflicts_before_moving_any_blob(tmp_path: Path) -> None:
    store = AttachmentStore(tmp_path)
    first = store.store("first.jpg", b"\xff\xd8\xff\x00\x10")
    second = store.store("second.jpg", b"\xff\xd8\xff\x00\x11")
    first_typed_path = Path(first.file_path)
    first_legacy_path = first_typed_path.with_suffix("")
    first_typed_path.replace(first_legacy_path)
    second_typed_path = Path(second.file_path)
    second_legacy_path = second_typed_path.with_suffix("")
    shutil.copyfile(second_typed_path, second_legacy_path)

    with pytest.raises(AttachmentBlobConversionError, match="Refusing to overwrite"):
        convert_attachment_blob_extensions(tmp_path)

    assert first_legacy_path.exists()
    assert not first_typed_path.exists()
    assert second_legacy_path.exists()
    assert second_typed_path.exists()


def test_converter_accepts_missing_attachment_directory(tmp_path: Path) -> None:
    result = convert_attachment_blob_extensions(tmp_path)

    assert result.converted == 0
    assert result.already_converted == 0
