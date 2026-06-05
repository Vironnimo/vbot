# Attachments

Blob-backed file attachment storage and attachment-specific message shaping for vBot. Owns persisted blobs under the data directory and the metadata needed to resolve them into chat content.

## Overview

`core/attachments/` is a storage-focused domain for uploaded and downloaded files. It stores blobs under `<data_dir>/attachments/`, writes one sidecar JSON per attachment, sniffs and validates MIME types, enforces the configured max file size, and extracts text content eagerly for `text/*`. It does not know about providers, model wire formats, or server transport. Chat and channel code consume attachment records and decide how they become `TextBlock`, `MediaBlock`, or `FileBlock` content.

## Data Model

- `AttachmentRecord`
  - `id: str` — UUID used as blob basename
  - `filename: str` — user- or platform-visible filename
  - `media_type: str` — server-sniffed MIME type
  - `size_bytes: int`
  - `stored_at: str` — UTC ISO 8601 with explicit offset
  - `file_path: str` — absolute path to the blob on disk
  - `text_content: str | None` — populated only for `text/*`
- Blob path: `<data_dir>/attachments/<uuid>`
- Sidecar path: `<data_dir>/attachments/<uuid>.json`
- There is no global index, no DB, and no cleanup pass.

## Interfaces

- `AttachmentStore(data_dir: Path, *, max_size_bytes: int = 20_971_520)` — rejects a non-positive `max_size_bytes` with `AttachmentError`
- `AttachmentStore.max_size_bytes` exposes the configured upload limit so transport layers can reject oversized payloads before materializing the full request body.
- `store(filename: str, data: bytes) -> AttachmentRecord` — checks size, sniffs MIME, validates the allowlist, writes blob and sidecar, extracts `text_content` for `text/*`
- `get(attachment_id: str) -> AttachmentRecord` — loads one attachment record from its sidecar
- `delete(attachment_id: str) -> None` — deletes blob and sidecar if present
- Expected domain errors:
  - `AttachmentError`
  - `AttachmentNotFoundError`
  - `AttachmentTooLargeError`
  - `AttachmentTypeNotAllowedError`

## Conventions

- MIME type is determined server-side from a bounded magic-bytes sniff, never from browser- or Telegram-supplied content types. Detection is magic-bytes only (no libmagic): images, PDF, and OOXML are pure signature matches; any UTF-8-decodable input sniffs to `text/plain` (never another `text/*` subtype); everything else becomes `application/octet-stream` and is then rejected by the allowlist.
- Exception to "ignore client metadata": legacy OLE Office files (`.doc`/`.xls`/`.ppt` and their siblings) are disambiguated by the filename extension on top of the OLE magic bytes, because the container alone does not reveal Word vs. Excel vs. PowerPoint.
- The allowlist is intentionally narrow: images (`jpeg`/`png`/`gif`/`webp`), any `text/*`, PDF, and common Office formats (OOXML + legacy OLE). Because the sniffer only ever produces `text/plain` for text, the broad `text/*` allowance is wider than anything actually reachable.
- Text extraction happens eagerly at `store()` time (UTF-8 decode), not lazily later; `text_content` stays `None` for every non-text type.
- Writes are atomic and ordered: blob first via temp file + `os.replace`, then the sidecar the same way; if the sidecar write fails the blob is rolled back. A present sidecar therefore implies a present blob.
- Logging goes through `vbot.attachments`.
- Storage writes use sidecar JSON beside the blob, not shared registries or indexes.

## Constraints & Gotchas

- `get()` accepts only canonical UUID4 ids: a non-UUID4 id raises `AttachmentNotFoundError` (surfaced as HTTP 404), not a validation error, and ids are lower-cased before lookup. It also re-checks that the blob exists and that the sidecar `id` matches, raising `AttachmentNotFoundError` / `AttachmentError` otherwise.
- `get()` recomputes `file_path` from the current `data_dir` and ignores the path stored in the sidecar — the persisted `file_path` is informational only, so moving the data directory does not break resolution.
- Image attachments only round-trip on the current user turn: `ContentBlockResolver` sends the current turn's image as base64 (requires `vision_supported`, else `ChatError`) and degrades images from earlier turns to a `[Bild: <filename>]` text placeholder. Non-image `media` blocks on the current turn are hard-rejected (`V1 supports only image/*`). `MediaBlock` storage stays format-generic; this image-only scope is a chat-layer decision, not a storage one.
- Text files never become `FileBlock`s: `text_content` is persisted in the sidecar and echoed by `POST /api/upload`, so clients build a `TextBlock` directly from it without re-fetching the blob. Other non-image files stay `FileBlock`s.
- `file_path` is intentionally surfaced to the chat layer in the `FileBlock` note (`[File: <name> (<type>) — Path: <file_path>]`) so agents can open the blob with the existing `read` tool. This is by design, not a leak.
- Cleanup of orphaned or deleted-session attachments is explicitly out of scope: there is no index, GC, or reference counting.
