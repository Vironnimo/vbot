# Web Fetch Tool

Fetches public HTTP(S) content: readable text for pages, a viewable image for image URLs, a short notice for other binaries.

## Interfaces

- Tool name: `web_fetch`
- Registration: `register_web_fetch_tool(registry, *, attachment_store)`. The handler is built by `make_web_fetch_handler(attachment_store)` (factory pattern, mirrors `read`) and is async; DNS validation and HTTP transport stay on the async path, while successful response sniffing, document/HTML parsing, output shaping, and image attachment persistence run through the Tool worker boundary. The injected store is consulted only for the image branch; every text/binary path is store-independent.
- Schema: required `url`; optional `output` is exactly one of `markdown` (cleaned HTML with Markdown link targets), `text` (cleaned HTML without link targets), or `raw` (HTML without cleanup) and defaults in the handler to `markdown` when omitted. The model-facing schema omits `additionalProperties` and JSON Schema defaults. The handler still rejects unknown arguments, and the former `include_links` and `raw` booleans remain invalid legacy fields. The OpenAI wire preserves the canonical optional field and explicitly sends `strict: false`; it never rewrites omission as required-nullable input.
- Success `data.content` is text for pages/text responses. An **image** URL instead promotes the fetched bytes to an attachment and returns a `read_media` artifact (same contract as `read` — built via `read_media_artifact`) so Chat resolves the image as Run-local content on the correlated Tool Result for a vision Model; a non-image **binary** returns a short `[Binary content …]` notice string.
- Display: summary field `url`.

## External Dependencies

- Uses `curl_cffi`'s `AsyncSession` with Chrome browser impersonation for HTTP (real TLS/HTTP-2 fingerprint, so fingerprint-based bot walls — Cloudflare, Akamai, DataDome — that reject a plain HTTP client no longer see one). `impersonate` target is `_IMPERSONATE_TARGET` (`"chrome"`, latest supported). Interactive JS-challenge interstitials (e.g. Cloudflare Turnstile) still can't be passed by any non-browser client — that's expected.
- Every response body is streamed into a bounded 50 MB buffer, not materialized by the HTTP client first; an oversized declared or chunked response ends as the non-retryable `response_too_large` failure.
- Uses BeautifulSoup for HTML-to-text extraction.
- PDF/Word/Excel text extraction is delegated to `core/tools/read_extract.py` (shared with the read tool: `pypdf` for PDF, stdlib `zipfile`/`xml.etree` for Office), not owned here.

## Constraints & Gotchas

- Allows only `http` and `https` URLs.
- Validates request targets after parsing and DNS resolution; rejects localhost/private/link-local/multicast/reserved targets, including obfuscated IP forms.
- Connects to the exact IP that cleared validation, pinned per request through curl's `CurlOpt.RESOLVE` map (`resolve_map` accumulates `(host, port) -> ip` across hops), so a DNS-rebinding answer cannot swap in a private address between validation and connection; the hostname still drives the Host header and TLS SNI/cert check.
- The session carries an automatic cookie jar across redirect hops, so a challenge/clearance cookie set on one hop is presented on the next.
- Follows redirects manually with validation per hop (redirects disabled on the session; each hop re-validated and re-pinned).
- The single network call is the `_http_get(session, url)` seam — tests substitute it to feed canned `_FetchResult`s without touching the network (curl_cffi is not respx-mockable).
- Retries transient transport failures and HTTP 429/5xx up to 3 times with exponential backoff and jitter, honoring a server `Retry-After` hint as a jittered floor (parsed via `core/utils/http_status.parse_retry_after`, delay math via `core/utils/retry.compute_retry_delay`).
- **Response shaping by media type** (after a 2xx). The response bytes are captured (`_FetchResult.content`) and sniffed with `sniff_media_type` (magic bytes, see `attachments.md`); the branch is chosen from the sniff plus the content-type header, not the URL extension:
  - **Image** → promoted to an attachment via the injected store (reusing its size limit and MIME allowlist) plus a `read_media` artifact, exactly like `read`'s image branch. Chat attaches the resolved media or non-vision path note to the correlated request-local Tool Result; an attachment-store rejection (oversize / disallowed type) maps to an `attachment_error` failure; a missing store degrades to a note.
  - **Document** (PDF/Word/Excel, via `detect_extractable_document` on the sniffed type or URL filename — checked *before* the binary branch) → returned as extracted text: `[Extracted text from <url> (<label>)]\n---\n<body>`, capped at the shared 100 KB. Its shared extractor admits at most 128 MB of uncompressed document content, so an Office archive cannot inflate without bound; an overflow returns the non-retryable `document_too_large` failure. A scanned PDF with no text layer becomes `(no extractable text)`; a malformed document returns `None` and falls through to the binary/text path.
  - **Binary** (audio/video/archive/executable — anything the sniff does not call `text/*` and is not an extractable document, or any payload with a NUL in its leading bytes; a PDF that failed extraction lands here too) → a short `[Binary content at <url> (<type>, <size> bytes)…]` notice instead of decoded mojibake.
  - **Text** → an HTML content-type follows `output`: `markdown` cleans it while preserving Markdown links, `text` cleans it without link targets, and `raw` returns the HTML unchanged; other text (JSON/XML/plain, or unlabeled UTF-8) is returned truncated regardless of the selected HTML mode. A **textual content-type** (`text/*`, or one containing `html`/`json`/`xml`/`javascript`) always forces the text path even for non-UTF-8 bytes, so legacy-charset text is never mistaken for binary.
- **Image, document, non-HTML text, and binary handling apply regardless of `output`** — the mode controls only HTML shaping; there is no textual `raw` form of an image, and raw bytes of an executable are still garbage.
