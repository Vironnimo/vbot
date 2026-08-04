# xAI Provider

This supplementary map covers xAI-specific Connection, OAuth, catalog, and Responses-wire behavior.

## Boundary

`core/providers/xai.py` is the xAI policy owner and deliberately reuses the deep OpenAI Responses transport and shared Responses codec. Provider and Connection declarations live in `resources/providers/xai.json`; durable Model facts and Connection allowlists live in `resources/models/xai.overrides.json`. Generic RFC 8628 polling and OAuth refresh stay in `auth_flow.py` and `token_getter.py`; xAI-specific handling there is selected by `device_flow: xai_oauth`.

## Connections and discovery

- `api-key` uses `XAI_API_KEY`; `subscription` uses xAI's Device Authorization endpoint and rotating refresh tokens. Both call the fixed `https://api.x.ai/v1` inference base and discover language Models through `/language-models`.
- The bundled OAuth endpoints and public client id are fixed configuration, not dynamic discovery. Keep every auth URL on `auth.x.ai` and inference/catalog traffic on `api.x.ai`; never allow credential-bearing redirects or configurable hosts through this built-in Provider.
- SuperGrok login does not itself prove API entitlement. An inference 401/403 remains a terminal auth/entitlement error and must be surfaced without retrying.
- Model discovery is additive and fail-soft. The hand override is the durable floor for verified chat Models; retired aliases and the undocumented Composer Model are not statically advertised.

## Responses policy

- Every xAI Connection uses `/responses`, `store: false`, canonical Tool rendering, and shared SSE/response normalization. Reasoning Models request `reasoning.encrypted_content`, and their complete opaque response items replay across same-Model turns.
- `prompt_cache_key` is derived from the cache-affinity id, not the Session id when a shared prefix exists. Only `service_tier: default|priority` is forwarded; arbitrary tier strings are dropped.
- The wire accepts only `image/jpeg` and `image/png` attachments. PDF, GIF, WebP, audio, and video must be rejected before request serialization.
- Reasoning controls are Model-scoped: Grok 4.5 supports `low|medium|high` and maps vBot `none` to `low`; Grok 4.3 supports `none|low|medium|high`; Grok 4.20 Multi-Agent supports `low|medium|high|xhigh`; Grok Build and the fixed Grok 4.20 reasoning variant reason without accepting an effort control; the Grok 4.20 non-reasoning variant suppresses all reasoning controls and replay.

## OAuth edge cases

- xAI returns RFC 8628 polling states such as `authorization_pending` and `slow_down` as HTTP 400 JSON. Only the four standard polling errors pass status classification; any other 400 remains terminal.
- Prefer `verification_uri_complete` when present so the browser URL carries the user code. Fall back to `verification_uri`, then the legacy `verification_url` field.
- Refresh responses may rotate the refresh token. Persist the replacement atomically; preserve the last token on retryable transport/5xx failures, but delete it after a terminal refresh 400/401/403 so the Connection clearly requires reconnecting.

## Verification

`tests/core/providers/test_xai.py` covers routing, reasoning ladders, encrypted replay, cache/tier fields, media, and auth headers. Shared OAuth regressions live in `test_auth_flow.py` and `test_token_getter.py`; Runtime/config/catalog wiring lives in `test_runtime_providers.py` and discovery tests. Live credentials are not available, so inference entitlements, endpoint response drift, and actual refresh-token rotation remain live-verification items.
