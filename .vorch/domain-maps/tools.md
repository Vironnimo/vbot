# Tools

Tool metadata registry, allowlist filtering, provider definitions, context-aware execution, stable result envelopes, display metadata, and async dispatch.

## Overview

`core/tools/` owns the registry of callable tools available to the agentic loop. It exposes provider/prompt definitions, filters tools by Agent allowlists, dispatches calls with `ToolContext`, and turns expected tool failures into stable result envelopes. Concrete built-in tool behavior lives in child maps under `.vorch/domain-maps/tools/`.

## Terms

Domain-specific vocabulary for tools. The core Tool term lives in `.vorch/GLOSSARY.md`.

### Readiness
**Definition:** A per-tool, cheap, I/O-free predicate (`Tool.ready`, a zero-arg `Callable[[], bool] | None`) evaluated at every prompt/tool-definition build; filter order is registered → allowed → ready. A not-ready tool vanishes from the System Prompt, the provider tool definitions, and the pickers but stays registered (its persisted permissions survive); a direct dispatch returns a clean `tool_not_ready` envelope instead of running the handler. A predicate that raises counts as not-ready.
**Not:** A permission — an allowlist answers "may this agent use it", readiness answers "can it run right now" (e.g. is the extension's token set). Not a stored extension state either: the Extensions tab's waiting status is a derived `ready_state`, not a third switch.

### Session Grant
**Definition:** An ephemeral capability derived from the current persisted Session state and carried through one request build and dispatch. A grant makes its matching Session-scoped Tool model-visible and may override the ordinary Agent allowlist, while an optional Run restriction can still deny the call at dispatch.
**Not:** Agent or Project configuration, a catalog choice, or permission to address another Session.

## Data Model

- `Tool`: `name`, `description`, `parameters`, `handler`, `internal`, `session_scoped`, `display`, `ready` (an optional zero-arg readiness predicate, `Callable[[], bool] | None`; `None` = always ready), `readiness_hint` (`str | None`, default `None` — English text explaining the readiness precondition, surfaced by `tool.list`; server-delivered content like the description, never frontend i18n), and `extension` (`str | None`, default `None` — the name of the owning extension, set at extension-tool apply time; `None` for a built-in). A Session-scoped Tool is hidden unless its name is present in the current Session grants.
- `ToolDisplay`: per-invocation presentation metadata. It builds `{ summary, hidden_argument_keys }` for `tool_call_started` events without adding provider-visible parameters.
- `ToolContext`: `agent_id`, `session_id`, `run_id`, `tool_call_id`, `tool_name`, `tool_call_index`, `workspace`, `cwd`, `project_id`, `app_root`, `data_root`, `nesting_depth`, `allowed_skills`, `session_tool_grants`, plus emit/cancel/note/skill hooks and per-call cancel hooks (`cancel_registration_hook`, `cancel_check_hook`).
  - `cwd` (`Path | None`) is the working directory relative file/shell Tools use: the selected working Project repository for a Rooted Identity Agent or Project Config Agent, otherwise Workspace. `ToolContext.effective_cwd` falls back to Workspace; Memory deliberately ignores cwd.
  - `project_id` (`str | None`) remains Session/address ownership, not working context. It stays `None` for a Rooted Identity Agent so bare Subagent targets remain Identity targets; `skill_project_id` separately carries the working Project's Skill scope.
- Result envelope: `{ ok, error, data, artifacts }`. Success uses `error: null`; failure uses `data: null` and `error.code`/`error.message`. The top-level key set is exactly `{ok, error, data, artifacts}` — `is_tool_result_envelope()` checks it exactly, so retry-signalling fields go *inside* `error`, never as new top-level keys.
- Failure retry signal (optional, inside `error`): `tool_failure(code, message, *, retryable=None, attempts_made=None)` may add `error.retryable: bool` and `error.attempts_made: int` (non-negative). They tell the model whether the failure is transient and how many attempts the tool already made, so it does not pointlessly re-invoke a tool that already exhausted its own retries. Convention: a tool that gives up after exhausting its own retries on a retryable status / transport error sets `retryable=True` with the real `attempts_made`; validation/fatal failures set `retryable=False`. Both keys are optional — when omitted (e.g. non-network tools) the model gets no signal. `is_tool_result_envelope()` accepts only these two optional error keys. The network tools (`web_fetch`, `web_search`, and the bundled Home Assistant extension's tools — see `.vorch/domain-maps/extensions/homeassistant.md`) populate them via the shared retry policy in `core/utils/http_status.py` (`is_retryable_status`, `HttpRequestFailure`, `parse_retry_after` — the `Retry-After` header parser shared with the provider adapters); backoff delay math (exponential + jitter, `Retry-After` honored as a capped floor) is shared via `core/utils/retry.compute_retry_delay`.
- Media-injection artifact: `read_media_artifact(attachment_id, filename, media_type)` (in `core/tools/tools.py`) builds the one cross-tool artifact `{ kind: "read_media", attachment_id, filename, media_type }`. A tool emits it to ask the chat loop to inject a stored image as a synthetic current-turn user message so a vision model actually sees it; both `read` (local image files) and `web_fetch` (image URLs) produce it. Consumed by `core/chat/tool_dispatch.py` via the `READ_MEDIA_ARTIFACT_KIND` constant. A new tool that wants to show an image should reuse this builder, not hand-roll the dict.
- Tool timing is not part of the result envelope. Completed tool calls expose a sibling `timing` object on `tool_call_result` Run events and on persisted `role: "tool"` ChatMessages: `{ started_at, completed_at, duration_ms }`. Durations are non-negative milliseconds measured with a monotonic clock.
- `ToolCall`: one requested tool invocation with stable id, name, and arguments; execution index is assigned when scheduling a sibling batch.

## Interfaces

- `ToolRegistry.register(name, description, parameters, handler, *, internal=False, session_scoped=False, display=None, ready=None, readiness_hint=None, extension=None) -> Tool` (all keyword-only; `ready` validated callable-or-None). `readiness_hint`/`extension` are metadata surfaced by `tool.list`; extension-tool registration passes `extension=<record.name>` and the declared `readiness_hint` through `_apply_one_tool`.
- `ToolRegistry.get(name) -> Tool`
- `ToolRegistry.display_for_call(name, arguments) -> dict` returns `{ summary, hidden_argument_keys }` for one invocation.
- `ToolRegistry.unregister(name) -> None`
- `ToolRegistry.list_tools(allowed_tools=None, *, include_internal=False, include_session_scoped=True, ready_only=False) -> list[Tool]` — filter order **registered → allowed → ready**; catalogs pass `include_session_scoped=False`, while runtime consumers can retain them for grant evaluation. `ready_only` applies the readiness filter *after* the other filters and defaults **off**.
- `ToolRegistry.provider_definitions(..., *, session_grants=(), ready_only=True) -> list[dict]` returns provider-visible `name`, `description`, and JSON Schema only. Ungranted Session-scoped Tools are hidden; a matching grant overrides the ordinary Agent allowlist. `ready_only` defaults **on** (model-facing surface).
- `ToolRegistry.prompt_definitions(..., *, session_grants=(), ready_only=True) -> list[dict]` applies the same grant, allowlist, and readiness contract and returns prompt-visible name/description pairs.
- `ToolRegistry.dispatch(context, arguments, allowed_tools=None) -> dict` executes a tool and validates the result envelope. It rejects an ungranted Session-scoped Tool with `<tool>_unavailable` before the ordinary allowlist check; a matching grant overrides that allowlist. **Dispatch is not list-filtered by readiness**, but it re-evaluates readiness live and, when not ready, returns a `tool_not_ready` failure envelope (`retryable=False`) without running the handler — the safety net for a prompt built moments before the credential vanished.
- `tool_is_ready(tool) -> bool` (module-level) — the readiness predicate contract: `ready is None` → ready; a predicate that raises is logged once at `warning` and counts as **not ready** (a broken predicate never takes a build down or makes a tool spuriously available). Cheap, I/O-free (a string-nonempty check, never a network ping) — it runs on every prompt/tool-definition build.
- `ToolContext.on_cancel(callback)` registers a per-tool-call cancel callback (no-op when no registration hook is wired); `ToolContext.was_cancelled_by_user() -> bool` reports whether the current call was user-cancelled (False when no check hook is wired). These hooks are wired by the chat dispatcher through `ToolExecutionConfig` so a tool can plug into `Run.register_tool_cancel` / `Run.tool_call_cancelled` without importing the Run domain.
- Result-envelope validation failures raise `InvalidToolResultError` (a `ValueError` subclass), distinct from plain `ValueError` argument failures. The chat loop maps the former to an `invalid_tool_result` failure envelope and the latter to `invalid_arguments`, without inspecting error message text.
- `ToolExecutor.execute_many(tool_calls, config) -> list[dict]` executes sibling tool calls concurrently and returns results in original call order.
- `core.tools.availability.effective_agent_allowed_tools(...)` applies Agent-level derived availability before runtime dispatch and merges current Session grants. The `memory` tool is added when `memory_prompt_mode` is not `off` and removed when it is `off`, independent of persisted `allowed_tools`; a Session grant independently adds its matching Session-scoped Tool.
- Extensions register their own tools through `api.register_tool(name, description, parameters, handler, *, internal=False, display=None)` (`.vorch/domain-maps/extensions.md`), which routes into this same `ToolRegistry.register` during runtime bootstrap — applied after the last built-in tool, right before `SystemPromptManager` consumes the registry. Extension tools are **normal tools** afterward: same provider/prompt definitions, allowlist filtering, and dispatch with no special-casing. A name colliding with a built-in or another extension's tool is skipped (the existing tool wins) and diagnosed on the extension's record — extensions never override a registered tool.
- `ToolPromptBlockRegistry` is the **tool-side System Prompt block-declaration seam**: a tool that wants prompt content declares a block here (`register(tool_name, *, default_text=None, render=None)`, exactly one of text/render, first-wins-with-warning on a duplicate tool name), and the runtime gathers `block_definitions()` and merges them with the extension blocks before handing the list to the prompt manager. A declared block is id `tool:<name>` and owner `tool:<name>`, so gate 2 renders it only when `<name>` is on the agent's effective allowlist; static or dynamic, the same split as a core or extension block. **The prompt domain imports no tool classes** — it only ever consumes a list of `core.prompts.BlockDefinition` (the `core.prompts` import in `block_definitions()` is lazy so the tools domain has no import-time dependency on prompts either). No built-in tool declares a block today; the seam exists and is proven by a test. See `.vorch/domain-maps/prompts.md` (block model) and `.vorch/domain-maps/extensions.md` (the parallel extension-block path).
- `core.tools.arguments` is the shared home for lenient coercion of model-supplied arguments: `optional_string`/`required_string`, `optional_int`/`required_int`, `optional_number`, `coerce_bool`, and `normalize_aliases`. They raise `ToolArgumentError` (a `ValueError`) so a tool's existing `except ValueError` parse guard — and the dispatch layer's `ValueError → invalid_arguments` mapping — surface them as `invalid_arguments`. Built-in tools use these instead of re-deriving per-tool `isinstance` checks. It also hosts `looks_like_line_numbered_content` and the shared `LINE_NUMBER_GUTTER_SEPARATOR`: a content-shape guard (returns a bool, not a coercion) that `write`/`edit` use to reject text echoing read's `N|` line-number gutter back into a file (see `tools/read.md`).

## Specific Specs

- `tools/read.md` - `read`
- `tools/edit.md` - `edit`
- `tools/write.md` - `write`
- `tools/file_state.md` - shared read-before-write / stale-file guard (`FileReadState`) used by `read`/`write`/`edit`
- `tools/glob.md` - `glob`
- `tools/grep.md` - `grep`
- `tools/web_fetch.md` - `web_fetch`
- `tools/web_search.md` - `web_search`
- `tools/bash.md` - `bash`
- `tools/process.md` - `process` and `ProcessManager`
- `tools/status.md` - `status`
- `tools/memory.md` - `memory`
- `tools/image.md` - `image_generation`
- `tools/session_search.md` - `session_search`
- `tools/history.md` - Session-scoped `history` access after Compaction
- `tools/skill.md` - `skill` (ordinary allow-list tool; default-on in the Project Tool Whitelist)
- `skill_manage` - agent skill authoring (no child map; see `skills.md` → Authoring & Write Scope). An ordinary allow-list tool, but **identity-only** — withheld from an empty-`workspace` (config/project) agent at both the dispatch-time allowlist (`effective_agent_allowed_tools`, `core/tools/availability.py`) and the prompt layer's visibility pass, and excluded from the Project Tool Whitelist.
- `tools/subagent.md` - `subagent` and `subagent_result` registration wrapper
- `tools/cron.md` - `cron`
- `tools/channel_send.md` - `channel_send`
- `tools/speech.md` - `text_to_speech`

## Conventions

- **Argument coercion is lenient by policy.** Nothing validates arguments against the JSON Schema before a handler runs — the schema is only a hint the model may ignore — and models routinely encode an omitted optional field as `""`, an int as `"5"`, or a bool as `"true"`. So via `core.tools.arguments`: a blank optional string means "omitted" (use the default), `"5"`/`5.0` coerce to an int, and `"true"/"false"/"yes"/"no"/"on"/"off"/0/1` coerce to a bool. Genuinely wrong types (a word where a number belongs, an object where a string belongs) still fail `invalid_arguments`. Required-content fields keep their own checks and may be empty (`write` content, `edit` new_string). Unknown arguments stay rejected (a real model mistake), except known camelCase aliases normalized via `normalize_aliases` (currently `edit`'s `oldString`/`newString`/`replaceAll`).
- **Readiness is a separate axis from the allowlist and Session grants.** A not-ready tool stays *registered* (persisted permissions survive) but is filtered out of the **model-facing** surfaces and returns a `tool_not_ready` envelope on a direct dispatch. Two surfaces apply the readiness filter: `provider_definitions` and `prompt_definitions` (both default `ready_only=True`, so the prompt tool list, the provider tool definitions, and — through the prompt definitions — gate 2 of a `tool:<name>`-owned prompt block all drop a not-ready tool). The `tool.list` RPC calls `list_tools(include_session_scoped=False)` without the readiness filter — it returns every registered normal Tool, each carrying `ready` (bool, via `tool_is_ready`, a raising predicate counting as false), `readiness_hint` (or null), and `extension` (owning extension or null) — so the Agent Tool picker and Project Tool Whitelist editor see a not-ready Tool but never expose a Session-scoped capability. Consumers that keep seeing not-ready tools with no styling deliberately leave `ready_only` at its default: extension collision detection, dispatch allowlist computation, and the runtime startup inventory count. A `tool:<name>`-owned prompt block still drops when its Tool is not ready, because gate 2 reads the readiness-filtered prompt definitions.
- `allowed_tools=None` and `["*"]` mean all registered normal tools; `allowed_tools=[]` means no normal tools.
- Agent `allowed_tools` is the configurable allowlist for normal tools except `memory`; Memory mode owns that tool's effective availability.
- For a **config (project) agent**, `allowed_tools` is not stored on the agent: the resolver computes it as the project Tool Whitelist (`project.allowed_tools`, the ceiling) minus the agent's scanned OpenCode denials — the agent can only narrow the ceiling, never widen it. The whitelist filtering here is unchanged; it just receives a computed list instead of `["*"]` (see `projects.md` → Effective tools/skills).
- Internal tools bypass normal `allowed_tools` filtering and must enforce their own domain rules.
- Provider-visible definitions must not expose handlers, runtime context, internal flags, or display metadata.
- Display labels are not tool parameters. Do not add generic arguments such as `description` only to affect UI chrome; use `ToolDisplay`.
- Tool result failures should be returned as failure envelopes where possible instead of raising through the Run.
- Same-turn sibling tool calls may execute concurrently, including multiple calls to the same tool.
- Tool timing metadata must never be forwarded to provider adapters; provider tool messages contain only the tool role, call correlation, name, and content.

## Constraints & Gotchas

- Tool results must be JSON objects matching the stable envelope; non-envelope results are rejected.
- Disallowed normal tools fail at dispatch even if a provider asks for them.
- An ungranted Session-scoped Tool fails with its stable `<tool>_unavailable` code; a Run restriction remains a later dispatch-only intersection and can return `tool_not_allowed` even while the Provider was correctly told the Tool exists.
- Parallel result persistence must preserve the assistant's original tool-call order even when execution finishes out of order.
- Relative filesystem paths resolve from `ToolContext.effective_cwd` (the project repo for a project Session, else `workspace`); absolute paths bypass it unless a specific tool forbids them. The file tools (`read`/`write`/`edit`/`grep`/`glob`/`search`) and `bash` resolve against `effective_cwd`; `memory` deliberately stays on `workspace` (it is the identity/memory home, not project-relative).
