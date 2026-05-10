# Tools

Tool metadata registry, allowlist filtering, provider definitions, context-aware execution, stable result envelopes, and async dispatch.

## Overview

`core/tools/` owns the registry of callable tools available to an agentic loop. Normal tools use the same allowlist filtering for prompt-visible tools and official provider API tool definitions. Tools execute with a typed runtime `ToolContext` and return stable result envelopes so normal tool failures can be returned to the agent instead of failing the run. Built-in tools are registered during runtime startup.

## Data Model

- `Tool`: `name`, `description`, `parameters`, `handler`, and `internal`.
- `parameters` is a JSON Schema object for provider tool definitions.
- `handler` receives `(ToolContext, arguments)` and returns a JSON result envelope, synchronously or asynchronously.
- `ToolContext`: `agent_id`, `session_id`, `run_id`, `tool_call_id`, `tool_name`, `tool_call_index`, `workspace`, `app_root`, `data_root`, plus small runtime hooks.
- Tool runtime hooks include lifecycle event emission, cancellation checks, and an optional note hook for adding kernel-internal background reminders to the current chat Session without exposing the Session object to tools.
- Tool runtime hooks also include an optional skill activation hook plus `allowed_skills` for the internal `skill` tool.
- Result envelope: `{ ok, error, data, artifacts }`. Success uses `error: null`; failure uses `data: null` and `error.code`/`error.message`.
- `ToolCall`: one requested tool invocation with stable id, index, name, and arguments.
- Built-in `read` tool: flat name `read`; schema includes required `path` plus
  optional line-based `offset`/`limit`. It does **not** have a `description`
  argument. Relative paths resolve from `ToolContext.workspace`; absolute paths
  are allowed.
- Built-in `edit` tool: flat name `edit`; schema includes required `path`,
  `old_string`, and `new_string`, plus optional boolean `replace_all`. It
  replaces exact text in an existing file. Relative paths resolve from
  `ToolContext.workspace`; absolute paths are allowed.
- Built-in `write` tool: flat name `write`; schema includes required `path` and
  `content`. It writes full file contents, creates parent directories, and
  replaces existing file contents. Relative paths resolve from
  `ToolContext.workspace`; absolute paths are allowed.
- Built-in `glob` tool: flat name `glob`; schema includes required `pattern`
  and optional `path`. It returns matching file and directory paths relative to
  the search root; directory entries end with `/`. Relative paths resolve from
  `ToolContext.workspace`; absolute paths are allowed.
- Built-in `grep` tool: flat name `grep`; schema includes required `pattern`
  and optional `path`, `glob`, `ignoreCase`, `literal`, `context`, `limit`, and
  `output_mode`. It searches file contents by regex by default or fixed string
  when `literal: true`. Relative paths resolve from `ToolContext.workspace`;
  absolute file or directory paths are allowed.
- Internal `skill` tool: flat name `skill`; schema includes required `name`.
  It loads an allowed skill's `SKILL.md` body, wraps it in `<skill_content>`,
  lists activation-time resources, and stores the context in the current Session.
  It is system-managed and not part of user-managed tool catalogs.

## Interfaces

- `ToolRegistry.register(name, description, parameters, handler, internal=False) -> Tool`
- `get(name) -> Tool`
- `list_tools(allowed_tools=None, include_internal=False) -> list[Tool]`
- `provider_definitions(allowed_tools=None, include_internal=False) -> list[dict]` — name, description, JSON Schema.
- `prompt_definitions(allowed_tools=None, include_internal=False) -> list[dict]` — name and description only.
- `dispatch(context, arguments, allowed_tools=None) -> dict` — executes `context.tool_name` through an async interface and returns a result envelope.
- `ToolExecutor.execute_many(...) -> list[ToolExecutionResult]` — executes sibling tool calls concurrently, applies per-run/global concurrency limits, and returns terminal results in original tool-call order.
- `ToolContext.add_note(content) -> None` — calls the configured note hook when present; otherwise it is a no-op. Chat wires this to `ChatSession.add_note()` so a tool can inject a background reminder for the next model request.
- `ToolContext.activate_skill(name, data) -> dict | None` — calls the configured skill activation hook when present; otherwise returns `None`.
- `register_read_tool(registry) -> None` — registers the built-in `read` tool.
- `register_edit_tool(registry) -> None` — registers the built-in `edit` tool.
- `register_write_tool(registry) -> None` — registers the built-in `write` tool.
- `register_glob_tool(registry) -> None` — registers the built-in `glob` tool.
- `register_grep_tool(registry) -> None` — registers the built-in `grep` tool.
- `register_skill_tool(registry, skill_registry) -> None` — registers the internal `skill` tool.

## Conventions

- `allowed_tools=None` and `['*']` mean all registered tools.
- `allowed_tools=[]` means no tools.
- Explicit allowlists match exact tool names; unknown names are ignored for listing and fail if dispatched.
- Provider-visible definitions include only `name`, `description`, and `parameters`; handlers and runtime context are internal.
- Internal tools are hidden from normal `list_tools()`, `prompt_definitions()`, and `provider_definitions()` unless `include_internal=True` is explicitly requested by the system prompt manager.
- The internal `skill` tool is governed by `allowed_skills`, not `allowed_tools`; normal tool allowlists must not block it. Normal tools remain blocked by `allowed_tools=[]`.
- Same-turn sibling tool calls may execute concurrently, including multiple calls to the same tool.
- Tool execution failures are represented as failure envelopes where possible.
- `read` is the authoritative read-like tool. It is the vControl-derived
  implementation adapted for vBot module naming, `ToolContext` path resolution,
  registry metadata/schema, and stable result envelopes.
- `read` schemas must never contain a provider/tool parameter named
  `description`. A tool's metadata description is separate from model-supplied
  arguments. Display labels are not a read-tool argument.
- `read` accepts exactly `path`, `offset`, and `limit`; `additionalProperties` is
  false. `path` is required. `offset` and `limit` are positive 1-indexed line
  controls when supplied.
- `read` decodes file bytes as UTF-8 with replacement, returns file content under
  `data.content` only, truncates output to the built-in line/byte limits, and
  reports expected file/argument/read-time filesystem errors as failure envelopes.
- Successful `read` results do not include `data.path`. The agent already knows
  the requested path from the tool call arguments.
- `edit` is for precise, surgical replacement in existing files. `old_string`
  must be non-empty and different from `new_string`; it must match exactly and
  uniquely unless `replace_all: true` is supplied. The tool normalizes line
  endings for matching/replacement, preserves the file's line-ending style where
  practical, and reports missing text, ambiguous matches, validation failures,
  and expected filesystem errors as failure envelopes.
- `edit` success data includes a human-readable `message`, the resolved `path`,
  `first_changed_line`, and `replacements` count.
- `write` is for replacing an entire file or creating a new file. It creates
  parent directories automatically and writes UTF-8 text. It is not for partial
  edits or appends; use `edit` for surgical changes.
- `write` success data includes a human-readable `message`, the resolved `path`,
  and written byte count.
- `glob` is for path discovery. It accepts glob-style relative patterns such as
  `**/*.py`, includes files and directories, sorts matches relative to the
  search root, caps output at 100 matches, and returns no-match messages as
  success envelopes under `data.content`.
- `grep` is for content search. It supports regex mode, `literal` fixed-string
  mode, `ignoreCase`, optional candidate-file `glob` filters, `context` lines,
  `limit`, and `output_mode` values `content`, `files_with_matches`, and
  `count`. Successful textual output is returned under `data.content`; no-match
  messages are success envelopes. Invalid arguments, invalid regexes, and
  expected path/search errors are failure envelopes.
- `grep` may use `rg`/ripgrep when available on `PATH`, but must work via the
  Python fallback without requiring ripgrep as a dependency.

## Constraints & Gotchas

- Tool results must be JSON objects that match the stable result envelope. Non-envelope results are rejected.
- Disallowed normal tools are blocked at dispatch time even if a provider asks for them. Internal tools bypass `allowed_tools` and must perform their own domain-specific checks.
- Parallel result persistence must preserve the assistant's original tool-call order even when completion order differs.
