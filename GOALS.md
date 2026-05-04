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

- **Create**: New agent → `data_dir/agents/<id>/agent.json` + workspace seeded from `resources/workspace-templates/` (`SOUL.md`, `IDENTITY.md`, `AGENTS.md`, `USER.md`). `workspace` field in agent.json defaults to `<data_dir>/workspace-<id>/`.
- **Delete**: Agent deleted → all files (agent.json, sessions, workspace) moved to `archive/<agent-id>/`. Not permanently destroyed — can be inspected or restored.
- **Update**: Any field except `id` can be changed. `id` is immutable (it's the directory name).

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
