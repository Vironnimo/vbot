# Phase 2+ Goals — Contracts

Items not yet decided are marked **(open)**. This document captures the stable
cross-phase contracts and current defaults, starting with Phase 2 and extended
where later phases already need architectural clarification. It does not define
the implementation plan (see ROADMAP for that).

## 1. Agent Schema

Minimal JSON for `agent.json`. May gain more parameters later,
but never fewer.

```json
{
  "id": "coder",
  "name": "Coder Agent",
  "model": "openrouter/deepseek/deepseek-v4-pro",
  "fallback_model": "",
  "workspace": "",
  "current_session_id": "",
  "temperature": 0.1,
  "thinking_effort": "",
  "allowed_tools": ["*"],
  "allowed_skills": ["*"],
  "created_at": "2026-05-03T12:00:00Z",
  "updated_at": "2026-05-03T12:00:00Z"
}
```

- `id`: unique, also used as directory name. Immutable — cannot be changed after creation.
- `model`: `<provider>/<model-id>` (from Phase 1). Empty = error at chat time ("no model set").
  Must reference an existing provider — if provider not found, error. If model-id doesn't exist
  at that provider, the provider API will error (we don't pre-validate model existence).
- `fallback_model`: empty = no fallback configured. Exact automatic fallback behavior
  is still **(open)**.
- `workspace`: absolute path to the agent's workspace directory. Default on creation is
  `<data_dir>/workspace-<id>/`. User can override to a custom path.
- `current_session_id`: session identifier of the agent's current/active chat.
  This is stored with the agent so the current chat is explicit rather than
  inferred from filesystem ordering.
- `thinking_effort`: `none` / `minimal` / `low` / `medium` / `high` / `xhigh` / `max` —
  reasoning effort level. Empty string = provider default. Each adapter translates this
  into its provider's wire format.
- `allowed_tools`: `["*"]` = all, `[]` = none, otherwise explicit list. Only allowed
  tools appear in the `{tools}` prompt block and in the official provider API tool
  definitions. Tools not on the allowlist are blocked by the system.
- `allowed_skills`: `["*"]` = all, `[]` = none, otherwise explicit list. Only allowed
  skills appear in the `{skills}` prompt block. Skills are not hard-blocked outside
  the prompt.
- `created_at` / `updated_at`: ISO 8601, explicit UTC offset

## Agent Lifecycle

- **Create**: New agent → `data_dir/agents/<id>/agent.json` + workspace seeded from `resources/workspace-templates/` (`SOUL.md`, `IDENTITY.md`, `AGENTS.md`, `USER.md`). `workspace` field in agent.json defaults to `<data_dir>/workspace-<id>/`. Each new agent also gets an initial Session immediately, and `current_session_id` points to it.
- **Bootstrap / first start**: When a new instance creates its data directory for the first time, the system also creates a default agent with `id: "main"` and `name: "Main"`.
- **Delete**: Agent deleted → all files (agent.json, sessions, workspace) moved to `archive/<agent-id>/`. Not permanently destroyed — can be inspected or restored. Deletion is only allowed when at least one other agent will remain afterwards.
- **Update**: Any field except `id` can be changed. `id` is immutable (it's the directory name).

The product must always have at least one agent. Zero-agent state is invalid.

## 2. Data Directory Structure

```                     ← VBOT_DATA_DIR
├── .env
├── settings.json
├── .tmp/
├── agents/<id>/
│   ├── agent.json
│   └── sessions/
├── workspace-<id>/
│   ├── SOUL.md
│   ├── AGENTS.md
│   ├── IDENTITY.md
│   └── USER.md
├── archive/
├── channels/
├── cron/
├── oauth/
├── prompts/
├── skills/
└── logs/
```

- `data_dir` = `~/.vbot` (default), passed via `--data-dir` argument at server start
- Multiple instances: each has its own data-dir and port. Second instance:
  `vbot server start --data-dir ./dev-data` (port from that instance's settings.json)
- Port priority: `--port` > `VBOT_SERVER_PORT` (env) > `settings.json` > `8420` (default)
- `agents/<id>/sessions/`: agent session history (JSONL — one message per line)
- Session files use JSONL because sessions are append-only: each new message is a
  single line appended to the file. No need to parse/rewrite the entire file on
  every turn. Crash-safe — at most the last line is lost.
- `workspace-<id>/`: agent workspace, seeded with the four files on creation
- `prompts/`: prompt templates and snippets
- `skills/`: skill definitions (SKILL.md + optional resources/)

## 3. System Prompt Assembly

The system prompt is assembled at runtime from templates and snippets.
No hardcoded strings.

### Main Template

```
You are an agent for vBot, App version: {app_version}.
Use the instructions below and the tools available to you to assist the user.

{runtime}

{tools}

{skills}

{include:SOUL.md}
{include:IDENTITY.md}
{include:AGENTS.md}
{include:USER.md}
```

- `{app_version}`: application version
- `{runtime}`: injected runtime snippet
- `{tools}`: injected tool snippet
- `{skills}`: injected skill snippet
- `{include:<filename>}`: content of the named workspace file inserted inline

### Runtime Snippet

Injected into `{runtime}`:

```
## Runtime

Here is useful information about the environment you are running in:

- Host: {host}
- OS: {os}
- You are powered by the model {model}
- Your Workspace (HOME, your CWD for tools, where you and your files live): {agent_workspace}
- App Path: {app_dir}
- Data Path: All app data (sessions, workspaces, skills, configs, etc.) lives here: {data_root}
- Thinking level: {thinking_effort}
- Date: {current_date}
- Current time: use the `status` tool if you need the time.
```

### Tool Snippet

Injected into `{tools}`:

```
## Tool Call Style

- Relative paths in tool calls are always resolved to your workspace path, so use full paths when working outside of your workspace.
- Call tools directly without first explaining what you will do.
- If a tool returns an error, read it, correct parameters, and retry.
- Use the fitting tool instead of asking the user to do manual steps.
- For action-based tools, always set action and all required parameters.

## Available Tools

{tool_list}
```

- `{tool_list}` contains only the tool name and description for each allowed tool.
- The same allowed tool set is used in two places: the official provider API tool
  definitions and this prompt reminder block.
- The official provider API tool definitions contain the tool name, a description,
  and a parameter schema (JSON Schema).
- If tools are not allowed, they are omitted from both places.

### Skill Snippet

Injected into `{skills}`:

```
## Available Skills

Each skill below has a description and a path to its directory.
When a skill's description matches the current task, read the SKILL.md
inside the skill directory for detailed instructions to follow.

{skill_list}
```

`{skill_list}` is replaced with XML (agentskills.io schema):

```xml
<available_skills>
  <skill>
    <name>agent-cli</name>
    <description>Delegate coding tasks to an external AI coding agent CLI...</description>
    <path>C:\Users\Viro\.vbot\skills\agent-cli\SKILL.md</path>
  </skill>
  <skill>
    <name>get-news</name>
    <description>Fetch current news via RSS feeds...</description>
    <path>C:\Users\Viro\.vbot\skills\get-news\SKILL.md</path>
  </skill>
</available_skills>
```

- `<name>`: skill identifier
- `<description>`: short description of what the skill does and when to use it
- `<path>`: absolute path to the skill's `SKILL.md`
- `{skill_list}` is filtered by `allowed_skills`.

## 4. Session Storage

Session files are JSONL — one message per line, append-only, crash-safe (at most the last line is lost).

- **Filename**: `<data_dir>/agents/<id>/sessions/<uuid>.jsonl` — UUID as session identifier
- **Encoding**: UTF-8, one JSON object per line, no trailing commas, no pretty-printing

### ChatMessage Format

Every persisted session line is a ChatMessage. This is also the canonical
message type between the chat layer and the adapter — no separate schema
needed.

The `system` role exists in the canonical message model, but the runtime
normally assembles the system prompt at request time instead of appending it to
the session JSONL history.

```jsonl
{"id":"d4e5f6","timestamp":"2026-05-03T14:30:01Z","role":"user","content":"What's the weather in Berlin?"}
{"id":"g7h8i9","timestamp":"2026-05-03T14:30:05Z","role":"assistant","model":"anthropic/claude-sonnet-4","content":null,"reasoning":"I need to call the weather tool...","reasoning_meta":{"signature":"opaque..."},"tool_calls":[{"id":"call_abc","name":"get_weather","arguments":{"city":"Berlin"}}]}
{"id":"j0k1l2","timestamp":"2026-05-03T14:30:06Z","role":"tool","tool_call_id":"call_abc","name":"get_weather","content":"{\"temp\":22,\"condition\":\"sunny\"}"}
{"id":"m3n4o5","timestamp":"2026-05-03T14:30:08Z","role":"assistant","model":"anthropic/claude-sonnet-4","content":"The weather in Berlin is 22°C and sunny.","reasoning_meta":{"signature":"opaque..."}}
```

### Field Reference

| Field | system | user | assistant | tool |
|---|---|---|---|---|
| `id` | ✅ | ✅ | ✅ | ✅ |
| `timestamp` | ✅ | ✅ | ✅ | ✅ |
| `role` | ✅ | ✅ | ✅ | ✅ |
| `model` | ✅ | — | ✅ | — |
| `content` | ✅ | ✅ | ✅ (nullable) | ✅ (result) |
| `reasoning` | — | — | ✅ (nullable) | — |
| `reasoning_meta` | — | — | ✅ (nullable) | — |
| `tool_calls` | — | — | ✅ (nullable array) | — |
| `tool_call_id` | — | — | — | ✅ |
| `name` | — | — | — | ✅ |

- `id`: UUID — unique per message, searchable, correlatable
- `timestamp`: ISO 8601 with explicit UTC offset
- `model`: `<provider>/<model-id>` — which provider/model produced this message. Used by the adapter to decide whether to round-trip `reasoning_meta` opaque data.
- `reasoning`: readable thinking text from the model. Stored for search, display, and as normal plain-text context.
- `reasoning_meta`: opaque provider-specific data (Anthropic `signature`/`redacted_thinking`, OpenAI `encrypted_content`, Gemini `thoughtSignature`, OpenRouter `reasoning_details`). The adapter serializes this from the API response and uses it for round-tripping when needed. The chat layer never interprets it. Unknown fields are ignored — new fields can be added later without breaking old files.

### CoT Round-Trip Rules

- **Tool-use loop (mandatory)**: `reasoning_meta` from the current assistant turn must be sent back unchanged with the tool result. Dropping opaque data breaks model continuity.
- **After a completed turn (fresh user message)**: by default, old `reasoning_meta` is not sent again. The readable `reasoning` text can still remain in history as normal context.
- **Different provider, same session**: stale `reasoning_meta` from the old provider is never sent to the new provider.
- **Keep this easy to change**: provider-specific resend rules after completed turns are still **(open)** and should stay easy to adjust later.

## Decided

- **Reasoning effort levels**: `none` / `minimal` / `low` / `medium` / `high` / `xhigh` / `max` — single string in agent schema, adapters translate to wire format
- **System prompt `{runtime}` block**: concrete format defined in Section 3
- **System prompt `{tools}` block**: concrete reminder format defined in Section 3
- **`{tool_list}` contents**: name + description only; parameter schemas stay in the official provider API tool definitions
- **Workspace prompt includes**: `SOUL.md`, `IDENTITY.md`, `AGENTS.md`, `USER.md`
- **Allowed tools**: filter both the prompt tool block and the official provider API tool definitions
- **Allowed skills**: filter the prompt skill block only
- **ChatMessage format**: JSONL with fields per role as defined in Section 4
- **CoT storage and round-tripping**: `reasoning` for readable text, `reasoning_meta` for opaque provider data. Mandatory during tool-use loops; old `reasoning_meta` is not resent after completed turns by default.

## Still open

- **Fallback behavior**: exact automatic behavior for `fallback_model`
- **Provider-specific `reasoning_meta` resend after completed turns**

## 5. Phase 3 Server Contract Decisions

These are architectural decisions for the server layer. They intentionally stop
before exact transport payload schemas.

### Client/Server vs. Provider Separation

- **Client ↔ vBot server** is a stable public contract for WebUI, Desktop, CLI,
  and later other accessors.
- **vBot kernel ↔ provider** may differ per provider and stays hidden behind the
  adapter layer.
- Provider transport details do not leak into the external vBot server contract.

### Session and Run

- A **Session** is the persisted chat container for one agent.
- At the product/server level, creating a new session is an explicit action; the
  session file and history are then created and maintained by the system.
- A **Run** is one active execution inside a session: user turn, model work,
  visible thinking, tool calls/results, and assistant output until completion,
  failure, or cancellation.
- `cancel` always targets a **Run**, not a whole session.

### Concurrency

- At most **one active run per session**.
- Multiple sessions may run in parallel.
- Since a session belongs to one agent, multiple agents may work in parallel by
  using different sessions.

### Client Transport Roles

- **RPC over HTTP**: commands and normal request/response actions
- **SSE**: incremental output stream of a single run
- **WebSocket**: general asynchronous server events

The server exposes streaming to clients through SSE, regardless of how a
provider internally implements streaming.

### Visibility Contract

Within the chat experience, the following should be visible to the user when
available:

- assistant thinking/reasoning blocks in readable form
- every tool call as its own visible step
- tool activity / tool results
- every intermediate or final assistant response

Opaque provider-specific `reasoning_meta` remains internal and is not itself a
user-facing contract.

### Shared Execution Model

- `send`, `stream`, and `cancel` are different access modes for the **same** run
  execution model.
- vBot should not grow separate chat engines for non-streaming and streaming.

### Cancel Semantics

- Cancel is **best effort** and should stop ongoing work as quickly as possible.
- It should stop forwarding new model output, prevent new tool steps, try to
  stop the current provider/tool work, and ignore late results that arrive after
  cancellation.
- If a currently running tool cannot be hard-stopped, the cancellation remains
  in effect and the run ends as cancelled as soon as control returns to vBot.

### Persistence Boundary

- The session JSONL file remains the canonical persisted chat history.
- Additional runtime-only coordination state for an active run may live in
  memory.

## 6. Phase 4 WebUI Product Decisions

These are product-level decisions for the first real WebUI. They define how the
UI presents existing server/kernel concepts; they do not replace the Phase 3
server contract.

### App Shell Layout

- The WebUI uses a two-pane layout: navigation on the left, content on the
  right.
- The left navigation contains at least these entries:
  - `Chat`
  - `Agents`
  - `System Prompt`
  - `Settings`
- Additional menu entries may be added later.

### Agents in the Product/UI

- The app always requires at least one agent to exist.
- On first start, the default bootstrap agent is `main` / `Main`.
- The Agents screen allows users to create, edit, and delete agents.
- Deleting an agent is rejected if it would leave the app with zero agents.

### Chat Surface Model

- In the UI, the primary selection is the **Agent**, not the Session.
- In the product model, each agent has one current/active session — this is the
  chat shown in the Chat view.
- The identity of that current/active session is persisted with the agent in
  `current_session_id`.
- `New Session` creates a fresh session for the selected agent and makes that
  new session the active one shown in the UI.
- Starting a new session does **not** delete or overwrite the old session file;
  previous sessions remain persisted as JSONL.
- The WebUI does **not** present a list of old chats/sessions.
- The UI exposes agent selection plus a `New Session` action; session history
  browsing stays out of scope for this product surface.

### Run and Queue Behavior in the Chat UI

- If the selected agent already has a running Run, a newly submitted user
  message for that same current chat is placed into a FIFO queue.
- The queue is scoped to the selected agent's current/active chat.
- Queued messages must be visible in the UI.
- Queued messages must be cancellable/removable in the UI before they are sent.
- `New Session` is not queued. Creating a new session is blocked while the
  current session has a running Run; the user must cancel or wait for the Run to
  finish first.
- Switching to another agent while a Run is active is allowed.

### Accessor-local UI Restoration

- Accessors may remember the last selected agent locally and restore it on the
  next start/reload.
- This last-selected-agent memory is not part of the shared server/domain data
  model and should not be stored in the shared instance data directory.
- For WebUI and Desktop, this preference is low priority and may be implemented
  later in accessor-local storage.

### Relation to the Server Contract

- Sessions remain explicit and persisted at the system/server level.
- The WebUI may create sessions explicitly under the hood, but session IDs are
  not the main user-facing concept in the chat experience.

## 7. Phase 5 CLI Server Management Decisions

These are product-level decisions for the local CLI that manages vBot server
processes. They define the user-visible lifecycle contract, not the internal
implementation details.

### Scope

- Phase 5 adds `server start`, `server stop`, `server restart`, and
  `server status`.
- The CLI is used by both human users and agents, so these commands must remain
  non-interactive and automation-safe.
- The CLI never opens a browser. It prints the resolved server URL instead.

### Local Instance Identity

- For `server start`, the local server instance is identified by its `data_dir`.
- The server port for that instance resolves as `--port` > `VBOT_SERVER_PORT` >
  `settings.json` > `8420`.
- Logs for that instance belong under `<data_dir>/logs/`.

### Start Contract

- `server start` is successful only when the target server is actually reachable
  and `/health` responds.
- A reachable server counts as a **vBot server** only when `GET /health`
  returns the expected vBot health response. In the current contract, that is
  HTTP `200` with JSON body `{ "status": "ok" }`.
- If a vBot server is already running on the target address/port,
  `server start` reports that cleanly instead of starting a second server.
- Phase 5 does not require separate stale PID or launch-metadata recovery
  rules. Live reachability and vBot-server detection are authoritative.

### Stop, Restart, and Status Targeting

- `server stop`, `server restart`, and `server status` are not limited to
  servers that were started by the same CLI invocation.
- These commands may target any already-running local **vBot server** for the
  chosen address/port.
- A non-vBot process occupying the target address/port must not be stopped or
  treated as a restart target.
- If the target address/port is occupied by a non-vBot process,
  `server start`, `server stop`, and `server restart` fail with a clear
  "port occupied by non-vBot process" style error instead of taking action.
- `restart` re-resolves the current start configuration for the chosen
  `data_dir` instead of relying on cached launch arguments.

### Status Contract

- `server status` reports whether a vBot server is reachable at the target
  address/port.
- `server status` output includes at least: running / not running, the resolved
  URL, WebUI available / unavailable, and the resolved `data_dir`.
- If the target address/port is occupied by a non-vBot process,
  `server status` still reports "not running" for vBot but adds a conflict note
  that another service is using the target address/port.
- When reachable, it reports the resolved URL and whether the WebUI is
  available from that server.

### Shutdown Semantics

- Shutdown is best effort: try graceful stop first, wait a bounded timeout,
  then force-stop if the process does not exit.
- On Windows, forced termination may be abrupt. In-flight Runs may be
  interrupted and late work may be ignored.

### WebUI Availability

- Built WebUI assets remain optional at runtime.
- If `webui/dist` is missing, the API server may still start successfully.
- In that case, CLI output must say that the server is running but the WebUI is
  not available.

## 8. Phase 6 Desktop Shell Decisions

These are product-level decisions for the pywebview desktop accessor. They
define the long-lived contract so Phase 6 stays a thin client instead of
growing server-management or native-bridge responsibilities by accident.

### Scope

- Phase 6 adds a pywebview-based desktop shell in `desktop/main.py`.
- The Desktop app is an **Accessor** like WebUI and CLI: it talks to the vBot
  server and does not talk to providers directly.
- Phase 6 is intentionally a thin client, not a second server-management layer.

### Server Relationship

- The Desktop app does **not** start, stop, restart, or otherwise manage a vBot
  server process.
- It connects to an already reachable server at the configured host and port.
- Supported targets in Phase 6 are localhost and LAN-reachable vBot servers.
- No authentication or TLS-specific Desktop behavior is added in Phase 6;
  normal unencrypted HTTP is acceptable for this scope.

### URL and UI Surface

- The Desktop app loads the normal WebUI from the server root path `/`.
- Phase 6 does **not** introduce a separate desktop-only frontend build,
  desktop-only route, or alternate HTML shell.
- The desktop window should therefore behave like an embedded version of the
  existing WebUI accessor, backed by the same server contract.

### Missing WebUI Behavior

- A healthy server without `webui/dist` is still a valid vBot server, as already
  established in the CLI/server contract.
- In that case, the Desktop app does **not** crash or throw an unhandled error.
- Instead, it shows a clear in-window message that the target server does not
  provide a WebUI, including the host/port context when practical.

### Window Lifecycle

- Closing the Desktop window ends the Desktop client process only.
- Closing the Desktop window has no effect on the target vBot server process.
- Phase 6 does not introduce tray behavior, background persistence after close,
  or hidden long-running desktop daemons.

### Desktop-local Settings

- The Desktop app may persist its last-used connection target (at minimum host
  and port) in an accessor-local settings file.
- This settings file belongs to the Desktop app itself and is **not** part of
  the shared server `data_dir`.
- Desktop-local preferences remain separate from shared agent/server state,
  consistent with the accessor-local restoration rule already chosen for WebUI
  and Desktop.

### Native Integration Boundary

- Phase 6 does **not** define a Python↔JavaScript bridge as part of the product
  contract.
- The Desktop app is only an embedded web client in this phase.
- Native OS integrations (tray, file dialogs, notifications, local bridge APIs,
  etc.) remain out of scope unless explicitly introduced by a later phase.
