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

- `AttachmentStore(data_dir: Path, *, max_size_bytes: int = 20_971_520)`
- `AttachmentStore.max_size_bytes` exposes the configured upload limit so transport layers can reject oversized payloads before materializing the full request body.
- `store(filename: str, data: bytes) -> AttachmentRecord` — sniffs MIME, validates allowlist and size, writes blob and sidecar, extracts `text_content` for `text/*`
- `get(attachment_id: str) -> AttachmentRecord` — loads one attachment record from its sidecar
- `delete(attachment_id: str) -> None` — deletes blob and sidecar if present
- Expected domain errors:
  - `AttachmentError`
  - `AttachmentNotFoundError`
  - `AttachmentTooLargeError`
  - `AttachmentTypeNotAllowedError`

## Conventions

- MIME type is always determined server-side from bytes; do not trust browser or Telegram metadata as authoritative.
- The allowlist is intentionally narrow: images, `text/*`, PDF, and common Office formats only.
- Text extraction happens eagerly at `store()` time, not lazily later.
- Logging goes through `vbot.attachments`.
- Storage writes use sidecar JSON beside the blob, not shared registries or indexes.

## Constraints & Gotchas

- `MediaBlock` stays generic at the chat layer, but attachment storage itself does not decide product scope; the chat layer currently uses image media only.
- Text files are fully embedded later as `TextBlock`s; non-text files remain file references unless the chat layer has a specific binary-resolution path.
- `file_path` is intentionally surfaced to the chat layer for `FileBlock` notes so agents can use the existing `read` tool. This is by design, not a leak.
- Cleanup of orphaned or deleted-session attachments is explicitly out of scope.
