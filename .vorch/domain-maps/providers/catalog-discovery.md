# Provider Catalog Discovery

Read this reference only for Provider Model discovery and refresh. Model-DB layers, assembly, and loaded Model semantics live in `models.md`; this file owns the Provider-facing fetch/normalization side.

## Boundary

`core/models/discovery.py` is the refresh pipeline, but Provider Adapters supply wire knowledge: discovery headers/params, normalization, optional enrichment, and optional task feeds. Refresh writes Provider projection artifacts; it does not own cross-file Model assembly or fetch during Model load.

A discovery target is one Connection with an effective `models_endpoint`. By default it must be usable and contributes its selected Account credential; a Connection with `catalog_requires_credentials: false` may refresh a public catalog with an empty discovery credential even while no usable Account exists. The effective base URL and endpoint use Connection overrides before Provider defaults. Discovered Models are tagged with the local Connection id, not the Account.

RPC callers (`model.refresh_db` and the Debug `debug.model_probe`) resolve the discovery Connection and credential through `server/rpc/provider_access._discovery_credential`: it applies the credential policy above, honors an explicit Account suffix in an OAuth Connection id (otherwise the first usable Account), and points GitHub Copilot at the Account's exchanged API endpoint.

An OpenAI-compatible Custom Provider participates without a separate discovery path: its implicit `default` Connection, optional Settings `models_endpoint`, Adapter selector, and credential resolver feed this same pipeline. Manual Custom Model facts are applied later by `ModelRegistry` and therefore override a discovered Model with the same wire id without deleting discovered-only Models. Only runtime-target refreshes discover Custom Providers; a system-target refresh skips them (`models.md`).

## Fetch and normalization

- `build_discovery_request(provider_config, connection)` resolves the primary catalog request once: effective base URL, models URL with resolved discovery params, and `DiscoveryRequest.headers(credential)` (Provider extra headers, Connection auth header unless keyless or empty, then Adapter discovery headers). `refresh_models` sends it and `debug.model_probe` reuses it, so a probe cannot drift from refresh. A Connection without an effective `models_endpoint` raises `ValueError`.
- Primary catalog GET accepts top-level `data` or `models` lists and passes entries through the selected Adapter class's catalog filter/normalizer.
- `discovery_headers()`, `discovery_params()`, and `supplementary_discovery_params()` let a Provider describe catalog auth/query variants without branching generic discovery by Provider id. `discovery_headers`, `discovery_params`, and `resolve_discovery_params` receive the target Connection as keyword `connection` (headers fall back to the Provider's first Connection), so one Adapter can serve Connections with different catalogs; `accepts_discovered_model(raw, connection)` filters raw entries per Connection before normalization.
- Discovery accepts a static credential or the selected Account's live `TokenGetter`; OAuth RPC refresh keeps the getter open through discovery. Each authenticated GET/POST attempt rebuilds headers from the current token, including Adapter-derived Account routing.
- `resolve_discovery_params(fetch_json)` may replace time-sensitive query parameters from a public JSON source; failure is logged and keeps the static `discovery_params()` fallback. OpenAI Subscription uses the stable `@openai/codex` package version because `/codex/models` gates newly available Models by `client_version`; only a strict numeric `x.y.z` version is accepted.
- `enrich_discovered_models(normalized_models, post_json)` supports bounded Provider detail calls after primary normalization; a per-Model enrichment failure keeps the conservative baseline.
- `finalize_discovered_model(model, connection)` applies facts known only from Connection scope after baseline normalization and before optional enrichment. Keep the default identity implementation; override it when the same Adapter serves scopes with different durable facts, such as forcing every direct Ollama Cloud catalog entry remote.
- `discover_task_models(normalized_models, fetch_json)` adds Provider task-capability feeds. A task-catalog failure degrades the task projection and does not fail the primary refresh.
- Refresh writes only the normalized projection; raw primary, supplementary, enrichment, and task responses are not retained. Runtime reads only normalized/assembled Model data.

Runtime and discovery use separate Adapter-selector maps: `_ADAPTER_MAP` constructs live chat Adapters; `_DISCOVERY_ADAPTER_MAP` selects static catalog behavior. A new Adapter selector that supports discovery must be registered in both places.

## Connection-scoped merge

Every discovered Model receives `connections: [<local_connection_id>]`. Refresh replaces only the existing generated Models whose allowlist includes the current Connection and keeps Models belonging to other Connections. Multiple Accounts on the same Connection never create duplicate catalog partitions.

Adapter normalizers must preserve durable discoverable facts such as modalities, output/context limits, reasoning capability, task options, and Provider-scoped wire metadata. Missing optional facts stay unknown; sparse OpenAI-compatible lists still produce usable text-chat Models rather than authoritative negatives.

LM Studio discovery uses its native `GET /api/v1/models` response rather than the sparse OpenAI-compatible `/v1/models` list so installed chat Models retain display name, capabilities, theoretical context, locality, and loaded-instance facts. Entries of any type other than `llm` and `embedding` are skipped.

Embedding Models are tagged from each Provider's own catalog facts and normalized through `text_embedding_capabilities()` (`core/models/models.py`): text in, `embeddings` out, no Tools. That output derives only the `text_embedding` task, so such a Model never becomes a chat Model. The facts per Provider: OpenRouter's `output_modalities=embeddings` feed (`providers/openrouter.md`), OpenAI Platform `text-embedding-*` ids on the `api-key` Connection (`providers/openai.md`), Mistral non-chat `*-embed` ids, Ollama's `embedding` capability without `completion`, and LM Studio's native `type: embedding`. GitHub Copilot keeps skipping its embedding entries (`providers/github-copilot.md`).

Provider-generated data can be enriched from that Provider's own models.dev section under the Models-domain fill-without-overwrite rules. At a canonical join, enrichment keeps an Adapter-reported reasoning control and every non-control reasoning fact; only a bare `supported` flag yields to the canonical ladder (`models.md` -> Typed reasoning). Hand-maintained overrides are for durable facts the upstream feeds cannot supply and are applied at Model load, not discovery.

## Retry and failure behavior

Catalog GET requests run inside `retry_async` with the shared transport/status classification. Timeouts and transport errors, plus 429/500/502/503/504, retry with exponential backoff; `Retry-After` is honored as a capped floor. Every OAuth Connection uses the shared `OAuthRequestRecovery` contract in `providers/connections.md`; there is no Adapter allowlist. Primary, supplementary, task, and POST-enrichment requests each own one recovery budget and rebuild headers from the same Account getter. Repeated rejection, an ordinary permission 403, missing refresh capability, or failed recovery propagates; a token-getter failure alone never triggers catalog auth recovery. Other fatal statuses and malformed required bodies abort the Connection refresh as `ModelDiscoveryError`. POST-only enrichment retains its non-idempotent status policy, so HTTP 500 remains fatal. Public catalogs receive static empty credentials and cannot trigger OAuth renewal. An `auto_refresh` Connection is usually a local server that is simply not running, so both the automatic sweep and the manual refresh run its `refresh_models` call under `caller_owns_retries()`: one attempt per request, the sweep TTL being its retry policy.

`refresh_models` raises `ModelDiscoveryError` without logging it; callers log what they catch - the manual refresh warns once per failed Connection, the automatic sweep as described below. A supplementary request failure is logged and skipped. Provider enrichment and task hooks define their own documented fail-soft granularity. One failed Connection must not erase the last known generated Models for unrelated Connections.

The RPC catches expected credential-resolution and OAuth-refresh failures at the same Connection boundary as discovery failures. Global and multi-Connection refreshes record them in `errors`, retain that Connection's old Models, and continue; a single-Provider refresh with no successful Connection fails without publishing its staged snapshot.

## Local auto-refresh and reachability

`ProviderRuntime.model_database_refresh()` owns one Runtime-local admission guard shared by manual RPC refreshes and automatic local sweeps, exposed through the Runtime facade. It spans active-root copy, network discovery, complete-root publication, and in-place registry reload. Acquiring only at publication would let a queued old snapshot erase another completed refresh. Cancellation discards its unpublished staging copy before releasing admission.

Connections with `auto_refresh: true` are refreshed by `Runtime.maybe_refresh_local_catalogs()` only while enabled and usable. Startup triggers a background sweep; `model.list` waits within a short budget; sweeps are throttled, including failures, so an offline local server is not probed on every picker open. The 30 s TTL (`LOCAL_CATALOG_REFRESH_TTL_SECONDS`) runs from the end of the previous sweep, so a sweep slower than the TTL does not start the next one immediately. A sweep copies, validates, commits, and discards the complete Model DB on the `local-catalog` worker pool and swaps the live registry through `ModelRegistry.reload_async`, so a picker open never blocks the Event Loop on catalog files; the staged copy is discarded even when the sweep is cancelled.

Success reloads the existing `ModelRegistry` in place and records reachable. When that reload changed the loaded catalog, `ProviderRuntime` calls its catalog-changed callbacks (`Runtime.add_model_catalog_changed_callback`; a failing callback is logged and skips none of the others); the server bridge in `server/_app_lifecycle.py` publishes `resource_changed` kind `models`, so open windows reload their Model lists. An unchanged republish and a failed sweep signal nothing. The manual `model.refresh_db` publishes its own `models` change. Failure keeps the previous catalog and records unreachable; the sweep logs a failure at DEBUG and warns only on a reachable->unreachable transition (INFO on recovery). `model.list` exposes `reachable: false` only when every usable serving Connection is an auto-refresh Connection whose last probe failed; remote or unprobed alternatives prevent that claim.

## Source and tests

- Provider fetch/normalization: `core/models/discovery.py`
- Model layers and assembly: `core/models/`, `models.md`
- Local sweep/reachability: `core/providers/runtime.py`, `server/rpc/model_methods.py`
- Provider-specific normalization: the concrete Adapter modules under `core/providers/`
- Focused coverage: Provider catalog tests under `tests/core/providers/`, discovery/Models tests, and local-refresh Runtime/RPC tests
