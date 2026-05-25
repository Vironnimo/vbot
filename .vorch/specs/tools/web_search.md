# Web Search Tool

Searches the public web through Brave Search and returns normalized results.

## Interfaces

- Tool name: `web_search`
- Registration: `register_web_search_tool(registry, credential_resolver)`
- Schema: required `query`; optional `count`, `freshness`, `date_after`, and `date_before`; `additionalProperties: false`.
- Success data returns normalized Brave results with rank, title, url, description, and trust metadata.
- Display: summary field `query`.

## External Dependencies

- Brave Search API.
- Credential key: `BRAVE_API_KEY`, resolved through runtime env/data-dir credential lookup.

## Constraints & Gotchas

- The tool always registers. Missing Brave credentials produce a `missing_api_key` failure envelope at call time.
- Date/freshness validation errors return `validation_error`.
- Brave/network failures map to `provider_request_failed`.
- Result content is marked as untrusted web content.
