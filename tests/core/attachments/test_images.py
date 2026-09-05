"""Real raster conversion, target switching, and bounded-cache regressions."""

import io
from pathlib import Path
from unittest.mock import patch

import pytest
from PIL import Image

import core.attachments.images as image_module
from core.attachments import AttachmentStore, sniff_media_type
from core.attachments.images import ImageConversionError, ImageConverter


def raster(format: str, mode: str = "RGB", **options) -> bytes:
    image = Image.new(mode, (12, 8), (21, 105, 230, 70) if mode == "RGBA" else (21, 105, 230))
    stream = io.BytesIO()
    image.save(stream, format=format, **options)
    return stream.getvalue()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "format,media_type", [("BMP", "image/bmp"), ("TIFF", "image/tiff"), ("AVIF", "image/avif")]
)
async def test_additional_formats_store_original_and_convert_to_png(tmp_path, format, media_type):
    data = raster(format)
    store = AttachmentStore(tmp_path)
    record = store.store("misleading.txt", data)
    assert record.media_type == media_type
    assert sniff_media_type(data, "wrong.jpg") == media_type
    result, actual_type = await ImageConverter().convert(data, media_type, frozenset({"image/png"}))
    assert actual_type == "image/png"
    with Image.open(io.BytesIO(result)) as converted, Image.open(io.BytesIO(data)) as original:
        assert converted.size == original.size
        assert converted.convert("RGB").tobytes() == original.convert("RGB").tobytes()
    assert Path(store.get(record.id).file_path).read_bytes() == data


@pytest.mark.asyncio
async def test_native_image_bytes_are_not_decoded_or_reencoded():
    data = raster("PNG")
    with patch.object(Image, "open", side_effect=AssertionError("unexpected decode")):
        result, media_type = await ImageConverter().convert(
            data, "image/png", frozenset({"image/png"})
        )
    assert result is data
    assert media_type == "image/png"


@pytest.mark.asyncio
async def test_target_switch_and_alpha_preservation():
    data = raster("TIFF", "RGBA")
    converter = ImageConverter()
    for target in ("image/png", "image/webp", "image/jpeg"):
        result, actual_type = await converter.convert(data, "image/tiff", frozenset({target}))
        assert actual_type == sniff_media_type(result, "ignored") == target
        with Image.open(io.BytesIO(result)) as converted:
            assert converted.size == (12, 8)
            if target != "image/jpeg":
                assert converted.convert("RGBA").getpixel((0, 0)) == (21, 105, 230, 70)
            else:
                assert converted.mode == "RGB"
                assert all(channel > 150 for channel in converted.getpixel((0, 0)))


@pytest.mark.asyncio
async def test_orientation_is_applied_without_resampling():
    exif = Image.Exif()
    exif[274] = 6
    data = raster("JPEG", exif=exif)
    result, _ = await ImageConverter().convert(data, "image/jpeg", frozenset({"image/png"}))
    with Image.open(io.BytesIO(result)) as converted:
        assert converted.size == (8, 12)
        assert converted.getexif().get(274, 1) == 1


@pytest.mark.asyncio
async def test_repeated_conversion_uses_cache_and_source_changes_invalidate_it(monkeypatch):
    converter = ImageConverter()
    data = raster("BMP")
    first, _ = await converter.convert(data, "image/bmp", frozenset({"image/png"}))
    with patch.object(Image, "open", side_effect=AssertionError("cached image decoded")):
        again, _ = await converter.convert(data, "image/bmp", frozenset({"image/png"}))
    assert again is first
    monkeypatch.setattr(image_module, "_CACHE_BYTES", len(first))
    altered = raster("TIFF")
    await converter.convert(altered, "image/tiff", frozenset({"image/png"}))
    assert len(converter._cache) == 1
    assert converter._cache_bytes <= len(first)


@pytest.mark.asyncio
async def test_conversion_failures_are_structured_and_do_not_discard_frames(monkeypatch):
    converter = ImageConverter()
    with pytest.raises(ImageConversionError) as unsupported:
        await converter.convert(raster("BMP"), "image/bmp", frozenset())
    assert unsupported.value.reason == "unsupported_target"
    with pytest.raises(ImageConversionError) as corrupt:
        await converter.convert(b"broken", "image/bmp", frozenset({"image/png"}))
    assert corrupt.value.reason == "conversion_failed"
    data = raster("TIFF", save_all=True, append_images=[Image.new("RGB", (12, 8), "red")])
    with pytest.raises(ImageConversionError) as multiple:
        await converter.convert(data, "image/tiff", frozenset({"image/png"}))
    assert multiple.value.reason == "multiple_frames"
    monkeypatch.setattr(image_module, "_PIXEL_LIMIT", 10)
    with pytest.raises(ImageConversionError) as oversized:
        await converter.convert(raster("BMP"), "image/bmp", frozenset({"image/png"}))
    assert oversized.value.reason == "image_too_large"
