# OpenAI Provider

Single `openai` provider covering both OpenAI Platform API-key access and ChatGPT Plus/Pro subscription access. One `OpenAIAdapter` class resolves the runtime wire from both the Connection and the selected Model through its wire profile: Platform API-key Models may use `/chat/completions` or `/responses`, while subscription Models use `/codex/responses`.

## Interfaces

- Provider config: `resources/providers/openai.json`
- Adapter selector: `openai`
- Adapter class: `OpenAIAdapter` (subclass of `OpenAICompatibleAdapter`)
- Wire profile: `resources/wire/openai.json` holds the request choices per protocol, Connection and Model: the protocol (`protocol`), allowed optional parameters, reasoning floor/off spelling/`reasoning.context`, and media. The Adapter reads only the resolved `protocol` to route a request and keeps the Codex/Platform transports, WebSocket continuation, the default `instructions`, and catalog normalization in code.
- Connections:
  - `openai:api-key` - `type: api_key`, `auth.credential_key: OPENAI_API_KEY`, `base_url` defaults to the provider-level OpenAI Platform URL. A Model entry in the wire profile with `connections.api-key.protocol: responses` selects public Responses; a Model without one keeps the conservative `/chat/completions` fallback.
  - `openai:subscription` - `type: oauth`, `base_url: https://chatgpt.com/backend-api`, `mode: codex_responses`, `models_endpoint: /codex/models`. ChatGPT Plus/Pro Codex OAuth device flow.
- Runtime endpoints: `POST <base_url>/chat/completions` or `POST <base_url>/responses` (api-key, selected per Model); subscription Runs prefer `WS(S) <base_url>/codex/responses` and retain `POST <base_url>/codex/responses` SSE as the compatibility path.
- Catalog: the provider has no provider-level `models_endpoint`. `subscription` discovers the Codex catalog at `/codex/models`; `api-key` discovers `GET /v1/models` only for its embedding Models (see Response And Catalog Normalization). Its chat, speech, image, and Live Voice Models stay curated in `openai.overrides.json`.

## Connection Configuration

Per-connection fields carried by `ConnectionConfig`:

- `mode: str | None` - adapter-interpreted endpoint selector. `OpenAIAdapter` reads it at construction: `codex_responses` sends Responses requests to `/codex/responses` (with the Codex headers, instructions default and WebSocket continuation); any other value sends them to the Platform `/responses`. Whether a request is Responses or Chat Completions is the wire profile's `protocol` (the `subscription` Connection block sets `responses` for every Model).
- `models_endpoint: str | None` - discovery endpoint, overrides the provider-level value. Used by `subscription` for `/codex/models`.
- `base_url: str | None` - overrides the provider-level base URL. Used by `subscription` to point at `chatgpt.com/backend-api`.

`mode` and `models_endpoint` must be strings when present; non-string values are a config error.

The adapter is selected by provider `adapter`, not by connection, so the same `OpenAIAdapter` class is instantiated for both connections. `get_adapter` threads the connection's `mode` into the adapter as `connection_mode`.

## Wire Contract

Every OpenAI function Tool definition carries `strict: false` on Chat Completions, public Responses, and subscription Codex Responses. This is mandatory on Responses because OpenAI may otherwise normalize an omitted field into strict mode. A live `openai:subscription` GPT-5.6 Luna probe on 2026-07-31 forced a Tool Call whose only required argument was `url`; with the explicit opt-out the model omitted both optional Boolean arguments as requested and the canonical schema remained unchanged.

### Chat Completions (conservative `api-key` fallback)

Used on the `api-key` Connection when the wire profile's `protocol` is `chat_completions` (every Model without a Responses entry). Delegates to `OpenAICompatibleAdapter`; behavior is the generic OpenAI-compatible contract, with the wire file's `protocols.chat_completions` choices (explicit non-strict Tool schemas, `none` in the effort floor, images plus WAV/MP3 audio and PDF documents):

- Canonical system/user/assistant messages stay in the OpenAI-style `messages` array.
- Canonical `tool` messages become `role: tool` messages with `tool_call_id`.
- Canonical assistant `tool_calls` become OpenAI function-call structures.
- Provider tool definitions become `{"type":"function","function":{...}}` entries.
- Streaming uses `stream: true`, SSE `data:` frames, `[DONE]`, and `stream_options.include_usage: true`; caller-provided `stream_options` are preserved except `include_usage` is forced true.
- The Codex extra headers (`OpenAI-Beta`, `originator`) must **not** be added on this path.

### Platform Responses (`api-key` connection, selected Models)

- A Model whose wire-profile entry sets `connections.api-key.protocol: responses` uses public `POST /responses`; entries cover GPT-5.2, GPT-5.5, the `gpt-5.6` alias, GPT-5.6 Luna/Sol/Terra, GPT-6 Luna/Sol, and GPT-6.1 Sol. Other Platform Models remain on Chat Completions.
- Requests use the shared Responses item protocol with `store: false`. Assistant history replays each original output item from `reasoning_meta.response_output`, preserving encrypted reasoning, ids, Tool items, ordering, and assistant `phase`, with look-alike System Reminder tags neutralized only in readable `output_text`, `summary_text` and `reasoning_text` (`chat/request-building.md`); reconstruction exists only for legacy Sessions that predate item capture.
- Structured `error`, `response.error`, and `response.failed` events use the shared Responses error classifier with OpenAI's lenient policy (Stream Error Classification below). Chat restarts a retryable failure before visible output and continues from the preserved partial after it (`chat/run-execution.md`); `response.incomplete` instead completes with a safe non-Tool terminal outcome.
- Public Responses may send native PDF `input_file` parts. Its optional request parameters (`protocols.responses.request.allowed_parameters`: `max_tokens`, `max_output_tokens`, `top_p`) are distinct from the private subscription wire. A Model the catalog does not know is treated as a reasoning Model (`reasoning.supported: true`).
- Establishment runs through the shared rejection learning (`providers/request-policy.md` -> HTTP, retry, and streaming): a rejected `top_p` or a rejected effort the request named is learned and the request is rebuilt and retried once. This covers send and stream on the Platform `/responses` (also OpenCode Zen's Responses route and xAI) and on the Codex wire over SSE and WebSocket. The Codex WebSocket reports a rejected request as an error frame before the first delta; the failed exchange drops its socket and continuation, so the retry opens a fresh socket with the full context (`test_a_rejection_event_before_the_first_delta_is_learned_on_a_fresh_socket`).

### Codex Responses (`subscription` connection - `mode: codex_responses`)

- Every Model on this Connection speaks Responses (`connections.subscription.protocol` in the wire file). Requests use the shared Responses payload builder and post to `/codex/responses` relative to the connection's `base_url`.
- The Codex backend requires an `instructions` field. The adapter uses assembled system instructions when present and falls back to `You are a helpful assistant.`.
- The Codex backend requires `store: false`; omission is rejected like an enabled store request.
- The Codex backend rejects output-token limit parameters and sampling `top_p`. The wire file allows no optional parameter here (`connections.subscription.request.allowed_parameters: []`), so `max_tokens`, `max_output_tokens`, and `top_p` are filtered instead of forwarding provider defaults or caller kwargs, and the local output-limit clamp is skipped entirely on this wire so a still-fitting request cannot die on a reserve that is never sent. An unspecified Chat-loop `top_p` (`None`) is dropped before the payload is built so it cannot become `"top_p": null`.
- The wire requires `stream: true`, including when a caller uses the adapter's logical `send()` interface. `stream()` yields normalized vBot deltas; `send()` consumes exactly one streaming exchange internally through a terminal event and accumulates text, reasoning, metadata, usage, Tool Call fragments, and the normalized terminal outcome across the whole stream into one canonical response. The live wire may leave `response.completed.response.output` empty even after emitting text deltas, so the completed object alone is not an answer. Do not retry a rejected non-streaming request as a stream - that would create a second billable request.
- A subscription call with a conversation id prefers one persistent WebSocket for the active Run. The first Model step sends a full `response.create`; a later step on the identical route may send `previous_response_id` plus only the newly appended input suffix when every non-input request field still matches and canonical input is exactly the preceding input followed by the preceding response output. Any mismatch uses a full request.
- WebSocket continuation is isolated by conversation, Model, and ChatGPT Account; the adapter instance already isolates Provider Connection and one Run. Changing any route component closes the old socket and clears its continuation. Run cleanup closes the socket.
- A connection-scoped `previous_response_not_found` response clears the continuation, opens a fresh socket, and retries that step once with full context. Any WebSocket transport failure disables WebSocket for that route for the rest of the adapter lifetime. Before any Provider event, the adapter falls back directly to full-context SSE; after Provider events, it never replays the same in-flight exchange internally and instead propagates the failure to Chat. If no Assistant answer text was emitted, Chat may safely restart the stream, and that next attempt uses full-context SSE because the route is disabled; after answer text, Chat preserves the partial result instead of replaying it. Calls without a conversation id and the explicit `sse` test/compatibility mode use SSE directly.

### Stream Error Classification

- Both Responses paths - subscription Codex Responses over WebSocket or SSE, and Platform `/responses` - create lenient stream states (`openai.py::_responses_stream_state`): an in-band error whose code vBot does not know is retryable and enters Chat's bounded recovery (nine attempts per route, thirty minutes). Known deterministic codes stay fatal, including OpenAI's quota and spend codes (`insufficient_quota`, `credit_balance_exhausted`, `organization_spend_limit_exceeded`, `project_spend_limit_exceeded`), `usage_not_included`, `invalid_prompt`, and the policy codes `cyber_policy`, `bio_policy` and `misalignment_policy_violation` (`errors.py`).
- A wrapped WebSocket error frame `{"type": "error", "status": <HTTP status>, "error": {...}}` is classified by its status like an HTTP failure: 401/403 auth, 429 rate limit, 502/503/504 transient, any other 5xx retryable through leniency. Any other 4xx rejects the request and stays fatal unless its code is known retryable (`websocket_connection_limit_reached`). Persisted failures carry the error object, code and status as trailing JSON (`providers/request-policy.md`).
- Evidence: on 2026-09-29 a Swarm Run on `openai/gpt-6-luna::subscription` failed at once with the in-band message "Unable to verify model access right now. Please retry." because its unknown code was fatal, while the official Codex CLI retried the same outage ("Reconnecting... n/5"). The policy mirrors `openai/codex` at commit `26dd19ef478c` (read 2026-09-29): `codex-api/src/sse/responses_error.rs::parse_failed_response` retries every unlisted `response.failed` code, `codex-api/src/endpoint/responses_websocket.rs::map_wrapped_websocket_error_event` treats a frame's status as an HTTP error, and `protocol/src/error.rs::CodexErr::retry_delay` defines which errors stop. Codex is OpenAI's own client for the private subscription wire, but its source is no official contract. The exact code and status of the 2026-09-29 error were not captured and remain unknown.
- Deliberate difference: Codex also retries HTTP 500 at stream establishment; vBot's shared policy still treats a Provider POST 500 as fatal.
- Regression anchor: `test_openai_websocket.py::test_codex_error_events_follow_codex_retry_classification`.

## OAuth (subscription connection)

- The flow is marked with `oauth.device_flow: openai_codex`; this is distinct from the standard RFC 8628-style Device Flow used by GitHub OAuth.
- Device authorization posts JSON `{"client_id": ...}` to `https://auth.openai.com/api/accounts/deviceauth/usercode`; the user verifies at `https://auth.openai.com/codex/device`.
- Polling posts JSON `{"device_auth_id": ..., "user_code": ...}` to the matching `/token` device-auth endpoint. HTTP 403 and 404 are treated as `authorization_pending` for this provider.
- Successful polling returns an authorization code and PKCE verifier; vBot exchanges them at `https://auth.openai.com/oauth/token` with `grant_type=authorization_code` and `redirect_uri=https://auth.openai.com/deviceauth/callback`.
- Refresh uses the OAuth `refresh_token` grant against the same token endpoint. Refreshed tokens keep a replacement refresh token when OpenAI sends one and preserve the existing token otherwise.
- A subscription HTTP/SSE establishment rejected with HTTP 401 forces one OAuth refresh even when the stored token expiry is still in the future, rebuilds the token-derived Authorization and ChatGPT Account headers, and retries the original establishment once. A second 401, HTTP 403, or a getter without refresh capability leaves the auth error final; a refresh failure retains its own error type; an exchange that has emitted Provider events is never replayed by this recovery.
- Subscription catalog GETs use the same rejected-token getter capability with one recovery per request. Discovery keeps the selected Account's getter instead of freezing its access token before fetching; Authorization and `chatgpt-account-id` are rebuilt after recovery. The policy now belongs to the shared `OAuthRequestRecovery` in `token_getter.py`, also used by the other OAuth Providers and by OpenAI Usage; static API-key credentials have no recovery capability. Regression coverage lives in `tests/core/models/test_discovery_auth_recovery.py` and `tests/server/rpc/test_model_methods_refresh.py`.
- Live recovery check (2026-09-09): a controlled invalid JWT signature produced a real catalog 401; one rejected-token recovery reused the valid token already in the shared store, and the next catalog request returned 200. The mocked OAuth regression separately verifies the refresh-token POST, replacement-token persistence, and rebuilt Account headers when the stored access token itself is rejected despite a future expiry.
- The shared-recovery live check on 2026-09-09 also covered `GET /wham/usage` and GPT-5.6 Luna `POST /codex/responses` through `Adapter.send()` (internally SSE): each returned 401 then 200, rebuilt headers, invoked recovery once, and left stored credentials unchanged by reusing the valid token already present. Other OAuth Providers have local contract coverage; this OpenAI observation is not their live evidence.
- The OAuth token file path is `<data_dir>/oauth/openai-subscription.json` for the `default` Account and `openai-subscription--<account>.json` for additional named Accounts (see `providers/connections.md` -> Identity and Accounts).

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

Provider-level `extra_headers` are never merged on the Codex path, so a stray config entry cannot leak onto it. A wire profile's per-Model `request.extra_headers` is a deliberate declaration: the Codex SSE requests and the WebSocket handshake add it after the headers above (`providers/request-policy.md` -> Wire request extras).

## Codex Continuation And Prompt Caching

The ChatGPT Codex backend routes its prompt cache by **per-request transport headers scoped to the conversation** - SSE uses `session_id` plus `x-client-request-id`; WebSocket uses `session-id` plus `x-client-request-id` - **not** by the body-level `prompt_cache_key` field. Live-verified 2026-07-09 on SSE: sending `prompt_cache_key` in the body has no measurable effect (~1/6 hit rate, same as sending nothing), while a stable conversation scope on the two routing headers lifts hits to ~5/6 (only the cold first request misses). Mirrors the Codex CLI and the `hermes-agent` `codex_responses` transport.

- Chat hands `ProviderAdapter.request_context_kwargs(...)` both the Session-unique conversation identity and the Session domain's separate `prompt_cache_affinity_id`. `OpenAIAdapter` returns both as internal request kwargs: the affinity alone stamps the SSE/WebSocket cache-routing headers, while `conversation_id = agent_id:session_id` alone keys the local WebSocket connection and `previous_response_id` continuation. Same-Agent, same-Project forks therefore share the best available cache route for their inherited prefix but can never consume each other's stateful continuation. Both values are adapter-internal and never enter the request body; the affinity is a **routing hint only**, so the exact wire prefix still decides cache correctness.
- **Fork-affinity live verification (2026-07-31):** two `gpt-5.6-terra` subscription requests used distinct conversation ids and one shared cache-affinity header value. The source request was cold (`0 / 23,827` cache-read/input tokens); the fork-shaped prefix extension read `23,296 / 23,839` tokens from cache. The adapter's WebSocket test independently asserts that the second Session sends no `previous_response_id`, so the hit comes from shared prompt-cache routing rather than cross-Session continuation.
- The `api-key` `/chat/completions` path ignores the conversation id (it pops and drops it) and relies on OpenAI's default prefix-hash cache routing. Every non-OpenAI adapter never receives it (the base `request_context_kwargs` returns `{}`), so no other wire sees an unknown field.
- `store: true` remains rejected (`{"detail":"Store must be set to false"}`). The WebSocket beta nevertheless supports connection-scoped `previous_response_id` continuation while that socket retains the response; it is an optimization, not durable server state. Full-context replay remains the correctness path after socket loss, route change, prefix mismatch, or missing continuation.

## Reasoning

- vBot `thinking_effort` and raw `reasoning_effort` use the selected Model's catalog ladder. Without one, the wire file's floor applies (Chat Completions `none/low/medium/high`, Responses `low/medium/high/xhigh`); a richer catalog preserves higher supported levels rather than clamping them globally. Both protocols plan the effort with `ReasoningWire.plan()`; Responses spells it in the `responses_reasoning` dialect (`reasoning: {effort, summary: auto}`, plus `include: [reasoning.encrypted_content]` for a Model known to reason).
- Generic OpenAI-compatible gateways omit explicit `none`; the direct OpenAI provider may send `none` only when catalog data confirms reasoning support.
- If injected `model_lookup` says reasoning is unsupported, reasoning request controls are stripped.
- Replay scope is the shared wire-profile default `full_history` for the current reasoning Models on their allowed Connections; `resources/wire/openai.json` narrows none. The wire file also carries the Responses protocol and the public `reasoning.context`. Assistant `phase` remains semantic history and is preserved across Runs.
- On 2026-09-02, public Platform GPT-5.2, GPT-5.4, GPT-5.4 Mini, GPT-5.5, and GPT-5.6 used `/responses`; GPT-5.6 alone adds the `all_turns` persisted-reasoning request contract. The public Platform path is documentation- and local-contract-verified on 2026-09-02 but not live-verified because the development API-key Connection is not usable.
- On public Platform Responses, GPT-5.6 (the alias and the Luna/Sol/Terra variants) sends `reasoning.context: "all_turns"` so prior output items are available as Session-wide reasoning context; the wire file sets it as `reasoning.options.context` in their `api-key` Connection blocks. The private subscription `/codex/responses` contract is also full-history replay for those Models, but vBot does not send the public `reasoning.context` field there because support is not documented or live-verified.
- Opaque reasoning fields such as `encrypted_content` and the complete Responses `output` array stay in `reasoning_meta` for exact round-tripping. A GPT-5.6 full-history context therefore depends on both the all-turns request control where supported and the prior output items actually being present.
- Live exact Adapter/history probes on 2026-09-02 verified complete output-item and encrypted-reasoning replay across later Runs plus Tool continuations for subscription GPT-5.3 Codex Spark, GPT-5.4, GPT-5.4 Mini, GPT-5.5, GPT-5.6 Luna/Sol/Terra, and the upstream `gpt-reserve` and `codex-auto-review` slugs. The last two remain excluded from the selectable catalog because `/codex/models` marks them `visibility: hide`.
- On the Codex Responses path, the selected Model's catalog ladder controls effort rendering. GPT-6 Astra supports `low/medium/high/xhigh/max`; `minimal` maps to `low`. Its wire-file entry sets `reasoning.off: low` on the subscription Connection, so `none` sends `low` and the displayed Reasoning intent agrees. A Codex Model without such an entry omits reasoning for `none` (`connections.subscription.reasoning.off: omit`); the private wire never receives the `none` effort. Omitting effort remains the Provider default. Other Models retain their own ladders and policies.
- Shared OpenAI wire ids do not imply shared Context limits. The public Platform values are GPT-5.2 and GPT-5.4 Mini at 400,000 and GPT-5.4, GPT-5.5, GPT-5.6 Luna/Sol/Terra, GPT-6 Sol/Luna, and GPT-6.1 Sol at 1,050,000; the successfully refreshed subscription Codex catalog reports 272,000 for its shared ids, including all three Sol/Luna ids on 2026-10-02. `connection_context_windows` preserves the active Connection's limit for Chat compaction and Adapter output budgeting.

### GPT-6.1 Sol profile (2026-10-02)

The official [Model page](https://developers.openai.com/api/docs/models/gpt-6.1-sol), [reasoning guidance](https://developers.openai.com/api/docs/guides/reasoning), and [ChatGPT Model guide](https://learn.chatgpt.com/docs/models) were read on 2026-10-02. The exact id is `gpt-6.1-sol`; Responses is required for Tool calls. Public Platform facts are text/image input, text output, 1,050,000 Context, 128,000 output, and `low/medium/high/xhigh/max` (default `medium`). Bundled canonical facts/prices come from the complete models.dev refresh. The manual canonical join exposes API-key and subscription, with Responses on both and Connection-specific Context limits of 1,050,000 and 272,000 respectively. Its wire-file entry sets `reasoning.off: low` for both Connections: `none` maps to `low`, and `minimal` snaps to `low`. No `reasoning.context` field is inferred from GPT-5.6.

Independent raw subscription SSE requests accepted all five efforts, returned default `medium` on omission, and rejected `none` and `minimal` with HTTP 400 `unsupported_value`. `max_output_tokens`, `temperature`, and `top_p` were also rejected on this private endpoint; public Platform request support must not be transferred to it. The existing exact Adapter probe passed plain and Tool continuations through normal conversation-context transport. The public API-key Connection has documentation and local request coverage but no usable development credential for a live request.

Actual streamed plain and two-Tool responses were accumulated by Chat, saved to disposable SQLite, reopened, and continued through fresh Adapters with normal conversation context. Original output items, encrypted Reasoning, Tool ids, and order survived exactly. A omitted historical Reasoning; B retained the exact returned items; C added visible accounting text (the returned summary when present, otherwise a synthetic calibration). Controls and Tool definitions stayed fixed within each comparison:

| Request shape | A: absent | B: exact Reasoning | C: visible control |
| --- | ---: | ---: | ---: |
| Completed plain history, later Run without Tools | 67 | 171 | 176 |
| Completed plain history, later Run with Tools | 110 | 214 | 219 |
| Immediate two-Tool continuation | 180 | 203 | 264 |
| Completed Tool history, later Run without Tools | 163 | 200 | 405 |
| Completed Tool history, later Run with Tools | 206 | 243 | 448 |

The positive B increases support inherited `full_history` on this exact subscription route. The streamed persisted Tool continuation returned terminal Usage (203 input/20 output) and the correct result. Canonical PNG/JPEG/GIF/WebP fixtures and a strict Responses JSON schema also completed correctly. Maximum capacities, sampling efficacy, and cache effectiveness were not measured.

Regression anchors: the expanded GPT-6 catalog/effort cases in `tests/core/models/test_models_resources.py` and `tests/core/providers/test_openai_requests.py`, plus persisted exact output and route isolation in `tests/core/providers/test_reasoning_route_switch_conformance.py`.

### GPT-6 Sol/Luna profiles (2026-09-22)

The public Model pages and API changelog document `gpt-6-sol` and `gpt-6-luna` on Responses with text/image input, text output, Tools, 1,050,000 Context, 128,000 output, and `none/low/medium/high/xhigh/max` efforts. Chat Completions function calling is restricted to effort `none`, so bundled API-key profiles select Responses. The Adapter sends explicit `reasoning.effort: none` on direct Platform Responses when the Model's catalog ladder allows it; omission would select the Provider default instead. OpenAI's [ChatGPT model guide](https://learn.chatgpt.com/docs/models) also lists both ids for Codex with ChatGPT sign-in, so the same override exposes the subscription Connection through Codex Responses. Its subscription off effort is conservatively `low`, as for Astra, because the public API's `none` support does not establish private Codex wire support. The public models.dev refresh supplies canonical capabilities and Standard price tiers; `openai.overrides.json` provides the exact canonical joins; the wire file selects Responses on `api-key` and the `low` off effort on `subscription`. The last bundled subscription catalog predates these ids, and the development account's token refresh failed on 2026-09-22, so this route and its 272,000-token limit remain best-effort until an authenticated refresh and inference probe succeed. No `reasoning.context` mode is pinned: current public reasoning guidance explicitly documents `all_turns` for GPT-5.6, but does not establish it for GPT-6 Sol/Luna.

### GPT-6 Astra verification (2026-09-10)

- The refreshed subscription catalog exposes `gpt-6-astra`, with a 272,000-token Context window, 128,000-token output fact, Tools, and all five effort levels. It remains subscription-only in the bundled Model DB. The public Model page's larger Context window does not replace the subscription limit.
- Live raw `/codex/responses` accepted each published effort. Explicit `none` returned HTTP 400; omission selected `medium`. The Adapter now maps `none` to `low`. Streaming exposed text, Reasoning metadata, Tool Call deltas, and terminal Usage. Requests include `reasoning.encrypted_content` and `store: false`; the private wire receives no public `reasoning.context` field, although responses reported `all_turns` themselves.
- Real streamed output was saved to SQLite, reopened, shaped by Chat, and serialized by the Adapter. Encrypted Reasoning items, Tool items, ids, order, and phase survived exactly. Controlled input-token comparisons matched independent raw HTTP: absent/exact carrier was 76/89 for later-Run history without Tools, 114/127 with Tools, and 138/160 for a Tool continuation. Visible-content controls increased input too; opaque and readable representations need not cost the same tokens. These positive replay observations support `full_history`.
- Additional live Adapter checks accepted canonical PNG/JPEG/GIF/WebP media and identified the generated solid-color fixtures correctly. A strict Responses JSON schema returned the required integer object. The lowest-effort request produced two correctly correlated parallel Tool Calls. The exact replay probe also passed through normal conversation-context transport, in addition to the SSE/raw accounting checks.
- Regression anchors: the GPT-6 catalog/effort cases in `tests/core/models/test_models_resources.py` and `tests/core/providers/test_openai_requests.py`, plus `tests/core/providers/test_reasoning_route_switch_conformance.py` for persisted exact output and route isolation. `scripts/probe_reasoning_replay_exact.py` uses the active checkout and Adapter request context, so its live calls carry the same conversation-routing contract as Chat.

## Response And Catalog Normalization

- Shared Responses normalization projects `reasoning.summary` into ordered `reasoning_summary` text sections. Stream identity is `(output_index, summary_index)`; summary delta/done, part added/done, item snapshots, and terminal snapshots backfill without duplicating sections. Canonical `reasoning_delta` additionally carries a sequential `summary_index` plus `summary_text`; flat `text` inserts paragraph separators between sections. Opaque output items remain unchanged. Official reasoning/event docs and local normalization/persistence tests cover this presentation contract (`test_openai_streaming.py`, 2026-09-22); this change has no new live OpenAI compatibility claim.
- Text becomes `content` or `content_delta`; provider reasoning text fields such as `reasoning_content`/`thinking` become visible `reasoning`/`reasoning_delta`.
- Malformed Tool Call argument JSON produces a canonical rejected Call instead of fake empty arguments; valid sibling Calls are preserved. The shared canonical normalization contract applies to both Responses and Chat Completions.
- Generic `/models` entries may expose modalities, supported parameters, context windows, and output limits through raw fields, `architecture`, or `top_provider`. Normalize discoverable facts into `Model.capabilities` and `Model.metadata`; do not treat sparse catalogs as negative evidence for every missing capability.
- Missing per-model output-token limits remain `max_output_tokens: null`; request fallback limits come from provider defaults such as `max_tokens: 8192`.
- `OpenAIAdapter.normalize_catalog_entry()` preserves provider-discovered ids, names, modalities, and limits, and normalizes capability parameters to vBot runtime names such as `tools`, `response_format`, `reasoning`, and `parallel_tool_calls`.
- Platform discovery (`api-key`, `GET /v1/models`) lists bare ids with no capability facts. The Codex discovery hooks (account routing, Codex headers, `client_version`) apply only to a `codex_responses` Connection, so this request carries just the API key. `accepts_discovered_model` keeps only `text-embedding-*` ids, which normalize to text embedding Models (`dimensions` only for `text-embedding-3-*`); price and limits arrive from the models.dev `openai` section during refresh. Every other Platform Model stays curated. A curated Model that also has a generated `api-key` entry needs a `canonical` pointer in its override (as `gpt-5.2` has), because an `api-key` refresh replaces the generated `api-key` entries and the override alone must still load. xAI and OpenCode Zen inherit this Adapter but accept every entry of their own listings.

## Codex Catalog (`/codex/models`)

- The 2026-09-09 refresh no longer lists `gpt-5.4` or `gpt-5.4-mini`; their partial bundled overrides were removed with the catalog update. Earlier dated compatibility observations below and above do not establish current catalog availability. `test_models_resources.py::test_every_bundled_provider_override_loads` rejects leftover overrides that lack a loadable base.
- `models_endpoint` is `/codex/models`; the `subscription` connection participates in `model.refresh_db` after OAuth is usable.
- Discovery sends the same account-routing and beta/originator headers as runtime requests. `/codex/models` gates newly available Models by `client_version`: a refresh fetches the current stable `@openai/codex` npm version; if that fetch fails the adapter sends fallback `0.144.0`. Older values such as `0.1.0` can return a valid but empty list. GPT-5.6 Luna/Sol/Terra advertise `minimal_client_version` `0.144.0`. Chat `/codex/responses` requests do not send `client_version`.
- `/codex/models` may return entries in a top-level `models` list rather than `data`, with ids/names exposed as `slug` and `display_name`.
- The generated selectable catalog excludes entries whose Codex metadata says `visibility: hide`.
- Sparse `/codex/models` entries remain usable as text Codex Responses models: tools, structured output, and reasoning default to supported unless the catalog explicitly says otherwise. Unknown context-window and max-output-token facts stay `null` (the OpenAI-compatible base normalizer the Codex path delegates to emits honest `None`, never a placeholder `0`); the read-side `resolve_context_window` chain fills a window when needed.
- Do not hand-edit `resources/models/openai.json` for Codex entries; model refresh owns that file.

## Per-Model `connections` Allowlist

Each `Model` carries `connections: tuple[str, ...]`, loaded from `Model.connections` in the sanitized catalog:

- Empty tuple means the model is valid on every connection of its provider.
- A non-empty tuple restricts the model to the listed connection ids of its provider. Connection-bound Codex models (`connections: ["subscription"]`) are only offered on the subscription connection; Platform models (`connections: ["api-key"]`) only on the api-key connection.
- The rule is enforced everywhere via `Model.allows_connection(connection_id)` (the single source): target expansion in `core/model_tasks/` skips forbidden connections; the WebUI model dropdown (`modelSelection.js`) only offers a model on connections it permits; and the server rejects a save (`agent.create`/`agent.update`, `settings.update` for the default agent and compaction summary models) that pins a model to a forbidden connection - so a subscription-only Codex model can no longer be saved against an api-key connection and fail only at run time.
- Refresh tags every discovered model with `connections: [<credential_connection.id>]` and merges into the existing catalog by replacing only models whose `connections` include the current connection id; models belonging to other connections are preserved.

OpenAI task-model overrides use the same allowlist to keep offered targets honest: OpenAI TTS/STT, DALL-E, `gpt-image-1`, `gpt-image-1-mini`, and `gpt-image-1.5` are `connections: ["api-key"]` because no working subscription task wire is verified for them. `gpt-image-2` stays unrestricted: on `api-key` it uses the Platform image endpoint, and on `subscription` it renders through the Codex image-generation tool, which the backend currently routes to the `gpt-image-2-codex` family.

The public `gpt-5.6` alias is also `connections: ["api-key"]`: the subscription endpoint live-rejected that slug on 2026-09-02 with HTTP 400 while accepting GPT-5.6 Luna/Sol/Terra. Never infer alias availability from the named variants or transfer a Platform alias to the Codex wire.

## Codex Image Generation (`subscription` task wire)

Live-verified against the real ChatGPT Plus/Pro subscription connection on 2026-07-03: `openai/gpt-image-2::subscription` can generate images by posting to `POST https://chatgpt.com/backend-api/codex/responses` with the Codex header recipe (`Authorization: Bearer <fresh OAuth token>`, `chatgpt-account-id` derived from the current JWT via `extract_chatgpt_account_id`, plus `CODEX_EXTRA_HEADERS`). This is an internal/undocumented wire; if it breaks, first re-verify the raw wire before changing model visibility or UI behavior.

A carrier chat model drives an `image_generation` tool call; the backend forces the `gpt-image-2-codex` family regardless of the requested model. The full request/response shape, the tool-option rules, and the re-verification playbook are task-gated -> `providers/openai/codex-image.md`.

## Usage Probe (`/wham/usage`)

The subscription usage fetcher in `core/providers/usage.py` (see `providers/usage.md`). Live-verified against the real endpoint 2026-06-16 (HTTP 200):

- `GET <connection.base_url>/wham/usage` (base_url `https://chatgpt.com/backend-api`).
- Headers mirror the Codex runtime path: `Authorization: Bearer <oauth token>`, `chatgpt-account-id: <id>` (from the JWT via `extract_chatgpt_account_id`, falling back to token-store `extra.chatgpt_account_id`), plus `CODEX_EXTRA_HEADERS` (`OpenAI-Beta`, `originator`). A missing account id -> snapshot error "Reconnect required".
- Body (verified shape): `rate_limit.primary_window` + `secondary_window`, each `{used_percent, limit_window_seconds, reset_at}` with `reset_at` an **epoch-seconds** int; top-level `plan_type` (lowercase, e.g. `"plus"`); `credits.{has_credits, balance}` where `balance` is a **string**.
- Normalization: primary window label = `{hours}h` from `limit_window_seconds`; secondary label = `Week` / `Day` / `{hours}h` by cadence; `plan = plan_type`, with `· <balance> credits` appended only when `has_credits` is true and the (string) balance parses > 0.

## Error Classification

- 401/403 -> `ProviderAuthError`
- Shared OAuth request recovery may handle one initial HTTP 401 after classification; API-key wires have no refresh capability.
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
- Only one adapter class (`OpenAIAdapter`) exists for this provider; the endpoint variant (Codex or Platform) is selected per construction from `connection_mode`, the protocol per Model from the wire profile. Do not introduce a separate `openai_subscription` provider or adapter, and do not route Models by id in Adapter code - add a wire-file entry.
- OpenCode Zen and xAI subclass `OpenAIAdapter`. xAI is profile-driven like OpenAI (`providers/xai.md`). Zen's Responses Models use the same profile-driven Responses path, with the Responses protocol of `resources/wire/opencode-zen.json` (floor `low..xhigh`, `max_tokens`/`max_output_tokens`/`top_p`, images only) and rejection learning (`providers/opencode-zen.md`). There is no declared-Responses hook; a Responses request shape change belongs in the subclass's wire file.
- Do not route the `subscription` connection through the generic `/chat/completions` path; its supported runtime path is `/codex/responses`.
- The OpenAI Codex Device Flow fields are provider-specific metadata parsed by `OAuthConfig`; standard OAuth providers should continue using `device_flow: oauth2`.
- Token values, authorization codes, user codes, refresh tokens, and account ids must never be logged.

## References

Read only when your task matches - not by default.

- Re-validating GPT-6 Astra Reasoning or stateless replay -> `https://developers.openai.com/api/docs/models/gpt-6-astra` and `https://developers.openai.com/api/docs/guides/reasoning` (read 2026-09-10)
- Building on or debugging subscription image generation -> `providers/openai/codex-image.md`
