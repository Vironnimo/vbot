# Providers

`core/providers/` owns Provider configuration, credential and Account resolution, adapter contracts, shared wire policy, and Provider-specific request/response translation.

## Overview

Providers translate canonical vBot requests and responses at the external-service boundary. Bundled Provider and Connection definitions live in `resources/providers/*.json`; user-defined OpenAI-compatible Providers live as secret-free records under `settings.json` `providers.custom` and are materialized through the same registry. Model facts and refreshable catalogs belong to the Models domain. Runtime resolves one exact Provider Connection, creates the configured outer Adapter, and injects credentials, Model lookup, connection mode, and optional debug capture. Chat and task domains consume normalized interfaces and must not know Provider wire fields.

A Provider can expose multiple Connection variants through one Adapter or route Models to different wire implementations inside a Provider-owned Adapter. Runtime selects only the outer Adapter from Provider config; per-Model protocol selection, request rules and Model admission are Provider policy, resolved per `(provider, connection, model)` into a wire profile from `resources/wire/<provider>.json` or a Custom Provider's Settings `wire` block (`providers/wire-profiles.md`).

## Defaults and concrete exceptions

New listed chat Models are selectable with inferred defaults, even when their exact version or wire details are unknown. Defaults follow the newest established generation of each family; concrete older behavior stays an explicit exception, not the baseline for future Models. Provider/Connection-scoped `family_defaults` reuse the ordinary ordered rule matchers (family, id prefix, metadata and Connection); they supply behavior below exact catalog facts and explicit corrective `rules` (`providers/wire-profiles.md` -> Family defaults). Do not reorder existing `rules`: their precedence is also the durable Custom Provider contract. Unknown protocol details, a free promotion or an id suffix such as `-free` are not denial evidence; explicit admission restrictions describe concrete known access or lifecycle problems, scoped to the affected Connection where appropriate.

## Terms

Core terms Provider, Model, and Reasoning live in `.vorch/GLOSSARY.md`; Model-DB terms live in `models.md`.

### Adapter

**Definition:** The code owner that translates between vBot's normalized contracts and one or more external Provider wire protocols. It owns request shaping, response/SSE normalization, wire capability, and Provider-specific policy.

**Not:** A Provider config, Connection, Account, or Model.

### Connection

**Definition:** A statically declared authentication and wire variant inside one Provider, addressed `provider:connection`. It selects auth type, optional base URL, optional discovery endpoint, wire mode, and automatic-catalog behavior.

**Not:** An Account or an HTTP connection. Accounts choose credentials without changing wire or catalog identity.

### Account

**Definition:** A named credential slot on one Connection, addressed `provider:connection[:account]`; the omitted Account resolves deterministically to the first usable slot (`default`, then sorted named Accounts).

`accounts.py::Account` validates a resolved slot and composes its public address; OAuth lifecycle and token operations take this value. `TokenStore` hides durable key encoding.

**Not:** A product user identity or a distinct Model catalog.

### Usable

**Definition:** A Connection that is both enabled and credentialed. `ProviderCredentialResolver.is_usable()` is the behavior gate; `has_credentials()` answers only the credential-management question.

**Not:** Merely configured or reachable. A keyless Connection is credentialed by definition but disabled by default until the user opts in.

### ConnectionRef

**Definition:** Frozen Provider Connection selection (`provider_id` plus `provider:connection[:account]`) used by Runtime adapter/token seams and their consumers. An omitted Account resolves later; it differs from `Account` (`accounts.py`).

**Not:** A `ConnectionConfig` (the resolved configuration record) or a credential-resolver query: resolver methods keep the optional-`connection_id` form where `None` means "any Connection of the Provider".

## Boundaries and invariants

- `ProviderRegistry` owns immutable Provider/Connection configuration assembled from bundled JSON plus the current data directory's normalized Custom Provider overlay, including exact `catalog_exclusions` for ids a Provider advertises but cannot serve (strict and tolerant loading, caching: `providers/connections.md` -> Configuration model). `ProviderCredentialResolver` owns credentials, Accounts, enabled overrides, and usability. Runtime owns wiring them into live Adapters.
- Connection identity determines wire/auth/catalog behavior; Account identity determines only which credential is used. Discovery and task-target expansion stay Connection-scoped and never multiply catalogs per Account.
- Request rules that differ per Provider, Connection, or Model - protocol, reasoning dialect and ladder, parameter rules, output-limit field, Tool-call ids, replay scope and fidelity, media types and limits, prompt caching, listing announced Tools, Model admission - belong in the Provider's wire profile over the protocol defaults in `core/providers/_wire_protocol_defaults.py` (layers and which codecs apply which rule: `providers/wire-profiles.md`). Adapter code keeps transport, Connection routing, catalog normalization, wire encoders, status classification, and per-conversation cache routing that no static value can express (`request_context_kwargs()`). Do not add per-Model request branches to an Adapter when its wire profile can express the rule.
- Native Reasoning replay scope (`replay.scope`, default `full_history`) and fidelity (`replay.fidelity`) are wire-profile declarations, narrowed only with live transport evidence; one Assistant turn never carries both reasoning classes (`providers/request-policy.md` -> Chain of Thought). Do not hide replay policy in provider metadata or Adapter fallback sets, add generic Model fields for one Provider's protocol quirk, or route Provider Models in Runtime/Chat.
- vBot never enables Provider strict mode (`core/providers/tool_schema.py`): every Responses wire emits `strict: false`, since OpenAI Responses may otherwise normalize an omitted flag into strict mode; other wires ship the unchanged canonical schema. Never make optional fields required, add nullable placeholders, or strip schema keywords for Provider compatibility; runtime argument validation stays authoritative.
- `core/providers/adapter.py::normalize_tool_call_ids()` owns target-wire Tool-call identifier constraints and copies on write; canonical Session ids never change, and only wires with an explicit verified profile apply it (`providers/request-policy.md` -> Tool-call terminal safety and correlation).
- Model facts stay honest and belong to `models.md`: unknown limits stay absent, read-side helpers never write fake facts back, and generated `resources/models/<provider>.json` catalogs are refresh artifacts, never a hand-edit surface for durable behavior.
- All secrets and tokens remain behind credential resolvers or token getters. Never log API keys, access/refresh tokens, authorization/user codes, Provider Account ids, or raw auth headers.

## Interfaces

- `ProviderAdapter.send()`, `stream()`, and `normalize_response()` are the Chat-facing translation boundary. Kernel callers stream every Model request: Chat through `request_runner.py`, everyone else through `core/chat/streaming.py::stream_model_response` (`chat.md`). `send()` is called only as the fallback after `ProviderStreamingUnsupportedError` before any output; a guard test (`tests/core/chat/test_streaming.py::test_model_requests_stream_outside_the_documented_fallbacks`) lists the two allowed callers, and a new one needs a written reason there. Adapters still implement `send()`.
- Completed responses and stream finishes carry one canonical terminal outcome. `normalize_tool_call_candidates()` preserves every recognizable Tool attempt in order and marks malformed names or arguments with canonical rejection metadata instead of dropping the Call or raising during Chat finalization (`providers/request-policy.md` -> Adapter ownership). Valid arguments leave intake as plain JSON values with recursively sorted object keys - the form the Session store persists - so the in-Run follow-up, a later Run, a fork and Compaction serialize the same call identically on every wire; no consumer depends on the Model's key order (`test_valid_arguments_become_recursively_key_sorted_json_values`).
- Raw SSE event names, response chunks, opaque auth state, and Provider payloads never escape the Adapter layer. Adapters treat the request messages they receive, including every nested value, as read-only: Chat passes its live request view without copying it, so wire-only changes (id rewrites, cache markers, dropped fields) go into copies or freshly built payload objects.
- `ProviderAdapter.estimate_request_input_tokens()` is the single Adapter-owned seam for local Context budgeting: output-limit clamps, Chat's measured pre-send guard, and automatic Compaction all ask it for the selected Model's actual wire footprint (estimator shape contract: `providers/request-policy.md` -> Request limits and context).
- `core/providers/http.py` is the public HTTP surface for Adapters, task clients and discovery: request preparation, clients, retries, errors and stream framing.
- Source routing: `providers.py` owns config/registry; `accounts.py` Account identity; `credentials.py` credential selection/usability; `adapter.py` the Adapter contract. OAuth layout: `providers/connections.md` -> Source and tests; wire/transport layout: `providers/request-policy.md` -> Source and tests. `ProviderRuntime` owns live Adapter/token construction, Context and catalog refresh behind the Runtime facade.

## Conventions and gotchas

- **Before changing or judging any Provider wire behavior, read `providers/compatibility-verification.md` and the Provider-specific reference.** New Models may inherit defaults without prior per-Model verification (Defaults and concrete exceptions; user decision, 2026-10-15); label this inference honestly. Controlled live evidence remains necessary to claim verified transport behavior or pin a restrictive compatibility exception; search snippets, accepted payloads, behavioral recall, serializer tests, generated catalogs, and third-party implementations are not transport proof by themselves.
- Adapter creation requires an exact compositional Connection id. It never silently falls back to another Connection; only an omitted Account within that Connection resolves to the first usable Account.
- Provider JSON uses `connections`; the obsolete single-provider `auth` shape is invalid. Provider ids cannot contain `:`; Connection ids cannot contain `:` or `--`.
- A Custom Provider cannot shadow a bundled Provider id or take a reserved name, since Model discovery names files after it (`.vorch/PROJECT.md` -> Object IDs; `provider.custom_save` -> `invalid_request`). Its normalized Settings record selects the allowlisted `openai_compatible` Adapter with one `default` Connection (auth and key naming: `providers/connections.md` -> Configuration model); Settings/RPC never contain the key value. Its optional `wire` block is its whole wire profile file and applies live on save (`providers/wire-profiles.md` -> Custom Provider wire block).
- Adapters rebuild authorization headers inside every retry attempt and never cache raw OAuth tokens outside `TokenGetter`; rejected-token recovery is the shared `OAuthRequestRecovery`, which tells token rejection from entitlement before one renewal (`providers/connections.md`).
- `NetworkError` is retryable and remains Provider-neutral. Chat exhausts same-Model recovery first, then its normalized network interruption may advance an explicitly configured Model fallback chain because a different route can reach another host. Only `ProviderStreamingUnsupportedError` permits Chat's streaming-to-nonstreaming fallback.
- Kernel-internal note messages (`role: "note"`) never reach adapters: Chat embeds persisted notes into requests as system reminders before wire translation. See `model-communication.md`.
- A Provider listing that contains proven-unusable ids uses `catalog_exclusions` in its static Provider config; discovery omits only those exact ids from the usable Model projection (logged per model at DEBUG, since it repeats on every refresh) and retains no raw response. Do not use this as a preference allow/deny list.

## References

Read these only when your task matches - not by default.

- Verifying or revising a Provider/Connection/Model's live compatibility, endpoint choice, Reasoning control/replay, Tool interaction, Usage, media, sampling, Context, output limit, or error behavior -> `providers/compatibility-verification.md`
- Changing a wire profile file or a Custom Provider's `wire` block, protocol or layer resolution, family defaults, rules and admission, learned wire facts, or the wire statuses `model.list`/`model.get` report -> `providers/wire-profiles.md`
- Changing Provider/Connection config, Accounts, credentials, enablement, OAuth/device flow, token storage/refresh, Connection RPCs, or local reachability -> `providers/connections.md`
- Changing Model endpoint discovery, catalog normalization, connection-scoped merge, supplementary/task feeds, refresh retry behavior, or local auto-refresh -> `providers/catalog-discovery.md`
- Changing Adapter request/response/SSE behavior, error/retry policy, reasoning, CoT replay, media support, output/context limits, token estimation, shared task HTTP plumbing, or the internal module layout of `core/providers/` -> `providers/request-policy.md`
- Changing live subscription limits, usage fetchers/parsers, caching, timeout/fail-open behavior, the automatic usage history in `provider-usage.db`, or the `provider.usage*` RPCs -> `providers/usage.md`
- Adding a Provider, Connection variant, Adapter selector or composed inner Adapter, discovery normalizer, or Provider-specific map -> `providers/add-a-provider.md`
- Changing native Anthropic Messages behavior -> `providers/anthropic.md`
- Changing GitHub Copilot auth, routing, policy, or catalog metadata -> `providers/github-copilot.md`
- Changing Kimi Coding Plan/Platform Connections, reasoning replay, media, or catalog behavior -> `providers/kimi.md`
- Changing MiniMax wire, reasoning, catalog, or usage parsing -> `providers/minimax.md`
- Changing Mistral request policy or catalog normalization -> `providers/mistral.md`
- Changing Nous API-key/Portal OAuth Connections, single-use refresh, catalog policy, or request limits -> `providers/nous.md`
- Changing StepFun Direct API/Step Plan Connections, routing, request limits, or catalog policy -> `providers/stepfun.md`
- Changing Ollama native chat, local/cloud Connections, enrichment, or context enforcement -> `providers/ollama.md`
- Changing LM Studio native discovery, lazy loading, or OpenAI-compatible Chat behavior -> `providers/lmstudio.md`
- Changing OpenAI Platform or ChatGPT subscription behavior -> `providers/openai.md`
- Changing OpenCode Go's per-Model Chat/Messages/Responses routing or Reasoning controls -> `providers/opencode-go.md`
- Changing OpenCode Zen Connections, four-wire routing, Gemini replay/media, catalog policy, or errors -> `providers/opencode-zen.md`
- Changing OpenRouter runtime, routing, prompt caching, catalog, reasoning, or task discovery -> `providers/openrouter.md`
- Changing xAI API-key/SuperGrok auth, Responses policy, reasoning replay, media, or catalog behavior -> `providers/xai.md`
- Adding a kernel-to-Model notification channel or authoring reminder content -> `model-communication.md`
