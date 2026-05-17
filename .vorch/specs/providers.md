# Providers

Last updated: 2026-05-13 — provider auth is connection-based. Provider JSON files use `connections`, not the old single `auth` object. OAuth connections may use the Token Store instead of an environment credential. GitHub Copilot request shaping is conservative for `/responses` temperature and normalizes Messages output-token aliases to `max_tokens`.

Provider configuration, registry, and adapters. Translates vBot requests into provider-specific wire formats.

## Data Model

### AuthConfig

```python
@dataclass(frozen=True)
class AuthConfig:
    header: str       # HTTP header name for API key (e.g. "Authorization", "x-api-key")
    prefix: str       # Value prefix prepended to the key (e.g. "Bearer ", "" for Anthropic)
    credential_key: str = ""  # Credential identifier used to resolve provider credentials; required for api_key
```

### OAuthConfig

```python
@dataclass(frozen=True)
class OAuthConfig:
    flow: str                    # "device" only
    client_id: str
    device_auth_url: str
    token_url: str
    scopes: list[str]
    token_exchange_url: str | None = None
```

### ConnectionConfig

```python
@dataclass(frozen=True)
class ConnectionConfig:
    id: str                  # Provider-local connection slug, e.g. "api-key" or "oauth"
    type: str                # Supported values: "api_key" or "oauth"
    label: str               # Human-readable display label
    auth: AuthConfig         # Credential lookup and auth header metadata
    base_url: str | None     # Optional base URL override for this connection
    oauth: OAuthConfig | None # Optional OAuth flow metadata
```

Connection IDs exposed outside provider config are compositional: `<provider_id>:<connection.id>` (for example, `openai:api-key`). The local `id` only needs to be unique within one provider. Multiple connections may share the same `type`; only duplicate local IDs are rejected.

### ProviderConfig

```python
@dataclass(frozen=True)
class ProviderConfig:
    id: str                              # Unique provider identifier, used as registry key
    name: str                            # Human-readable name
    adapter: str                         # Adapter class selector, e.g. "openai_compatible", "openrouter", "github_copilot", or "anthropic"
    base_url: str                        # Base URL for the provider API
    connections: list[ConnectionConfig]  # Authentication connections in display/preference order
    defaults: dict[str, Any] | None      # Default request params (max_tokens, temperature)
    extra_headers: dict[str, str] | None # Provider-specific HTTP headers
    models_endpoint: str | None          # Path to models listing endpoint (future use)

    def get_connection(local_id: str) -> ConnectionConfig: ...
```

Source: `resources/providers/<name>.json`. One file per provider, keyed by `id`.

Bundled provider configs now include `resources/providers/github-copilot.json`,
which uses an OAuth Device Flow connection (`github-copilot:oauth`) with public
GitHub Copilot OAuth app client ID `Iv1.b507a08c87ecfe98` and Copilot token
exchange URL `https://api.github.com/copilot_internal/v2/token`.

GitHub Copilot's Device Flow scope is `read:user`. After GitHub returns the
OAuth token, Copilot exchange is a `GET` to the exchange URL with these headers:
`Accept: application/json`, `Authorization: Bearer <github_oauth_token>`,
`Copilot-Integration-Id: vscode-chat`, and `Editor-Version: vBot/0.1.0`.
Do not use GitHub's older `Authorization: token ...` scheme for this exchange;
Copilot rejects it.

**Adapter field** selects the class at runtime:
- `"openai_compatible"` → `OpenAICompatibleAdapter`
- `"openrouter"` → `OpenRouterAdapter`
- `"github_copilot"` → `GitHubCopilotAdapter`
- `"anthropic"` → `AnthropicAdapter`
- Unknown value → `ConfigError` at adapter creation time

**Connections field** replaces the old single provider-level auth field. Each connection owns its auth metadata and credential key. Unknown connection `type` values are rejected with `ConfigError` during provider config load. Connection array order is display order and preference order when a caller needs the first usable connection. `api_key` connections must define `auth.credential_key`; `oauth` connections may omit it when an `oauth` block defines the token-store backed flow. Unknown OAuth flow values are rejected with `ConfigError`; only `device` is currently valid.

**Auth field compatibility:** adapters still read auth header metadata from provider config until the adapter factory becomes fully connection-aware. Provider JSON files use only `connections`; old `auth` JSON is not supported.

**defaults** are merged into the request payload with lower priority than caller-supplied kwargs. Applied via `dict.setdefault` so caller values always win.

**extra_headers** are merged into every request after auth headers. OpenRouter uses `HTTP-Referer` and `X-Title`; OpenAI sends none.

**models_endpoint** is used by dynamic model refresh to fetch the provider's
catalog endpoint. Providers without a configured endpoint do not support manual
model refresh.

### ProviderRegistry

```python
class ProviderRegistry:
    def __init__(self, configs: dict[str, ProviderConfig])
    @classmethod
    def load(cls, resources_dir: Path) -> ProviderRegistry  # reads resources/providers/*.json, caches
    def get(self, provider_id: str) -> ProviderConfig          # raises KeyError if missing
    def list_ids(self) -> list[str]                           # sorted list of all provider IDs
```

Module-level cache keyed by resolved `resources_dir` path. Second call with the same path returns the cached instance. Duplicate provider IDs across JSON files raise `KeyError`.

Source: `core/providers/providers.py`.

### Token Store

`core/providers/token_store.py` persists OAuth tokens below `<data_dir>/oauth/` using filenames of the form `<provider_id>-<local_connection_id>.json`. `OAuthToken` stores `access_token`, optional `refresh_token`, and optional UTC `expires_at`; serialization uses ISO 8601 timestamps with explicit timezone offsets.

`TokenStore` writes atomically through `<data_dir>/.tmp/`, loads missing tokens as `None`, deletes tokens idempotently, and considers a token usable when it exists and is either unexpired or has a refresh token. Token values must never be logged.

### OAuth Device Flow

`core/providers/auth_flow.py` owns server-side OAuth Device Flow polling. `DeviceFlowEngine.start_device_flow()` requests `{ device_code, user_code, verification_uri, expires_in, interval }` from the provider; `_poll_for_token()` polls the configured token URL until success, terminal failure, or local session expiry, persists the result through `TokenStore`, and calls an injected completion callback. Active polling tasks are process-local and keyed by `(provider_id, local_connection_id)`.

Polling completion must notify the caller with `success=False` for expected
provider-side failures that happen after the user authorizes the device code,
including Copilot token-exchange HTTP/auth failures. Unexpected crashes notify
failure first and then re-raise so the UI does not wait forever while logs still
surface the bug.

For GitHub Copilot, the OAuth token is exchanged through `OAuthConfig.token_exchange_url`. The Token Store persists the Copilot API token as `access_token`, its expiry as `expires_at`, and the GitHub OAuth token in token `extra.github_oauth_token` so later refreshes can exchange it again. Device Flow logs provider/connection IDs and state only, never token values.

Copilot token refresh uses the same Bearer authorization and Copilot integration
headers as initial token exchange.

### Token Getters

`core/providers/token_getter.py` defines the async `TokenGetter` protocol used by adapters:

```python
class TokenGetter(Protocol):
    async def __call__(self) -> str: ...
```

- `StaticTokenGetter` wraps static `api_key` credentials.
- `OAuthTokenGetter` loads OAuth tokens from `TokenStore`, returns unexpired tokens, refreshes expiring Copilot tokens through `token_exchange_url` using `extra.github_oauth_token`, and raises `ProviderAuthError("OAuth token expired — please reconnect")` when no refresh path exists.

`OAuthTokenGetter` serializes refresh work with an `asyncio.Lock` so concurrent provider requests do not perform duplicate refreshes for the same adapter instance. It can be used as an async context manager; internally created HTTP clients are closed on exit, while injected clients remain caller-owned.

## Adapter Hierarchy

```
ProviderAdapter (ABC)          — core/providers/adapter.py
  ├── OpenAICompatibleAdapter  — core/providers/openai_compatible.py
  │   ├── OpenRouterAdapter    — core/providers/openrouter.py
  │   └── GitHubCopilotAdapter — core/providers/github_copilot.py
  └── AnthropicAdapter         — core/providers/anthropic.py
```

### ProviderAdapter (ABC)

```python
class ProviderAdapter(ABC):
    @abstractmethod
    async def send(self, messages: list[dict], *, model_id: str, **kwargs) -> dict: ...
    @abstractmethod
    def stream(self, messages: list[dict], *, model_id: str, **kwargs) -> AsyncIterator[dict]: ...
    def normalize_response(self, response: dict) -> dict: ...
```

- `send()` — non-streaming request, returns parsed response dict
- `stream()` — streaming request, yields normalized provider-agnostic delta dicts (`content_delta`, `reasoning_delta`, `tool_call_delta`, internal-only `reasoning_meta`, `usage`, `finish`), never raw provider SSE chunks
- `messages` is a list of dicts — the chat layer serializes `ChatMessage` objects via `.to_dict()` before passing them to the adapter
- `model_id` is the exact string sent to the provider API (no remapping)
- `**kwargs` carries provider-specific overrides (temperature, max_tokens, thinking config, etc.)
- `normalize_response()` converts provider raw responses into canonical assistant fields: `content`, `reasoning`, `reasoning_meta`, and `tool_calls`.

### OpenAICompatibleAdapter

**Wire protocol** — used directly by OpenAI, Groq, Together, and any provider
that fully follows the `/chat/completions` convention. Providers that are mostly
compatible but have provider-specific runtime or discovery behavior subclass
this adapter.

**Endpoint:** `POST /chat/completions`

**Request format:**
```json
{
  "model": "<model_id>",
  "messages": [
    {"role": "system", "content": "..."},
    {"role": "user", "content": "..."}
  ],
  "max_tokens": 4096,
  "temperature": 0.7,
  "stream": false
}
```

- Canonical `tool` messages become OpenAI `role: tool` messages with `tool_call_id`.
- Canonical assistant `tool_calls` become OpenAI `tools`/`function` call structures.
- Provider tool definitions become OpenAI `{"type":"function","function":{...}}` entries.
- Malformed provider-returned function-call argument JSON normalizes to an empty argument object instead of leaking a raw JSON parse exception.
- `ProviderConfig.defaults` merged in via `setdefault` (lower priority)
- Caller `**kwargs` merged in last (highest priority)
- `extra_headers` added to request headers
- Auth: `Authorization: Bearer <api_key>` (configurable via `AuthConfig`)

**Streaming:** `stream: true` in payload. SSE lines prefixed with `data: `. Stream ends on `data: [DONE]`. The base adapter always merges `stream_options: {"include_usage": true}` into streaming payloads so OpenAI-compatible usage chunks can be captured without provider-name checks. Each provider chunk is normalized before leaving the adapter: text becomes `content_delta`, supported reasoning text becomes `reasoning_delta`, recognized opaque reasoning fields become internal-only `reasoning_meta`, tool-call fragments become `tool_call_delta` keyed by stable tool-call IDs, and finish reasons become `finish` with `reason: "stop" | "tool_calls"`.

**Error format** — standard OpenAI error:
```json
{
  "error": {
    "message": "...",
    "type": "...",
    "code": "..."
  }
}
```

Errors are classified by HTTP status code (not by parsing the body):
- 401/403 → `ProviderAuthError` (fatal, not retried)
- 429 → `ProviderRateLimitError` (retryable)
- 502/503 → `ProviderError(retryable=True)`
- Other 4xx/5xx → `ProviderError(retryable=False)`
- Timeout → `ProviderTimeoutError` (retryable)
- ConnectError → `NetworkError` (retryable, does not trigger model fallback)

**Reasoning:** vBot `thinking_effort` is adapter-translated. The generic OpenAI-compatible adapter emits `reasoning_effort: "low" | "medium" | "high"` for supported non-`none` values. Provider-specific subclasses own alternate wire formats.

**Response normalization:** Reads assistant `content`, `reasoning`/`reasoning_content`, opaque `encrypted_content`/`reasoning_details`, and function `tool_calls` into canonical assistant fields.

**Catalog normalization:** `normalize_catalog_entry(raw, defaults)` reads standard OpenAI-compatible `/models` fields into a `Model`, including context window, max output tokens, vision, tools, JSON mode, and reasoning capability. Missing optional values fall back to provider defaults where applicable.

### OpenRouterAdapter

OpenRouter is OpenAI-compatible for chat completions but has provider-specific
reasoning and catalog schema.

- Runtime reasoning: non-`none` `thinking_effort` values accepted by OpenRouter
  (`minimal`, `low`, `medium`, `high`, `xhigh`, `max`) are sent as
  `reasoning: {"effort": ...}` plus `include_reasoning: true`.
- Streaming usage: inherited from the generic OpenAI-compatible stream behavior.
- Catalog normalization: reads OpenRouter `/models` fields such as
  `architecture.input_modalities`, `supported_parameters`, `context_length`, and
  `top_provider.max_completion_tokens`.

### GitHubCopilotAdapter

GitHub Copilot is endpoint-aware at runtime, not treated as one uniform
OpenAI-compatible provider. `GitHubCopilotAdapter` subclasses
`OpenAICompatibleAdapter` for shared auth/client/retry behavior and the
`/chat/completions` fallback path, but Copilot-specific routing and request
feature gating live in `core/providers/github_copilot_policy.py`.

**Endpoint families:**

- `/chat/completions` — delegated to `OpenAICompatibleAdapter` after the
  Copilot policy filters request kwargs. This remains the conservative fallback
  for unknown models and for models that advertise only chat completions.
- `/responses` — owned by `core/providers/github_copilot_responses.py`. This is
  OpenAI Responses-like: the helper builds payloads, normalizes `output` items,
  preserves opaque reasoning metadata when present, extracts usage, and converts
  semantic SSE events into vBot deltas.
- `/v1/messages` — owned by `core/providers/github_copilot_messages.py`. This is
  Anthropic Messages-like but Copilot-specific: the helper builds top-level
  `system`, content blocks, tools, and thinking/output configuration, then
  normalizes text/thinking/tool-use responses and SSE events into vBot fields.

`ws:/responses` is deliberately ignored by runtime policy for now; websocket
Responses frames are not implemented.

**Dynamic-first runtime policy:** Runtime adapter creation passes
`GitHubCopilotAdapter` a narrow model metadata lookup backed by `ModelRegistry`.
The adapter calls the policy once per `send()`/`stream()` request. When
`metadata.github_copilot` exists for the exact model, it is the primary source
for vendor, family, supported endpoints, allowed reasoning efforts, thinking
budget bounds, adaptive thinking support, tool support, streaming support, and
structured-output support. Static policy entries are fallback facts for missing
metadata or exact-model overrides for validated quirks; they should not hide
current catalog metadata by default.

Endpoint selection currently follows these rules:

- Claude-like models prefer `/v1/messages` when that endpoint is advertised.
- Gemini-like models stay chat-first and do not get forced onto `/responses`.
- OpenAI/GPT-like models prefer `/responses` when that endpoint is advertised.
- Otherwise the first safe endpoint is used, with `/chat/completions` as the
  conservative final fallback.

Request shaping is policy-gated per model and endpoint. Unsupported optional
features are omitted rather than sent optimistically: tools/tool choice,
parallel tool calls, structured output/JSON-style controls, OpenAI-style
reasoning efforts, thinking budgets, and adaptive thinking. Unknown Copilot
models default to `/chat/completions`, no explicit reasoning/thinking controls,
no tools, and no structured-output controls.

For `/responses`, `temperature` is currently treated as unsupported unless a
future Copilot policy explicitly adds positive support. Partial or incomplete
Copilot metadata must stay on that conservative omission path rather than
forwarding `temperature` optimistically. Copilot Responses tool-call payloads
may use nested `function.{name,arguments}` shapes; helper code must preserve a
non-empty tool name from either the top-level or nested form.

Validated exact-model quirks can keep one safe feature while stripping adjacent
unsupported controls. `claude-haiku-4.5`, for example, keeps adaptive visible
thinking on `/v1/messages` but must omit reasoning-effort and thinking-budget
controls unless Copilot explicitly proves support.

### Catalog Guardrail

`resources/models/<provider>.json` is refreshable provider data. Model-db
updates may overwrite it at any time, so durable product fixes must not depend
on hand-edited catalog entries. For GitHub Copilot specifically, permanent
behavioral corrections belong in `_STATIC_EXACT_OVERRIDES_BY_ID` in
`core/providers/github_copilot_policy.py`, not in
`resources/models/github-copilot.json`.

Tests that verify permanent routing, reasoning, tool-call, or exact-model
behavior must use synthetic metadata or frozen raw-fixture inputs. Do not bind
behavioral tests to the refreshable bundled Copilot catalog.

Observed Copilot `/models` shape:

```json
{
  "object": "list",
  "data": [
    {
      "id": "gpt-4o",
      "name": "GPT-4o",
      "vendor": "Azure OpenAI",
      "version": "gpt-4o-2024-11-20",
      "supported_endpoints": ["/chat/completions"],
      "capabilities": {
        "family": "gpt-4o",
        "limits": {
          "max_context_window_tokens": 128000,
          "max_output_tokens": 4096
        },
        "supports": {
          "tool_calls": true,
          "vision": true
        }
      }
    }
  ]
}
```

`GitHubCopilotAdapter.normalize_catalog_entry()` reads
`capabilities.limits.max_context_window_tokens`,
`capabilities.limits.max_output_tokens`, and `capabilities.supports`. Reasoning
is supported when Copilot advertises `reasoning_effort` or thinking-budget
support. The normalizer must use the reported source values; for example,
`gpt-4o` may legitimately report `max_output_tokens: 4096`. The top-level
`capabilities` object is required structural shape, but nested `limits` and
`supports` sections are optional per model and fall back to empty mappings.
Individual numeric limit fields are also optional: missing or non-numeric
context limits fall back to `0`, and missing or non-numeric output limits fall
back to provider `max_tokens` or the hard default.

The normalizer stores only a sanitized runtime subset under
`metadata.github_copilot`: `vendor`, `family`, `version`, `supported_endpoints`,
`reasoning_efforts`, thinking budget bounds, `adaptive_thinking`,
`parallel_tool_calls`, `streaming`, `structured_outputs`, and `tool_calls` when
Copilot advertises them. Full raw `/models` entries, policy terms, credentials,
and other unsanitized provider data are not stored in model catalogs.

### AnthropicAdapter

**Wire protocol** — Anthropic Messages API, own format.

**Endpoint:** `POST /messages`

**Required headers:**
- Auth header from config (`x-api-key` for Anthropic, no Bearer prefix)
- `anthropic-version: 2023-06-01`
- `extra_headers` from config (merged after auth)

**Request format:**
```json
{
  "model": "<model_id>",
  "system": "You are helpful.",       // system messages extracted to top level
  "messages": [
    {"role": "user", "content": "..."}  // no system role in messages array
  ],
  "max_tokens": 4096,
  "temperature": 0.7
}
```

Key differences from OpenAI:
1. System messages extracted into top-level `system` field (not in messages array)
2. Content blocks instead of flat `content` strings
3. Auth uses `x-api-key` header (no `Bearer` prefix)
4. Required `anthropic-version` header

- Consecutive canonical `tool` messages become one user message containing multiple `tool_result` blocks.
- Canonical assistant `tool_calls` become `tool_use` content blocks.
- Provider tool definitions become Anthropic `tools` entries with `input_schema`.

**Thinking/reasoning parameters** (passed via `**kwargs`):
- `thinking.type`: `"disabled"` | `"enabled"` | `"adaptive"`
  - `"enabled"` deprecated on Opus 4.6/Sonnet 4.6, rejected on Opus 4.7
  - `"adaptive"` is the new recommended mode
- `thinking.budget_tokens`: int, token budget for thinking (when `type: "enabled"`)
- `output_config.effort`: `"low"` | `"medium"` | `"high"` | `"xhigh"` | `"max"`
- `thinking.display`: `"summarized"` | `"omitted"` — controls thinking visibility

Copilot's `/v1/messages` helper must always send top-level `max_tokens`.
Caller-provided `max_output_tokens` and `max_completion_tokens` aliases may be
accepted for convenience, but they are normalized to on-wire `max_tokens` and
are never forwarded as raw top-level keys.

Copilot Claude Messages responses may expose readable thinking text under either
`thinking` or `text` on a `thinking` content block. Streaming can likewise send
visible thinking through `text_delta` on a thinking block, which must normalize
to visible `reasoning_delta` while preserving the opaque thinking block and
signature in `reasoning_meta` for round-tripping.

**Response format:** content blocks array, each with a `type` field:
- `type: "text"` — regular text content
- `type: "thinking"` — thinking/reasoning content block
- `type: "tool_use"` — tool call

**Response normalization:** Concatenates text blocks into `content`, thinking blocks into `reasoning`, preserves supported opaque thinking/redacted-thinking content blocks under `reasoning_meta.content_blocks`, and maps `tool_use` blocks into canonical `tool_calls`. When resending current-turn metadata, these content blocks are emitted unchanged before text/tool-use blocks.

**Streaming:** `stream: true` in payload. SSE uses both `event:` and `data:` lines (unlike OpenAI's `data:` only). Stream ends on `event: message_stop` (detected via `"type": "message_stop"` in parsed data). The adapter tracks content block indexes internally and yields only normalized deltas: text blocks become `content_delta`, thinking deltas become `reasoning_delta`, supported thinking/redacted-thinking blocks become internal-only `reasoning_meta`, tool-use input fragments become `tool_call_delta`, and `message_delta.stop_reason` becomes `finish` with `reason: "stop" | "tool_calls"`.

**Error format** — Anthropic-specific:
```json
{
  "type": "error",
  "error": {
    "type": "authentication_error",
    "message": "..."
  }
}
```

Error classification parses this format for richer messages. Status codes:
- 401/403 → `ProviderAuthError` (fatal)
- 429 → `ProviderRateLimitError` (retryable)
- 529 → `ProviderError(retryable=True)` — Anthropic-specific "overloaded" status
- 502/503 → `ProviderError(retryable=True)`
- Other errors → `ProviderError(retryable=False)`

## Error Classification

Provider-specific API failures inherit from `ProviderError` (→ `VBotError` → `Exception`).
Network connectivity failures use `NetworkError`, which inherits directly from
`VBotError` so it remains retryable without being eligible for provider/model
fallback.

| Error class | `retryable` | When raised | Retried? |
|---|---|---|---|
| `ProviderError` | varies | Catch-all for unclassified HTTP errors | Only if `retryable=True` |
| `ProviderAuthError` | `False` | 401/403 | Never |
| `ProviderRateLimitError` | `True` | 429 | Yes |
| `ProviderTimeoutError` | `True` | Request timeout errors | Yes |
| `NetworkError` | `True` | Connection failures and dropped network reads | Yes |

Source: `core/providers/errors.py`, `core/utils/errors.py`.

Retry uses `retry_async()` from `core/utils/retry.py`: exponential backoff + jitter, max 3 retries. Only retries when `error.retryable is True`. Auth errors, validation errors, and `ProviderError(retryable=False)` fail immediately.

## Integration with Runtime

`Runtime` wires providers into the application lifecycle:

```python
runtime = Runtime(config)
runtime.start()

# Read-only registry access
config = runtime.providers.get("openai")      # → ProviderConfig
ids = runtime.providers.list_ids()              # → ["anthropic", "openai", "openrouter"]

# Adapter factory — resolves connection credentials centrally, instantiates adapter
adapter = runtime.get_adapter("openai", "openai:api-key")        # → OpenAICompatibleAdapter instance
adapter = runtime.get_adapter("anthropic", "anthropic:api-key")  # → AnthropicAdapter instance

# Model lookup convenience
model = runtime.get_model("openrouter", "anthropic/claude-sonnet-4")  # → Model
```

**`runtime.get_adapter(provider_id, connection_id)`** flow:
1. Looks up `ProviderConfig` from registry
2. Validates that `connection_id` has the same provider prefix and maps to a known provider-local connection ID
3. Resolves connection credentials through the central provider credential resolver — `api_key` credentials come from environment or data-dir `.env`; token-store backed `oauth` credentials come from `TokenStore`; missing credential → `ConfigError`
4. Selects adapter class: `provider_config.adapter` → `_ADAPTER_MAP` lookup — unknown → `ConfigError`
5. Instantiates adapter with `(provider_config, token_getter, connection.base_url, connection.auth)`; adapters use the provider base URL unless the connection overrides it. GitHub Copilot additionally receives a narrow model metadata lookup so its runtime policy can read `Model.metadata` for exact model IDs.
6. Returns wired `ProviderAdapter` instance

`ProviderCredentialResolver` supports both provider-level and connection-level calls:

```python
has_credentials(provider_id: str, connection_id: str | None = None) -> bool
get_credentials(provider_id: str, connection_id: str | None = None) -> str
```

When `connection_id` is supplied it must use the compositional `<provider_id>:<local_id>` form. For `api_key` connections, the credential is resolved from that specific connection's `AuthConfig.credential_key`. For `oauth` connections with an `oauth` block, or no `credential_key`, the credential resolver uses `TokenStore` and returns the stored access token. Unknown connection IDs raise `ConfigError`.

Protocol interface: `ProviderRegistryProtocol` in `core/runtime/interfaces.py`.

Source: `core/runtime/runtime.py`.

## Constraints & Gotchas

- **Adapter selection is config-driven.** The `adapter` field in `resources/providers/<name>.json` determines which class is instantiated. Adding a fully OpenAI-compatible provider requires only a JSON file. Adding a mostly compatible provider with runtime or discovery differences requires an OpenAI-compatible subclass and matching adapter-map entries. Adding a fundamentally different wire protocol requires a new adapter class and an entry in `_ADAPTER_MAP`.

- **Credential resolution for API keys happens at adapter creation.** The runtime asks the central provider credential resolver for the configured credential value when `get_adapter()` is called, then wraps it in `StaticTokenGetter`. Process environment currently has precedence over the data-dir `.env` fallback snapshot for `api_key` credentials. Token-store backed OAuth adapters receive `OAuthTokenGetter` and can refresh short-lived API tokens during requests. If the credential is empty or missing, `ConfigError`/`ProviderAuthError` is raised. Credentials are not stored on the `ProviderConfig`.

- **Token Store filenames are validated.** Provider IDs and local connection IDs used for OAuth token files may contain only ASCII letters, numbers, underscores, and hyphens, and must start with a letter or number. Invalid values are rejected before a token path is constructed.

- **`get_adapter()` requires a connection ID.** There is no runtime fallback to the first usable connection. The chat loop or RPC caller is responsible for selecting a connection before adapter creation.

- **Auth header construction differs per provider.** OpenAI and OpenRouter use `Authorization: Bearer <key>`. Anthropic uses `x-api-key: <key>` (no prefix). This is controlled by `AuthConfig.header` and `AuthConfig.prefix`.

- **Provider defaults have lower priority than caller kwargs.** Defaults are applied via `dict.setdefault` — any key the caller explicitly sets overrides the provider default. This means `temperature=0` passed by the caller wins over `defaults.temperature: 0.7`.

- **System message handling differs.** Anthropic extracts system-role messages from the messages array into a top-level `system` field. OpenAI-compatible providers keep system messages in the array. The adapter handles this translation.

- **No request/response types yet.** `send()` and `stream()` accept and return plain dicts. Phase 2 will define the canonical ChatMessage type. The adapter will translate between ChatMessage and the provider's wire format.

- **Streaming adapters hide provider wire formats.** Chat code consumes normalized deltas only and must not branch on OpenAI-compatible or Anthropic raw chunk shapes. Opaque metadata stays inside canonical `reasoning_meta` for provider round-tripping and must not be exposed publicly.

- **Streaming is best-effort per provider/model.** OpenAI-compatible providers vary in streamed reasoning fields, tool-call IDs, and finish reasons. Adapters should tolerate missing optional deltas, generate stable tool-call IDs when needed, ignore unknown fields unless needed for opaque metadata, and degrade gracefully instead of leaking raw provider chunks upward.

- **Streaming retry only covers connection establishment.** `retry_async` wraps the initial HTTP connection. Once the SSE stream is open, retry is not attempted — errors mid-stream propagate directly.

- **httpx.AsyncClient timeout is hardcoded at 60s.** Not configurable per-request in Phase 1.
