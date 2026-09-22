"""Real raster conversion, target switching, and bounded-cache regressions."""

import io
import random
from pathlib import Path
from unittest.mock import patch

import pytest
from PIL import Image

import core.attachments.images as image_module
from core.attachments import AttachmentStore, canonical_extension_for_media_type, sniff_media_type
from core.attachments.images import ImageConversionError, ImageConverter


def raster(format: str, mode: str = "RGB", **options) -> bytes:
    image = Image.new(mode, (12, 8), (21, 105, 230, 70) if mode == "RGBA" else (21, 105, 230))
    stream = io.BytesIO()
    image.save(stream, format=format, **options)
    return stream.getvalue()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "format,media_type",
    [("BMP", "image/bmp"), ("TIFF", "image/tiff"), ("AVIF", "image/avif"), ("HEIF", "image/heic")],
)
async def test_additional_formats_store_original_and_convert_to_png(tmp_path, format, media_type):
    data = raster(format)
    store = AttachmentStore(tmp_path)
    record = store.store("misleading.txt", data)
    assert record.media_type == media_type
    assert sniff_media_type(data, "wrong.jpg") == media_type
    prepared = await ImageConverter().convert(data, media_type, frozenset({"image/png"}))
    result, actual_type = prepared.data, prepared.media_type
    assert actual_type == "image/png"
    with Image.open(io.BytesIO(result)) as converted, Image.open(io.BytesIO(data)) as original:
        assert converted.size == original.size
        assert converted.convert("RGB").tobytes() == original.convert("RGB").tobytes()
    assert Path(store.get(record.id).file_path).read_bytes() == data


@pytest.mark.asyncio
async def test_native_image_bytes_are_validated_without_reencoding():
    data = raster("PNG")
    with patch.object(Image.Image, "save", side_effect=AssertionError("unexpected encode")):
        prepared = await ImageConverter().convert(data, "image/png", frozenset({"image/png"}))
    assert prepared.data is data
    assert prepared.media_type == "image/png"
    assert prepared.note is None


@pytest.mark.asyncio
async def test_target_switch_and_alpha_preservation():
    data = raster("TIFF", "RGBA")
    converter = ImageConverter()
    for target in ("image/png", "image/webp", "image/jpeg"):
        prepared = await converter.convert(data, "image/tiff", frozenset({target}))
        result, actual_type = prepared.data, prepared.media_type
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
    prepared = await ImageConverter().convert(data, "image/jpeg", frozenset({"image/png"}))
    result = prepared.data
    with Image.open(io.BytesIO(result)) as converted:
        assert converted.size == (8, 12)
        assert converted.getexif().get(274, 1) == 1


@pytest.mark.asyncio
async def test_repeated_conversion_uses_cache_and_source_changes_invalidate_it(monkeypatch):
    converter = ImageConverter()
    data = raster("BMP")
    first = await converter.convert(data, "image/bmp", frozenset({"image/png"}))
    with patch.object(Image, "open", side_effect=AssertionError("cached image decoded")):
        again = await converter.convert(data, "image/bmp", frozenset({"image/png"}))
    assert again is first
    monkeypatch.setattr(image_module, "_CACHE_BYTES", len(first.data))
    altered = raster("TIFF")
    await converter.convert(altered, "image/tiff", frozenset({"image/png"}))
    assert len(converter._cache) == 1
    assert converter._cache_bytes <= len(first.data)


@pytest.mark.asyncio
async def test_conversion_failures_are_structured_and_do_not_discard_frames(monkeypatch):
    converter = ImageConverter()
    with pytest.raises(ImageConversionError) as unsupported:
        await converter.convert(raster("BMP"), "image/bmp", frozenset())
    assert unsupported.value.reason == "unsupported_target"
    with pytest.raises(ImageConversionError) as corrupt:
        await converter.convert(b"broken", "image/bmp", frozenset({"image/png"}))
    assert corrupt.value.reason == "invalid_image"
    data = raster("TIFF", save_all=True, append_images=[Image.new("RGB", (12, 8), "red")])
    with pytest.raises(ImageConversionError) as multiple:
        await converter.convert(data, "image/tiff", frozenset({"image/png"}))
    assert multiple.value.reason == "multiple_frames"
    monkeypatch.setattr(image_module, "_PIXEL_LIMIT", 10)
    with pytest.raises(ImageConversionError) as oversized:
        await converter.convert(raster("BMP"), "image/bmp", frozenset({"image/png"}))
    assert oversized.value.reason == "image_too_large"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "format,media_type", [("PNG", "image/png"), ("JPEG", "image/jpeg"), ("WEBP", "image/webp")]
)
async def test_corrupt_native_images_fail_before_delivery(format, media_type):
    data = raster(format)
    with pytest.raises(ImageConversionError) as failure:
        await ImageConverter().convert(data[: len(data) // 2], media_type, frozenset({media_type}))
    assert failure.value.reason == "invalid_image"


@pytest.mark.asyncio
async def test_native_validation_cache_retains_no_image_bytes(monkeypatch):
    converter = ImageConverter()
    data = raster("PNG")
    await converter.convert(data, "image/png", frozenset({"image/png"}))
    with patch.object(Image, "open", side_effect=AssertionError("repeated decoding")):
        again = await converter.convert(data, "image/png", frozenset({"image/png"}))
    assert again.data is data
    assert not converter._cache
    monkeypatch.setattr(image_module, "_PIXEL_LIMIT", 10)
    with pytest.raises(ImageConversionError) as failure:
        await converter.convert(data, "image/png", frozenset({"image/png"}))
    assert failure.value.reason == "image_too_large"
    assert failure.value.pixels == 96
    assert failure.value.max_pixels == 10


@pytest.mark.asyncio
async def test_known_limit_tries_lossless_alternative_before_jpeg_or_resizing():
    converter = ImageConverter()
    data = raster("BMP")
    png = await converter.convert(data, "image/bmp", frozenset({"image/png"}))
    webp = await converter.convert(data, "image/bmp", frozenset({"image/webp"}))
    assert len(webp.data) < len(png.data)
    result = await converter.convert(
        data,
        "image/bmp",
        frozenset({"image/png", "image/webp", "image/jpeg"}),
        max_output_bytes=len(webp.data),
    )
    assert result.media_type == "image/webp"
    assert not result.lossy
    assert result.size == result.original_size == (12, 8)
    with Image.open(io.BytesIO(result.data)) as actual:
        assert actual.convert("RGB").getpixel((0, 0)) == (21, 105, 230)


@pytest.mark.asyncio
async def test_native_oversize_image_fits_limit_and_reports_resampling():
    source = Image.frombytes("RGB", (128, 64), random.Random(3).randbytes(128 * 64 * 3))
    stream = io.BytesIO()
    source.save(stream, format="PNG")
    converter = ImageConverter()
    original = stream.getvalue()
    unbounded = await converter.convert(original, "image/png", frozenset({"image/png"}))
    bounded = await converter.convert(
        original,
        "image/png",
        frozenset({"image/png"}),
        max_output_bytes=512,
    )
    assert unbounded.data is original
    assert len(bounded.data) <= 512
    assert bounded.reencoded and bounded.size != bounded.original_size
    assert bounded.original_size == (128, 64)
    with Image.open(io.BytesIO(bounded.data)) as actual:
        actual.load()
        assert actual.size == bounded.size
        assert abs(actual.width - actual.height * 2) <= 1
    again = await converter.convert(original, "image/png", frozenset({"image/png"}))
    assert again.data is original


@pytest.mark.asyncio
async def test_native_multiframe_is_preserved_but_never_flattened_to_fit():
    stream = io.BytesIO()
    Image.new("RGB", (12, 8), "red").save(
        stream,
        format="GIF",
        save_all=True,
        append_images=[Image.new("RGB", (12, 8), "blue")],
    )
    original = stream.getvalue()
    converter = ImageConverter()
    result = await converter.convert(original, "image/gif", frozenset({"image/gif"}))
    assert result.data is original
    with pytest.raises(ImageConversionError) as failure:
        await converter.convert(
            original,
            "image/gif",
            frozenset({"image/gif", "image/png"}),
            max_output_bytes=len(original) - 1,
        )
    assert failure.value.reason == "multiple_frames"


@pytest.mark.asyncio
async def test_impossible_byte_limit_remains_an_explicit_failure():
    with pytest.raises(ImageConversionError) as failure:
        await ImageConverter().convert(
            raster("PNG"), "image/png", frozenset({"image/png"}), max_output_bytes=1
        )
    assert failure.value.reason == "output_too_large"
    assert failure.value.max_bytes == 1


@pytest.mark.parametrize(
    "major,compatible,media_type",
    [
        (b"heic", b"mif1", "image/heic"),
        (b"mif1", b"heix", "image/heic"),
        (b"mif1", b"msf1", "image/heif"),
        (b"mif1", b"avif", "image/avif"),
        (b"isom", b"mp42", "video/mp4"),
    ],
)
def test_bmff_image_brands_precede_video_fallback(major, compatible, media_type):
    data = (20).to_bytes(4, "big") + b"ftyp" + major + bytes(4) + compatible
    assert sniff_media_type(data, "wrong.mp4") == media_type
    if media_type in {"image/heic", "image/heif"}:
        assert (
            canonical_extension_for_media_type(media_type)
            == f".{media_type.removeprefix('image/')}"
        )


@pytest.mark.asyncio
async def test_native_multiframe_pixel_budget_counts_every_frame(monkeypatch):
    data = raster("GIF", save_all=True, append_images=[Image.new("RGB", (12, 8), "red")])
    monkeypatch.setattr(image_module, "_PIXEL_LIMIT", 150)
    with pytest.raises(ImageConversionError) as failure:
        await ImageConverter().convert(data, "image/gif", frozenset({"image/gif"}))
    assert failure.value.reason == "image_too_large"
    assert failure.value.pixels == 192


@pytest.mark.asyncio
async def test_full_resolution_jpeg_is_reported_before_resizing():
    # Gradients compress well as JPEG but poorly enough as PNG at this ceiling.
    source = Image.frombytes(
        "RGB", (128, 64), bytes((x * 7 + y * 3) % 256 for y in range(64) for x in range(128 * 3))
    )
    stream = io.BytesIO()
    source.save(stream, format="BMP")
    converter = ImageConverter()
    jpeg = await converter.convert(stream.getvalue(), "image/bmp", frozenset({"image/jpeg"}))
    prepared = await converter.convert(
        stream.getvalue(),
        "image/bmp",
        frozenset({"image/jpeg"}),
        max_output_bytes=len(jpeg.data),
    )
    assert prepared.size == prepared.original_size == (128, 64)
    assert prepared.lossy
    assert "fine details may be reduced" in prepared.note
