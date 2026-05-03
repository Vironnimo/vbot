# Phase 2 Goals — Contracts

Items not yet decided are marked **(open)**. This document captures how things
*will* look, not how they'll be built (see ROADMAP for the build plan).

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
- `fallback_model`: empty = no fallback
- `workspace`: absolute path to the agent's workspace directory. Default on creation is
  `<data_dir>/workspace-<id>/`. User can override to a custom path.
- `thinking_effort`: `none` / `minimal` / `low` / `medium` / `high` / `xhigh` / `max` —
  reasoning effort level. Empty string = provider default. Each adapter translates this
  into its provider's wire format.
- `allowed_tools` / `allowed_skills`: `["*"]` = all, otherwise explicit list
- `created_at` / `updated_at`: ISO 8601, explicit UTC offset

## Agent Lifecycle

- **Create**: New agent → `data_dir/agents/<id>/agent.json` + workspace seeded from `resources/workspace-templates/` (the four files). `workspace` field in agent.json defaults to `<data_dir>/workspace-<id>/`.
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
You are an agent for vControl, App version: {app_version}.
Use the instructions below and the tools available to you to assist the user.

{runtime}

{tools}

{skills}

{include:AGENTS.md}
{include:USER.md}
```

- `{app_version}`: application version
- `{runtime}`: runtime info **(open)**
- `{tools}`: injected tool snippet
- `{skills}`: injected skill snippet
- `{include:<filename>}`: content of the workspace file inserted inline

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
    <path>C:\Users\Viro\AppData\Local\vControl\skills\agent-cli\SKILL.md</path>
  </skill>
  <skill>
    <name>get-news</name>
    <description>Fetch current news via RSS feeds...</description>
    <path>C:\Users\Viro\AppData\Local\vControl\skills\get-news\SKILL.md</path>
  </skill>
</available_skills>
```

- `<name>`: skill identifier
- `<description>`: short description of what the skill does and when to use it
- `<path>`: absolute path to the skill's `SKILL.md`

## 4. Session Storage

Session files are JSONL — one message per line, append-only, crash-safe (at most the last line is lost).

- **Filename**: `<data_dir>/agents/<id>/sessions/<uuid>.jsonl` — UUID as session identifier
- **Encoding**: UTF-8, one JSON object per line, no trailing commas, no pretty-printing

### ChatMessage Format

Every line is a ChatMessage. This is the canonical message type between the chat layer and the adapter — no separate schema needed.

```jsonl
{"id":"a1b2c3","timestamp":"2026-05-03T14:30:00Z","role":"system","model":"anthropic/claude-sonnet-4","content":"You are an agent for..."}
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
- `reasoning`: readable thinking text from the model. Stored for search, display, and as context for any provider.
- `reasoning_meta`: opaque provider-specific data (Anthropic `signature`/`redacted_thinking`, OpenAI `encrypted_content`, Gemini `thoughtSignature`, OpenRouter `reasoning_details`). The adapter serializes this from the API response and uses it for round-tripping. The chat layer never interprets it. Unknown fields are ignored — new fields can be added later without breaking old files.

### CoT Round-Trip Rules

- **Same provider, same session**: Adapter round-trips `reasoning_meta` opaque data unchanged — full reasoning continuity.
- **Different provider, same session**: Adapter skips stale `reasoning_meta` from the old provider. The `reasoning` text stays in the message and can be sent as normal context. Reasoning continuity is lost for the old provider, but the new provider starts fresh.
- **Tool-use loop (mandatory)**: CoT data from the current assistant turn must be sent back unchanged with the tool result. Dropping opaque data breaks model continuity (Anthropic, Gemini 3) or loses reasoning context (OpenAI).
- **After a completed turn (fresh user message)**: Provider-dependent. Anthropic recommends sending all previous thinking blocks. Other providers may not require it. This is not yet decided for vBot — the session stores everything, but what gets sent is an adapter decision we'll refine per provider.

## Decided

- **Reasoning effort levels**: `none` / `minimal` / `low` / `medium` / `high` / `xhigh` / `max` — single string in agent schema, adapters translate to wire format
- **System prompt `{runtime}` block**: will contain app version, OS, workspace path, etc. Exact content not decided yet — but the mechanism needs to work from the start (placeholder is fine, what matters is that it's assembled and injected).
- **Fallback behavior**: model fails + no fallback model → error. Simple.
- **ChatMessage format**: JSONL with fields per role as defined in Section 4
- **CoT storage and round-tripping**: `reasoning` for readable text, `reasoning_meta` for opaque provider data. Mandatory during tool-use loops, provider-dependent after completed turns.

## Still open

- **Tool snippet format**: how tool documentation looks