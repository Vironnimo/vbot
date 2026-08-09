# Provider Usage

Read this reference only for Provider subscription limits, usage fetchers/parsers, caching, automatic history, or the `provider.usage*` RPCs. Session usage/cost statistics are separate domains.

## Boundary

`ProviderUsageService` probes a logged-in Connection's upstream subscription state and owns its normalized automatic observation history. It does not read or aggregate vBot Sessions. `core/statistics/` remains a read-only local aggregation; the WebUI correlates the two independent sources only by time.

The Runtime owns one service instance for shared caching and the hourly collector. Live state is exposed by `provider.usage`; persisted observations by `provider.usage_history`; explicit deletion by `provider.usage_history.clear`. The service never participates in Chat execution. Its local `UsageProbeRuntime` Protocol avoids importing concrete Runtime; an injectable transport keeps tests offline.

## Report contract

The frozen serializable projection is:

- `UsageWindow`: label, `used_percent` clamped to 0–100, ISO-8601 UTC `reset_at` or null, optional window duration, optional used/remaining/total unit counts and unit name, and optional unlimited marker.
- `ProviderUsageSnapshot`: base Connection id, exact Account id, display name, optional plan, optional structured credits, windows, and optional error.
- `UsageReport`: generation timestamp and Provider snapshots.

`report(connections=None)` supports an optional Connection filter. Only Connections with registered fetchers and `is_usable()` are targets; disabled or uncredentialed Connections are never probed. A snapshot with neither windows nor an error is omitted. The CLI exposes this as `provider usage [--connection <provider:connection-id>]...`; repeated filters are passed as one `connections` list and output includes used and derived remaining percentages, reset timestamps, and per-Provider errors.

## Automatic history

On Runtime startup inside an active event loop, the collector reads the newest valid stored sample. It samples immediately only when no sample exists or the newest sample is at least one hour old; otherwise it waits for the remainder of the hour. It never backfills missed intervals. Each automatic attempt reuses the same live cache/coalescing path as `provider.usage`; 10-second WebUI live polls are not written.

Meaningful reports are appended as schema-validated monthly JSONL files under `<data_dir>/statistics/provider-usage/`. Storage owns this canonical placement, but Providers remains the writer and owner of the normalized upstream source data; Statistics neither writes nor caches it. Error snapshots are stored so upstream outages remain visible gaps with reasons; an empty report caused by having no supported usable Connection is not stored. History is unbounded by default and survives Connection removal because files are not coupled to current configuration. Storage contains only the normalized public projection, never raw upstream responses or credentials. Invalid rows fail soft on reads and are logged.

`provider.usage_history` accepts an inclusive optional `{since?, until?}` ISO-8601 window. `provider.usage_history.clear` deletes all history files; the WebUI places an explicit confirmation in front of that destructive RPC.

## Concurrency, cache, and failures

Fetchers fan out concurrently. Each has a bounded timeout and fails open into its own snapshot: timeout, HTTP status, unsupported/invalid shape, or generic unavailable. One Provider cannot fail siblings.

Successful snapshots cache per exact Connection+Account target for 10 seconds; error snapshots for 60 seconds. A per-target async lock coalesces concurrent cache misses so multiple browser windows and the automatic collector do not multiply outbound requests. The service caches normalized snapshots, never raw OAuth tokens.

Every fetch acquires fresh auth through Runtime token getters or reads narrowly required token-store extras. Logs include no token data. Provider-specific endpoint/header/shape facts remain in each Provider's map.

The hourly sampler isolates each automatic attempt. An unexpected collection failure is logged at error level, then the same sampler waits for the normal interval and tries again; it does not silently terminate or spin in an immediate restart loop. An unexpected initial freshness-check failure is logged and degrades to sampling immediately.

## Supported Connections

- `openai:subscription`: ChatGPT usage windows/credits and account-scoped Codex headers; verified endpoint details in `providers/openai.md`.
- `github-copilot:oauth`: Copilot entitlement/usage using stored GitHub OAuth extra; details in `providers/github-copilot.md`.
- `ollama-cloud:api-key`: Ollama Cloud session/weekly quota ratios and observed per-Model request counts; details in `providers/ollama.md`.
- `minimax:api-key`: MiniMax token-plan remains projection; details in `providers/minimax.md`.

OpenAI and Ollama Cloud are live-verified as documented in their maps. Ollama Cloud's endpoint is not publicly documented and must remain strict and fail-open. Copilot and MiniMax parsing is intentionally fail-open against inferred upstream shapes; a mismatch must remain an error snapshot, not break the report.

## Source and tests

- Service, shapes, fetchers, parsers: `core/providers/usage.py`
- Durable schema, validation, retention, and deletion: `core/providers/usage_history.py`
- RPC validation/projection: `server/rpc/provider_usage_methods.py`
- WebUI polling/presentation: `webui/src/components/StatisticsView.svelte`, `webui/src/components/statistics/LimitHistory.svelte`, `webui/src/lib/statisticsView.js`
- Focused coverage: `tests/core/providers/test_usage.py`, `tests/core/providers/test_usage_history.py`, `tests/core/runtime/test_runtime_provider_usage.py`, `tests/server/rpc/test_provider_usage_methods.py`, and Statistics WebUI tests
