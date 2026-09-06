# Attachments

Blob-backed original-file storage, attachment-specific message shaping, and shared image format conversion. Owns persisted blobs under the data directory plus the metadata resolving them into chat content.

## Overview

`core/attachments/` is storage-focused: blobs under `<data_dir>/artifacts/attachments/`, one JSON sidecar per attachment, server-side MIME sniffing/validation, configured size limits. It knows nothing about Provider identities or wire serialization; Chat and channels decide how records become `TextBlock`/`MediaBlock`/`FileBlock`.

## Data Model

- `AttachmentRecord`: `id` (opaque blob basename; newly generated `att_` plus 12 lowercase base32 characters), `filename` (display name), `media_type` (server-sniffed), `size_bytes`, `stored_at`, `file_path` (informational - recomputed on read), optional cached `transcription` written on first STT.
- Blob at `<id><canonical-extension>` (extension from sniffed type, never client metadata); sidecar `<id>.json`. No index, no DB, no cleanup pass.

## Contracts

- Store rejects non-positive limits and exposes `max_size_bytes` so transports reject oversized payloads before materializing bodies. `ensure_within_limit(reported_size)` pre-checks platform-reported sizes before download (`None` skips, leaving the post-download check as backstop).
- `store(filename, data)` checks size, sniffs MIME, enforces the allowlist, appends the canonical extension when the display filename lacks one, reserves the sidecar filename exclusively, then publishes the extension-bearing blob and valid sidecar atomically in that order. Collisions across extensions and orphan blobs retry without replacing files; a failed write removes the new blob and reservation. An interrupted reservation is invalid metadata, never a readable attachment.
- `get(id)` accepts bounded lowercase alphanumeric/underscore/hyphen basenames (normalizing case) (anything else is `AttachmentNotFoundError`/404, not a validation error), re-checks blob existence and sidecar id match, and **recomputes** `file_path` from current data-dir + id + persisted type instead of trusting the stored path - moving the data directory cannot break resolution.
- `sniff_media_type(data, filename)` is the public side-effect-free wrapper (no disk, no allowlist) used by tools to branch before storing; `set_transcription` caches STT results rejecting empty text; expected errors are `AttachmentError`/`NotFound`/`TooLarge`/`TypeNotAllowed`.

## Sniffing & conventions

- Accepted raster originals are JPEG, PNG, GIF, WebP, BMP, TIFF, and AVIF; AVIF brands are checked before the shared ISO-BMFF video fallback.
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

## Image conversion

`core/attachments/images.py::ImageConverter` is the shared byte-to-byte format compatibility service used by Chat and isolated image understanding. It accepts source bytes/MIME plus the caller's target MIME set; no Provider facts live here. Native formats pass unchanged. Necessary conversion prefers PNG, then lossless WebP, then JPEG quality 95 without chroma subsampling; JPEG composites transparency onto white. Orientation is applied without resizing, ICC profiles are preserved (profiled CMYK is transformed to sRGB), and original blobs are never rewritten. One bounded worker owns decoding and per-instance 32 MiB output caches keyed by source-content hash and destination MIME. Conversion refuses more than 32 million pixels, malformed inputs, unsupported destinations, and multi-frame/multi-page inputs rather than silently taking a first frame. Structured ImageConversionError reasons let each caller author its existing path-note/Tool-error response. Pillow is a core dependency. Source and pixel-preservation/target-switch/cache tests: `tests/core/attachments/test_images.py`; full Chat read routing is exercised in `tests/core/chat/test_chat_integration.py`.
