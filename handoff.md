# GitHub Copilot model/runtime handoff

## Why this file exists

GitHub Copilot runtime behavior varies by exact model and endpoint family. vBot
now has endpoint-aware Copilot routing, but live validation and some advanced
fallback behavior remain follow-up work. This file captures the practical context
for future Copilot model, endpoint, reasoning, and visible-thinking work.

## Current state

- `GitHubCopilotAdapter.normalize_catalog_entry()` reads Copilot's real
  `/models` schema and writes provider-specific capability facts into
  `resources/models/github-copilot.json`.
- The generated Copilot catalog stores a sanitized runtime metadata subset under
  `metadata.github_copilot`. This includes vendor/family/version, supported
  endpoints, advertised reasoning-effort values, thinking-budget bounds,
  adaptive thinking, tools, parallel tool calls, streaming, and structured-output
  support when Copilot advertises them.
- `ModelRegistry.load()` preserves optional model `metadata`, and `Runtime`
  wires `GitHubCopilotAdapter` with a narrow exact-model metadata lookup.
- `core/providers/github_copilot_policy.py` is the central Copilot runtime policy.
  Runtime routing is dynamic-first from `metadata.github_copilot`; static policy
  entries are fallback/override rules only.
- `GitHubCopilotAdapter.send()` and `.stream()` select among three endpoint
  families:
  - `/chat/completions` through the generic OpenAI-compatible fallback path.
  - `/responses` through `core/providers/github_copilot_responses.py`.
  - `/v1/messages` through `core/providers/github_copilot_messages.py`.
- Unknown Copilot models default to safe behavior: `/chat/completions`, no
  explicit reasoning/thinking controls, no tools, and no structured-output
  controls.
- `OpenAICompatibleAdapter` remains generic; Copilot-specific runtime behavior
  stays in the Copilot adapter, policy, and endpoint helper modules.

## Representative routing outcomes

Routing is based on preserved model metadata when available:

- Claude-like models prefer `/v1/messages` when Copilot advertises it.
  Representative examples: `claude-sonnet-4.6`, `claude-opus-4.7`,
  `claude-haiku-4.5`.
- OpenAI/GPT-like models prefer `/responses` when Copilot advertises it.
  Representative examples: `gpt-5-mini`, `gpt-5.4`, `gpt-5.5`.
- Gemini-like models are conservative. `gemini-3.1-pro-preview` stays on
  `/chat/completions` because the current metadata advertises only chat;
  `gemini-2.5-pro` without clear endpoint metadata is not forced onto
  `/responses`.
- Unknown model IDs fall back to `/chat/completions` with optional request
  features stripped.

## What was fixed

### Discovery / catalog refresh

The old bug was that Copilot discovery looked at the wrong fields and collapsed
many models to fallback values like:

- `context_window: 0`
- `max_output_tokens: 4096`
- `vision: false`
- `reasoning.supported: false`

Current behavior:

- top-level `capabilities` object is required
- nested `capabilities.limits` may be missing / `null` / malformed
- nested `capabilities.supports` may be missing / `null` / malformed
- missing/malformed `limits` and `supports` are treated as empty mappings
- missing context window falls back to `0`
- missing max output falls back to provider `max_tokens` or hard default
- sanitized `metadata.github_copilot` facts are preserved for runtime routing

This prevents one partial Copilot model entry from killing the whole refresh and
lets future refreshes update routing inputs when Copilot changes model metadata.

### Runtime safety for thinking/reasoning

The generic OpenAI-compatible adapter would otherwise map non-`none`
`thinking_effort` to `reasoning_effort` in the request payload. That is too
optimistic for Copilot.

Current behavior:

- The Copilot policy filters request kwargs before any endpoint helper builds the
  provider payload.
- Reasoning/thinking controls are sent only when the exact model's metadata or a
  static fallback/override allows them.
- Unsupported requested effort values are stripped.
- Thinking budgets and adaptive thinking are gated separately from OpenAI-style
  reasoning efforts.

## Important distinction: capability vs request support

Do **not** conflate these two things:

1. `reasoning.supported` in the local model catalog
2. support for a specific runtime request field such as `reasoning_effort`,
   `thinking`, `thinking_budget`, or `output_config`

A Copilot model can be reasoning-capable in the catalog and still reject a
specific runtime control field on a specific endpoint.

This distinction is the core lesson from the debugging and research done here.

## Known limitations

- `ws:/responses` is intentionally not implemented. The policy strips that
  advertised endpoint from runtime choices.
- There is no adaptive 400 retry fallback yet. For example, vBot does not retry a
  failed `/responses` request on `/chat/completions`, and it does not retry a
  rejected reasoning parameter without that parameter.
- Gemini behavior remains conservative until exact-model live evidence proves a
  richer endpoint or thinking-control path.
- This implementation was validated with mocked tests and catalog refresh data,
  not a full live Copilot request matrix.
- Visible thinking remains endpoint/model dependent. Helpers preserve and
  normalize known reasoning/thinking fields, but not every Copilot model exposes
  readable thinking.

## User-observed runtime behavior before endpoint-aware routing

These observations came before the current endpoint-aware implementation and are
kept as context for future live validation:

- `claude-haiku-4.5` worked through Copilot after stripping explicit
  `reasoning_effort`, but visible thinking was not shown.
- `gpt-5-mini` worked. There were long pauses around tool calls / before final
  output that felt like hidden reasoning, but visible thinking was not shown.
- `gpt-5.4` did not show visible thinking in the user's test and also did not
  feel obviously like hidden reasoning.

## Research conclusions worth keeping

### High-confidence conclusions

- Copilot runtime behavior is heterogeneous by model family and exact model.
- Some Copilot models require different endpoints.
- Some Copilot models reject `reasoning_effort` entirely.
- Some Copilot models may accept only a subset of reasoning-effort values.
- Anthropic models behind Copilot are not safely treatable as generic OpenAI chat
  models.
- Gemini models remain the next major compatibility area.

### Known public evidence

- `claude-haiku-4.5` rejecting `reasoning_effort` through Copilot:
  https://github.com/Kilo-Org/kilocode/issues/9070
- Copilot behavior varying across model families and endpoints:
  https://github.com/openclaw/openclaw/issues/74159
- Copilot clients branching by model capability / endpoint:
  https://raw.githubusercontent.com/CopilotC-Nvim/CopilotChat.nvim/main/lua/CopilotChat/config/providers.lua
- Capability-vs-parameter distinction discussed publicly:
  https://github.com/microsoft/vscode/issues/308078
- Claude/Copilot reasoning-effort restrictions by model/value:
  https://github.com/github/copilot-cli/issues/3080

## Design direction that should be preserved

Keep Copilot-specific runtime behavior centralized.

The intended path is:

- one Copilot policy lookup per model
- policy decides endpoint path
- policy decides optional parameter allow/deny behavior
- policy decides allowed reasoning-effort values and thinking controls
- endpoint helper modules translate only their own wire protocol
- unknown models stay conservative by default

Do **not** re-spread Copilot-specific logic into generic
`OpenAICompatibleAdapter` branches if this can be avoided.

## Recommended next steps

1. Run a live validation matrix for representative models:
   - `claude-haiku-4.5`
   - `claude-sonnet-4.6` / `claude-opus-4.7`
   - `gpt-5-mini`
   - `gpt-5.4`
   - `gpt-5.5`
   - `gemini-2.5-pro`
   - `gemini-3.1-pro-preview`
2. Capture raw streaming chunks for models that appear to think but do not show
   visible reasoning.
3. Add exact-model static overrides only for validated quirks that metadata alone
   cannot represent.
4. Consider safe adaptive runtime retries for known compatibility errors, for
   example:
   - rejected reasoning control → retry once without explicit reasoning control
   - unsupported endpoint for model → retry the next conservative endpoint

## Relevant files

- `core/providers/github_copilot.py`
- `core/providers/github_copilot_policy.py`
- `core/providers/github_copilot_responses.py`
- `core/providers/github_copilot_messages.py`
- `core/runtime/runtime.py`
- `core/models/models.py`
- `core/models/discovery.py`
- `tests/core/providers/test_github_copilot.py`
- `tests/core/providers/test_github_copilot_policy.py`
- `tests/core/providers/test_github_copilot_responses.py`
- `tests/core/providers/test_github_copilot_messages.py`
- `tests/core/runtime/test_runtime_providers.py`
- `resources/models/github-copilot.json`
- `.vorch/PROJECT.md`
- `.vorch/specs/providers.md`
- `.vorch/specs/models.md`
- `.vorch/specs/chat.md`

## Final note

The Copilot catalog is now both more truthful and more useful at runtime, but
catalog capabilities and request compatibility remain different layers. Preserve
that separation when extending Copilot behavior.
