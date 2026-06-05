# Tools

Tool metadata registry, allowlist filtering, provider definitions, context-aware execution, stable result envelopes, display metadata, and async dispatch.

## Overview

`core/tools/` owns the registry of callable tools available to the agentic loop. It exposes provider/prompt definitions, filters tools by Agent allowlists, dispatches calls with `ToolContext`, and turns expected tool failures into stable result envelopes. Concrete built-in tool behavior lives in child specs under `.vorch/specs/tools/`.

## Data Model

- `Tool`: `name`, `description`, `parameters`, `handler`, `internal`, and `display`.
- `ToolDisplay`: per-invocation presentation metadata. It builds `{ summary, hidden_argument_keys }` for `tool_call_started` events without adding provider-visible parameters.
- `ToolContext`: `agent_id`, `session_id`, `run_id`, `tool_call_id`, `tool_name`, `tool_call_index`, `workspace`, `app_root`, `data_root`, `nesting_depth`, `allowed_skills`, plus emit/cancel/note/skill hooks.
- Result envelope: `{ ok, error, data, artifacts }`. Success uses `error: null`; failure uses `data: null` and `error.code`/`error.message`.
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
- Result-envelope validation failures raise `InvalidToolResultError` (a `ValueError` subclass), distinct from plain `ValueError` argument failures. The chat loop maps the former to an `invalid_tool_result` failure envelope and the latter to `invalid_arguments`, without inspecting error message text.
- `ToolExecutor.execute_many(tool_calls, config) -> list[dict]` executes sibling tool calls concurrently and returns results in original call order.
- `core.tools.availability.effective_agent_allowed_tools(...)` applies Agent-level derived availability before runtime dispatch. The `memory` tool is added when `memory_prompt_mode` is not `off` and removed when it is `off`, independent of persisted `allowed_tools`.

## Specific Specs

- `tools/read.md` - `read`
- `tools/edit.md` - `edit`
- `tools/write.md` - `write`
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
- `tools/subagent.md` - `subagent` and `subagent_result` registration wrapper
- `tools/cron.md` - `cron`
- `tools/channel_send.md` - `channel_send`
- `tools/speech.md` - `text_to_speech`

## Conventions

- `allowed_tools=None` and `["*"]` mean all registered normal tools; `allowed_tools=[]` means no normal tools.
- Agent `allowed_tools` is the configurable allowlist for normal tools except `memory`; Memory mode owns that tool's effective availability.
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
- Relative filesystem paths resolve from `ToolContext.workspace`; absolute paths bypass it unless a specific tool forbids them.
