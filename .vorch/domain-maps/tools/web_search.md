# Web Search Tool

Searches the public web through the configured first-party search provider and returns normalized results.

## Interfaces

- Tool name: `web_search`
- Registration: `register_web_search_tool(registry, credential_resolver, settings_resolver=None)`
- Model-facing schema: required non-empty `query`; optional `domains` (one to ten non-empty hostnames, each at most 253 characters), `count` (1–20), `page` (1–10, 1-based), and provider-neutral `recency` (`day`, `month`, or `year`). It omits `additionalProperties`; the handler rejects unknown or malformed arguments. When `count` is omitted, the settings default applies (`web_search.default_count`, 12 out of the box), so `count` deliberately has no static schema default. `page` retains the stable numeric schema default `1`, which the handler also applies. Search operators written directly in `query` pass through unchanged when `domains` is omitted.
- `domains` is the provider-neutral hard output restriction: entries are IDNA-normalized, case-insensitively deduplicated hostnames without scheme, port, path, query, or wildcard. A domain matches itself and its subdomains; selecting a specific subdomain narrows that boundary. The tool adds `site:` operators to the provider query, then independently filters normalized result URLs by parsed hostname so provider leakage, suffix lookalikes, query-string mentions, and malformed URLs cannot escape the requested boundary. Multiple domains share one provider request and are joined with `OR`; `count` remains a maximum and may be undershot after filtering.
- Success data returns normalized results with provider, rank, title, url, description, optional `page_age` (page publish/age date when the provider reports one; omitted otherwise), and trust metadata; the payload echoes `page`, canonical `recency` when requested, and, for Brave, `more_results_available`. Provider wire values such as Brave `pm` and SearXNG `time_range` never leak into the Agent contract. A domain-restricted success also returns the normalized `applied_domains` list.
- Result text is flattened to plain text: HTML tags are stripped and entities unescaped (Brave is additionally asked for undecorated snippets via `text_decorations=false`).
- Display: summary field `query`.
- Count/page bounds and the built-in default live in `core/search_config.py` (`DEFAULT_WEB_SEARCH_COUNT`, `MIN/MAX_WEB_SEARCH_COUNT`, `MAX_WEB_SEARCH_PAGE`), shared with the settings layer.

## External Dependencies

- Provider selection comes from `settings.json` key `web_search.provider`; supported values are `brave` and `searxng`. `web_search.default_count` (integer 1–20) sets the result count used when the call omits `count`.
- Brave Search API uses credential key `BRAVE_API_KEY`, resolved through runtime env/data-dir credential lookup. Pagination maps `page` to Brave's zero-based `offset` (in units of `count`); `recency` maps `day`/`month`/`year` to `pd`/`pm`/`py`; `page_age` comes from Brave's `page_age`/`age` fields.
- SearXNG uses `settings.web_search.searxng.base_url` and calls `<base_url>/search` with `format=json`; the SearXNG instance must allow JSON output in its own `search.formats` setting. Pagination maps `page` to `pageno`; canonical `recency` maps directly to `time_range`; `page_age` comes from `publishedDate`. Its configured engines may ignore `site:` syntax or recency, so domain-restricted results carry a completeness warning while the tool's post-filter still guarantees that returned URLs belong to `applied_domains`, and recency-restricted results disclose the engine-level limitation.

## Constraints & Gotchas

- The tool always registers. Missing Brave credentials produce a `missing_api_key` failure envelope at call time only when Brave is the selected provider.
- Provider choice is not exposed as a tool argument; the Settings selection is the source of truth so agents cannot choose a different provider per call.
- Settings are resolved at call time (before argument-count parsing, since the default comes from them); an invalid `web_search` settings section returns `configuration_error`.
- Invalid `recency` values return `validation_error`; the retired `freshness`, `date_after`, and `date_before` fields are rejected as unknown arguments.
- Provider/network failures map to `provider_request_failed`.
- Transient-status retries honor a server `Retry-After` hint as a floor (parsed via `core/utils/http_status.parse_retry_after`, delay math via `core/utils/retry.compute_retry_delay`).
- `recency` is the provider-adapter invariant for current and future providers: every adapter must translate `day`, `month`, and `year` into a real upstream request parameter and echo the canonical value on success. An adapter must never silently omit a requested window.
- Result content is marked as untrusted web content.
