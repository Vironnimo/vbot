# Tools

Tool metadata registry, allowlist filtering, provider definitions, context-aware execution, stable result envelopes, and async dispatch.

## Overview

`core/tools/` owns the registry of callable tools available to an agentic loop. Normal tools use the same allowlist filtering for prompt-visible tools and official provider API tool definitions. Tools execute with a typed runtime `ToolContext` and return stable result envelopes so normal tool failures can be returned to the agent instead of failing the run. Built-in tools are registered during runtime startup.

## Data Model

- `Tool`: `name`, `description`, `parameters`, `handler`, and `internal`.
- `parameters` is a JSON Schema object for provider tool definitions.
- `handler` receives `(ToolContext, arguments)` and returns a JSON result envelope, synchronously or asynchronously.
- `ToolContext`: `agent_id`, `session_id`, `run_id`, `tool_call_id`, `tool_name`, `tool_call_index`, `workspace`, `app_root`, `data_root`, `nesting_depth`, plus small runtime hooks.
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
- Built-in `web_fetch` tool: flat name `web_fetch`; schema includes required
  `url` plus optional `include_links` and `raw` booleans. It fetches HTTP(S)
  content with browser-like headers, rejects known SSRF-local targets, returns
  non-HTML or `raw=True` bodies directly, and otherwise converts HTML into a
  readable text summary under `data.content`.
- Built-in `web_search` tool: flat name `web_search`; schema includes required
  `query` plus optional `count`, `freshness`, `date_after`, and `date_before`.
  It always registers at startup, resolves `BRAVE_API_KEY` through the runtime
  environment-credential lookup, returns a `missing_api_key` failure envelope
  when Brave is not configured, and otherwise calls the Brave Search API and
  returns normalized search results under `data`.
- Built-in `bash` tool: flat name `bash`; schema includes required `command` and
  optional `workdir`, `env`, `yield_after`, `background`, and `timeout`. It runs
  through the host shell, streams foreground stdout/stderr as Run delta events,
  and returns either a foreground completion envelope or a background process
  `session_id`.
- Built-in `process` tool: flat name `process`; schema includes required
  `action` plus action-specific `session_id`, `timeout_ms`, `offset`, `limit`,
  `data`, and `eof`. It manages background process sessions started by `bash`.
- `ProcessManager` is the in-memory service behind host process execution. It
  stores process sessions by process `session_id`, isolates access by
  `ToolContext.agent_id`, scopes cancellation by `ToolContext.run_id`, keeps a
  capped combined output buffer, caps foreground stdout/stderr capture to the
  same budget, kills process trees for explicit kill/timeout/cancel operations,
  and reaps finished sessions after its TTL.
- Internal `skill` tool: flat name `skill`; schema includes required `name`.
  It loads an allowed skill's `SKILL.md` body, wraps it in `<skill_content>`,
  stores the context in the current Session, and returns only a minimal status
  envelope with the skill name, activation status, message, and resource list.
  It is system-managed and not part of user-managed tool catalogs.
- Built-in `subagent` tool: flat name `subagent`; schema includes required
  `content`, optional `agent_id`, optional `blocking`, and optional
  `session_id`. When `session_id` is provided the tool routes into that
  existing Session instead of creating a new one; it fails with
  `session_not_found` if the session file does not exist, `session_busy` if
  that Session already has an active Run, or `invalid_arguments` if the caller
  targets its own active Session. It otherwise creates a new persisted Session
  for the target Agent, starts a sub-agent Run, enforces configured
  depth/per-turn limits, and returns either a running descriptor or a completed
  result envelope.
- Built-in `subagent_result` tool: flat name `subagent_result`; schema includes
  required `session_id` plus optional `agent_id` and `run_id`. It fetches a
  live Run result when available or falls back to the last non-empty assistant
  message in the target JSONL Session.
- Built-in `cron` tool: flat name `cron`; schema includes required `action` and
  action-specific job fields for `create`, `list`, `update`, `delete`,
  `enable`, and `disable`. It delegates to `CronService`, validates cron
  expressions with `croniter`, and returns `next_fire_at` for active cron jobs
  in `list` responses.
- Built-in `channel_send` tool: flat name `channel_send`; schema includes
  required `channel_id`, optional `message`, optional `file_paths`, and optional
  `platform_target`. It delegates to `ChannelService.send()` and resolves
  `platform_target` from session metadata `last_reply_target` when omitted.

## Interfaces

- `ToolRegistry.register(name, description, parameters, handler, internal=False) -> Tool`
- `get(name) -> Tool`
- `unregister(name) -> None` — removes a registered tool when present; used for replacing internal tool handlers after runtime skill reloads.
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
- `register_web_fetch_tool(registry) -> None` — registers the built-in
  `web_fetch` tool.
- `register_web_search_tool(registry, credential_resolver) -> None` — registers
  the built-in `web_search` tool backed by a credential resolver closure.
- `register_bash_tool(registry, process_manager, trigger_service=None) -> None` — registers the
  built-in `bash` tool backed by the shared `ProcessManager`. When `trigger_service` is
  provided and a process transitions to background (explicit `background=True` or after
  `yield_after` expiry), a fire-and-forget watcher task fires `trigger_service.trigger_run`
  with the command, exit code, and full output once the process finishes.
- `register_process_tool(registry, process_manager) -> None` — registers the
  built-in `process` tool backed by the shared `ProcessManager`.
- `register_cron_tool(registry, cron_service) -> None` — registers the built-in
  `cron` scheduling tool backed by `CronService`.
- `register_channel_send_tool(registry, channel_service, chat_sessions) -> None`
  — registers the built-in `channel_send` outbound messaging tool backed by
  `ChannelService` plus `ChatSessionManager` metadata lookup.
- `ProcessManager.spawn(scope_key, agent_id, argv, *, env, cwd) -> str` — starts
  a subprocess session for the given Run scope and Agent.
- `ProcessManager.poll/log/write/submit/kill/clear(..., agent_id=...)` — manages
  existing process sessions while returning not-found semantics for both missing
  and cross-agent sessions.
- `ProcessManager.list_sessions(agent_id) -> list[ProcessSession]` — lists only
  sessions owned by the calling Agent.
- `ProcessManager.cancel_scope(scope_key) -> None` — kills all active sessions
  associated with a Run regardless of Agent owner.
- `register_skill_tool(registry, skill_registry) -> None` — registers the internal `skill` tool.
- `register_subagent_tools(registry, runtime, trigger_service, batch_tracker) -> None` — registers the built-in sub-agent tools.
- `SubAgentBatchTracker(trigger_service)` — tracks in-memory sub-agent batches by parent `(agent_id, session_id, run_id)` and sends one internal automation trigger when all unfetched sub-agent Runs finish. The trigger continues the parent Agent but is stored as a system-reminder note so normal history/WebUI do not show it as a user turn.

## Conventions

- `allowed_tools=None` and `['*']` mean all registered tools.
- `allowed_tools=[]` means no tools.
- Explicit allowlists match exact tool names; unknown names are ignored for listing and fail if dispatched.
- Provider-visible definitions include only `name`, `description`, and `parameters`; handlers and runtime context are internal.
- Internal tools are hidden from normal `list_tools()`, `prompt_definitions()`, and `provider_definitions()` unless `include_internal=True` is explicitly requested by the system prompt manager.
- The internal `skill` tool is governed by `allowed_skills`, not `allowed_tools`; normal tool allowlists must not block it. Normal tools remain blocked by `allowed_tools=[]`.
- The internal `skill` tool result must not include the full `<skill_content>` payload or raw skill body. The session-scoped skill note is the single source of full instructions for the next provider request.
- Same-turn sibling tool calls may execute concurrently, including multiple calls to the same tool.
- Tool execution failures are represented as failure envelopes where possible.
- Sub-agent batch tracking is in-memory only; it does not persist across process restarts.
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
- `channel_send` is registered via a closure helper, not a new `ToolContext`
  hook. When `platform_target` is omitted, it reads `last_reply_target` from
  `ChatSessionManager` metadata for the current `(agent_id, session_id)` and
  fails clearly if no target is available.
- `channel_send` requires at least one of `message` or `file_paths`. When both
  are present, `message` acts as the caption or accompanying text for the file send.
- `channel_send` is present only while the runtime has at least one active
  channel. Runtime enable/disable flows re-register the tool to keep catalogs in
  sync.
- `channel_send.file_paths` are ordinary local paths. The tool reads the files,
  sniffs MIME types, builds channel `FileData` payloads, and keeps Telegram-specific
  batching or media-group decisions inside the adapter layer.
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
- `web_fetch` accepts exactly `url`, `include_links`, and `raw`;
  `additionalProperties` is false. It allows only `http` and `https` URLs,
  validates each request target as public after URL parsing and DNS resolution,
  rejects localhost/private/link-local/multicast/reserved targets including
  obfuscated IP forms, retries transient HTTP 429/5xx responses up to 3 times
  with exponential backoff plus jitter, and returns extracted text under
  `data.content`.
- `web_fetch` uses `httpx.AsyncClient` with browser-like headers and follows
  redirects through explicit per-hop validation instead of blind auto-follow.
  HTML responses are converted to readable text via BeautifulSoup; non-HTML or
  `raw=True` responses return truncated response text unchanged.
- `web_search` accepts exactly `query`, `count`, `freshness`, `date_after`, and
  `date_before`; `additionalProperties` is false. It is Brave-only in v1 and
  exposes no provider-selection argument.
- `web_search` is always registered. At call time it resolves `BRAVE_API_KEY`
  through the runtime credential resolver, returns `missing_api_key` when the
  key is absent, validates caller-supplied date and freshness filters as
  `validation_error`, and maps Brave/network failures to
  `provider_request_failed`.
- `web_search` uses `httpx.AsyncClient` with manual retry for transient 429/5xx
  responses, normalizes Brave results to `{rank, title, url, description,
  content_trust}`, and marks both per-result and top-level content as
  `untrusted_web_content`.
- `bash` resolves relative working directories from `ToolContext.workspace` and
  accepts absolute working directories unchanged. It uses the platform-native
  shell (`pwsh` on Windows, `bash -c` elsewhere) and blocks sensitive environment
  overrides such as `PATH`, loader hooks, and shell startup hooks.
- `bash` probes a login shell environment once per process and falls back to
  `os.environ` on failure or timeout. Timed-out probe processes are killed and
  reaped best-effort before the fallback is returned.
- `bash` non-zero process exits are successful tool results with an exit code;
  only spawn failures and tool-enforced timeouts are failure envelopes.
- `process poll` output is incremental since the previous poll. `process log`
  returns a line window from the combined capped output buffer.
- `process poll` includes `waiting_for_input` as a best-effort hint only. Slow
  commands without output may look idle even when they do not need stdin.
- Process session identifiers are distinct from chat Session identifiers. Tool
  and server code should use context (`run_id` and `agent_id`) rather than chat
  session paths to manage processes.

## Constraints & Gotchas

- Tool results must be JSON objects that match the stable result envelope. Non-envelope results are rejected.
- Disallowed normal tools are blocked at dispatch time even if a provider asks for them. Internal tools bypass `allowed_tools` and must perform their own domain-specific checks.
- Parallel result persistence must preserve the assistant's original tool-call order even when completion order differs.
