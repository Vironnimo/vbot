# Web Search Tool

Searches the public web through the configured first-party search provider and returns normalized results.

## Interfaces

- Tool name: `web_search`
- Registration: `register_web_search_tool(registry, credential_resolver, settings_resolver=None)`
- Schema: required non-empty `query`; optional `domains` (one to ten non-empty hostnames, each at most 253 characters), `count` (1–20), `page` (1–10, 1-based), `freshness`, `date_after`, and `date_before` (`YYYY-MM-DD` pattern); `additionalProperties: false`. When `count` is omitted, the settings default applies (`web_search.default_count`, 12 out of the box) — the schema deliberately carries no static `default` for `count` so the model is not told a value the settings may override. Search operators written directly in `query` pass through unchanged when `domains` is omitted.
- `domains` is the provider-neutral hard output restriction: entries are IDNA-normalized, case-insensitively deduplicated hostnames without scheme, port, path, query, or wildcard. A domain matches itself and its subdomains; selecting a specific subdomain narrows that boundary. The tool adds `site:` operators to the provider query, then independently filters normalized result URLs by parsed hostname so provider leakage, suffix lookalikes, query-string mentions, and malformed URLs cannot escape the requested boundary. Multiple domains share one provider request and are joined with `OR`; `count` remains a maximum and may be undershot after filtering.
- Success data returns normalized results with provider, rank, title, url, description, optional `page_age` (page publish/age date when the provider reports one; omitted otherwise), and trust metadata; the payload echoes `page` and, for Brave, `more_results_available`. A domain-restricted success also returns the normalized `applied_domains` list.
- Result text is flattened to plain text: HTML tags are stripped and entities unescaped (Brave is additionally asked for undecorated snippets via `text_decorations=false`).
- Display: summary field `query`.
- Count/page bounds and the built-in default live in `core/search_config.py` (`DEFAULT_WEB_SEARCH_COUNT`, `MIN/MAX_WEB_SEARCH_COUNT`, `MAX_WEB_SEARCH_PAGE`), shared with the settings layer.

## External Dependencies

- Provider selection comes from `settings.json` key `web_search.provider`; supported values are `brave` and `searxng`. `web_search.default_count` (integer 1–20) sets the result count used when the call omits `count`.
- Brave Search API uses credential key `BRAVE_API_KEY`, resolved through runtime env/data-dir credential lookup. Pagination maps `page` to Brave's zero-based `offset` (in units of `count`); `page_age` comes from Brave's `page_age`/`age` fields.
- SearXNG uses `settings.web_search.searxng.base_url` and calls `<base_url>/search` with `format=json`; the SearXNG instance must allow JSON output in its own `search.formats` setting. Pagination maps `page` to `pageno`; `page_age` comes from `publishedDate`. Its configured engines may ignore `site:` syntax, so domain-restricted results carry a completeness warning while the tool's post-filter still guarantees that returned URLs belong to `applied_domains`.

## Constraints & Gotchas

- The tool always registers. Missing Brave credentials produce a `missing_api_key` failure envelope at call time only when Brave is the selected provider.
- Provider choice is not exposed as a tool argument; the Settings selection is the source of truth so agents cannot choose a different provider per call.
- Settings are resolved at call time (before argument-count parsing, since the default comes from them); an invalid `web_search` settings section returns `configuration_error`.
- Date/freshness validation errors return `validation_error`.
- Provider/network failures map to `provider_request_failed`.
- Transient-status retries honor a server `Retry-After` hint as a floor (parsed via `core/utils/http_status.parse_retry_after`, delay math via `core/utils/retry.compute_retry_delay`).
- SearXNG supports `time_range` values `day`, `month`, and `year`; unsupported exact date filters and week freshness are ignored with warnings in the tool result.
- Result content is marked as untrusted web content.
