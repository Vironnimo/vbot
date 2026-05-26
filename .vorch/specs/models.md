# Models

Model data classes and registry. A model is always a model **at a provider** — no canonical model concept, no remapping.

## Data Model

### ReasoningCapabilities

```python
@dataclass(frozen=True)
class ReasoningCapabilities:
    supported: bool  # Whether reasoning is available for this model at this provider
```

A boolean flag. Whether reasoning can be configured is a provider-specific fact. How reasoning is configured (effort levels, thinking mode, budget) is the adapter's job — not stored here.

### Capabilities

```python
@dataclass(frozen=True)
class Capabilities:
    vision: bool
    tools: bool
    json_mode: bool
    reasoning: ReasoningCapabilities
    input_modalities: tuple[str, ...] = ()
    output_modalities: tuple[str, ...] = ()
    supported_parameters: tuple[str, ...] = ()
    task_types: tuple[str, ...] = ()
```

Provider-specific truths. The same underlying model can have different capabilities depending on the provider. For example, Claude Sonnet 4 through OpenRouter may have a 128k context window without reasoning, while through Anthropic directly it has 200k with reasoning. Both are correct — they describe reality at that provider.

`input_modalities`, `output_modalities`, and `supported_parameters` preserve
sanitized facts from provider catalogs when available. `task_types` is a coarse
filtering projection derived from modalities when not provided explicitly. Known
task values include `chat`, `text_output`, `image_input`,
`image_understanding`, `file_input`, `file_understanding`, `audio_input`,
`speech_to_text`, `video_input`, `video_understanding`, `image_generation`,
`image_edit`, `audio_generation`, `text_to_speech`, and `video_generation`.
Missing modality data defaults to text-in/text-out so sparse OpenAI-compatible
catalogs remain usable as chat model catalogs.

### Model

```python
@dataclass(frozen=True)
class Model:
    model_id: str              # Exact string sent in API requests — no remapping
    name: str                  # Human-readable name (e.g. "Claude Sonnet 4")
    capabilities: Capabilities  # Provider-specific capability flags
    context_window: int         # Total context window in tokens
    max_output_tokens: int      # Maximum output tokens
    metadata: Mapping[str, Any] # Optional provider-specific runtime facts
```

- `model_id` is the exact string the API expects. `"anthropic/claude-sonnet-4"` at OpenRouter is sent as `"model": "anthropic/claude-sonnet-4"`. No rewriting, no overrides, no indirection.
- `context_window` and `max_output_tokens` are provider-specific. Not canonical values.
- `metadata` is optional provider-specific data needed at runtime. It must stay
  sanitized and small; do not store full raw provider catalog entries or
  credentials here.

### ModelRegistry

```python
class ModelRegistry:
    def __init__(self, models: dict[tuple[str, str], Model])
    @classmethod
    def load(cls, resources_dir: Path) -> ModelRegistry  # reads resources/models/*.json, caches
    def get(self, provider_id: str, model_id: str) -> Model  # raises KeyError if missing
    def list_for_provider(self, provider_id: str) -> list[Model]  # sorted by model_id, empty list if none
```

Class-level cache (`_cache: ClassVar[dict[Path, ModelRegistry]]`) keyed by resolved `resources_dir` path. Second call with the same path returns cached instance.

Index key: `(provider_id, model_id)` tuple. Lookup by provider ID + model ID.

Source: `core/models/models.py`. Dynamic refresh helpers live in
`core/models/discovery.py`.

## Model ID Convention

The model ID is the **exact string sent in the API request**. No remapping, no canonical IDs.

- At OpenRouter: `"anthropic/claude-sonnet-4"` → sent as `"model": "anthropic/claude-sonnet-4"`
- At Anthropic: `"claude-sonnet-4-20250219"` → sent as `"model": "claude-sonnet-4-20250219"`
- At OpenAI: `"gpt-5.2"` → sent as `"model": "gpt-5.2"`

**User-facing format:** `<provider>/<model-id-at-provider>` — e.g. `openrouter/anthropic/claude-sonnet-4`, `anthropic/claude-sonnet-4-20250219`, `openai/gpt-5.2`.

The `<model-id-at-provider>` part is what gets looked up in the registry and passed to the adapter. The `<provider>` part selects the provider config.

## Storage Format

Model discovery may write up to three file types beside each other under
`resources/models/`:

| File | Written by | Read by | Purpose |
|---|---|---|---|
| `<provider>.json` | `refresh_models()` | `ModelRegistry.load()` | App catalog (sanitized) |
| `<provider>.raw.json` | `refresh_models()` | nobody | Inspection / debugging |
| `<provider>.overrides.json` | human | `apply_overrides()` | Research-only non-discoverable corrections |

### Sanitized file

The app-facing catalog remains one JSON file per provider at
`resources/models/<provider>.json`:

```json
{
  "provider_id": "openrouter",
  "models": {
    "anthropic/claude-sonnet-4": {
      "name": "Claude Sonnet 4",
      "capabilities": {
        "vision": true,
        "tools": true,
        "json_mode": true,
        "input_modalities": ["text", "image"],
        "output_modalities": ["text"],
        "supported_parameters": ["tools", "response_format", "reasoning"],
        "task_types": ["chat", "text_output", "image_input", "image_understanding"],
        "reasoning": {
          "supported": true
        }
      },
      "context_window": 128000,
      "max_output_tokens": 64000,
      "metadata": {
        "github_copilot": {
          "vendor": "Anthropic",
          "family": "claude-sonnet-4.6",
          "supported_endpoints": ["/chat/completions", "/v1/messages"],
          "reasoning_efforts": ["low", "medium", "high"],
          "tool_calls": true,
          "streaming": true
        }
      }
    }
  }
}
```

- Top-level key `provider_id` links to a `ProviderConfig.id`
- Keys in `models` are the exact model IDs sent in API requests
- `capabilities` are provider-specific — not canonical claims
- `reasoning.supported` is a boolean: can this model reason through this provider, yes or no
- `input_modalities`, `output_modalities`, `supported_parameters`, and
  `task_types` are persisted under `capabilities` when normalized. The registry
  tolerates older/sparse catalogs by deriving missing task types and defaulting
  missing modalities to text-in/text-out.
- Generated files may include top-level `source` and `fetched_at` metadata.
  `ModelRegistry.load()` ignores those fields and reads only `provider_id` and
  `models`.
- Individual model entries may include optional `metadata`. `ModelRegistry.load()`
  preserves this on `Model.metadata` for runtime consumers and freezes nested
  mappings/lists so loaded model data remains immutable.

`refresh_models()` writes the sanitized file after provider-specific
normalization and optional overrides. The app, runtime, and UI use only this
sanitized file.

### Raw file

For providers with discovery, the same `refresh_models()` call also writes
`resources/models/<provider>.raw.json`:

```json
{
  "provider_id": "openrouter",
  "fetched_at": "2026-01-01T00:00:00+00:00",
  "raw_response": {
    "data": []
  }
}
```

- `raw_response` stores the full parsed provider HTTP response body
- Raw output is written before any `raw_filter` is applied, so it preserves the
  unfiltered provider response
- The app does not read raw files at runtime; they exist only for inspection,
  debugging, and future normalization work
- `ModelRegistry.load()` skips `*.raw.json` files the same way it skips
  `*.overrides.json`

Optional override files live beside generated model files as
`resources/models/<provider>.overrides.json`:

```json
{
  "provider_id": "openrouter",
  "models": {
    "anthropic/claude-sonnet-4": {
      "name": "Claude Sonnet 4"
    }
  }
}
```

Override fields replace fetched model fields at the top level. Nested objects are
replaced wholesale rather than deep-merged. Override-only models are included in
the generated output and must provide the full `Model` shape.
`ModelRegistry.load()` skips `*.overrides.json` files so overrides are never
parsed as model catalogs.

Overrides are intentionally narrow:

- Use them only for durable facts the provider APIs do not expose and that were
  verified through external research.
- Do not use them to patch fields that can be derived from `/models`, other
  provider catalog endpoints, or probe inference requests. Those facts belong in
  adapter `normalize_catalog_entry()` implementations or in adapter runtime
  request/response behavior.
- Do not hand-edit `resources/models/<provider>.json` for lasting fixes.
  `refresh_models()` can rewrite generated catalogs at any time.
- Override-only models are exceptional and only make sense when the provider
  cannot disclose that model through discovery and the full model shape was
  verified externally.

Current provider files include `openai.json`, `openrouter.json`,
`anthropic.json`, `github-copilot.json`, `mistral.json`, and
`opencode-go.json`. Generated catalogs may be marked
with top-level `"source": "discovery"`; bundled/static catalogs may use other
source labels. Model refresh can replace generated provider catalogs.

## Capabilities Structure

```
Capabilities
├── vision: bool                       # Can the model process images in chat?
├── tools: bool                        # Can the model use tool calls?
├── json_mode: bool                    # Does the model support JSON output mode?
├── reasoning: ReasoningCapabilities
│   └── supported: bool                # Can the model perform reasoning?
├── input_modalities: tuple[str, ...]  # Provider-reported accepted inputs
├── output_modalities: tuple[str, ...] # Provider-reported outputs
├── supported_parameters: tuple[str, ...]
│                                      # Sanitized provider request controls
└── task_types: tuple[str, ...]        # Coarse derived filters
```

All are provider-specific. A model through OpenRouter may have `reasoning.supported: true` while the same underlying model through a different provider might not support reasoning in the same way, or might have it disabled.

`task_types` is for filtering and routing affordances, not for provider request
shaping. Adapter runtime behavior must still decide the exact wire parameters.
Accessors should not hide user-configured local models merely because optional
capability facts such as `tools` or large `context_window` are missing or
conservative; local OpenAI-compatible catalogs can be sparse or user-tuned.

**`reasoning.supported` is a boolean only.** It does not store effort levels, budget, or thinking mode configuration. Those are the adapter's responsibility — each provider has a different wire protocol for reasoning:
- Anthropic: `thinking.type`, `thinking.budget_tokens`, `output_config.effort`, `thinking.display`
- OpenAI: `reasoning_effort` (single string)
- OpenRouter: `reasoning` (object) + `include_reasoning` (boolean)

The adapter translates vBot's internal reasoning configuration into the provider's format. When a provider/model exposes only a subset of vBot's effort levels, the adapter maps the requested `thinking_effort` to the nearest supported level instead of requiring an exact match.

For GitHub Copilot, `reasoning.supported` still means only that Copilot advertises
some reasoning/thinking capability for that model. Runtime request compatibility
is decided by `GitHubCopilotAdapter` through `metadata.github_copilot` and the
central Copilot policy, because a model can be reasoning-capable but reject a
specific control field such as OpenAI-style `reasoning_effort` on a specific
endpoint.

## Integration with Runtime

`Runtime` wires models into the application lifecycle:

```python
runtime = Runtime(config)
runtime.start()

# Direct registry access
models = runtime.models                          # → ModelRegistry
model = runtime.models.get("openrouter", "anthropic/claude-sonnet-4")  # → Model
openrouter_models = runtime.models.list_for_provider("openrouter")      # → list[Model]

# Convenience method (delegates to ModelRegistry.get)
model = runtime.get_model("openrouter", "anthropic/claude-sonnet-4")   # → Model
```

Both `runtime.models` and `runtime.get_model()` raise `RuntimeError` if called before `runtime.start()`. `get()` raises `KeyError` if the provider/model combination is not found.

Protocol interface: `ModelRegistryProtocol` in `core/runtime/interfaces.py`.

## Constraints & Gotchas

- **Model data is provider-specific.** There are no canonical models — no `Claude Sonnet 4` that exists independently of providers. The same underlying AI model has different IDs, different capabilities, different context windows at different providers.

- **No model ID remapping.** The ID in the JSON file is the exact string sent in the API request. vBot never transforms it — what's in the file is what goes on the wire.

- **Capabilities are per-provider truths.** If OpenRouter exposes Claude Sonnet 4 with `reasoning.supported: true` and `context_window: 128000`, that's what goes in the OpenRouter models file. If Anthropic exposes the same model with `reasoning.supported: true` and `context_window: 200000`, that goes in the Anthropic models file. Both are correct for their provider.

- **Registry is keyed by `(provider_id, model_id)` tuple.** To look up a model you must know both the provider and the model ID at that provider. There is no cross-provider search.

- **Dynamic model refresh writes the same JSON format the registry already
  reads.** `core/models/discovery.py` fetches provider catalogs, normalizes them,
  applies optional overrides, writes `resources/models/<provider>.json`, and
  invalidates the registry cache. The registry remains the read path and does not
  know about provider APIs.

- **Provider discovery schemas differ and are owned by provider adapters.**
  `core/models/discovery.py` dispatches by provider `adapter` string to
  `adapter_class.normalize_catalog_entry(raw, defaults)`. OpenRouter catalog
  normalization lives in `OpenRouterAdapter`; GitHub Copilot catalog
  normalization lives in `GitHubCopilotAdapter`. Discovery should not branch on
  provider IDs or contain provider-specific normalizer functions.
- **Modalities and task filters come from adapter normalization.** If a provider
  exposes input/output modalities or supported request parameters, normalize
  those facts into `Capabilities` instead of leaving them only in raw catalogs.
  When a provider exposes only model IDs, preserve a usable text chat default
  rather than treating every missing fact as a hard negative.
- **Overrides are not a second discovery layer.** If a value can be obtained by
  inspecting provider catalog responses or by sending a probe request to the
  real inference endpoint, put that knowledge in the adapter family rather than
  in `*.overrides.json`.
- **GitHub Copilot `/models` entries store capability facts under
  `capabilities`.** Context and output limits come from
  `capabilities.limits.max_context_window_tokens` and
  `capabilities.limits.max_output_tokens`. Vision/tools/structured output and
  reasoning indicators come from `capabilities.supports`. The reported value is
  authoritative even when it equals an old fallback value, such as `gpt-4o`
  reporting `max_output_tokens: 4096`. The top-level `capabilities` object is
  required, but nested `limits` and `supports` sections may be missing or
  malformed and are treated as empty mappings. Individual numeric limit fields
  are also optional per model; missing context limits fall back to `0`, and
  missing output limits fall back to provider `max_tokens` or the hard default
  so one partial Copilot entry does not fail the whole refresh.
- **GitHub Copilot capability facts are not the same as runtime control
  support.** `reasoning.supported` in the catalog means the model is advertised
  as reasoning-capable through Copilot; it does not by itself authorize the
  adapter to send OpenAI-style `reasoning_effort`. Runtime request shaping is a
  provider-adapter concern and is policy-driven per Copilot model.
- **GitHub Copilot stores sanitized runtime metadata.**
  `GitHubCopilotAdapter.normalize_catalog_entry()` preserves only the subset the
  runtime policy needs under `metadata.github_copilot`: vendor/family/version,
  supported endpoints, advertised reasoning effort values, thinking budget
  bounds, adaptive thinking, tools, parallel tool calls, streaming, and
  structured-output support. Full raw Copilot `/models` entries are not stored.
- **GitHub Copilot routing is dynamic-first.** Runtime passes Copilot adapters a
  lookup for exact-model `Model.metadata`. When `metadata.github_copilot` exists,
  endpoint selection and optional request-feature gating are driven by those
  catalog facts. Static policy entries are fallback/override rules only.

- **Immutability.** `Model`, `Capabilities`, and `ReasoningCapabilities` are frozen dataclasses. Once loaded, model data cannot be modified.
