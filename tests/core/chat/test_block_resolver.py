"""ContentBlockResolver: current-turn native delivery, earlier-turn notes, text and Tool images."""

from __future__ import annotations

import asyncio
import base64
import io
import threading
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from core.attachments import AttachmentStore
from core.attachments.images import ImageConverter
from core.chat.block_resolver import ContentBlockResolver
from core.chat.file_mentions import file_mention_request_text
from core.tools.read import render_text_file
from tests.core.chat.block_resolver_test_support import (
    CURRENT,
    EARLIER,
    IMAGE_WIRE,
    TEXT_IMAGE,
    TEXT_IMAGE_AUDIO,
    TEXT_ONLY,
    attachment_message,
    path_note,
    resolve,
)

pytestmark = pytest.mark.usefixtures("current_format_data_directory")


def _png_bytes() -> bytes:
    stream = io.BytesIO()
    Image.new("RGB", (12, 8), "blue").save(stream, format="PNG")
    return stream.getvalue()


PNG_BYTES = _png_bytes()
PDF_BYTES = b"%PDF-1.7\n1 0 obj\n"
MP4_BYTES = b"\x00\x00\x00\x18ftypisomvideo-payload"
TEXT_IMAGE_PDF = frozenset({"text", "image", "pdf"})
TEXT_VIDEO = frozenset({"text", "video"})
IMAGE_PDF_WIRE = IMAGE_WIRE | frozenset({"application/pdf"})
IMAGE_NOTE = "[Image: photo.png (image/png) — Path: {path}]"
VIDEO_NOTE = "[Video: clip.mp4 (video/mp4) — Path: {path}]"
PDF_NOTE = "[File: report.pdf (application/pdf) — Path: {path}]"


@pytest.mark.parametrize(
    ("filename", "payload", "block_type", "message_id", "modalities", "wire", "native", "note"),
    [
        ("photo.png", PNG_BYTES, "media", CURRENT, TEXT_IMAGE, IMAGE_WIRE, True, IMAGE_NOTE),
        (
            "photo.png",
            PNG_BYTES,
            "media",
            EARLIER,
            TEXT_IMAGE,
            IMAGE_WIRE,
            False,
            "[Image from an earlier turn: photo.png (image/png) — Path: {path}]",
        ),
        # A model without vision must not abort the Run: the image degrades to its path.
        (
            "photo.png",
            PNG_BYTES,
            "media",
            CURRENT,
            TEXT_ONLY,
            IMAGE_WIRE,
            False,
            "[Image: photo.png (image/png) — this model has no vision capability, so the "
            "image itself cannot be shown; only the stored file path is provided — Path: {path}]",
        ),
        ("clip.mp4", MP4_BYTES, "media", CURRENT, TEXT_VIDEO, {"video/mp4"}, True, VIDEO_NOTE),
        ("clip.mp4", MP4_BYTES, "media", CURRENT, TEXT_IMAGE_AUDIO, IMAGE_WIRE, False, VIDEO_NOTE),
        ("clip.mp4", MP4_BYTES, "media", EARLIER, TEXT_VIDEO, {"video/mp4"}, False, VIDEO_NOTE),
        ("report.pdf", PDF_BYTES, "file", CURRENT, TEXT_IMAGE_PDF, IMAGE_PDF_WIRE, True, PDF_NOTE),
        ("report.pdf", PDF_BYTES, "file", CURRENT, TEXT_IMAGE, IMAGE_PDF_WIRE, False, PDF_NOTE),
        # An unverified OpenAI-compatible wire cannot carry the PDF the model accepts.
        ("report.pdf", PDF_BYTES, "file", CURRENT, TEXT_IMAGE_PDF, IMAGE_WIRE, False, PDF_NOTE),
        ("report.pdf", PDF_BYTES, "file", EARLIER, TEXT_IMAGE_PDF, IMAGE_PDF_WIRE, False, PDF_NOTE),
    ],
    ids=[
        "image-current",
        "image-earlier",
        "image-without-vision",
        "video-current",
        "video-without-video-modality",
        "video-earlier",
        "pdf-current",
        "pdf-without-pdf-modality",
        "pdf-on-a-wire-without-pdf",
        "pdf-earlier",
    ],
)
def test_attachment_goes_native_only_for_the_current_turn_on_model_and_wire_support(
    tmp_path: Path,
    filename: str,
    payload: bytes,
    block_type: str,
    message_id: str,
    modalities: frozenset[str],
    wire: frozenset[str],
    native: bool,
    note: str,
) -> None:
    store = AttachmentStore(tmp_path)
    record = store.store(filename, payload)
    messages = [attachment_message(record, block_type=block_type, message_id=message_id)]
    persisted = deepcopy(messages)

    resolved = resolve(
        ContentBlockResolver(store),
        messages,
        input_modalities=modalities,
        wire_media_types=frozenset(wire),
    )

    encoded = base64.b64encode(payload).decode("ascii")
    native_block = (
        {"type": "media", "base64": encoded, "media_type": record.media_type}
        if block_type == "media"
        else {
            "type": "document",
            "base64": encoded,
            "media_type": record.media_type,
            "filename": filename,
        }
    )
    # A native block always rides with its path note, so the Agent keeps a file handle.
    assert resolved[0]["content"] == [
        *([native_block] if native else []),
        path_note(record, note),
    ]
    assert messages == persisted


@pytest.mark.parametrize(
    ("message_id", "label"),
    [(CURRENT, "Image 2"), (EARLIER, "Image 2 from an earlier turn")],
)
def test_image_reference_numbers_the_image_label(
    tmp_path: Path, message_id: str, label: str
) -> None:
    store = AttachmentStore(tmp_path)
    record = store.store("photo.png", PNG_BYTES)
    message = attachment_message(record, message_id=message_id, image_reference=2)

    resolved = resolve(ContentBlockResolver(store), [message], input_modalities=TEXT_IMAGE)

    assert resolved[0]["content"][-1] == path_note(
        record, f"[{label}: photo.png (image/png) — Path: {{path}}]"
    )


@pytest.mark.parametrize(
    ("filename", "payload", "message_id", "note"),
    [
        (
            "gone.png",
            PNG_BYTES,
            EARLIER,
            "[Image from an earlier turn: gone.png (image/png) — file no longer available]",
        ),
        (
            "gone.mp4",
            MP4_BYTES,
            CURRENT,
            "[Video: gone.mp4 (video/mp4) — file no longer available]",
        ),
    ],
    ids=["image-earlier", "video-current"],
)
def test_deleted_attachment_degrades_to_an_unavailable_note(
    tmp_path: Path, filename: str, payload: bytes, message_id: str, note: str
) -> None:
    store = AttachmentStore(tmp_path)
    record = store.store(filename, payload)
    store.delete(record.id)

    resolved = resolve(
        ContentBlockResolver(store),
        [attachment_message(record, message_id=message_id)],
        input_modalities=TEXT_IMAGE_AUDIO,
    )

    assert resolved[0]["content"] == [{"type": "text", "text": note}]


def test_text_blocks_keep_their_order_and_string_content_passes_through(tmp_path: Path) -> None:
    store = AttachmentStore(tmp_path)
    record = store.store("photo.png", PNG_BYTES)
    image = attachment_message(record)["content"][0]
    mention = {
        "type": "file_mention",
        "path": "src/app.py",
        "status": "inlined",
        "text": "value = 1\n",
        "size_bytes": 10,
    }
    messages: list[dict] = [
        {"id": "system", "role": "system", "content": "System prompt"},
        {"id": "plain", "role": "user", "content": "Simple text"},
        {"id": EARLIER, "role": "user", "content": [{"type": "text", "text": "hello"}, mention]},
        {"id": CURRENT, "role": "user", "content": [{"type": "text", "text": "Look:"}, image]},
    ]

    resolved = resolve(ContentBlockResolver(store), messages, input_modalities=TEXT_IMAGE)

    assert resolved[:2] == messages[:2]
    # A mention is a durable snapshot: it renders identically on every turn.
    mention_text = {"type": "text", "text": file_mention_request_text(mention)}
    assert resolved[2]["content"] == [{"type": "text", "text": "hello"}, mention_text]
    assert resolve(
        ContentBlockResolver(store),
        [{"id": CURRENT, "role": "user", "content": [mention]}],
        input_modalities=TEXT_ONLY,
    )[0]["content"] == [mention_text]
    assert resolved[3]["content"] == [
        {"type": "text", "text": "Look:"},
        {
            "type": "media",
            "base64": base64.b64encode(PNG_BYTES).decode("ascii"),
            "media_type": "image/png",
        },
        path_note(record, IMAGE_NOTE),
    ]


@pytest.mark.parametrize("message_id", [CURRENT, EARLIER])
def test_text_attachment_renders_like_the_read_tool_and_drops_a_persisted_full_copy(
    tmp_path: Path, message_id: str
) -> None:
    # Older Sessions stored the complete text right after the file block; every request
    # replaces it with the read Tool's bounded rendering. Other following text stays.
    store = AttachmentStore(tmp_path)
    source = b"".join(f"line {index}\n".encode() for index in range(1, 2_500))
    record = store.store("notes.txt", source)
    message = attachment_message(record, block_type="file", message_id=message_id)
    message["content"] += [
        {"type": "text", "text": source.decode("utf-8")},
        {"type": "text", "text": "Please review."},
    ]

    resolved = resolve(
        ContentBlockResolver(store),
        [message],
        input_modalities=frozenset({"text", "image", "file"}),
        wire_media_types=frozenset({"text/plain"}),
    )

    assert resolved[0]["content"] == [
        path_note(record, "[File: notes.txt (text/plain) — Path: {path}]"),
        {"type": "text", "text": render_text_file(source)},
        {"type": "text", "text": "Please review."},
    ]


@pytest.mark.asyncio
async def test_attachment_reads_run_off_the_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Earlier turns and the current turn read attachment metadata and blobs on
    # worker threads; the loop keeps running while a read is blocked.
    store = AttachmentStore(tmp_path)
    notes_text = b"line one\n"
    notes = store.store("notes.txt", notes_text)
    report = store.store("report.pdf", b"%PDF-1.4 test-owned")
    resolver = ContentBlockResolver(store)
    get_record = store.get
    entered = threading.Event()
    release = threading.Event()
    threads: list[int] = []

    def blocked_get(attachment_id: str) -> Any:
        threads.append(threading.get_ident())
        entered.set()
        release.wait(timeout=5)
        return get_record(attachment_id)

    monkeypatch.setattr(store, "get", blocked_get)
    messages = [
        attachment_message(notes, block_type="file", message_id=EARLIER),
        attachment_message(report, block_type="file"),
    ]
    resolving = asyncio.create_task(
        resolver.resolve_messages(
            messages,
            current_user_message_id=CURRENT,
            input_modalities=frozenset({"text", "pdf"}),
            wire_media_types=frozenset({"application/pdf"}),
        )
    )
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        loop = asyncio.get_running_loop()
        ticked_at = loop.time()
        for _ in range(5):
            await asyncio.sleep(0.01)
        assert loop.time() - ticked_at < 1
        assert not resolving.done()
    finally:
        release.set()
    resolved = await asyncio.wait_for(resolving, timeout=5)

    assert threads and threading.get_ident() not in threads
    assert resolved[0]["content"][1] == {"type": "text", "text": render_text_file(notes_text)}
    assert resolved[1]["content"][0]["type"] == "document"


def _bmp_tool_image(path: str, encoded: str) -> dict[str, str]:
    return {"path": path, "filename": Path(path).name, "media_type": "image/bmp", "base64": encoded}


@pytest.mark.asyncio
async def test_local_tool_image_converts_loaded_pixels_without_its_source_file() -> None:
    source = io.BytesIO()
    Image.new("RGB", (12, 8), "blue").save(source, format="BMP")
    image = _bmp_tool_image("removed-original.bmp", base64.b64encode(source.getvalue()).decode())

    parts = await ContentBlockResolver.resolve_tool_image(
        image, TEXT_IMAGE, frozenset({"image/jpeg"}), ImageConverter()
    )

    assert parts[0]["media_type"] == "image/jpeg"
    with Image.open(io.BytesIO(base64.b64decode(parts[0]["base64"]))) as delivered:
        assert delivered.size == (12, 8)
    assert "converted copy" in parts[1]["text"]
    assert "removed-original.bmp" in parts[1]["text"]
    assert image["media_type"] == "image/bmp"


@pytest.mark.asyncio
async def test_local_tool_image_conversion_failure_retains_the_original_path() -> None:
    parts = await ContentBlockResolver.resolve_tool_image(
        _bmp_tool_image("broken.bmp", base64.b64encode(b"broken pixels").decode()),
        TEXT_IMAGE,
        frozenset({"image/png"}),
        ImageConverter(),
    )

    assert len(parts) == 1
    assert parts[0]["type"] == "text"
    assert "damaged or unreadable" in parts[0]["text"]
    assert "broken.bmp" in parts[0]["text"]
