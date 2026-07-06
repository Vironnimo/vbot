# Web Fetch Tool

Fetches public HTTP(S) content and returns readable text.

## Interfaces

- Tool name: `web_fetch`
- Registration: `register_web_fetch_tool(registry)`
- Schema: required `url`; optional booleans `include_links` and `raw`; `additionalProperties: false`.
- Success data returns extracted or raw text under `data.content`.
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
- Retries transient HTTP 429/5xx up to 3 times with exponential backoff and jitter.
- Non-HTML or `raw: true` responses return truncated response text unchanged.
