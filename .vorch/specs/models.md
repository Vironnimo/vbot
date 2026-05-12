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
```

Provider-specific truths. The same underlying model can have different capabilities depending on the provider. For example, Claude Sonnet 4 through OpenRouter may have a 128k context window without reasoning, while through Anthropic directly it has 200k with reasoning. Both are correct — they describe reality at that provider.

### Model

```python
@dataclass(frozen=True)
class Model:
    model_id: str              # Exact string sent in API requests — no remapping
    name: str                  # Human-readable name (e.g. "Claude Sonnet 4")
    capabilities: Capabilities  # Provider-specific capability flags
    context_window: int         # Total context window in tokens
    max_output_tokens: int      # Maximum output tokens
```

- `model_id` is the exact string the API expects. `"anthropic/claude-sonnet-4"` at OpenRouter is sent as `"model": "anthropic/claude-sonnet-4"`. No rewriting, no overrides, no indirection.
- `context_window` and `max_output_tokens` are provider-specific. Not canonical values.

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

One JSON file per provider at `resources/models/<provider>.json`:

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
        "reasoning": {
          "supported": true
        }
      },
      "context_window": 128000,
      "max_output_tokens": 64000
    }
  }
}
```

- Top-level key `provider_id` links to a `ProviderConfig.id`
- Keys in `models` are the exact model IDs sent in API requests
- `capabilities` are provider-specific — not canonical claims
- `reasoning.supported` is a boolean: can this model reason through this provider, yes or no
- Generated files may include top-level `source` and `fetched_at` metadata.
  `ModelRegistry.load()` ignores those fields and reads only `provider_id` and
  `models`.

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

Current provider files include `openai.json`, `openrouter.json`,
`anthropic.json`, and `github-copilot.json`. The bundled GitHub Copilot catalog
is marked with top-level `"source": "bundled"` so model refresh can replace it.

## Capabilities Structure

```
Capabilities
├── vision: bool          # Can the model process images?
├── tools: bool           # Can the model use tool calls?
├── json_mode: bool        # Does the model support JSON output mode?
└── reasoning: ReasoningCapabilities
    └── supported: bool   # Can the model perform reasoning?
```

All are provider-specific. A model through OpenRouter may have `reasoning.supported: true` while the same underlying model through a different provider might not support reasoning in the same way, or might have it disabled.

**`reasoning.supported` is a boolean only.** It does not store effort levels, budget, or thinking mode configuration. Those are the adapter's responsibility — each provider has a different wire protocol for reasoning:
- Anthropic: `thinking.type`, `thinking.budget_tokens`, `output_config.effort`, `thinking.display`
- OpenAI: `reasoning_effort` (single string)
- OpenRouter: `reasoning` (object) + `include_reasoning` (boolean)

The adapter translates vBot's internal reasoning configuration into the provider's format.

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
  provider-adapter concern and is currently policy-driven per Copilot model.

- **Immutability.** `Model`, `Capabilities`, and `ReasoningCapabilities` are frozen dataclasses. Once loaded, model data cannot be modified.
