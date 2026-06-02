# Web Search Tool

Searches the public web through the configured first-party search provider and
returns normalized results.

## Interfaces

- Tool name: `web_search`
- Registration: `register_web_search_tool(registry, credential_resolver, settings_resolver=None)`
- Schema: required `query`; optional `count`, `freshness`, `date_after`, and `date_before`; `additionalProperties: false`.
- Success data returns normalized results with provider, rank, title, url, description, and trust metadata.
- Display: summary field `query`.

## External Dependencies

- Provider selection comes from `settings.json` key `web_search.provider`;
  supported values are `brave` and `searxng`.
- Brave Search API uses credential key `BRAVE_API_KEY`, resolved through runtime
  env/data-dir credential lookup.
- SearXNG uses `settings.web_search.searxng.base_url` and calls
  `<base_url>/search` with `format=json`; the SearXNG instance must allow JSON
  output in its own `search.formats` setting.

## Constraints & Gotchas

- The tool always registers. Missing Brave credentials produce a `missing_api_key` failure envelope at call time only when Brave is the selected provider.
- Provider choice is not exposed as a tool argument; the Settings selection is the source of truth so agents cannot choose a different provider per call.
- Date/freshness validation errors return `validation_error`.
- Provider/network failures map to `provider_request_failed`.
- SearXNG supports `time_range` values `day`, `month`, and `year`; unsupported
  exact date filters and week freshness are ignored with warnings in the tool result.
- Result content is marked as untrusted web content.
