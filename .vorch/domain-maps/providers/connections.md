# Provider Connections and Accounts

Read this reference only for Provider/Connection configuration, Accounts, credentials, enabled state, OAuth/device flow, token storage/refresh, Connection RPCs, or local reachability. The root Provider boundary lives in `providers.md`.

## Configuration model

`ProviderConfig` owns id/name, Adapter selector, base URL, `connections`, request `defaults`, extra headers, optional Provider-level discovery endpoint/models.dev id, and an optional read-side `context_window` default. `ConnectionConfig` owns local id/label/type, auth metadata, optional OAuth metadata, optional `base_url`, `mode`, `models_endpoint`, and `auto_refresh`.

Supported Connection types are `api_key`, `oauth`, and `none`. Keyed Connections default enabled and are gated by credentials. A `none` Connection is keyless: auth may be empty, it resolves credential `""`, exposes one implicit usable default Account, and is disabled by default so vBot never probes a local endpoint without explicit opt-in.

Connection-level base URL and models endpoint override Provider-level values. `mode` is an Adapter-interpreted wire selector. `auto_refresh` is a boolean for local catalogs whose installed Model set changes independently of vBot; it changes refresh behavior, not credential semantics.

## Identity and Accounts

Public Connection ids use `provider:connection[:account]`. Account ids match `^[a-z0-9][a-z0-9_]{0,31}$`; `default` is the unnamed slot. Account selection is deterministic: default first, then remaining ids sorted.

API-key Accounts map to environment keys through `derive_credential_key()`: default uses the base key; a named Account uses `BASE__<ACCOUNT>`. `BASE__DEFAULT` is rejected as a second spelling. Account discovery scans process environment before the data-dir `.env`, so process values win for the same Account.

OAuth Accounts are stored per Account in `TokenStore`: `<provider>-<connection>.json` for default and `<provider>-<connection>--<account>.json` for named slots, under `<data_dir>/oauth/`. Id validation prevents path traversal. An expired OAuth token remains usable only when it has a refresh path.

Accounts are credential choices only. Model catalogs, discovery, Connection enablement, and task-target expansion stay Connection-level; Account suffixes are not a catalog or target cross-product.

## Enablement, credentials, and usability

`ProviderCredentialResolver` is the single decision owner:

- `has_credentials()` asks whether credentials exist and is used by management surfaces.
- `is_connection_enabled()` reads the live `providers.connections` override, else the type default; an Account suffix is ignored.
- `is_usable()` requires enabled and credentialed on the same Connection and is used for behavior gating.
- `list_accounts()` returns deterministic `ProviderAccount` projections with source and usable state.
- `resolve_account_id()` validates an explicit Account or chooses the first usable one.

Model listing, task targets, catalog auto-refresh, usage probes, Chat's unpinned-Connection selection, and Project model probes use `is_usable()`. A disabled Connection fails `Runtime.get_adapter()` with the disabled reason before credential resolution.

## Credential and OAuth lifecycle

API keys resolve from process environment first and data-dir `.env` second. Key set/unset RPCs can mutate only the data-dir value, so removing it can still leave the Account configured through process environment. They reload the fallback snapshot into the existing `ProviderCredentialResolver`; its stable identity ensures every already-injected consumer sees the new credential state on its next check. `none` Connections reject key mutation.

`StaticTokenGetter` wraps fixed API-key/keyless values. `OAuthTokenGetter` reads the Account token, refreshes near expiry, stores refreshed values under the same Account, and coalesces concurrent refresh. Adapters and task/usage clients ask the getter inside each request attempt rather than retaining raw access tokens.

`DeviceFlowEngine` owns OAuth device sessions, polling cadence, provider-specific exchange, terminal errors, and token persistence. Connection RPCs start/poll/disconnect against declared OAuth metadata; UI/CLI code must not reproduce flow policy.

## RPC and runtime projections

- `connection.list` combines static Provider/Connection config with Account/configured/enabled/usable state and optional local reachability. The Provider projection inside `settings.get` preserves the same Connection-level `configured`, `enabled`, and server-owned `usable` distinction so management surfaces and behavior gates do not infer one state from another.
- `connection.set_enabled` persists the explicit Connection override. Enabling an auto-refresh Connection forces an immediate local catalog attempt; enabled-but-unreachable remains a valid persisted state.
- `provider.set_key`/`unset_key` address API-key Accounts through derived keys and never return secret values.
- Provider connect/status/disconnect operate on OAuth Accounts and token-store state.

Local endpoint reachability is Runtime probe state, not credential state. `Runtime.connection_reachability()` is `True`, `False`, or `None` (not probed) for auto-refresh Connections and is surfaced separately from enabled/usable.

## Source and tests

- Config and defaults: `core/providers/providers.py`
- Accounts and credential resolution: `core/providers/accounts.py`, `core/providers/credentials.py`
- OAuth: `core/providers/token_store.py`, `token_getter.py`, `auth_flow.py`
- Runtime wiring and reachability: `core/runtime/runtime.py`
- RPCs: `server/rpc/connection_methods.py`
- Focused coverage: `tests/core/providers/test_accounts.py`, the `test_credentials_*.py` suites, `test_token_store.py`, `test_token_getter.py`, `test_auth_flow.py`, and Connection RPC/Runtime tests
