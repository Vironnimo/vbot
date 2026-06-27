# Read Tool

Reads a file from the Agent workspace or an absolute path. Text files return their contents with a compact `N|` line-number gutter; image, audio, and video files are handled by media type.

## Interfaces

- Tool name: `read`
- Registration: `register_read_tool(registry, *, attachment_store, speech_service)`. The handler is built by `make_read_handler(attachment_store, speech_service)` (factory pattern, mirrors `image_generation`) and is **async** — the runtime executor already awaits async tool handlers.
- Schema: required `path`; optional positive 1-indexed `offset` and `limit` line controls; `additionalProperties: false`.
- Success: `{ ok: true, data: { content }, error: null, artifacts: [...] }`. `data` is always `{ content }`; `artifacts` is empty except for the image branch.
- Display: summary field `path`.

## Conventions

- Relative paths resolve from `ToolContext.workspace`; absolute paths are allowed.
- `read` is the authoritative read-like tool and must not include a provider/tool parameter named `description`.
- Successful results do not include `data.path`; the agent already knows the requested path from arguments.
- After the path is confirmed to be a file, the bytes are read once and classified with `sniff_media_type` (see `attachments.md`). The branch is chosen by the sniffed media type, not the file extension — **except** the Office/notebook extraction branch, which is chosen by extension (see below).

## Behavior by media type

- **text/\*** or anything not image/audio/video → a leading UTF-8 BOM is stripped, then UTF-8 decode with replacement, then each line is prefixed with a compact, unpadded `N|` reference gutter (file-absolute line numbers starting at the requested offset). Line/byte truncation (2000 lines or 50 KB) is applied *after* numbering, so the gutter counts against the byte budget. No attachment is created.
- **extractable documents** (`.ipynb`/`.docx`/`.xlsx`, matched by *extension* via `is_extractable_document`, checked *after* media routing but *before* the binary check) → rendered as plain text by `core/tools/read_extract.py` (stdlib only — `zipfile` + `xml.etree` + `json`, no third-party doc libs). Output is `[Extracted text from <name> (<label>)]:\n<body>` where the body is gutter-free (a rendering is not editable source) but still passes the shared line/byte truncation (`_render_text(..., number=False)`). docx → paragraph lines (tabs/breaks preserved); xlsx → tab-separated rows per sheet with a `# Sheet: <name>` header and row/column caps; ipynb → `# Cell N [type]` blocks. The branch is by extension because `.ipynb` sniffs as `text/plain` and docx/xlsx are NUL-laden zips that the binary check would otherwise dismiss. A malformed document raises `ExtractionError`, and the handler falls through to the binary-notice / text path instead of failing.
- **binary** (a NUL byte in the leading `_BINARY_DETECTION_BYTES`, checked *after* media routing so PNG/MP3/MP4 still take their own branch) → a short `[Binary file: <name> — Path: <resolved>]` notice instead of decoding to replacement-character garbage. NUL detection is content-based (more robust than an extension list) and catches binaries that happen to decode as valid UTF-8; non-UTF-8 *text* without a NUL still renders with replacement characters.
- **image/\*** → promote to an attachment via `AttachmentStore.store()` (reusing its size limit and MIME allowlist), then return a short note plus a `read_media` artifact `{kind: "read_media", attachment_id, filename, media_type}`. The chat loop consumes that artifact to inject the image as a synthetic current-turn user message so a vision model actually sees it (see `chat.md`). The tool itself never sends image bytes to the model.
- **audio/\*** → transcribe via `SpeechService.transcribe(bytes, filename=…, media_type=…)`; `data.content` is `[Transcription of <name> (<type>)]:\n<text>`. Transcription is plain text and legal in a tool result on every provider, so no message injection is needed.
- **video/\*** → a `[Video: <name> (<type>) — Path: <resolved>]` path note only; no attachment, no artifact (no provider wire accepts raw video).

## Constraints & Gotchas

- Expected file, argument, and read-time filesystem errors return failure envelopes (`invalid_arguments`, `file_not_found`, `not_a_file`, `file_read_error`).
- Image promotion failures (oversize or disallowed type from the attachment store) map to an `attachment_error` failure envelope, never a crash.
- Audio STT failures and empty/whitespace transcriptions map to a `transcription_failed` failure envelope; the run is never aborted.
- Each image read creates one attachment with no garbage collection, matching the existing attachment policy (GC is out of scope — see `attachments.md`).
- The `N|` gutter is display-only. A model that echoes it back into `write`/`edit` would corrupt the file with line-number prefixes, so both reject content dominated by a *consecutive* `N|` gutter with a `line_numbered_content` failure. The detector (`looks_like_line_numbered_content`) and the shared `LINE_NUMBER_GUTTER_SEPARATOR` live in `core/tools/arguments.py`. The gutter is the read tool's alone — prompt file inclusion (`{include:...}`) and the memory block render raw file content without it.
- A leading UTF-8 BOM is stripped on read so the model never sees a phantom `U+FEFF`. The `write` tool re-adds a BOM the file already had (see `write.md`) and `edit` round-trips it (the `U+FEFF` stays in the read content and is re-encoded on write), so the read→edit/write loop does not silently drop the marker.
