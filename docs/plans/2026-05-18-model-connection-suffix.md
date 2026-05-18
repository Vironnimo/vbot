## Plan: Move connection selection from Agent field to Model-String suffix

**Goal:** Remove `connection` and `fallback_connection` from the Agent dataclass. Connection selection moves to an optional `::connection-local-id` suffix on `model` and `fallback_model` (e.g. `openrouter/poolside/laguna-xs.2:free::api-key`). Without suffix, auto-resolution via `_first_usable_connection_id` remains unchanged.

**Context:** `connection` on the Agent is a pure override — when empty, the chat loop resolves the first usable connection for the model's provider. The provider part (`openai` from `openai/gpt-4o`) is already in the model string, making `connection` a duplication at the dataclass level. Moving the connection suffix onto the model string simplifies the Agent schema and makes future Markdown-agent integration cleaner (no separate connection field needed). Using `::` as separator avoids collision with `:` in model IDs like `openrouter/poolside/laguna-xs.2:free`. Using `rpartition("::")` makes parsing unambiguous. The suffix carries only the local connection ID (`api-key`), not the compositional form (`openai:api-key`), since the provider is already in the model string.

**Requirements:**
- R1: Agent dataclass has no `connection` and `fallback_connection` fields
- R2: The chat loop can extract a connection from the model string `::suffix`
- R3: Without suffix, behavior is identical to current `_first_usable_connection_id`

**Scope:**
- In: Agent dataclass, AgentStore, chat-loop resolution, server RPC, WebUI agent form, all related tests
- Out: Provider config, adapter hierarchy, credential resolution, token store, model registry, connection.list RPC — none of these change

**Assumptions:**
- `::` does not and will not appear in provider model IDs. Connection local IDs are simple slugs (`api-key`, `oauth`, `enterprise`) that also won't contain `::`.
- Current agents have no suffix in their model strings, so no migration script is needed. Auto-resolution picks the same connection as before (single connection per provider today).
- The pinning behavior (always storing an explicit connection so it doesn't shift when a second connection is added later) is preserved by the UI: the dropdown always sets the suffix, not leaving it empty.

### Key Design Decision: Separator

`::` (double colon) as the connection suffix separator. Reasons:
- Single `:` is already used in model IDs (e.g. `openrouter/poolside/laguna-xs.2:free`)
- `rpartition("::")` splits cleanly: everything before the *last* `::` is the model string, everything after is the connection suffix
- Consistent visual metaphor with the existing `provider:connection-id` convention
- Connection local IDs are simple slugs that never contain `::`

Parsing algorithm:
```
model_with_suffix = "openrouter/poolside/laguna-xs.2:free::api-key"
1. before, sep, suffix = model_with_suffix.rpartition("::")
   → before="openrouter/poolside/laguna-xs.2:free", sep="::", suffix="api-key"
   if sep is empty → no suffix, whole string is model
2. provider, slash, model_id = before.partition("/")
   → provider="openrouter", model_id="poolside/laguna-xs.2:free"
3. Reconstruct full connection_id = f"{provider}:{suffix}" → "openrouter:api-key"
```

### Phases

#### Phase 1: Core — Agent dataclass, parsing, chat-loop resolution
- [ ] Remove `connection` and `fallback_connection` fields from `Agent` dataclass — read: [`.vorch/specs/agent.md`, `.vorch/specs/chat.md`], files: [`core/agents/agents.py`]
- [ ] Remove `connection`/`fallback_connection` from `AgentStore.create()` signature and validation — files: [`core/agents/agents.py`]
- [ ] Remove `connection`/`fallback_connection` from `AgentStore.update()` validation (remove from `string_fields` set and `allow_empty` check) — files: [`core/agents/agents.py`]
- [ ] Remove `connection`/`fallback_connection` from `_agent_from_dict()` — files: [`core/agents/agents.py`]
- [ ] Add `parse_model_with_connection()` function in `core/chat/chat.py`: takes a model string, returns `(provider_id, model_id, connection_suffix)`. Uses `rpartition("::")` then `partition("/")`. — read: [`.vorch/specs/chat.md`], files: [`core/chat/chat.py`]
- [ ] Add `parse_bare_model()` helper: strips `::suffix` from a model string, returns just the `<provider>/<model-id>` part (for display, context-window lookup, model registry access) — files: [`core/chat/chat.py`]
- [ ] Rewrite `_resolve_agent_connection()` to use `parse_model_with_connection()`: extract provider + suffix from `agent.model`, if suffix present reconstruct `f"{provider_id}:{suffix}"`, else fall back to `_first_usable_connection_id()` — files: [`core/chat/chat.py`]
- [ ] Rewrite `_resolve_fallback()` to use `parse_model_with_connection()` on `agent.fallback_model` instead of reading `agent.fallback_connection` — files: [`core/chat/chat.py`]
- [ ] Update `_resolve_context_window()` in `server/delegates.py` to strip `::suffix` before model registry lookup — files: [`server/delegates.py`]
- [ ] Add tests for `parse_model_with_connection()`: no suffix, suffix present, model ID containing `:`, empty model, model with `/` in model-id part — files: [`tests/core/chat/test_chat_loop.py`]
- [ ] Update existing `_resolve_agent_connection` and `_resolve_fallback` tests: remove `connection`/`fallback_connection` from StubAgent, pass connection suffix in model strings — files: [`tests/core/chat/test_chat_loop.py`]

#### Phase 2: Server RPC + Agent JSON contract
- [ ] Remove `connection` and `fallback_connection` from `_agent_changes()` public_fields set in `server/delegates.py` — files: [`server/delegates.py`]
- [ ] Remove `connection`/`fallback_connection` from `_validate_agent_field()` string validation branch — files: [`server/delegates.py`]
- [ ] Remove `connection`/`fallback_connection` from `_agent_response()` serialization — files: [`server/delegates.py`]
- [ ] Update `AgentStore` tests: remove `connection`/`fallback_connection` from test fixtures, assertions, and validation tests — files: [`tests/core/agents/test_agents.py`]
- [ ] Update RPC tests: remove `connection`/`fallback_connection` from create/update/list assertions — files: [`tests/server/test_rpc.py`]

#### Phase 3: Frontend — WebUI agent form
- [ ] Remove `MODEL_CONNECTION_VALUE_SEPARATOR` constant and `connection`/`fallback_connection` from `formValues` — files: [`webui/src/components/AgentsView.svelte`]
- [ ] Remove `createConnectionAgentFormValues()` — its `connection`/`fallback_connection` additions are no longer needed — files: [`webui/src/components/AgentsView.svelte`]
- [ ] Remove the two lines that set `result.payload.connection` and `result.payload.fallback_connection` in `saveAgent()` — files: [`webui/src/components/AgentsView.svelte`]
- [ ] Rewrite `selectModelOptions()` to build options with `::suffix` directly in the model value instead of `\u001f`-separated compound — files: [`webui/src/components/AgentsView.svelte`]
- [ ] Rewrite `modelSelectionValue()` to return `model + "::" + connectionLocalId` (empty suffix when no connection) instead of `model + "\u001f" + connection` — files: [`webui/src/components/AgentsView.svelte`]
- [ ] Rewrite `parseModelSelectionValue()` to use `rpartition("::")` instead of `\u001f` split — files: [`webui/src/components/AgentsView.svelte`]
- [ ] Simplify `updateModelSelection()`: instead of setting both `model` and `connection` fields, set only `model` (with `::suffix` baked in). Same for fallback. Remove the `connectionFieldName` parameter. — files: [`webui/src/components/AgentsView.svelte`]
- [ ] Remove `connection` and `fallback_connection` from `selectModelValue()` function — it now only needs the model string (which already contains `::suffix`) — files: [`webui/src/components/AgentsView.svelte`]
- [ ] Update `modelSelectValue` / `fallbackModelSelectValue` derived state: remove `connection`/`fallback_connection` parameters — files: [`webui/src/components/AgentsView.svelte`]
- [ ] Update `SearchableDropdown` `onValueChange` handlers: `updateModelSelection('model', ...)` → `updateModelSelection('model', selectedValue)` (no connection field name) — files: [`webui/src/components/AgentsView.svelte`]
- [ ] Update frontend tests: remove `connection`/`fallback_connection` from mock agent data, update assertions to check `model` field contains `::suffix` format — files: [`webui/src/components/__tests__/AgentsView.test.js`]
- [ ] Update `ChatView.test.js` mock agent data: remove `connection`/`fallback_connection` — files: [`webui/src/components/__tests__/ChatView.test.js`]

#### Phase 4: Spec updates
- [ ] Update `.vorch/specs/agent.md`: remove `connection`/`fallback_connection` from Agent data model, document `::suffix` convention on `model`/`fallback_model` — files: [`.vorch/specs/agent.md`]
- [ ] Update `.vorch/specs/chat.md`: document that model strings may carry `::connection-suffix`, update `_resolve_agent_connection` description — files: [`.vorch/specs/chat.md`]

**Done when:**
- `Agent` dataclass has no `connection` and no `fallback_connection` field
- Chat loop correctly resolves connection from `model`'s `::suffix` or falls back to `_first_usable_connection_id`
- Fallback resolution correctly reads `::suffix` from `fallback_model`
- `agent.create` and `agent.update` RPC endpoints accept `model` with `::suffix` and reject unknown fields including `connection`/`fallback_connection`
- WebUI agent form sends `model` with `::suffix` baked in, no separate `connection` field
- All existing tests pass (with `connection`/`fallback_connection` removed from fixtures and assertions)
- `_resolve_context_window` correctly strips `::suffix` before model registry lookup

**Risks / Assumptions:**
- **`::` in model IDs**: We assume `::` will never appear in any provider's model ID. This is safe — no known provider uses `::` in model IDs, and `::` has no standard meaning in any LLM API. If it ever did appear, `rpartition("::")` would incorrectly split it. The parser could validate that the suffix is a known connection ID for the provider, but this adds runtime coupling to the provider config; the safer bet is that `::` simply won't appear.
- **Existing agents without suffix**: Current agents have `model` without `::suffix`. When the `connection` field is removed, they fall back to `_first_usable_connection_id`, which picks the same connection (since each provider has only one today). If a second connection is later added to a provider, existing agents without suffix would auto-resolve to the first usable connection, which could differ from the previously-pinned one. The UI will always set the suffix when saving, so new saves are pinned.
- **Display of `::suffix` in model strings**: In the agent list, `model` like `openai/gpt-4o::api-key` will be shown as-is. The `modelOptionLabel` function already handles this: when one connection, it shows just `model.id`; with multiple, it shows `model.id (connection label)`. The actual stored `model` value contains `::suffix`, but the dropdown label is human-friendly. Detail views that show `displayValue(model)` will need to strip the suffix — this is handled by the fact that `displayValue` already shows the selected dropdown label, not the raw model string.

**New Dependencies:** None