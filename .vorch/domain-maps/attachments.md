# Attachments

Blob-backed file attachment storage and attachment-specific message shaping. Owns persisted blobs under the data directory plus the metadata resolving them into chat content.

## Overview

`core/attachments/` is storage-focused: blobs under `<data_dir>/artifacts/attachments/`, one JSON sidecar per attachment, server-side MIME sniffing/validation, configured size limits. It knows nothing about providers, wire formats, or transport; Chat and channels decide how records become `TextBlock`/`MediaBlock`/`FileBlock`.

## Data Model

- `AttachmentRecord`: `id` (UUID, blob basename), `filename` (display name), `media_type` (server-sniffed), `size_bytes`, `stored_at`, `file_path` (informational - recomputed on read), optional cached `transcription` written on first STT.
- Blob at `<uuid><canonical-extension>` (extension from sniffed type, never client metadata); sidecar `<uuid>.json`. No index, no DB, no cleanup pass.

## Contracts

- Store rejects non-positive limits and exposes `max_size_bytes` so transports reject oversized payloads before materializing bodies. `ensure_within_limit(reported_size)` pre-checks platform-reported sizes before download (`None` skips, leaving the post-download check as backstop).
- `store(filename, data)` checks size, sniffs MIME, enforces the allowlist, appends the canonical extension when the display filename lacks one, writes extension-bearing blob then sidecar atomically in that order - a failed sidecar rolls back the blob, so a present sidecar implies a present blob.
- `get(id)` accepts only lower-cased UUID4 ids (anything else is `AttachmentNotFoundError`/404, not a validation error), re-checks blob existence and sidecar id match, and **recomputes** `file_path` from current data-dir + id + persisted type instead of trusting the stored path - moving the data directory cannot break resolution.
- `sniff_media_type(data, filename)` is the public side-effect-free wrapper (no disk, no allowlist) used by tools to branch before storing; `set_transcription` caches STT results rejecting empty text; expected errors are `AttachmentError`/`NotFound`/`TooLarge`/`TypeNotAllowed`.

## Sniffing & conventions

- MIME comes from bounded magic-bytes only - never client-supplied content types, no libmagic. Signature matches: images, PDF, OOXML, Ogg/MP3/WAVE/FLAC/M4A, MP4/QuickTime/WebM/AVI; UTF-8-decodable input becomes `text/plain`; everything else `application/octet-stream` then allowlist-rejected. Known simplifications: Ogg always classifies audio, EBML always webm.
- Every accepted type has one canonical storage extension; blobs always carry it so filesystem consumers get typed paths even without source filenames, while meaningful original display suffixes survive.
- Legacy OLE Office files disambiguate Word/Excel/PowerPoint via filename extension on top of container magic - the sanctioned client-metadata exception.
- The allowlist covers images, any `text/*`, any `audio/*`/`video/*`, PDF, and Office formats; because sniffing only ever produces `text/plain`, the wildcard allowances are wider than reachable.

## Constraints & Gotchas

- OOXML sniffing opens the uploaded ZIP's `[Content_Types].xml` - an unbounded decompression a within-limit zip bomb could inflate to gigabytes. The reader caps at 1 MiB treating overflow as "not OOXML"; the upload limit bounds compressed bytes only, never make this read unbounded.
- Suffixless blobs from the old layout are invalid - convert explicitly with `scripts/converters/attachment_blob_extensions.py` (pre-flighting, collision-refusing, idempotent).
- `GET /api/attachments/{id}` serves sniffed type with inline disposition and the display filename.
- Media resolution lives in the chat layer as a provider-agnostic intersection: native only when current turn AND model modality AND adapter wire support align; otherwise degraded - always one block in, one or more out, every attachment leaving a `Path:` handle, degradation never aborting a Run. Per-modality policies live in `chat/request-building.md`.
- Tool-produced images use the same resolver without becoming user content: `web_fetch` and remote Tool media persist compact artifacts resolved into request-only content for the active Run. Local `read` images bypass blob storage and transfer loaded pixels in memory; file mentions are not attachment-backed either.
- Text attachments persist as one `FileBlock`; request build reads the blob rendering through the shared capped text renderer, omitting a following duplicate TextBlock. The Model-facing path note rides natively-sent media too by design - agents open blobs with `read`.
- Cleanup of orphaned attachments is explicitly out of scope: no index, GC, or reference counting. Local image reads create no new attachments.
