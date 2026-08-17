# Nous Portal Provider

Read `providers.md` first. This reference owns vBot's Nous API-key and Portal-subscription Connections plus the Portal-specific policy layered onto the shared OpenAI Chat Completions transport.

## Connections and auth

- `nous:api-key` uses `NOUS_API_KEY` as an `Authorization: Bearer` credential against `https://inference-api.nousresearch.com/v1`. The key may spend Portal credits or a subscription entitlement; vBot does not infer the account type from the credential.
- `nous:subscription` is a separate OAuth Device Flow Connection. It requests only `inference:invoke` from `https://portal.nousresearch.com/api/oauth/device/code`, polls `/api/oauth/token`, and uses the returned short-lived access JWT on the same explicit inference endpoint.
- Nous refresh tokens rotate and are single-use. `OAuthTokenGetter` sends the credential only in `x-nous-refresh-token`, never in the form body, does not retry an ambiguous refresh POST, persists the rotated token, and removes a login rejected for terminal reuse/authorization failure. An explicitly advertised scope that lacks `inference:invoke` is rejected.
- Credential and endpoint remain separate facts. The bundled Connections select the production endpoint directly; token contents, credential prefixes, and response aliases never choose a Connection or rewrite its endpoint.

## Request and response policy

- `NousAdapter` extends `OpenAICompatibleAdapter`; the shared Adapter continues to own Chat Completions messages, Tool schemas/calls/results, SSE parsing, reasoning extraction/replay shaping, usage, retry classification, and canonical terminal outcomes.
- The current public Nous OpenAPI documents `temperature` from 0 through 2 and a `max_tokens` ceiling of 32,000. The Adapter rejects an invalid temperature locally, removes undocumented sampling controls, normalizes every output-limit alias to `max_tokens`, and caps it at 32,000.
- Nous reasoning uses `reasoning: {enabled: true, effort?: ...}` for an enabled/on effort. A disabled intent omits the field; it does not send `enabled: false`. Replay inherits the shared `full_history` default; a demonstrated incompatibility must use an explicit Provider/Model override rather than a hidden Adapter fallback.
- OpenRouter routing preferences, `session_id`, and explicit `cache_control` are not part of the Portal contract and are never added. No explicit prompt-cache behavior is claimed.
- The public Chat Completions schema documents string message content only. `wire_media_support()` therefore advertises no native media formats until a live/documented contract proves multipart media and size/count limits; Chat degrades attachments through its normal non-native path.
- HTTP 402 is a non-retryable payment/subscription-entitlement error. HTTP 401/403, 429 with `Retry-After`, transient gateway statuses, malformed bodies, incomplete streams, Tool terminal outcomes, and content-filter/output-truncation outcomes retain the shared Provider policy.

## Catalog policy

- Both Connections discover from authenticated `GET /v1/models`; discovery remains Connection-scoped and preserves the complete response in `resources/models/nous.raw.json` before normalization.
- A catalog entry receives Tools, JSON, or reasoning capabilities only from explicit response evidence. Sparse future entries remain visible but do not become agent-capable by assumption. Every discovered output ceiling is capped to the Provider's documented 32,000-token maximum.
- Nous explicitly says its Hermes 4 family is meant for chat/reasoning rather than an agentic Tool loop. Those ids remain in the raw audit and are omitted from vBot's usable Model projection, matching Hermes Agent's own picker policy.
- The bundled credential-free fallback catalog contains only the exact current agentic slugs Nous documents as recommendations: `anthropic/claude-sonnet-4.6`, `openai/gpt-5.5-pro`, `google/gemini-3-pro-preview`, and `deepseek/deepseek-v4-pro`. There are no aliases, retired-id redirects, or silent Model fallbacks; authenticated discovery is authoritative for the account's current allowlist.

## Verification

- Request/catalog/stream policy: `tests/core/providers/test_nous.py`
- Device login and scope: `tests/core/providers/test_auth_flow.py`
- Rotation, reuse quarantine, and no-replay refresh: `tests/core/providers/test_token_getter.py`
- Connection-scoped raw/generated discovery: `tests/core/models/test_discovery_provider_refresh.py`
- Bundled config, fallback Catalog, and Runtime Adapter selection: `tests/core/runtime/test_runtime_providers.py`
