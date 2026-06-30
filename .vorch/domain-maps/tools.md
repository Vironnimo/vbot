# Tools

Tool metadata registry, allowlist filtering, provider definitions, context-aware execution, stable result envelopes, display metadata, and async dispatch.

## Overview

`core/tools/` owns the registry of callable tools available to the agentic loop. It exposes provider/prompt definitions, filters tools by Agent allowlists, dispatches calls with `ToolContext`, and turns expected tool failures into stable result envelopes. Concrete built-in tool behavior lives in child maps under `.vorch/domain-maps/tools/`.

## Data Model

- `Tool`: `name`, `description`, `parameters`, `handler`, `internal`, and `display`.
- `ToolDisplay`: per-invocation presentation metadata. It builds `{ summary, hidden_argument_keys }` for `tool_call_started` events without adding provider-visible parameters.
- `ToolContext`: `agent_id`, `session_id`, `run_id`, `tool_call_id`, `tool_name`, `tool_call_index`, `workspace`, `cwd`, `project_id`, `app_root`, `data_root`, `nesting_depth`, `allowed_skills`, plus emit/cancel/note/skill hooks and per-call cancel hooks (`cancel_registration_hook`, `cancel_check_hook`).
  - `cwd` (`Path | None`) is the working directory file tools resolve relative paths against — the project repo for a project Session, else the agent's `workspace`. `ToolContext.effective_cwd` returns `cwd` when set, falling back to `workspace`; both `cwd` and `project_id` flow `ToolExecutionConfig → ToolContext` and default to `None`, so identity Sessions and every existing caller are unchanged.
  - `project_id` (`str | None`) is the project of the running Run, set by the chat loop. Project-aware tools (e.g. `subagent`) read it to inherit the parent's project; `None` means global/identity.
- Result envelope: `{ ok, error, data, artifacts }`. Success uses `error: null`; failure uses `data: null` and `error.code`/`error.message`. The top-level key set is exactly `{ok, error, data, artifacts}` — `is_tool_result_envelope()` checks it exactly, so retry-signalling fields go *inside* `error`, never as new top-level keys.
- Failure retry signal (optional, inside `error`): `tool_failure(code, message, *, retryable=None, attempts_made=None)` may add `error.retryable: bool` and `error.attempts_made: int` (non-negative). They tell the model whether the failure is transient and how many attempts the tool already made, so it does not pointlessly re-invoke a tool that already exhausted its own retries. Convention: a tool that gives up after exhausting its own retries on a retryable status / transport error sets `retryable=True` with the real `attempts_made`; validation/fatal failures set `retryable=False`. Both keys are optional — when omitted (e.g. non-network tools) the model gets no signal. `is_tool_result_envelope()` accepts only these two optional error keys. The network tools (`web_fetch`, `web_search`, Home Assistant) populate them via the shared retry policy in `core/utils/http_status.py` (`is_retryable_status`, `HttpRequestFailure`).
- Tool timing is not part of the result envelope. Completed tool calls expose a sibling `timing` object on `tool_call_result` Run events and on persisted `role: "tool"` ChatMessages: `{ started_at, completed_at, duration_ms }`. Durations are non-negative milliseconds measured with a monotonic clock.
- `ToolCall`: one requested tool invocation with stable id, name, and arguments; execution index is assigned when scheduling a sibling batch.

## Interfaces

- `ToolRegistry.register(name, description, parameters, handler, *, internal=False, display=None) -> Tool` (`internal`/`display` are keyword-only)
- `ToolRegistry.get(name) -> Tool`
- `ToolRegistry.display_for_call(name, arguments) -> dict` returns `{ summary, hidden_argument_keys }` for one invocation.
- `ToolRegistry.unregister(name) -> None`
- `ToolRegistry.list_tools(allowed_tools=None, include_internal=False) -> list[Tool]`
- `ToolRegistry.provider_definitions(...) -> list[dict]` returns provider-visible `name`, `description`, and JSON Schema only.
- `ToolRegistry.prompt_definitions(...) -> list[dict]` returns prompt-visible name/description pairs.
- `ToolRegistry.dispatch(context, arguments, allowed_tools=None) -> dict` executes a tool and validates the result envelope.
- `ToolContext.on_cancel(callback)` registers a per-tool-call cancel callback (no-op when no registration hook is wired); `ToolContext.was_cancelled_by_user() -> bool` reports whether the current call was user-cancelled (False when no check hook is wired). These hooks are wired by the chat dispatcher through `ToolExecutionConfig` so a tool can plug into `Run.register_tool_cancel` / `Run.tool_call_cancelled` without importing the Run domain.
- Result-envelope validation failures raise `InvalidToolResultError` (a `ValueError` subclass), distinct from plain `ValueError` argument failures. The chat loop maps the former to an `invalid_tool_result` failure envelope and the latter to `invalid_arguments`, without inspecting error message text.
- `ToolExecutor.execute_many(tool_calls, config) -> list[dict]` executes sibling tool calls concurrently and returns results in original call order.
- `core.tools.availability.effective_agent_allowed_tools(...)` applies Agent-level derived availability before runtime dispatch. The `memory` tool is added when `memory_prompt_mode` is not `off` and removed when it is `off`, independent of persisted `allowed_tools`.
- Extensions register their own tools through `api.register_tool(name, description, parameters, handler, *, internal=False, display=None)` (`.vorch/domain-maps/extensions.md`), which routes into this same `ToolRegistry.register` during runtime bootstrap — applied after the last built-in tool, right before `SystemPromptManager` consumes the registry. Extension tools are **normal tools** afterward: same provider/prompt definitions, allowlist filtering, and dispatch with no special-casing. A name colliding with a built-in or another extension's tool is skipped (the existing tool wins) and diagnosed on the extension's record — extensions never override a registered tool.
- `ToolPromptBlockRegistry` is the **tool-side System Prompt block-declaration seam** (D6): a tool that wants prompt content declares a block here (`register(tool_name, *, default_text=None, render=None)`, exactly one of text/render, first-wins-with-warning on a duplicate tool name), and the runtime gathers `block_definitions()` and merges them with the extension blocks before handing the list to the prompt manager. A declared block is id `tool:<name>` and owner `tool:<name>`, so gate 2 renders it only when `<name>` is on the agent's effective allowlist; static or dynamic, the same split as a core or extension block. **The prompt domain imports no tool classes** — it only ever consumes a list of `core.prompts.BlockDefinition` (the `core.prompts` import in `block_definitions()` is lazy so the tools domain has no import-time dependency on prompts either). No built-in tool declares a block today; the seam exists and is proven by a test. See `.vorch/domain-maps/prompts.md` (block model) and `.vorch/domain-maps/extensions.md` (the parallel extension-block path).
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
- `tools/homeassistant.md` - `ha_list_entities`, `ha_get_state`, `ha_list_services`, `ha_call_service`
- `tools/bash.md` - `bash`
- `tools/process.md` - `process` and `ProcessManager`
- `tools/status.md` - `status`
- `tools/memory.md` - `memory`
- `tools/image.md` - `image_generation`
- `tools/session_search.md` - `session_search`
- `tools/skill.md` - internal `skill`
- internal `skill_manage` - agent skill authoring (no child map; see `skills.md` → Authoring & Write Scope). Always registered, but **model-exposed only to identity agents** (the prompt layer gates it on a non-empty `workspace`).
- `tools/subagent.md` - `subagent` and `subagent_result` registration wrapper
- `tools/cron.md` - `cron`
- `tools/channel_send.md` - `channel_send`
- `tools/speech.md` - `text_to_speech`

## Conventions

- **Argument coercion is lenient by policy.** Nothing validates arguments against the JSON Schema before a handler runs — the schema is only a hint the model may ignore — and models routinely encode an omitted optional field as `""`, an int as `"5"`, or a bool as `"true"`. So via `core.tools.arguments`: a blank optional string means "omitted" (use the default), `"5"`/`5.0` coerce to an int, and `"true"/"false"/"yes"/"no"/"on"/"off"/0/1` coerce to a bool. Genuinely wrong types (a word where a number belongs, an object where a string belongs) still fail `invalid_arguments`. Required-content fields keep their own checks and may be empty (`write` content, `edit` new_string). Unknown arguments stay rejected (a real model mistake), except known camelCase aliases normalized via `normalize_aliases` (currently `edit`'s `oldString`/`newString`/`replaceAll`).
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
- Parallel result persistence must preserve the assistant's original tool-call order even when execution finishes out of order.
- Relative filesystem paths resolve from `ToolContext.effective_cwd` (the project repo for a project Session, else `workspace`); absolute paths bypass it unless a specific tool forbids them. The file tools (`read`/`write`/`edit`/`grep`/`glob`/`search`) and `bash` resolve against `effective_cwd`; `memory` deliberately stays on `workspace` (it is the identity/memory home, not project-relative).
