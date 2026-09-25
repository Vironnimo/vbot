# Attachments

Blob-backed original-file storage, attachment-specific message shaping, and shared image format conversion. Owns persisted blobs under the data directory plus the metadata resolving them into chat content.

## Overview

`core/attachments/` is storage-focused: blobs under `<data_dir>/artifacts/attachments/`, one JSON sidecar per attachment, server-side MIME sniffing/validation, configured size limits. It knows nothing about Provider identities or wire serialization; Chat and channels decide how records become `TextBlock`/`MediaBlock`/`FileBlock`.

## Data Model

- `AttachmentRecord`: `id` (opaque blob basename; newly generated `att_` plus 12 lowercase base32 characters), `filename` (display name), `media_type` (server-sniffed), `size_bytes`, `stored_at`, `file_path` (the blob's current location, derived on load and never stored), optional cached `transcription` written on first STT.
- Blob at `<id><canonical-extension>` (extension from sniffed type, never client metadata); sidecar `<id>.json`. No index, no DB, no cleanup pass.
- The sidecar is a durable JSON document under the Generation 1 contract (`settings.md` -> JSON Document Contract; registry kind `attachment_metadata`): `format_version` 1 plus `id`, `filename`, `media_type`, `size_bytes`, `stored_at` and an optional `transcription` (`null` also reads as none). `validate_attachment_metadata_file` backs `doctor config`. Sidecars are not data-snapshot members: a snapshot holds no blobs. The Generation 1 converter stamped existing sidecars and dropped their stored `file_path` and the retired `text_content` cache.

## Contracts

- Store rejects non-positive limits and exposes `max_size_bytes` so transports reject oversized payloads before materializing bodies. `ensure_within_limit(reported_size)` pre-checks platform-reported sizes before download (`None` skips, leaving the post-download check as backstop).
- `store(filename, data)` checks size, sniffs MIME, enforces the allowlist, appends the canonical extension when the display filename lacks one, reserves the sidecar filename exclusively, then publishes the extension-bearing blob and valid sidecar atomically in that order. Collisions across extensions and orphan blobs retry without replacing files; a failed write removes the new blob and reservation. An interrupted reservation is invalid metadata, never a readable attachment.
- `store_async(filename, data)` runs the same `store` on the four-worker `attachments` pool. Event-Loop callers use it (WebUI upload endpoint, Telegram/Discord downloads, network channel adapter); `web_fetch` already stores from its Tool worker.
- `get(id)` accepts bounded lowercase alphanumeric/underscore/hyphen basenames (normalizing case) (anything else is `AttachmentNotFoundError`/404, not a validation error), validates the sidecar (a missing, older or newer `format_version` or an invalid field is an `AttachmentError`; unknown fields are ignored), re-checks blob existence and sidecar id match, and derives `file_path` from current data-dir + id + stored type - moving the data directory cannot break resolution.
- `sniff_media_type(data, filename)` is the public side-effect-free wrapper (no disk, no allowlist) used by tools to branch before storing; `set_transcription` caches STT results rejecting empty text, rewrites the sidecar through `write_json_document` (unknown fields kept) and never rewrites one that fails to load; expected errors are `AttachmentError`/`NotFound`/`TooLarge`/`TypeNotAllowed`.

## Sniffing & conventions

- BMP sniffing additionally requires zero reserved fields, a recognized DIB header size, and a pixel offset after that header and within the supplied bytes; ordinary UTF-8 text starting with `BM` stays text (`test_attachments.py`).
- ASCII magic words also require their binary header fields, so text such as `ID3 tags` or `OggS notes` stays text for uploads and `read`: GIF needs a `GIF87a`/`GIF89a` header followed, after the optional global colour table, by an extension/image/trailer marker (`0x21`/`0x2C`/`0x3B`; at most 782 leading bytes, so 64 KiB probes and whole uploads agree), ID3 a version byte 2-4 and syncsafe size bytes, Ogg version 0 with valid page flags, FLAC a 34-byte STREAMINFO block header. Known simplification: text whose bytes happen to satisfy these header fields (e.g. `GIF89a` plus a marker character at the block offset) still classifies as that media type.
- Accepted raster originals are JPEG, PNG, GIF, WebP, BMP, TIFF, AVIF, and HEIC/HEIF. ISO-BMFF brands distinguish AVIF first, then HEIC/HEIF, before the video fallback.
- MIME comes from bounded magic-bytes only - never client-supplied content types, no libmagic. Signature matches: images, PDF, OOXML, Ogg/MP3/WAVE/FLAC/M4A, MP4/QuickTime/WebM/AVI; UTF-8-decodable input becomes `text/plain`; everything else `application/octet-stream` then allowlist-rejected. Known simplifications: Ogg always classifies audio, EBML always webm.
- Every accepted type has one canonical storage extension; blobs always carry it so filesystem consumers get typed paths even without source filenames, while meaningful original display suffixes survive.
- Legacy OLE Office files disambiguate Word/Excel/PowerPoint via filename extension on top of container magic - the sanctioned client-metadata exception.
- The allowlist covers images, any `text/*`, any `audio/*`/`video/*`, PDF, and Office formats; because sniffing only ever produces `text/plain`, the wildcard allowances are wider than reachable.

## Constraints & Gotchas

- OOXML sniffing opens the uploaded ZIP's `[Content_Types].xml` - an unbounded decompression a within-limit zip bomb could inflate to gigabytes. The reader caps at 1 MiB treating overflow as "not OOXML"; encrypted entries, unsupported compression and malformed compressed data also stay unrecognized instead of escaping as unexpected exceptions. The upload limit bounds compressed bytes only, never make this read unbounded.
- Suffixless blobs from the layout before typed blobs are invalid. Their explicit converter was removed with Generation 1; a data directory that still has them must first be converted with a vBot release before Generation 1.
- `GET /api/attachments/{id}` serves sniffed type with inline disposition and the display filename. An unknown id and an unreadable/corrupt sidecar (`AttachmentError`) both return 404; the corrupt case also logs a warning, never an HTTP 500 (`tests/server/test_attachment_endpoints.py`).
- Media resolution lives in the chat layer as a provider-agnostic intersection: native only when current turn AND model modality AND adapter wire support align; otherwise degraded - always one block in, one or more out, every attachment leaving a `Path:` handle, degradation never aborting a Run. Per-modality policies live in `chat/request-building.md`.
- Tool-produced images use the same resolver without becoming user content: `web_fetch` and remote Tool media persist compact artifacts resolved into request-only content for the active Run. Local `read` images bypass blob storage and transfer loaded pixels in memory; file mentions are not attachment-backed either.
- Text attachments persist as one `FileBlock`; request build reads the blob rendering through the shared capped text renderer, omitting a following duplicate TextBlock. The Model-facing path note rides natively-sent media too by design - agents open blobs with `read`.
- Cleanup of orphaned attachments is explicitly out of scope: no index, GC, or reference counting. Local image reads create no new attachments.

## Image conversion

`core/attachments/images.py::ImageConverter` accepts source bytes/MIME, destination MIME types, and an optional encoded-file byte ceiling; it owns no Provider facts. Every image is format-checked and decoded, including supported formats. Valid native inputs within the ceiling remain byte-identical. `PreparedImage` returns encoded bytes/type, original/sent dimensions and conversion/loss flags; its optional note explains changed copies while original files stay untouched.

Preparation tries full-resolution PNG, lossless WebP, then JPEG without chroma subsampling (white background for transparency). Only a concrete byte ceiling enables reduced JPEG quality or bounded resize attempts. Orientation is corrected and ICC profiles are preserved, transforming profiled CMYK to sRGB. Native multi-frame files remain intact when accepted and within limits; any needed conversion or reduction refuses to discard frames/pages. A 32-million-pixel decoding budget covers all frames/pages, including native inputs. Classified failures distinguish unreadable data, decoding-size limits, unsupported destinations, multi-frame conversion and encoded-size failure, with recovery text appropriate to each cause.

One bounded worker owns validation and conversion. A 256-entry validation cache retains hashes/dimensions only; per-instance converted-output caches hold at most 32 MiB and key by content hash, source MIME, destination formats and byte ceiling. Pillow and pillow-heif are core dependencies; the HEIF plugin disables thumbnail substitution. Regression coverage: `tests/core/attachments/test_images.py`, plus Chat/read routing and fallback in `tests/core/chat/test_chat_integration_images.py` and `tests/core/chat/test_chat_loop_fallback.py`.
