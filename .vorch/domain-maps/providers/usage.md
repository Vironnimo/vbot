# Provider Usage

Read this reference only for Provider subscription limits, usage fetchers/parsers, caching, automatic history, or the `provider.usage*` RPCs. Session usage/cost statistics are separate domains.

## Boundary

`ProviderUsageService` probes a logged-in Connection's upstream subscription state and owns its normalized automatic observation history. It does not read or aggregate vBot Sessions. `core/statistics/` remains a read-only local aggregation; the WebUI correlates the two independent sources only by time.

The Runtime owns one service instance for shared caching and the hourly collector. Live state is exposed by `provider.usage`; persisted observations by `provider.usage_history`; explicit deletion by `provider.usage_history.clear`. The service never participates in Chat execution. Its local `UsageProbeRuntime` Protocol avoids importing concrete Runtime; an injectable transport keeps tests offline.

## Report contract

The frozen serializable projection is:

- `UsageWindow`: label, `used_percent` clamped to 0-100, ISO-8601 UTC `reset_at` or null, optional window duration, optional used/remaining/total unit counts and unit name, and optional unlimited marker.
- `ProviderUsageSnapshot`: base Connection id, exact Account id, display name, optional plan, optional structured credits, windows, and optional error.
- `UsageReport`: generation timestamp and Provider snapshots.

`report(connections=None)` supports an optional Connection filter. Only Connections with registered fetchers and `is_usable()` are targets; disabled or uncredentialed Connections are never probed. A snapshot with neither windows, enabled credits, nor an error is omitted. The CLI exposes this as `provider usage [--connection <provider:connection-id>]...`; repeated filters are passed as one `connections` list and output includes used and derived remaining percentages, reset timestamps, and per-Provider errors.

## Automatic history

On Runtime startup inside an active event loop, the collector reads the newest stored sample time (`MAX(sampled_at)`). It samples immediately only when no sample exists or the newest sample is at least one hour old; otherwise it waits for the remainder of the hour. It never backfills missed intervals. Each automatic attempt reuses the same live cache/coalescing path as `provider.usage`; 10-second WebUI live polls are not written.

Meaningful reports are stored in the canonical `<data-dir>/provider-usage.db` (kernel name `provider_usage`, `database.md`), registered in the data-store marker and in `Runtime.canonical_databases()` / `canonical_database_specs`, so snapshots, status and restore cover it. `ProviderUsageService` opens it through `ProviderUsageHistoryStore.open(data_root)` in every startup mode (verification checks it; only normal startup samples) and closes the store it opened in `close()`/`aclose()`; an injected store stays with its caller. Providers is the only writer and owner of this normalized upstream data; Statistics neither writes nor caches it. Error snapshots are stored so upstream outages remain visible gaps with reasons; an empty report caused by having no supported usable Connection is not stored. History is unbounded (retention is manual, via clear) and survives Connection removal because rows are not coupled to current configuration. Storage contains only the normalized public projection, never raw upstream responses or credentials.

Schema (Generation 1, `core/providers/usage_history.py`): `usage_samples` (AUTOINCREMENT `sample_key`, `sampled_at`, indexed by `usage_samples_by_time` for the range read and the MAX read), `usage_snapshots` (one row per Provider snapshot, keyed by sample and report ordinal, with flattened plan/credits/error) and `usage_windows` (keyed by sample, snapshot ordinal and window ordinal). Children cascade from their sample. Every stored timestamp (`sampled_at`, `reset_at`) is canonical UTC `YYYY-MM-DDTHH:MM:SS.ffffffZ`; offsets are normalized on write and a naive timestamp is rejected. A sample is validated completely before one write transaction (exact keys, finite numbers, `used_percent` clamped to 0-100), so an invalid report stores nothing. Reads reassemble the same public `{sampled_at, providers}` projection in report order.

All store I/O runs on the database's worker pool, off the Event Loop; the sampler's `DatabaseError` and invalid-sample failures log a warning and skip that attempt. `provider.usage_history` accepts an inclusive optional `{since?, until?}` ISO-8601 window and returns samples oldest first. `provider.usage_history.clear` deletes every sample in one transaction and returns `{deleted_samples, deleted_files}`; `deleted_files` is a legacy field that is 1 when the clear removed samples and 0 when the history was already empty - the database file itself always remains. The WebUI places an explicit confirmation in front of that destructive RPC; the CLI is `provider history list|clear --yes`.

## Concurrency, cache, and failures

Fetchers fan out concurrently. Each has a bounded timeout and fails open into its own snapshot: timeout, HTTP status, unsupported/invalid shape, or generic unavailable. One Provider cannot fail siblings.

Successful snapshots cache per exact Connection+Account target for 10 seconds; error snapshots for 60 seconds. A per-target async lock coalesces concurrent cache misses so multiple browser windows and the automatic collector do not multiply outbound requests. The service caches normalized snapshots, never raw OAuth tokens.

Every fetch acquires fresh auth through Runtime token getters or reads narrowly required token-store extras. Logs include no token data. Provider-specific endpoint/header/shape facts remain in each Provider's map.

OpenAI Usage uses the same `OAuthRequestRecovery` as Chat and discovery, renewing once after an actual HTTP 401 and rebuilding both Authorization and Account routing. Repeated rejection/failed renewal remains an error snapshot within the existing timeout. Copilot Usage cannot use the Copilot bearer recovery: its request uses the original GitHub OAuth extra, while the getter renews only the exchanged Copilot API token. API-key probes have no renewal capability. Coverage: `test_usage.py`.

The hourly sampler isolates each automatic attempt. An unexpected collection failure is logged at error level, then the same sampler waits for the normal interval and tries again; it does not silently terminate or spin in an immediate restart loop. An unexpected initial freshness-check failure is logged and degrades to sampling immediately.

## Supported Connections

- `openai:subscription`: ChatGPT usage windows/credits and account-scoped Codex headers; verified endpoint details in `providers/openai.md`.
- `github-copilot:oauth`: Copilot entitlement/usage using stored GitHub OAuth extra; details in `providers/github-copilot.md`.
- `ollama-cloud:api-key`: Ollama Cloud session/weekly quota ratios and observed per-Model request counts; details in `providers/ollama.md`.
- `minimax:api-key`: MiniMax token-plan remains projection; details in `providers/minimax.md`.
- `openrouter:api-key`: account credits balance plus an optional API-key spending-cap window; a failed `/key` probe degrades to a credits-only snapshot. Verified endpoint details in `providers/openrouter.md`.

OpenAI and Ollama Cloud are live-verified as documented in their maps. Ollama Cloud's endpoint is not publicly documented and must remain strict and fail-open. Copilot and MiniMax parsing is intentionally fail-open against inferred upstream shapes; a mismatch must remain an error snapshot, not break the report. OpenRouter's `/credits` shape is live-verified; the spending-cap window path is unit-tested only (the probe key had no cap set).

## Source and tests

- Service lifecycle, cache, Connection selection and fetchers: `core/providers/usage.py`
- Report shapes and probe contracts: internal `core/providers/_usage_types.py`; Provider response parsing: `_usage_parsers.py`. Existing public imports remain available from `usage.py`.
- Durable schema, validation, `provider-usage.db` store and deletion: `core/providers/usage_history.py`
- RPC validation/projection: `server/rpc/provider_usage_methods.py`
- WebUI polling/presentation: `webui/src/components/statistics/ProviderLimits.svelte`, `webui/src/components/statistics/LimitHistory.svelte`, `webui/src/lib/statisticsView.js`
- Focused coverage: `tests/core/providers/test_usage.py` (service), `test_usage_parsing.py`, `test_usage_durable_history.py` (sampling), `test_usage_history.py` (storage), `tests/core/runtime/test_runtime_provider_usage.py`, `tests/server/rpc/test_provider_usage_methods.py`, and Statistics WebUI tests
