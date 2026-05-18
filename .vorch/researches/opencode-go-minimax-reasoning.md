## Research: opencode-go Minimax M2.7 Reasoning/Thinking

### Question
How does opencode-go (`https://opencode.ai/zen/go/v1`) handle reasoning/thinking for Minimax M2.7? Specifically: (1) does it support reasoning, (2) what's the request wire format, (3) what keys are used in streaming responses, (4) why does the models endpoint return bare entries?

### TL;DR
Minimax M2.7 uses **different endpoints and API protocols** depending on whether you're on the Zen (pay-per-use) or Go (subscription) plan. In **Zen**, it uses OpenAI-compatible chat completions with `reasoningEffort`; in **Go**, it uses the Anthropic Messages API with `thinking: { type: "enabled", budgetTokens }`. However, opencode's source code explicitly returns **no built-in reasoning variants** for minimax models — reasoning must be configured manually. The streaming response format follows the upstream API: `reasoning_content` for OpenAI-compatible, `thinking_delta` for Anthropic Messages.

---

### Findings

#### 1. Two Different Plans, Two Different Endpoints

MiniMax M2.7 is handled differently between Zen and Go:

| Plan | Model ID | Endpoint | AI SDK Package |
|---|---|---|---|
| **Zen** | `minimax-m2.7` | `https://opencode.ai/zen/v1/chat/completions` | `@ai-sdk/openai-compatible` |
| **Go** | `minimax-m2.7` | `https://opencode.ai/zen/go/v1/messages` | `@ai-sdk/anthropic` |

The Go plan routes Minimax through the **Anthropic Messages API**, not the OpenAI-compatible API. This is unique among Go models — most others use `chat/completions`.

Source: [OpenCode Go docs — Endpoints table](https://opencode.ai/docs/go/#endpoints)

#### 2. Does Minimax M2.7 Support Reasoning?

**No built-in variants.** The opencode source code in `packages/opencode/src/provider/transform.ts` explicitly excludes minimax from auto-generated reasoning variants:

```typescript
// From transform.ts — variants() function (~line 530):
if (
    id.includes("deepseek-chat") ||
    ...
    id.includes("minimax") ||    // <-- explicitly returns empty
    id.includes("glm") ||
    id.includes("kimi") ||
    ...
  )
    return {}  // no built-in variants
```

This means even if the upstream model supports reasoning (and `capabilities.reasoning` may be `true` in the model catalog), opencode generates **zero** pre-configured variant shortcuts (no `low`/`medium`/`high` presets).

**Reasoning can still be configured manually** by setting model options in your `opencode.json` config, but there are no convenience variants.

Source: [transform.ts in the opencode repo](https://raw.githubusercontent.com/anomalyco/opencode/dev/packages/opencode/src/provider/transform.ts)

#### 3. Request Wire Format for Enabling Reasoning

The wire format depends on which AI SDK package is used (i.e., which plan you're on):

**Go Plan (`@ai-sdk/anthropic`) — Anthropic Messages API format:**
```json
{
  "model": "minimax-m2.7",
  "messages": [...],
  "max_tokens": 4096,
  "thinking": {
    "type": "enabled",
    "budgetTokens": 16000
  },
  "stream": true
}
```

**Zen Plan (`@ai-sdk/openai-compatible`) — OpenAI Chat Completions format:**
```json
{
  "model": "minimax-m2.7",
  "messages": [...],
  "reasoning_effort": "medium",
  "stream": true
}
```

**Neither** plan uses the OpenRouter format (`reasoning: {effort: "..."}` + `include_reasoning: true`). That's specific to the `@openrouter/ai-sdk-provider` package.

Source: Anthropic API docs (extended thinking), opencode transform.ts for variant option shapes

#### 4. Streaming Response Format

The streaming response keys differ by protocol:

| Plan | Protocol | Streaming Key for Reasoning |
|---|---|---|
| Zen | OpenAI-compatible SSE | `reasoning_content` in delta objects |
| Go | Anthropic Messages SSE | `thinking_delta` events: `{"type":"thinking_delta","thinking":"..."}` |

**For OpenAI-compatible**: The reasoning content is typically returned in `delta.reasoning_content` alongside `delta.content`. OpenCode internally remaps this to `providerOptions.openaiCompatible.reasoning_content` using the `capabilities.interleaved.field` setting (set to `"reasoning_content"` by default for DeepSeek models; this field determines where reasoning is stored in the message structure).

**For Anthropic Messages**: Reasoning is streamed as `thinking_delta` events within `content_block_delta`:
```sse
event: content_block_start
data: {"type":"content_block_start","index":0,"content_block":{"type":"thinking","thinking":"","signature":""}}

event: content_block_delta
data: {"type":"content_block_delta","index":0,"delta":{"type":"thinking_delta","thinking":"I need to..."}}

event: content_block_delta
data: {"type":"content_block_delta","index":0,"delta":{"type":"signature_delta","signature":"EqQBCg..."}}
event: content_block_stop
```

Source: [Anthropic Messages streaming docs](https://docs.anthropic.com/en/api/messages-streaming), opencode transform.ts `normalizeMessages()` for interleaved field handling

#### 5. Model Catalog — Bare Entries

The `/zen/go/v1/models` and `/zen/v1/models` endpoints return bare entries like:
```json
{"id":"minimax-m2.7","object":"model","created":1779112778,"owned_by":"opencode"}
```

This is the **standard OpenAI `/v1/models` format** — it only returns `id`, `object`, `created`, and `owned_by`. No capabilities, pricing, limits, or reasoning metadata.

Full model capabilities (reasoning support, temperature support, modalities, interleaved fields, cost, context limits, variants) come from **models.dev** — the AI SDK's centralized model catalog. OpenCode fetches from models.dev internally when initializing providers. The code in `provider.ts` shows:
```typescript
// provider.ts — fromModelsDevModel():
capabilities: {
    temperature: model.temperature ?? false,
    reasoning: model.reasoning ?? false,   // <-- from models.dev
    ...
    interleaved: model.interleaved ?? false, // <-- determines reasoning_content field
}
```

Source: opencode `provider.ts` — `fromModelsDevModel()` function

#### 6. Other Model-Specific Parameters

The transform.ts also sets these defaults for minimax models:
- **temperature**: `1.0`
- **topP**: `0.95` for m2.7, m2.5, m2.1
- **topK**: `20` for m2.7, `40` for m2.5/m2.1

Source: opencode `transform.ts` — `temperature()`, `topP()`, `topK()` functions

---

### Risks & Caveats
- The Go plan's use of the Anthropic Messages API for Minimax is **unusual** — nearly all other Go models use OpenAI-compatible. This means the Minimax upstream must support the Anthropic protocol natively, or the opencode-go gateway translates between protocols.
- The lack of built-in reasoning variants means you must manually configure thinking/reasoning options. There's no single "right" configuration — you'll need to test what the upstream Minimax gateway accepts.
- The interleaved field for reasoning (`reasoning_content` vs `reasoning_details`) is model-specific and depends on what models.dev reports. This may change as models are updated.
- The Go plan is in **beta** (as of May 2026); endpoints and model configurations may change.

### Sources
- [OpenCode Go Docs — Endpoints](https://opencode.ai/docs/go/#endpoints) — Shows MiniMax M2.7 on Go uses `@ai-sdk/anthropic` at `/zen/go/v1/messages`
- [OpenCode Zen Docs — Endpoints](https://opencode.ai/docs/zen/#endpoints) — Shows MiniMax M2.7 on Zen uses `@ai-sdk/openai-compatible` at `/zen/v1/chat/completions`
- [opencode source — transform.ts](https://raw.githubusercontent.com/anomalyco/opencode/dev/packages/opencode/src/provider/transform.ts) — Variants function (no variants for minimax), temperature/topP/topK defaults, interleaved reasoning handling, message normalization
- [opencode source — provider.ts](https://raw.githubusercontent.com/anomalyco/opencode/dev/packages/opencode/src/provider/provider.ts) — Model capabilities schema, fromModelsDevModel, provider SDK loading
- [Anthropic Messages Streaming API docs](https://docs.anthropic.com/en/api/messages-streaming) — Extended thinking streaming format with `thinking_delta` events
- [OpenCode Models Docs](https://opencode.ai/docs/models/) — How model variants and reasoning options are configured in opencode.json
- [OpenCode Go /v1/models endpoint](https://opencode.ai/zen/go/v1/models) — Returns bare model entries in OpenAI format
