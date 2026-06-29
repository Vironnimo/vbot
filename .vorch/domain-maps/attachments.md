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
  - `transcription: str | None` — cached speech-to-text result for audio attachments, written by `set_transcription()` on first transcription (default `None`)
- Blob path: `<data_dir>/attachments/<uuid>`
- Sidecar path: `<data_dir>/attachments/<uuid>.json`
- There is no global index, no DB, and no cleanup pass.

## Interfaces

- `AttachmentStore(data_dir: Path, *, max_size_bytes: int = 20_971_520)` — rejects a non-positive `max_size_bytes` with `AttachmentError`
- `AttachmentStore.max_size_bytes` exposes the configured upload limit so transport layers can reject oversized payloads before materializing the full request body.
- `AttachmentStore.ensure_within_limit(reported_size_bytes: int | None) -> None` — pre-check companion to `store`: raises `AttachmentTooLargeError` when a platform-reported size exceeds the limit, so a transport refuses an oversized file before downloading it. A `None` size (platform reported none) skips the pre-check and leaves `store`'s post-download size check as the backstop.
- `sniff_media_type(data: bytes, filename: str) -> str` — public, side-effect-free wrapper over the internal magic-bytes sniffer (`_sniff_mime`). Classifies bytes as image/audio/video/text/etc. **without** touching disk or the allowlist, so callers can decide how to handle a file before storing it. The `read` tool uses it to branch on media type.
- `store(filename: str, data: bytes) -> AttachmentRecord` — checks size, sniffs MIME, validates the allowlist, writes blob and sidecar, extracts `text_content` for `text/*`
- `get(attachment_id: str) -> AttachmentRecord` — loads one attachment record from its sidecar
- `set_transcription(attachment_id: str, transcription: str) -> AttachmentRecord` — persists a cached transcription into the sidecar (rejects empty text with `AttachmentError`)
- `delete(attachment_id: str) -> None` — deletes blob and sidecar if present
- Expected domain errors (all exported from `core.attachments`):
  - `AttachmentError`
  - `AttachmentNotFoundError`
  - `AttachmentTooLargeError`
  - `AttachmentTypeNotAllowedError`

## Conventions

- MIME type is determined server-side from a bounded magic-bytes sniff, never from browser- or Telegram-supplied content types. Detection is magic-bytes only (no libmagic): images, PDF, OOXML, audio (Ogg, ID3/frame-sync MP3, RIFF/WAVE, FLAC, `ftyp` M4A/M4B), and video (`ftyp` MP4/QuickTime, EBML→`video/webm`, RIFF/AVI) are pure signature matches; any UTF-8-decodable input sniffs to `text/plain` (never another `text/*` subtype); everything else becomes `application/octet-stream` and is then rejected by the allowlist. Known sniffing simplifications: Ogg always classifies as `audio/ogg` (Theora video would be mislabeled) and EBML always classifies as `video/webm` (audio-only WebM would be mislabeled).
- Exception to "ignore client metadata": legacy OLE Office files (`.doc`/`.xls`/`.ppt` and their siblings) are disambiguated by the filename extension on top of the OLE magic bytes, because the container alone does not reveal Word vs. Excel vs. PowerPoint.
- The allowlist covers: images (`jpeg`/`png`/`gif`/`webp`), any `text/*`, any `audio/*`, any `video/*`, PDF, and common Office formats (OOXML + legacy OLE). Because the sniffer only ever produces `text/plain` for text, the broad `text/*` allowance is wider than anything actually reachable; the same applies to the `audio/*`/`video/*` prefixes versus the concrete sniffed types.
- Text extraction happens eagerly at `store()` time (UTF-8 decode), not lazily later; `text_content` stays `None` for every non-text type.
- Writes are atomic and ordered: blob first via temp file + `os.replace`, then the sidecar the same way; if the sidecar write fails the blob is rolled back. A present sidecar therefore implies a present blob.
- Logging goes through `vbot.attachments`.
- Storage writes use sidecar JSON beside the blob, not shared registries or indexes.

## Constraints & Gotchas

- `get()` accepts only canonical UUID4 ids: a non-UUID4 id raises `AttachmentNotFoundError` (surfaced as HTTP 404), not a validation error, and ids are lower-cased before lookup. It also re-checks that the blob exists and that the sidecar `id` matches, raising `AttachmentNotFoundError` / `AttachmentError` otherwise.
- `get()` recomputes `file_path` from the current `data_dir` and ignores the path stored in the sidecar — the persisted `file_path` is informational only, so moving the data directory does not break resolution.
- OOXML sniffing opens the uploaded ZIP and reads `[Content_Types].xml`, which is otherwise an unbounded decompression — a within-upload-limit zip bomb could inflate that one entry to gigabytes. `_sniff_ooxml_media_type` therefore decompresses at most `_MAX_OOXML_CONTENT_TYPES_BYTES` (1 MiB) and treats any overflow as "not OOXML". The upload size limit only bounds the *compressed* bytes; the decompressed read must stay bounded here, never made unbounded.
- Media resolution lives in the chat layer (`ContentBlockResolver`) and is a **provider-agnostic intersection**: an attachment goes native only when it is the current turn **and** the model advertises the modality (`input_modalities`) **and** the adapter's wire carries the concrete media type (`wire_media_types`, from `wire_media_support` — see `providers.md`). Both sets are passed into `resolve_messages()`; the resolver holds no provider format constants. Resolution is **one block in, one or more out**: a natively-sent attachment is followed by its path note, so every attachment — native or degraded — leaves the agent a `Path:` handle to the original blob. Anything outside the intersection degrades by per-modality policy:
  - **Image**: native base64 on the current turn when the model has `"image"` and the image type is in the wire set. A current-turn image to a non-vision model still raises `ChatError`. A vision model whose wire cannot carry the type, or any earlier-turn image, degrades to an `[Image from an earlier turn: …]` / `[Image: …]` path note; a missing record degrades to a "file no longer available" note. The native image rides with an `[Image: …]` path note as well, so the agent always holds the file path, not only the pixels.
  - **Audio**: a cached `transcription` always wins (current and earlier turns, no STT call). Otherwise the current turn goes native base64 only when the model has `"audio"` **and** the media type is in the adapter's wire set (WAV/MP3 on OpenAI-compatible wires; image-only adapters carry no audio, so audio degrades even for an audio-capable model — the latent resolver/adapter contradiction this design closes). Everything else — including Ogg voice messages — degrades to a speech-to-text transcription with an "automatic transcription, may contain recognition errors" header (cached via `set_transcription()`, cache-write failures logged). No transcriber or STT failure raises `ChatError`. Earlier-turn audio without a cached transcription degrades to a path note. Native audio and transcription replies both carry a trailing `[Audio: …]` path note too.
  - **Document (`FileBlock`)**: a PDF (`application/pdf` → `pdf` modality; other files → `file` modality) on the current turn goes native — a canonical `{"type":"document",base64,media_type,filename}` block — when the model advertises the modality **and** the wire carries the type (today: OpenAI chat mode, Anthropic). Otherwise (unverified wire, non-capable model, or earlier turn) it stays the `[File: <name> (<type>) — Path: <file_path>]` path note. The native document rides with that path note as well. A `text/*` file reference is always a path note only — never a native document — because its content rides inline in a sibling `TextBlock` (see the text-attachment note below).
  - **Video**: always degrades to a `[Video: …]` path note (no supported provider wire accepts raw video).
  - Other media prefixes raise `ChatError`. `MediaBlock`/`FileBlock` storage stays format-generic; modality + wire scope is a chat-layer decision, not a storage one.
- `ContentBlockResolver.resolve_messages()` is async (transcription is a provider call); runtime injects the `SpeechService` as the resolver's transcriber.
- A text attachment is carried as **both** a `FileBlock` reference (which the resolver renders as a `[File: … — Path: …]` note, so the agent can forward or reopen the original) **and** a `TextBlock` with the content (so the content stays inline in context and durable in history). `text_content` is extracted at `store()` time and echoed by `POST /api/upload`, so clients build that `TextBlock` without re-fetching the blob; the reference block is sent alongside it. A text file with no extracted content sends the reference alone. Both the WebUI composer and the shared channel classifier (`content_blocks_for_attachment`) emit this pair, so inbound platform text files behave identically. Other non-image files stay a single `FileBlock`.
- `file_path` is intentionally surfaced to the chat layer in the path note (`[File: <name> (<type>) — Path: <file_path>]`, and the `[Image:/Audio:/Video: …]` variants) so agents can open the blob with the existing `read` tool or forward it as a file. It now rides alongside every natively-sent image/audio/document too, not only degraded ones. This is by design, not a leak.
- Cleanup of orphaned or deleted-session attachments is explicitly out of scope: there is no index, GC, or reference counting. The `read` tool also promotes disk image files to attachments via `store()` (so an image read grows the blob store), but this stays within the same no-GC policy — see `tools/read.md`.
