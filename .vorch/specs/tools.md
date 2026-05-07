# Tools

Tool metadata registry, allowlist filtering, provider definitions, context-aware execution, stable result envelopes, and async dispatch.

## Overview

`core/tools/` owns the registry of callable tools available to an agentic loop. The same allowlist filtering controls prompt-visible tools and official provider API tool definitions. Tools execute with a typed runtime `ToolContext` and return stable result envelopes so normal tool failures can be returned to the agent instead of failing the run. Built-in tools are registered during runtime startup.

## Data Model

- `Tool`: `name`, `description`, `parameters`, `handler`.
- `parameters` is a JSON Schema object for provider tool definitions.
- `handler` receives `(ToolContext, arguments)` and returns a JSON result envelope, synchronously or asynchronously.
- `ToolContext`: `agent_id`, `session_id`, `run_id`, `tool_call_id`, `tool_name`, `tool_call_index`, `workspace`, `app_root`, `data_root`, plus small runtime hooks.
- Result envelope: `{ ok, error, data, artifacts }`. Success uses `error: null`; failure uses `data: null` and `error.code`/`error.message`.
- `ToolCall`: one requested tool invocation with stable id, index, name, and arguments.
- Built-in `read` tool: flat name `read`; schema includes required `path`, optional `description` (for display labels in the UI, ignored by the handler), and optional line-based `offset`/`limit`; relative paths resolve from `ToolContext.workspace`; absolute paths are allowed.

## Interfaces

- `ToolRegistry.register(name, description, parameters, handler) -> Tool`
- `get(name) -> Tool`
- `list_tools(allowed_tools=None) -> list[Tool]`
- `provider_definitions(allowed_tools=None) -> list[dict]` — name, description, JSON Schema.
- `prompt_definitions(allowed_tools=None) -> list[dict]` — name and description only.
- `dispatch(context, name, arguments, allowed_tools=None) -> dict` — executes through an async interface and returns a result envelope.
- `ToolExecutor.execute_many(...) -> list[ToolExecutionResult]` — executes sibling tool calls concurrently, applies per-run/global concurrency limits, and returns terminal results in original tool-call order.
- `register_builtin_tools(registry) -> None` — registers built-in host tools such as `read`.

## Conventions

- `allowed_tools=None` and `['*']` mean all registered tools.
- `allowed_tools=[]` means no tools.
- Explicit allowlists match exact tool names; unknown names are ignored for listing and fail if dispatched.
- Provider-visible definitions include only `name`, `description`, and `parameters`; handlers and runtime context are internal.
- Same-turn sibling tool calls may execute concurrently, including multiple calls to the same tool.
- Tool execution failures are represented as failure envelopes where possible.
- `read` decodes text as UTF-8 with replacement, returns plain content without injected line numbers, and reports expected file/argument errors as failure envelopes.
- The optional `description` argument on `read` (and future tools) is a display-only parameter for the UI — handlers ignore it, and the frontend uses it as a human-readable label for the tool-call summary line.

## Constraints & Gotchas

- Tool results must be JSON objects that match the stable result envelope. Non-envelope results are rejected.
- Disallowed tools are blocked at dispatch time even if a provider asks for them.
- Parallel result persistence must preserve the assistant's original tool-call order even when completion order differs.
