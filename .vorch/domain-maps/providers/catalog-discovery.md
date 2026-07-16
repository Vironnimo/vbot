# Provider Catalog Discovery

Read this reference only for Provider Model discovery and refresh. Model-DB layers, assembly, and loaded Model semantics live in `models.md`; this file owns the Provider-facing fetch/normalization side.

## Boundary

`core/models/discovery.py` is the refresh pipeline, but Provider Adapters supply wire knowledge: discovery headers/params, normalization, optional enrichment, and optional task feeds. Refresh writes Provider projection artifacts; it does not own cross-file Model assembly or fetch during Model load.

A discovery target is one usable Connection with an effective `models_endpoint`. The effective base URL and endpoint use Connection overrides before Provider defaults. Account choice supplies a credential only; discovered Models are tagged with the local Connection id, not the Account.

## Fetch and normalization

- Primary catalog GET accepts top-level `data` or `models` lists and passes entries through the selected Adapter class's catalog filter/normalizer.
- `discovery_headers()`, `discovery_params()`, and `supplementary_discovery_params()` let a Provider describe catalog auth/query variants without branching generic discovery by Provider id.
- `enrich_discovered_models(normalized_models, post_json)` supports bounded Provider detail calls after primary normalization; a per-Model enrichment failure keeps the conservative baseline.
- `discover_task_models(normalized_models, fetch_json)` adds Provider task-capability feeds. A task-catalog failure degrades the task projection and does not fail the primary refresh.
- Raw primary, supplementary, enrichment, and task responses are retained in refresh artifacts for later projection changes, while Runtime reads only normalized/assembled Model data.

Runtime and discovery use separate Adapter-selector maps: `_ADAPTER_MAP` constructs live chat Adapters; `_DISCOVERY_ADAPTER_MAP` selects static catalog behavior. A new Adapter selector that supports discovery must be registered in both places.

## Connection-scoped merge

Every discovered Model receives `connections: [<local_connection_id>]`. Refresh replaces only the existing generated Models whose allowlist includes the current Connection and keeps Models belonging to other Connections. Multiple Accounts on the same Connection never create duplicate catalog partitions.

Adapter normalizers must preserve durable discoverable facts such as modalities, output/context limits, reasoning capability, task options, and Provider-scoped wire metadata. Missing optional facts stay unknown; sparse OpenAI-compatible lists still produce usable text-chat Models rather than authoritative negatives.

Provider-generated data can be enriched from that Provider's own models.dev section under the Models-domain fill-without-overwrite rules. Hand-maintained overrides are for durable facts the upstream feeds cannot supply and are applied at Model load, not discovery.

## Retry and failure behavior

Catalog requests run inside `retry_async` with the shared transport/status classification. Timeouts and transport errors, plus 429/502/503/504, retry with exponential backoff; `Retry-After` is honored as a capped floor. Auth/fatal statuses, HTTP 500 on this path, and malformed required bodies abort the Connection refresh as `ModelDiscoveryError`.

A supplementary request failure is logged and skipped. Provider enrichment and task hooks define their own documented fail-soft granularity. One failed Connection must not erase the last known generated Models for unrelated Connections.

## Local auto-refresh and reachability

Connections with `auto_refresh: true` are refreshed by `Runtime.maybe_refresh_local_catalogs()` only while enabled and usable. Startup triggers a background sweep; `model.list` waits within a short budget; sweeps are throttled, including failures, so an offline local server is not probed on every picker open.

Success reloads the existing `ModelRegistry` in place and records reachable. Failure keeps the previous catalog and records unreachable. `model.list` exposes `reachable: false` only when every usable serving Connection is an auto-refresh Connection whose last probe failed; remote or unprobed alternatives prevent that claim.

## Source and tests

- Provider fetch/normalization: `core/models/discovery.py`
- Model layers and assembly: `core/models/`, `models.md`
- Local sweep/reachability: `core/runtime/runtime.py`, `server/rpc/connection_methods.py`
- Provider-specific normalization: the concrete Adapter modules under `core/providers/`
- Focused coverage: Provider catalog tests under `tests/core/providers/`, discovery/Models tests, and local-refresh Runtime/RPC tests
