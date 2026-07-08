# Web Fetch Tool

Fetches public HTTP(S) content: readable text for pages, a viewable image for image URLs, a short notice for other binaries.

## Interfaces

- Tool name: `web_fetch`
- Registration: `register_web_fetch_tool(registry, *, attachment_store)`. The handler is built by `make_web_fetch_handler(attachment_store)` (factory pattern, mirrors `read`) and is async; the injected store is consulted only for the image branch, every text/binary path is store-independent.
- Schema: required `url`; optional booleans `include_links` and `raw`; `additionalProperties: false`.
- Success `data.content` is text for pages/text responses. An **image** URL instead promotes the fetched bytes to an attachment and returns a `read_media` artifact (same contract as `read` — built via `read_media_artifact`) so the chat loop injects the image as a current-turn message for a vision model; a non-image **binary** returns a short `[Binary content …]` notice string.
- Display: summary field `url`.

## External Dependencies

- Uses `curl_cffi`'s `AsyncSession` with Chrome browser impersonation for HTTP (real TLS/HTTP-2 fingerprint, so fingerprint-based bot walls — Cloudflare, Akamai, DataDome — that reject a plain HTTP client no longer see one). `impersonate` target is `_IMPERSONATE_TARGET` (`"chrome"`, latest supported). Interactive JS-challenge interstitials (e.g. Cloudflare Turnstile) still can't be passed by any non-browser client — that's expected.
- Uses BeautifulSoup for HTML-to-text extraction.

## Constraints & Gotchas

- Allows only `http` and `https` URLs.
- Validates request targets after parsing and DNS resolution; rejects localhost/private/link-local/multicast/reserved targets, including obfuscated IP forms.
- Connects to the exact IP that cleared validation, pinned per request through curl's `CurlOpt.RESOLVE` map (`resolve_map` accumulates `(host, port) -> ip` across hops), so a DNS-rebinding answer cannot swap in a private address between validation and connection; the hostname still drives the Host header and TLS SNI/cert check.
- The session carries an automatic cookie jar across redirect hops, so a challenge/clearance cookie set on one hop is presented on the next.
- Follows redirects manually with validation per hop (redirects disabled on the session; each hop re-validated and re-pinned).
- The single network call is the `_http_get(session, url)` seam — tests substitute it to feed canned `_FetchResult`s without touching the network (curl_cffi is not respx-mockable).
- Retries transient HTTP 429/5xx up to 3 times with exponential backoff and jitter, honoring a server `Retry-After` hint as a floor (parsed via `core/utils/http_status.parse_retry_after`, delay math via `core/utils/retry.compute_retry_delay`).
- **Response shaping by media type** (after a 2xx). The response bytes are captured (`_FetchResult.content`) and sniffed with `sniff_media_type` (magic bytes, see `attachments.md`); the branch is chosen from the sniff plus the content-type header, not the URL extension:
  - **Image** → promoted to an attachment via the injected store (reusing its size limit and MIME allowlist) plus a `read_media` artifact, exactly like `read`'s image branch. A non-vision model degrades to a text note **in the chat loop** (`_inject_read_media`), not here; an attachment-store rejection (oversize / disallowed type) maps to an `attachment_error` failure; a missing store degrades to a note.
  - **Binary** (audio/video/PDF/archive/executable — anything the sniff does not call `text/*`, or any payload with a NUL in its leading bytes) → a short `[Binary content at <url> (<type>, <size> bytes)…]` notice instead of decoded mojibake.
  - **Text** → returned as before: an HTML content-type is cleaned to readable text unless `raw`; other text (JSON/XML/plain, or unlabeled UTF-8) is returned truncated. A **textual content-type** (`text/*`, or one containing `html`/`json`/`xml`/`javascript`) always forces the text path even for non-UTF-8 bytes, so legacy-charset text is never mistaken for binary.
- **Image and binary handling apply regardless of `raw`** — `raw` only ever governed HTML text cleaning; there is no textual "raw" form of an image, and raw bytes of an executable are still garbage.
