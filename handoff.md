# GitHub Copilot model/runtime handoff

## Why this file exists

GitHub Copilot model support is only partially mapped in vBot right now. Model
catalog refresh is much better than before, but runtime behavior is still
incomplete and inconsistent across model families. This file is the handoff for
future work on Copilot models, endpoints, reasoning, and visible thinking.

## Current state

- `GitHubCopilotAdapter.normalize_catalog_entry()` now reads Copilot's real
  `/models` schema instead of falling back to generic OpenAI-compatible defaults.
- The local Copilot model catalog has been refreshed and is intentionally being
  committed in this branch at the user's request.
- `GitHubCopilotAdapter` now contains a conservative per-model runtime policy
  layer for request shaping.
- Unknown Copilot models default to **safe behavior**: do not send explicit
  OpenAI-style `reasoning_effort`.
- `gpt-5-mini` is currently the only explicitly allowlisted model for sending
  OpenAI-style `reasoning_effort`.
- The policy structure already has room for future per-model endpoint selection,
  parameter allow/deny lists, and allowed reasoning-effort values, but endpoint
  switching is **not implemented yet**.

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

This prevents one partial Copilot model entry from killing the whole refresh.

### Runtime safety for thinking/reasoning

The generic OpenAI-compatible adapter would otherwise map non-`none`
`thinking_effort` to `reasoning_effort` in the request payload. That is too
optimistic for Copilot.

Current behavior:

- `GitHubCopilotAdapter._build_payload()` filters request kwargs through a
  Copilot model policy before delegating to the generic OpenAI-compatible
  payload builder.
- For unknown Copilot models, explicit `thinking_effort` is stripped.
- For `gpt-5-mini`, OpenAI-style `reasoning_effort` is currently allowed.

This is intentionally conservative until more models are validated.

## User-observed runtime behavior so far

These are observations from the current conversation and should be treated as
working notes, not guaranteed protocol truths:

- `claude-haiku-4.5` now works through Copilot after stripping explicit
  `reasoning_effort`, but visible thinking is not shown.
- `gpt-5-mini` works. There are long pauses around tool calls / before final
  output that feel like hidden reasoning, but visible thinking is not shown.
- `gpt-5.4` did not show visible thinking in the user's test and also did not
  feel obviously like hidden reasoning.
- Practical user conclusion right now: **GitHub Copilot models are still not in
  a usable state for normal work**.

## Important distinction: capability vs request support

Do **not** conflate these two things:

1. `reasoning.supported` in the local model catalog
2. support for a specific request field such as `reasoning_effort`

A Copilot model can be reasoning-capable in the catalog and still reject an
OpenAI-style runtime control field on `/chat/completions`.

This distinction is the core lesson from the debugging and research done here.

## Research conclusions worth keeping

### High-confidence conclusions

- Copilot runtime behavior is **heterogeneous by model family and exact model**.
- Some Copilot models require different endpoints.
- Some Copilot models reject `reasoning_effort` entirely.
- Some Copilot models may accept only a subset of reasoning-effort values.
- Anthropic models behind Copilot are not safely treatable as generic OpenAI
  chat models.
- Gemini models are likely the next major compatibility problem area.

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

## What is still unknown

- Which Copilot models should use `/chat/completions` vs `/responses` vs any
  Anthropic-native path.
- Whether some Claude models support a different reasoning/thinking control when
  accessed through Copilot.
- Whether GPT-5 family models are actually reasoning through Copilot and we
  simply fail to render it, or whether they are running without exposed thinking.
- Whether Gemini models need dedicated request fields for thinking budgets or
  only endpoint/schema differences.
- Which Copilot headers are mandatory for which endpoint/model combinations.

## Design direction that should be preserved

Keep Copilot-specific runtime behavior **centralized in one place**.

The current intended evolution path is:

- one Copilot policy lookup per model
- policy decides endpoint path
- policy decides optional parameter allow/deny behavior
- policy decides allowed reasoning-effort values
- unknown models stay conservative by default

Do **not** re-spread Copilot-specific logic into generic
`OpenAICompatibleAdapter` branches if this can be avoided.

## Recommended next steps

1. Add a richer per-model Copilot runtime compatibility map:
   - endpoint
   - allowed params
   - denied params
   - allowed reasoning values
   - evidence/source/confidence
2. Run a live validation matrix for representative models:
   - `claude-haiku-4.5`
   - `claude-sonnet-4.5` / `claude-opus-4.7`
   - `gpt-5-mini`
   - `gpt-5.4`
   - `gemini-2.5-pro`
   - `gemini-3.1-pro-preview`
3. Capture raw streaming chunks for models that appear to think but do not show
   visible reasoning.
4. Decide whether model-catalog schema needs a second runtime-facing field set,
   separate from `reasoning.supported`.
5. Consider safe adaptive runtime retries for known compatibility errors, for
   example:
   - `invalid_reasoning_effort` → retry once without explicit reasoning control
   - `unsupported_api_for_model` on `/responses` → retry `/chat/completions`

## Relevant files

- `core/providers/github_copilot.py`
- `tests/core/providers/test_github_copilot.py`
- `resources/models/github-copilot.json`
- `resources/models/openrouter.json` (refreshed in the same working session and
  being committed because the user explicitly requested all current catalog
  changes be included)
- `.vorch/PROJECT.md`
- `.vorch/specs/providers.md`
- `.vorch/specs/models.md`

## Final note

The local catalog is now much more truthful than before, but runtime support is
still incomplete. The most important thing to remember is that **Copilot model
capability facts and Copilot request compatibility are different layers**.
