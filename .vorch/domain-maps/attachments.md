# Attachments

Blob-backed original-file storage, attachment-specific message shaping, and shared image format conversion. Owns persisted blobs under the data directory plus the metadata resolving them into chat content.

## Overview

`core/attachments/` is storage-focused: blobs under `<data_dir>/artifacts/attachments/`, one JSON sidecar per attachment, server-side MIME sniffing/validation, configured size limits. It knows nothing about Provider identities or wire serialization; Chat and channels decide how records become `TextBlock`/`MediaBlock`/`FileBlock`.

## Data Model

- `AttachmentRecord`: `id` (opaque blob basename; newly generated `att_` plus 12 lowercase base32 characters), `filename` (display name), `media_type` (server-sniffed), `size_bytes`, `stored_at`, `file_path` (the blob's current location, derived on load and never stored), optional cached `transcription`: Chat's speech-to-text of the audio, written the first time a user message carries it and reused when the same attachment arrives again. The user message's audio block carries the transcription the Session shows (`chat/request-building.md` -> Content Blocks).
- Blob at `<id><canonical-extension>` (extension from sniffed type, never client metadata); sidecar `<id>.json`. Sessions index their durable references and retain previously referenced ids for lifecycle collection (below).
- The sidecar is a durable JSON document under the Generation 1 contract (`json-documents.md`; registry kind `attachment_metadata`): `format_version` 1 plus `id`, `filename`, `media_type`, `size_bytes`, `stored_at` and an optional `transcription` (`null` also reads as none). `validate_attachment_metadata_file` backs `doctor config`. Sidecars are not data-snapshot members: a snapshot holds no blobs.

## Contracts

- Store rejects non-positive limits and exposes `max_size_bytes` so transports reject oversized payloads before materializing bodies. `ensure_within_limit(reported_size)` pre-checks platform-reported sizes before download (`None` skips, leaving the post-download check as backstop).
- `store(filename, data)` checks size, sniffs MIME, enforces the allowlist, appends the canonical extension when the display filename lacks one, reserves the id by creating its sidecar exclusively, creates the extension-bearing blob exclusively (`write_new_bytes`: fsync plus POSIX directory fsync, never replacing a file), then publishes the valid sidecar atomically. The sidecar reservation is the id's only uniqueness claim, across every blob extension; nothing lists the attachment directory. A blob already at the target, left by an interrupted store, keeps its bytes: the store releases its reservation and draws a fresh id. A failed write removes the new blob and reservation. Failure to create the attachment folder also raises `AttachmentError`, retaining the underlying `OSError` as its cause so callers can classify storage failures. The sidecar is the publication point: an in-progress or interrupted reservation is invalid metadata, never a readable attachment, even beside a partial blob (`test_attachments.py`).
- `store_async(filename, data)` runs the same `store` on the four-worker `attachments` pool. Event-Loop callers use it (WebUI upload endpoint, Telegram/Discord downloads, network channel adapter); `web_fetch` already stores from its Tool worker.
- `get(id)` accepts bounded lowercase alphanumeric/underscore/hyphen basenames (normalizing case) (anything else is `AttachmentNotFoundError`/404, not a validation error), validates the sidecar (a missing, older or newer `format_version` or an invalid field is an `AttachmentError`; unknown fields are ignored), re-checks blob existence and sidecar id match, and derives `file_path` from current data-dir + id + stored type - moving the data directory cannot break resolution. A missing sidecar is `AttachmentNotFoundError`; an unreadable sidecar is `AttachmentError` naming the read failure with its underlying `OSError` as cause, distinct from invalid metadata.
- `sniff_media_type(data, filename)` is the public side-effect-free wrapper (no disk, no allowlist) used by tools to branch before storing; `set_transcription` caches STT results rejecting empty text, rewrites the sidecar through `write_json_document` (unknown fields kept) and never rewrites one that fails to load; expected errors are `AttachmentError`/`NotFound`/`TooLarge`/`TypeNotAllowed`.
- MIME comes from bounded magic bytes only - never client-supplied content types, no libmagic. Legacy OLE Office files disambiguate Word/Excel/PowerPoint via filename extension on top of container magic - the sanctioned client-metadata exception. Every accepted type has one canonical storage extension that every blob carries. Signatures, allowlist and extensions: `attachments/sniffing.md`.

## Image conversion

`core/attachments/images.py::ImageConverter` prepares an image for given destination MIME types and optional encoded-byte and dimension ceilings; it owns no Provider facts (Chat passes the target Adapter's declaration). Every image is checked and decoded; valid native inputs within the ceilings stay byte-identical, and original files stay untouched. Details: `attachments/image-conversion.md`.

## Constraints & Gotchas

- Suffixless blobs from the layout before typed blobs are invalid; vBot has no converter for them.
- `GET /api/attachments/{id}` serves sniffed type with inline disposition and the display filename. An unknown id and an unreadable/corrupt sidecar (`AttachmentError`) both return 404; the corrupt case also logs a warning, never an HTTP 500 (`tests/server/test_attachment_endpoints.py`).
- Chat decides how records become request content: media routing, per-modality degradation, Tool-produced media and text-attachment rendering live in `chat/request-building.md` -> Content Blocks, mentions, and media. Text attachments persist as one `FileBlock`; file mentions are not attachment-backed.
- `collect_session_orphans(sessions)` reclaims previously Session-referenced blobs whose owners are gone. Runtime calls it before startup admission and after a completely successful asynchronous shutdown drain, before Sessions close. It never overlaps Runs, pending admission or producer writes, and synchronous stop and failed drains skip it. Sessions provide the candidate ids after excluding every retained data snapshot's references; archived Sessions, superseded own audit, fork lineage/materialized copies, structured user/Tool media, Tool display paths and inline attachment handles all retain blobs. Collection deletes only a validated sidecar's regular blob/sidecar pair, refuses redirected directories or links, retries partial removal, and leaves every candidate unchanged if the snapshot inventory cannot be read. `tests/core/attachments/test_attachment_collection.py`, `tests/core/runtime/test_runtime_attachment_collection.py`.
- Never-admitted uploads and historical files with no surviving evidence of prior Session ownership stay untouched: an accessor may still hold an upload for a draft. Expiring those files requires a separate upload-lifetime policy. Collection does not change temporary-file retention, user Workspace files or the archive retention period.

## Agent-facing text

This domain owns the image-conversion note `PreparedImage.note` (`core/attachments/images.py`). Before you change, add or delete a sentence of it, read `attachments/agent-facing-text.md`: it records why each sentence exists. Update its row for every sentence you change and add one for every sentence you add.

## References

Read these only when your task matches - not by default.

- Changing MIME sniffing, signatures, the allowlist, canonical extensions or OOXML detection -> `attachments/sniffing.md`
- Changing `ImageConverter` preparation, ceilings, formats, caches or failure classes -> `attachments/image-conversion.md`
