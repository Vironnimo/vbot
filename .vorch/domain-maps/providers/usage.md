# Provider Usage

Read this reference only for live Provider subscription limits, usage fetchers/parsers, caching, polling backend behavior, or the `provider.usage` RPC. Session usage/cost statistics are separate domains.

## Boundary

`ProviderUsageService` probes a logged-in Connection's live upstream subscription state: rolling-window percent used, reset time, plan, and fetch error. It does not read or aggregate vBot Sessions and owns no persistence. `core/statistics/` remains a read-only local aggregation; the WebUI merely presents both behind Statistics subviews.

The service is exposed by `provider.usage` and never participates in Chat execution. Its local `UsageProbeRuntime` Protocol avoids importing concrete Runtime; an injectable transport keeps tests offline.

## Report contract

The frozen serializable projection is:

- `UsageWindow`: label, `used_percent` clamped to 0–100, and ISO-8601 UTC `reset_at` or null.
- `ProviderUsageSnapshot`: compositional Connection id, display name, optional plan, windows, and optional error.
- `UsageReport`: generation timestamp and Provider snapshots.

`report(connections=None)` supports an optional Connection filter. Only Connections with registered fetchers and `is_usable()` are targets; disabled or uncredentialed Connections are never probed. A snapshot with neither windows nor an error is omitted.

## Concurrency, cache, and failures

Fetchers fan out concurrently. Each has a bounded timeout and fails open into its own snapshot: timeout, HTTP status, unsupported/invalid shape, or generic unavailable. One Provider cannot fail siblings.

Successful snapshots cache per Connection for 10 seconds; error snapshots for 60 seconds. A per-Connection async lock coalesces concurrent cache misses so multiple browser windows do not multiply outbound requests. The service caches normalized snapshots, never raw OAuth tokens.

Every fetch acquires fresh auth through Runtime token getters or reads narrowly required token-store extras. Logs include no token data. Provider-specific endpoint/header/shape facts remain in each Provider's map.

## Supported Connections

- `openai:subscription`: ChatGPT usage windows/credits and account-scoped Codex headers; verified endpoint details in `providers/openai.md`.
- `github-copilot:oauth`: Copilot entitlement/usage using stored GitHub OAuth extra; details in `providers/github-copilot.md`.
- `minimax:api-key`: MiniMax token-plan remains projection; details in `providers/minimax.md`.

OpenAI is live-verified as documented in its map. Copilot and MiniMax parsing is intentionally fail-open against inferred upstream shapes; a mismatch must remain an error snapshot, not break the report.

## Source and tests

- Service, shapes, fetchers, parsers: `core/providers/usage.py`
- RPC validation/projection: `server/rpc/provider_usage_methods.py`
- WebUI polling/presentation: `webui/src/components/StatisticsView.svelte`, `webui/src/lib/statisticsView.js`
- Focused coverage: `tests/core/providers/test_usage.py`, `tests/server/rpc/test_provider_usage_methods.py`, and Statistics WebUI tests
