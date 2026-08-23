# OpenAI Provider

Single `openai` provider covering both OpenAI Platform API-key access and ChatGPT Plus/Pro subscription access. One `OpenAIAdapter` class resolves the runtime wire from both the Connection and the selected Model: Platform API-key Models may use `/chat/completions` or `/responses`, while subscription Models use `/codex/responses`.

## Interfaces

- Provider config: `resources/providers/openai.json`
- Adapter selector: `openai`
- Adapter class: `OpenAIAdapter` (subclass of `OpenAICompatibleAdapter`)
- Connections:
  - `openai:api-key` — `type: api_key`, `auth.credential_key: OPENAI_API_KEY`, `base_url` defaults to the provider-level OpenAI Platform URL. Per-Model `metadata.openai.wire_policies.api-key.protocol` selects public Responses; absent metadata keeps the conservative `/chat/completions` fallback.
  - `openai:subscription` — `type: oauth`, `base_url: https://chatgpt.com/backend-api`, `mode: codex_responses`, `models_endpoint: /codex/models`. ChatGPT Plus/Pro Codex OAuth device flow.
- Runtime endpoints: `POST <base_url>/chat/completions` or `POST <base_url>/responses` (api-key, selected per Model); subscription Runs prefer `WS(S) <base_url>/codex/responses` and retain `POST <base_url>/codex/responses` SSE as the compatibility path.
- Catalog: the provider has no provider-level `models_endpoint`. Only the `subscription` connection carries `models_endpoint`; refresh of the `api-key` connection is not supported in this provider.

## Connection Configuration

Per-connection fields carried by `ConnectionConfig`:

- `mode: str | None` — adapter-interpreted wire-variant selector. `OpenAIAdapter` reads it at construction; `None` and the value `"chat_completions"` both mean the generic `/chat/completions` path. The only other defined value is `codex_responses`.
- `models_endpoint: str | None` — discovery endpoint, overrides the provider-level value. Used by `subscription` for `/codex/models`.
- `base_url: str | None` — overrides the provider-level base URL. Used by `subscription` to point at `chatgpt.com/backend-api`.

`mode` and `models_endpoint` must be strings when present; non-string values are a config error.

The adapter is selected by provider `adapter`, not by connection, so the same `OpenAIAdapter` class is instantiated for both connections. `get_adapter` threads the connection's `mode` into the adapter as `connection_mode`.

## Wire Contract

Every OpenAI function Tool definition carries `strict: false` on Chat Completions, public Responses, and subscription Codex Responses. This is mandatory on Responses because OpenAI may otherwise normalize an omitted field into strict mode. A live `openai:subscription` GPT-5.6 Luna probe on 2026-07-31 forced a Tool Call whose only required argument was `url`; with the explicit opt-out the model omitted both optional Boolean arguments as requested and the canonical schema remained unchanged.

### Chat Completions (conservative `api-key` fallback)

Used when `connection_mode` is `None` or `chat_completions`. Delegates to `OpenAICompatibleAdapter`; behavior is unchanged from the generic OpenAI-compatible contract:

- Canonical system/user/assistant messages stay in the OpenAI-style `messages` array.
- Canonical `tool` messages become `role: tool` messages with `tool_call_id`.
- Canonical assistant `tool_calls` become OpenAI function-call structures.
- Provider tool definitions become `{"type":"function","function":{...}}` entries.
- Streaming uses `stream: true`, SSE `data:` frames, `[DONE]`, and `stream_options.include_usage: true`; caller-provided `stream_options` are preserved except `include_usage` is forced true.
- The Codex extra headers (`OpenAI-Beta`, `originator`) must **not** be added on this path.

### Platform Responses (`api-key` connection, selected Models)

- A Model whose `metadata.openai.wire_policies.api-key.protocol` is `responses` uses public `POST /responses`; currently the durable profiles cover GPT-5.5, the `gpt-5.6` alias, and GPT-5.6 Sol/Terra/Luna. Unprofiled Platform Models remain on Chat Completions.
- Requests use the shared Responses item protocol with `store: false`. Assistant history replays each original output item verbatim from `reasoning_meta.response_output`, preserving encrypted reasoning, ids, Tool items, ordering, and assistant `phase`; reconstruction exists only for legacy Sessions that predate item capture.
- Structured `error`, `response.error`, and `response.failed` events use the shared Responses error classifier. Retryable failures may restart only before visible output; `response.incomplete` instead completes with a safe non-Tool terminal outcome.
- Public Responses may send native PDF `input_file` parts. Its optional request-parameter set is distinct from the private subscription wire.

### Codex Responses (`subscription` connection — `mode: codex_responses`)

- Requests use the shared Responses payload builder and post to `/codex/responses` relative to the connection's `base_url`.
- The Codex backend requires an `instructions` field. The adapter uses assembled system instructions when present and falls back to `You are a helpful assistant.`.
- The Codex backend requires `store: false`; omission is rejected like an enabled store request.
- The Codex backend rejects output-token limit parameters and sampling `top_p`. The adapter filters `max_tokens`, `max_output_tokens`, and `top_p` instead of forwarding provider defaults or caller kwargs, and it skips the local output-limit clamp entirely on this wire so a still-fitting request cannot die on a reserve that is never sent. An unspecified Chat-loop `top_p` (`None`) is dropped before the payload is built so it cannot become `"top_p": null`.
- The wire requires `stream: true`, including when a caller uses the adapter's logical `send()` interface. `stream()` yields normalized vBot deltas; `send()` consumes exactly one streaming exchange internally through a terminal event and accumulates text, reasoning, metadata, usage, Tool Call fragments, and the normalized terminal outcome across the whole stream into one canonical response. The live wire may leave `response.completed.response.output` empty even after emitting text deltas, so the completed object alone is not an answer. Do not retry a rejected non-streaming request as a stream — that would create a second billable request.
- A subscription call with a conversation id prefers one persistent WebSocket for the active Run. The first Model step sends a full `response.create`; a later step on the identical route may send `previous_response_id` plus only the newly appended input suffix when every non-input request field still matches and canonical input is exactly the preceding input followed by the preceding response output. Any mismatch uses a full request.
- WebSocket continuation is isolated by conversation, Model, and ChatGPT Account; the adapter instance already isolates Provider Connection and one Run. Changing any route component closes the old socket and clears its continuation. Run cleanup closes the socket.
- A connection-scoped `previous_response_not_found` response clears the continuation, opens a fresh socket, and retries that step once with full context. Any WebSocket transport failure disables WebSocket for that route for the rest of the adapter lifetime. Before any Provider event, the adapter falls back directly to full-context SSE; after Provider events, it never replays the same in-flight exchange internally and instead propagates the failure to Chat. If no Assistant answer text was emitted, Chat may safely restart the stream, and that next attempt uses full-context SSE because the route is disabled; after answer text, Chat preserves the partial result instead of replaying it. Calls without a conversation id and the explicit `sse` test/compatibility mode use SSE directly.

## OAuth (subscription connection)

- The flow is marked with `oauth.device_flow: openai_codex`; this is distinct from the standard RFC 8628-style Device Flow used by GitHub OAuth.
- Device authorization posts JSON `{"client_id": ...}` to `https://auth.openai.com/api/accounts/deviceauth/usercode`; the user verifies at `https://auth.openai.com/codex/device`.
- Polling posts JSON `{"device_auth_id": ..., "user_code": ...}` to the matching `/token` device-auth endpoint. HTTP 403 and 404 are treated as `authorization_pending` for this provider.
- Successful polling returns an authorization code and PKCE verifier; vBot exchanges them at `https://auth.openai.com/oauth/token` with `grant_type=authorization_code` and `redirect_uri=https://auth.openai.com/deviceauth/callback`.
- Refresh uses the OAuth `refresh_token` grant against the same token endpoint. Refreshed tokens keep a replacement refresh token when OpenAI sends one and preserve the existing token otherwise.
- The OAuth token file path is `<data_dir>/oauth/openai-subscription.json` for the `default` Account and `openai-subscription--<account>.json` for additional named Accounts (see `providers/connections.md` → Identity and Accounts).

## ChatGPT Account Header

- Access tokens are JWTs whose payload contains the claim `https://api.openai.com/auth`; that claim contains `chatgpt_account_id`.
- Runtime and discovery requests for the `subscription` connection must send both `Authorization: Bearer <token>` and `chatgpt-account-id: <account-id>`.
- If the account id is missing or blank, the adapter raises `ProviderAuthError` and asks the user to reconnect.
- `TokenStore` may mirror the account id in token metadata, but request headers are derived from the current JWT rather than guessed independently.

## Adapter-Owned Codex Headers

The Codex required extra headers live in the adapter, not in provider-level `extra_headers`:

```
CODEX_EXTRA_HEADERS = {"OpenAI-Beta": "responses=experimental", "originator": "vbot"}
```

`OpenAIAdapter._build_headers()` (and `discovery_headers()`) merge `CODEX_EXTRA_HEADERS` **only** on the `codex_responses` path. SSE and discovery use `OpenAI-Beta: responses=experimental`; WebSocket upgrade replaces that value with `OpenAI-Beta: responses_websockets=2026-02-06`, carries `session-id` plus `x-client-request-id`, and omits the SSE-only `session_id` spelling. Provider-visible prompt-cache affinity values are clamped to OpenAI's 64-character limit before either transport sends them; the full Session-unique conversation id remains the local WebSocket route key. The chat-completions path uses the inherited `OpenAICompatibleAdapter._build_headers()` and must never include Codex headers.

## Codex Continuation And Prompt Caching

The ChatGPT Codex backend routes its prompt cache by **per-request transport headers scoped to the conversation** — SSE uses `session_id` plus `x-client-request-id`; WebSocket uses `session-id` plus `x-client-request-id` — **not** by the body-level `prompt_cache_key` field. Live-verified 2026-07-09 on SSE: sending `prompt_cache_key` in the body has no measurable effect (~1/6 hit rate, same as sending nothing), while a stable conversation scope on the two routing headers lifts hits to ~5/6 (only the cold first request misses). Mirrors the Codex CLI and the `hermes-agent` `codex_responses` transport.

- Chat hands `ProviderAdapter.request_context_kwargs(...)` both the Session-unique conversation identity and the Session domain's separate `prompt_cache_affinity_id`. `OpenAIAdapter` returns both as internal request kwargs: the affinity alone stamps the SSE/WebSocket cache-routing headers, while `conversation_id = agent_id:session_id` alone keys the local WebSocket connection and `previous_response_id` continuation. Same-Agent, same-Project forks therefore share the best available cache route for their copied prefix but can never consume each other's stateful continuation. Both values are adapter-internal and never enter the request body; the affinity is a **routing hint only**, so the exact wire prefix still decides cache correctness.
- **Fork-affinity live verification (2026-07-31):** two `gpt-5.6-terra` subscription requests used distinct conversation ids and one shared cache-affinity header value. The source request was cold (`0 / 23,827` cache-read/input tokens); the fork-shaped prefix extension read `23,296 / 23,839` tokens from cache. The adapter's WebSocket test independently asserts that the second Session sends no `previous_response_id`, so the hit comes from shared prompt-cache routing rather than cross-Session continuation.
- The `api-key` `/chat/completions` path ignores the conversation id (it pops and drops it) and relies on OpenAI's default prefix-hash cache routing. Every non-OpenAI adapter never receives it (the base `request_context_kwargs` returns `{}`), so no other wire sees an unknown field.
- `store: true` remains rejected (`{"detail":"Store must be set to false"}`). The WebSocket beta nevertheless supports connection-scoped `previous_response_id` continuation while that socket retains the response; it is an optimization, not durable server state. Full-context replay remains the correctness path after socket loss, route change, prefix mismatch, or missing continuation.

## Reasoning

- vBot `thinking_effort` and raw `reasoning_effort` map to the nearest safe OpenAI effort: `minimal -> low`, `low/medium/high` stay exact, `xhigh/max -> high`.
- Generic OpenAI-compatible gateways omit explicit `none`; the direct OpenAI provider may send `none` only when catalog data confirms reasoning support.
- If injected `model_lookup` says reasoning is unsupported, reasoning request controls are stripped.
- Replay scope follows the shared Model → Provider → system hierarchy and therefore defaults to `full_history` for GPT-5.5, the `gpt-5.6` alias, and GPT-5.6 Sol/Terra/Luna on both Connections. `metadata.openai.wire_policies.<connection>` now carries wire-only facts such as Responses protocol and public `reasoning_context`; it no longer hides replay scope. Assistant `phase` remains semantic history and is preserved across Runs.
- On public Platform Responses, GPT-5.6 sends `reasoning.context: "all_turns"` so prior output items are available as Session-wide reasoning context. The private subscription `/codex/responses` contract is also full-history replay for those Models, but vBot does not send the public `reasoning.context` field there because support is not documented or live-verified.
- Opaque reasoning fields such as `encrypted_content` and the complete Responses `output` array stay in `reasoning_meta` for exact round-tripping. A GPT-5.6 full-history context therefore depends on both the all-turns request control where supported and the prior output items actually being present.
- On the Codex Responses path, supported reasoning efforts are `low`, `medium`, `high`, and `xhigh`; `max` maps to `xhigh`.

## Response And Catalog Normalization

- Text becomes `content` or `content_delta`; provider reasoning text fields such as `reasoning_content`/`thinking` become visible `reasoning`/`reasoning_delta`.
- Malformed tool-call argument JSON is ignored for that tool call instead of becoming fake empty arguments; valid sibling tool calls are preserved.
- Generic `/models` entries may expose modalities, supported parameters, context windows, and output limits through raw fields, `architecture`, or `top_provider`. Normalize discoverable facts into `Model.capabilities` and `Model.metadata`; do not treat sparse catalogs as negative evidence for every missing capability.
- Missing per-model output-token limits remain `max_output_tokens: null`; request fallback limits come from provider defaults such as `max_tokens: 8192`.
- `OpenAIAdapter.normalize_catalog_entry()` preserves provider-discovered ids, names, modalities, and limits, and normalizes capability parameters to vBot runtime names such as `tools`, `response_format`, `reasoning`, and `parallel_tool_calls`. Today only the `subscription` connection runs discovery; if `api-key` ever gains a `models_endpoint`, the adapter normalization must be reviewed for that path.

## Codex Catalog (`/codex/models`)

- `models_endpoint` is `/codex/models`; the `subscription` connection participates in `model.refresh_db` after OAuth is usable.
- Discovery sends the same account-routing and beta/originator headers as runtime requests. `/codex/models` also requires `client_version=0.136.0`; older values such as `0.1.0` can return a valid but empty model list.
- `/codex/models` may return entries in a top-level `models` list rather than `data`, with ids/names exposed as `slug` and `display_name`.
- Sparse `/codex/models` entries remain usable as text Codex Responses models: tools, structured output, and reasoning default to supported unless the catalog explicitly says otherwise. Unknown context-window and max-output-token facts stay `null` (the OpenAI-compatible base normalizer the Codex path delegates to emits honest `None`, never a placeholder `0`); the read-side `resolve_context_window` chain fills a window when needed.
- Do not hand-edit `resources/models/openai.json` for Codex entries; model refresh owns that file.

## Per-Model `connections` Allowlist

Each `Model` carries `connections: tuple[str, ...]`, loaded from `Model.connections` in the sanitized catalog:

- Empty tuple means the model is valid on every connection of its provider.
- A non-empty tuple restricts the model to the listed connection ids of its provider. Connection-bound Codex models (`connections: ["subscription"]`) are only offered on the subscription connection; Platform models (`connections: ["api-key"]`) only on the api-key connection.
- The rule is enforced everywhere via `Model.allows_connection(connection_id)` (the single source): target expansion in `core/model_tasks/` skips forbidden connections; the WebUI model dropdown (`modelSelection.js`) only offers a model on connections it permits; and the server rejects a save (`agent.create`/`agent.update`, `settings.update` for the default agent and compaction summary models) that pins a model to a forbidden connection — so a subscription-only Codex model can no longer be saved against an api-key connection and fail only at run time.
- Refresh tags every discovered model with `connections: [<credential_connection.id>]` and merges into the existing catalog by replacing only models whose `connections` include the current connection id; models belonging to other connections are preserved.

OpenAI task-model overrides use the same allowlist to keep offered targets honest: OpenAI TTS/STT, DALL-E, `gpt-image-1`, `gpt-image-1-mini`, and `gpt-image-1.5` are `connections: ["api-key"]` because no working subscription task wire is verified for them. `gpt-image-2` stays unrestricted: on `api-key` it uses the Platform image endpoint, and on `subscription` it renders through the Codex image-generation tool, which the backend currently routes to the `gpt-image-2-codex` family.

## Codex Image Generation (`subscription` task wire)

Live-verified against the real ChatGPT Plus/Pro subscription connection on 2026-07-03: `openai/gpt-image-2::subscription` can generate images by posting to `POST https://chatgpt.com/backend-api/codex/responses` with the Codex header recipe (`Authorization: Bearer <fresh OAuth token>`, `chatgpt-account-id` derived from the current JWT via `extract_chatgpt_account_id`, plus `CODEX_EXTRA_HEADERS`). This is an internal/undocumented wire; if it breaks, first re-verify the raw wire before changing model visibility or UI behavior.

A carrier chat model drives an `image_generation` tool call; the backend forces the `gpt-image-2-codex` family regardless of the requested model. The full request/response shape, the tool-option rules, and the re-verification playbook are task-gated → `openai/codex-image.md`.

## Usage Probe (`/wham/usage`)

The subscription usage fetcher in `core/providers/usage.py` (see `providers/usage.md`). Live-verified against the real endpoint 2026-06-16 (HTTP 200):

- `GET <connection.base_url>/wham/usage` (base_url `https://chatgpt.com/backend-api`).
- Headers mirror the Codex runtime path: `Authorization: Bearer <oauth token>`, `chatgpt-account-id: <id>` (from the JWT via `extract_chatgpt_account_id`, falling back to token-store `extra.chatgpt_account_id`), plus `CODEX_EXTRA_HEADERS` (`OpenAI-Beta`, `originator`). A missing account id → snapshot error "Reconnect required".
- Body (verified shape): `rate_limit.primary_window` + `secondary_window`, each `{used_percent, limit_window_seconds, reset_at}` with `reset_at` an **epoch-seconds** int; top-level `plan_type` (lowercase, e.g. `"plus"`); `credits.{has_credits, balance}` where `balance` is a **string**.
- Normalization: primary window label = `{hours}h` from `limit_window_seconds`; secondary label = `Week` / `Day` / `{hours}h` by cadence; `plan = plan_type`, with `· <balance> credits` appended only when `has_credits` is true and the (string) balance parses > 0.

## Error Classification

- 401/403 -> `ProviderAuthError`
- 429 -> `ProviderRateLimitError`
- 502/503 -> retryable `ProviderError`
- Other 4xx/5xx -> non-retryable `ProviderError`
- Timeout -> `ProviderTimeoutError`
- Connect errors -> `NetworkError`
- Responses in-band errors are classified by exact structured code rather than message text; auth, rate-limit, timeout, transient service, and fatal codes enter the same exception taxonomy. Unknown codes fail closed as non-retryable.

## Constraints & Gotchas

- Provider defaults are merged with `setdefault`; caller kwargs win.
- Extra headers are merged after auth headers.
- The Codex `OpenAI-Beta` and `originator` headers are adapter-owned and must never leak into the chat-completions path. Adding them to provider-level `extra_headers` is forbidden.
- Only one adapter class (`OpenAIAdapter`) exists for this provider; the wire variant is selected per construction from `connection_mode`. Do not introduce a separate `openai_subscription` provider or adapter.
- Do not route the `subscription` connection through the generic `/chat/completions` path; its supported runtime path is `/codex/responses`.
- The OpenAI Codex Device Flow fields are provider-specific metadata parsed by `OAuthConfig`; standard OAuth providers should continue using `device_flow: oauth2`.
- Token values, authorization codes, user codes, refresh tokens, and account ids must never be logged.

## References

Read only when your task matches — not by default.

- Building on or debugging subscription image generation → `openai/codex-image.md`
