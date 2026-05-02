# Providers

Provider configuration, registry, and adapters. Translates vBot requests into provider-specific wire formats.

## Data Model

### AuthConfig

```python
@dataclass(frozen=True)
class AuthConfig:
    header: str       # HTTP header name for API key (e.g. "Authorization", "x-api-key")
    prefix: str       # Value prefix prepended to the key (e.g. "Bearer ", "" for Anthropic)
    env_key: str      # Environment variable name holding the API key
```

### ProviderConfig

```python
@dataclass(frozen=True)
class ProviderConfig:
    id: str                              # Unique provider identifier, used as registry key
    name: str                            # Human-readable name
    adapter: str                         # Adapter class selector: "openai_compatible" or "anthropic"
    base_url: str                        # Base URL for the provider API
    auth: AuthConfig                     # Authentication config
    defaults: dict[str, Any] | None      # Default request params (max_tokens, temperature)
    extra_headers: dict[str, str] | None # Provider-specific HTTP headers
    models_endpoint: str | None          # Path to models listing endpoint (future use)
```

Source: `resources/providers/<name>.json`. One file per provider, keyed by `id`.

**Adapter field** selects the class at runtime:
- `"openai_compatible"` → `OpenAICompatibleAdapter`
- `"anthropic"` → `AnthropicAdapter`
- Unknown value → `ConfigError` at adapter creation time

**Auth field** drives HTTP header construction. Each provider has its own `env_key` — the runtime reads `os.environ[provider_config.auth.env_key]` to resolve the API key. Missing key → `ConfigError`.

**defaults** are merged into the request payload with lower priority than caller-supplied kwargs. Applied via `dict.setdefault` so caller values always win.

**extra_headers** are merged into every request after auth headers. OpenRouter uses `HTTP-Referer` and `X-Title`; OpenAI sends none.

**models_endpoint** is reserved for future dynamic model refresh. Not used in Phase 1.

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

## Adapter Hierarchy

```
ProviderAdapter (ABC)          — core/providers/adapter.py
  ├── OpenAICompatibleAdapter  — core/providers/openai_compatible.py
  └── AnthropicAdapter         — core/providers/anthropic.py
```

### ProviderAdapter (ABC)

```python
class ProviderAdapter(ABC):
    @abstractmethod
    async def send(self, messages: list[dict], *, model_id: str, **kwargs) -> dict: ...
    @abstractmethod
    def stream(self, messages: list[dict], *, model_id: str, **kwargs) -> AsyncIterator[dict]: ...
```

- `send()` — non-streaming request, returns parsed response dict
- `stream()` — streaming request, yields parsed SSE chunk dicts
- `messages` is a list of dicts (not typed — Phase 2 defines the chat layer's types)
- `model_id` is the exact string sent to the provider API (no remapping)
- `**kwargs` carries provider-specific overrides (temperature, max_tokens, thinking config, etc.)

### OpenAICompatibleAdapter

**Wire protocol** — used by OpenAI, OpenRouter, Groq, Together, and any `/chat/completions` provider.

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

- `ProviderConfig.defaults` merged in via `setdefault` (lower priority)
- Caller `**kwargs` merged in last (highest priority)
- `extra_headers` added to request headers
- Auth: `Authorization: Bearer <api_key>` (configurable via `AuthConfig`)

**Streaming:** `stream: true` in payload. SSE lines prefixed with `data: `. Stream ends on `data: [DONE]`. Each `data:` line between start and `[DONE]` is parsed as JSON and yielded as a dict.

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
- Timeout/ConnectError → `ProviderTimeoutError` (retryable)

**Reasoning:** `reasoning_effort: "low" | "medium" | "high"` — single string parameter. This is an OpenAI-only concern; OpenRouter uses a different format.

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

**Thinking/reasoning parameters** (passed via `**kwargs`):
- `thinking.type`: `"disabled"` | `"enabled"` | `"adaptive"`
  - `"enabled"` deprecated on Opus 4.6/Sonnet 4.6, rejected on Opus 4.7
  - `"adaptive"` is the new recommended mode
- `thinking.budget_tokens`: int, token budget for thinking (when `type: "enabled"`)
- `output_config.effort`: `"low"` | `"medium"` | `"high"` | `"xhigh"` | `"max"`
- `thinking.display`: `"summarized"` | `"omitted"` — controls thinking visibility

**Response format:** content blocks array, each with a `type` field:
- `type: "text"` — regular text content
- `type: "thinking"` — thinking/reasoning content block
- `type: "tool_use"` — tool call

**Streaming:** `stream: true` in payload. SSE uses both `event:` and `data:` lines (unlike OpenAI's `data:` only). Stream ends on `event: message_stop` (detected via `"type": "message_stop"` in parsed data). `event:` lines are skipped; data is parsed from `data:` lines.

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

All provider errors inherit from `ProviderError` (→ `VBotError` → `Exception`).

| Error class | `retryable` | When raised | Retried? |
|---|---|---|---|
| `ProviderError` | varies | Catch-all for unclassified HTTP errors | Only if `retryable=True` |
| `ProviderAuthError` | `False` | 401/403 | Never |
| `ProviderRateLimitError` | `True` | 429 | Yes |
| `ProviderTimeoutError` | `True` | Connection/timeout errors | Yes |

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

# Adapter factory — resolves API key from env, instantiates adapter
adapter = runtime.get_adapter("openai")        # → OpenAICompatibleAdapter instance
adapter = runtime.get_adapter("anthropic")      # → AnthropicAdapter instance

# Model lookup convenience
model = runtime.get_model("openrouter", "anthropic/claude-sonnet-4")  # → Model
```

**`runtime.get_adapter(provider_id)`** flow:
1. Looks up `ProviderConfig` from registry
2. Resolves API key: `os.environ[provider_config.auth.env_key]` — missing key → `ConfigError`
3. Selects adapter class: `provider_config.adapter` → `_ADAPTER_MAP` lookup — unknown → `ConfigError`
4. Instantiates adapter with `(provider_config, api_key)`
5. Returns wired `ProviderAdapter` instance

Protocol interface: `ProviderRegistryProtocol` in `core/runtime/interfaces.py`.

Source: `core/runtime/runtime.py`.

## Constraints & Gotchas

- **Adapter selection is config-driven.** The `adapter` field in `resources/providers/<name>.json` determines which class is instantiated. Adding a new OpenAI-compatible provider requires only a JSON file — no subclassing. Adding a fundamentally different wire protocol requires a new adapter class and an entry in `_ADAPTER_MAP`.

- **API key resolution happens at adapter creation.** The runtime reads `os.environ[config.auth.env_key]` when `get_adapter()` is called. If the env var is empty or missing, `ConfigError` is raised. Keys are not stored on the `ProviderConfig`.

- **Auth header construction differs per provider.** OpenAI and OpenRouter use `Authorization: Bearer <key>`. Anthropic uses `x-api-key: <key>` (no prefix). This is controlled by `AuthConfig.header` and `AuthConfig.prefix`.

- **Provider defaults have lower priority than caller kwargs.** Defaults are applied via `dict.setdefault` — any key the caller explicitly sets overrides the provider default. This means `temperature=0` passed by the caller wins over `defaults.temperature: 0.7`.

- **System message handling differs.** Anthropic extracts system-role messages from the messages array into a top-level `system` field. OpenAI-compatible providers keep system messages in the array. The adapter handles this translation.

- **No request/response types yet.** `send()` and `stream()` accept and return plain dicts. Phase 2 defines typed request/response classes.

- **Streaming retry only covers connection establishment.** `retry_async` wraps the initial HTTP connection. Once the SSE stream is open, retry is not attempted — errors mid-stream propagate directly.

- **httpx.AsyncClient timeout is hardcoded at 60s.** Not configurable per-request in Phase 1.