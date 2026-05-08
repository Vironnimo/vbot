# HANDOFF — auth variants per provider/model

## Current state

- The credential-sourcing cleanup is already done:
  - vBot no longer loads data-dir `.env` values into `os.environ`
  - provider credential lookup is centralized in `ProviderCredentialResolver`
  - current precedence is: **process environment first, data-dir `.env` second**
- The current implementation still effectively assumes **one credential string per provider**.
- We now need to support **multiple auth methods for the same provider at the same time**.

## Confirmed product requirement

- Providers like **OpenAI** and **Anthropic** may support both:
  - **OAuth**
  - **API key**
- Both methods must be usable **concurrently**.
- Different agents must be able to use the **same provider + same model** through different auth methods at the same time.

Example:

- Agent A → OpenAI GPT-5.4 via OAuth
- Agent B → OpenAI GPT-5.4 via API key

## Core decision: model ID stays clean

Keep the **canonical model ID** unchanged.

Example:

- `openai/gpt-5.4` stays the model ID

Do **not** bake the auth method into the stored canonical model string.

That means:

- `openai/gpt-5.4` remains the canonical model identifier
- but it is **not enough by itself** as the full runtime target once multiple auth variants exist
- the real target is: **connection + model**

## Core decision: the new concept is "connection"

The new concept is called **connection**. A connection represents one specific way to reach a provider — its auth method, its credential, and optionally an overridden base URL.

### Connection ID format

Connection IDs are **compositional**, using `<provider>:<type>`:

- `openai:api-key`
- `openai:oauth`
- `anthropic:api-key`

This mirrors the existing `<provider>/<model>` convention and makes the provider-relationship visible in the ID itself.

The local `id` field in provider JSON (e.g. `"api-key"`) is a free-form slug and does not need to match the `type` field (e.g. `"api_key"`). The `id` identifies the connection within a provider, the `type` classifies the authentication method.

### Connection config in provider JSON

The current single `auth` field on `ProviderConfig` becomes a `connections` list. Each connection has its own `auth` config, an optional `base_url` override, and a `type` field.

Example `openai.json`:

```json
{
  "id": "openai",
  "name": "OpenAI",
  "adapter": "openai_compatible",
  "base_url": "https://api.openai.com/v1",
  "connections": [
    {
      "id": "oauth",
      "type": "oauth",
      "label": "OAuth",
      "auth": {
        "header": "Authorization",
        "prefix": "Bearer ",
        "credential_key": "OPENAI_OAUTH_TOKEN"
      }
    },
    {
      "id": "api-key",
      "type": "api_key",
      "label": "API Key",
      "auth": {
        "header": "Authorization",
        "prefix": "Bearer ",
        "credential_key": "OPENAI_API_KEY"
      }
    }
  ],
  "defaults": { "max_tokens": 4096, "temperature": 0.7 }
}
```

Design notes:

- The `id` field is the local part (e.g. `"api-key"`), not the full compositional ID. The full ID is composed as `<provider_id>:<connection.id>`.
- A connection may optionally provide a `base_url` that overrides the provider-level `base_url`. Useful for enterprise endpoints with different auth.
- The `type` field (`"api_key"` | `"oauth"`) is forward-looking: it allows the system to distinguish connection kinds without inspecting credential details. Unknown `type` values are rejected at config load time with `ConfigError` — same pattern as the `adapter` field. This catches typos early and keeps the set of valid types explicit.
- Connections of type `"oauth"` that don't yet have a token are treated as **not usable** — same filtering behavior as providers without credentials today.
- The order of connections in the array is the display order in `connection.list` and determines which connection is preferred when no explicit selection exists (OAuth connections are listed first unless the provider config says otherwise — matching the "OAuth preferred" default).

### Connection and adapter

The adapter type stays on the provider. A connection does **not** change which adapter is used — both `openai:api-key` and `openai:oauth` use `OpenAICompatibleAdapter`. A connection only influences:

1. Which credential value is resolved and passed to the adapter
2. Optionally, which `base_url` to use (overrides the provider default)

## Agent data model: model + connection + fallback

The `Agent` dataclass gains two new fields:

- `connection: str` — the connection ID the agent uses (e.g. `"openai:api-key"`)
- `fallback_connection: str` — the connection ID for the fallback model

Both default to `""` (empty string), matching the existing `model` / `fallback_model` pattern.

An agent's full runtime target is:

- `model` → what model to use (e.g. `"openai/gpt-5.4"`)
- `connection` → how to reach that model (e.g. `"openai:api-key"`)
- `fallback_model` + `fallback_connection` → same for fallback

`connection` and `model` are independent. A different model from the same provider can use the same connection. A different connection from the same provider can use the same model.

## UI decision

The UI may still behave like a single model picker, but the labels should disambiguate only when needed.

### Rule 1: only one usable connection

If only one usable connection exists for a given provider/model, **do not append any suffix** in the UI.

Show just:

- `openai/gpt-5.4`

Reason: no extra noise when there is no ambiguity.

### Rule 2: multiple usable connections

If multiple usable connections exist for the same provider/model, show disambiguated UI labels.

Display examples:

- `openai/gpt-5.4 (OAuth)`
- `openai/gpt-5.4 (API Key)`

The label comes from the connection's `label` field in the provider config, not from the connection ID.

Important:

- this suffix is for **display / selection UI only**
- it should **not** redefine the canonical stored model ID

### Rule 3: new agent with multiple available connections

When creating a new agent and selecting a model where multiple connections are available, the UI defaults to OAuth (the first usable connection if the provider config lists OAuth first).

## Storage decision

Even if the UI hides the suffix because only one connection exists, the selected connection should still be stored explicitly.

Reason:

- if an agent was using API key while it was the only available method
- and later OAuth is added
- the system must still know that this agent was using **API key**

Then the UI can later reveal that explicitly once there is ambiguity.

So yes: the intended behavior is exactly this:

- when a second connection gets added later
- the UI should then be able to show that an existing agent is using e.g. the API Key connection
- because that connection was already stored separately

## Default behavior decision

For **new ambiguous selections** where both OAuth and API key are available, the preferred default should be **OAuth**.

Reason:

- user expectation is that OAuth is often the better / cheaper path

But:

- do **not** silently reinterpret already explicit selections
- if an agent is already stored as using API key, adding OAuth later must **not** auto-switch it

## Edge case: model without connection

If an agent has a `model` set but `connection` is empty (e.g. manual data editing), the runtime should pick the first usable connection for that provider, preferring OAuth. This is a fallback for a case that should not occur in normal operation — the UI and RPC always set both fields together.

## Backend contracts

### New RPC method: `connection.list`

Returns all connections with their usability status. This is a new endpoint; `model.list` stays but its internal filter changes from "provider has credential" to "provider has at least one usable connection".

Example response:

```json
{
  "connections": [
    {
      "id": "openai:api-key",
      "provider_id": "openai",
      "type": "api_key",
      "label": "API Key",
      "usable": true
    },
    {
      "id": "openai:oauth",
      "provider_id": "openai",
      "type": "oauth",
      "label": "OAuth",
      "usable": false
    }
  ]
}
```

Visibility rule: connections for providers whose provider-level config is missing from the registry are excluded. Connections within an existing provider are always listed (with `usable` reflecting whether credentials exist).

### `model.list` stays but its filter changes

The model list endpoint keeps the same response shape. Internally, the filter changes: instead of checking whether a provider has **a** credential (current behavior), it checks whether a provider has **at least one usable connection**. The visible result is the same — models for providers without usable connections are hidden.

### Agent RPC

Agent create/update accept `connection` and `fallback_connection` as new optional string fields, alongside `model` and `fallback_model`.

### Updated `_provider_settings_item()` response

The existing `_provider_settings_item()` currently returns a single auth config. With connections, the response shape changes to include a `connections` array, consistent with the `connection.list` format:

```json
{
  "id": "openai",
  "name": "OpenAI",
  "connections": [
    {
      "id": "openai:api-key",
      "type": "api_key",
      "label": "API Key",
      "configured": true
    },
    {
      "id": "openai:oauth",
      "type": "oauth",
      "label": "OAuth",
      "configured": false
    }
  ]
}
```

`configured` mirrors `usable` from `connection.list` — `true` if the credential is present and non-empty, `false` otherwise. The `usable` term is kept for `connection.list` (runtime readiness); `configured` is kept for provider settings (admin-facing).

## Runtime: connection-aware credential resolution

`runtime.get_adapter()` changes from `get_adapter(provider_id)` to `get_adapter(provider_id, connection_id)`:

- `connection_id` is **required** — there is no fallback to "first usable connection" in the runtime path. The caller always knows which connection to use because it comes from the agent's `connection` field.
- If `connection_id` does not map to a known connection for the provider, raise `ConfigError`.
- If the connection's credential is missing or empty, raise `ConfigError` (same as today).

`ProviderCredentialResolver` gains per-connection methods:

- `get_credentials(provider_id, connection_id)` → credential string for a specific connection
- `has_credentials(provider_id, connection_id)` → bool for a specific connection

The existing `get_credentials(provider_id)` / `has_credentials(provider_id)` signatures change: they delegate to the first usable connection for the provider. These remain useful for `model.list` filtering and similar provider-level checks.

The chat loop's `_execute_run` resolves the connection from the agent's `connection` field. If `connection` is empty (edge case — should not happen in normal operation), it picks the first usable connection for the provider, preferring OAuth.

The provider for a request is determined from the **connection ID**, not by parsing the model string. `_split_agent_model(agent.model)` is still used to extract the `model_id` part, but the `provider_id` comes from `connection_id.split(":")[0]`.

## Migration / legacy handling

Existing agents store only `model` and `fallback_model` — no `connection` or `fallback_connection`.

There is no automated migration script needed. The only user is the developer. After deploying this change, the developer updates their agent JSON manually: add `connection` and `fallback_connection` fields matching the model's provider. The app reads the new format only — no backwards compatibility.

The three provider JSON files (`openai.json`, `anthropic.json`, `openrouter.json`) are updated together with the code: `auth` → `connections` list. No mixed-format operation.

## OAuth scope

OAuth is not being implemented yet. But the architecture must not block it later.

What **is** in scope now:

- `ConnectionConfig.type` field supports `"api_key"` and `"oauth"` as values
- OAuth connections without a token are treated as **not usable** (filtered like providers without credentials)
- The `<data_dir>/oauth/` directory is ensured by `ensure_directories()` (already listed in Phase 2 directories; verified it is created)
- `ProviderCredentialResolver` resolves credentials per connection — when OAuth is implemented, the resolver for an `oauth`-type connection will read from `<data_dir>/oauth/` instead of from env variables

What is **not** in scope now:

- OAuth token refresh logic
- OAuth authorization flow endpoints (authorize, callback)
- OAuth-specific UI
- Token storage format or rotation in `<data_dir>/oauth/`

The key principle: the connection concept is structurally complete. Adding OAuth later means adding a new credential resolution path, not restructuring the data model.

## What this means for planning

The implementation should cover:

1. **Data model / storage**
   - `ConnectionConfig` dataclass in `core/providers/providers.py` (alongside `AuthConfig`)
   - `type` field validation: only `"api_key"` and `"oauth"` are valid values; unknown values are rejected at config load time with `ConfigError` (same pattern as `adapter` field)
   - Multiple connections of the same `type` per provider are allowed at the config level — no uniqueness constraint on `type`; the only uniqueness constraint is on the local `id` field within a provider
   - `ProviderConfig.auth` → `ProviderConfig.connections: list[ConnectionConfig]`
   - `ProviderConfig` gains a `get_connection(connection_id)` convenience method
   - `Agent` dataclass gains `connection: str` and `fallback_connection: str`
   - Provider JSON files transitioned from single `auth` to `connections` list (all three updated together)

2. **Registry and credential resolution**
   - `ProviderRegistry` parses the new `connections` list
   - `ProviderCredentialResolver` gains per-connection resolution methods
   - Provider-level `has_credentials(provider_id)` / `get_credentials(provider_id)` delegate to the first usable connection

3. **Runtime adapter factory**
   - `runtime.get_adapter()` requires `connection_id` — no fallback
   - Chat loop resolves connection from agent's `connection` field
   - Connection can override `base_url` for the adapter
   - Provider comes from connection ID, not from parsing the model string

4. **RPC / backend contracts**
   - New `connection.list` RPC method
   - `_provider_settings_item()` response changes from single auth config to `connections` array (same shape: `id`, `type`, `label`, `configured`)
   - `model.list` internal filter changes to connection-level usability
   - Agent create/update accept `connection` and `fallback_connection`
   - Agent response includes `connection` and `fallback_connection`

5. **UI behavior**
   - Model picker joins `model.list` + `connection.list` data
   - Conditional disambiguation: suffix shown only when multiple usable connections exist for the same provider/model
   - Editing existing agents shows the stored connection
   - New agent defaults to OAuth when multiple connections are available

6. **Developer data migration**
   - Update agent JSON manually after deploying
   - Update provider JSONs from `auth` to `connections`

7. **Tests**
   - Connection config parsing
   - Per-connection credential resolution
   - `has_credentials(provider_id)` / `get_credentials(provider_id)` still work via first usable connection
   - Runtime adapter factory with `connection_id`
   - `get_adapter()` raises on missing or unknown `connection_id`
   - Chat loop resolves connection from agent
   - Provider from connection ID, model ID from model string
   - RPC `connection.list` response format and filtering
   - Agent CRUD with new connection fields
   - `model.list` filters by connection usability

## Constraints / non-goals

- Do **not** solve this by redefining stored model IDs as `openai/gpt-5.4:oauth` or `:apikey`
- Do **not** lose the current canonical model ID schema
- Build on the recent credential centralization work; do not go back to direct env reads everywhere
- Do **not** implement OAuth token management, refresh, or authorization flow in this iteration
- Do **not** add legacy compatibility code to the app; format changes are handled manually
- The connection concept should **not** block multiple connections of the same type per provider in the future, but this iteration only guarantees one `api_key` and one `oauth` connection per provider work correctly
- Multiple connections of the same type per provider are **allowed at the config level** — the only uniqueness constraint is on the local `id` field within a provider. The `type` field is **not** validated for uniqueness. This keeps the data model structurally ready for future use cases without adding validation that would later need removal.