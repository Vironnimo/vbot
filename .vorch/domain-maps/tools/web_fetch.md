# Web Fetch Tool

Fetches public HTTP(S) content and returns readable text.

## Interfaces

- Tool name: `web_fetch`
- Registration: `register_web_fetch_tool(registry)`
- Schema: required `url`; optional booleans `include_links` and `raw`; `additionalProperties: false`.
- Success data returns extracted or raw text under `data.content`.
- Display: summary field `url`.

## External Dependencies

- Uses `httpx.AsyncClient` for HTTP.
- Uses BeautifulSoup for HTML-to-text extraction.

## Constraints & Gotchas

- Allows only `http` and `https` URLs.
- Validates request targets after parsing and DNS resolution; rejects localhost/private/link-local/multicast/reserved targets, including obfuscated IP forms.
- Connects to the exact IP that cleared validation (pinned via a custom httpcore network backend), so a DNS-rebinding answer cannot swap in a private address between validation and connection; the hostname still drives the Host header and TLS SNI/cert check.
- Follows redirects manually with validation per hop.
- Retries transient HTTP 429/5xx up to 3 times with exponential backoff and jitter.
- Non-HTML or `raw: true` responses return truncated response text unchanged.
