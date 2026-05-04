# Chat

Canonical backend chat messages, append-only JSONL sessions, and the chat run
execution model that Phase 3 will expose through the server layer.

## Overview

`core/chat/` owns the provider-agnostic conversation representation used between
the chat layer and provider adapters. Session files are append-only JSONL under
`<data_dir>/agents/<agent-id>/sessions/`, with one canonical message per line.
Provider-specific wire details stay in `core/providers/`; chat should assemble
canonical history and request options only. A Session is the persisted chat
container; a Run is one active execution inside that session.

## Data Model

- `ToolCall` — assistant-requested tool invocation: `id`, `name`, `arguments`.
- `ChatMessage` — persisted canonical message with role-specific fields:
  - common: `id`, `timestamp`, `role`
  - `system`: `model`, `content`
  - `user`: `content`
  - `assistant`: `model`, nullable `content`, nullable `reasoning`, nullable `reasoning_meta`, nullable `tool_calls`
  - `tool`: `tool_call_id`, `name`, `content`
- `reasoning` is readable thinking text. `reasoning_meta` is opaque provider data and must not be interpreted by chat.

## Interfaces

- `ChatMessage.system(content, model)` / `.user(content)` / `.assistant(...)` / `.tool(...)` — constructors for role-specific messages.
- `ChatMessage.to_dict()` / `ChatMessage.from_dict(data)` — canonical JSON-compatible conversion.
- `ChatSession.create(sessions_dir, session_id=None)` — creates an empty session file. Public/server-facing session creation uses a server-generated UUID session ID.
- `ChatSession.append(message)` — appends one compact UTF-8 JSON object plus newline.
- `ChatSession.load()` — returns validated `ChatMessage` objects in file order.
- `ChatSessionManager(data_dir)` — resolves `agents/<id>/sessions/` and creates/gets/lists/deletes sessions.
- `RunEvent` — provider-agnostic visible timeline event for one Run. Payloads must not expose opaque provider fields such as `reasoning_meta`.
- `Run` — active execution state with replayable events, subscription, cancellation request flag, terminal status, and final result/error.
- `ChatRunManager` — starts Runs with one active Run per `(agent_id, session_id)`, stores recent Runs by ID, exposes lookup/cancel, and allows parallel Runs in different Sessions.
- `ChatLoop(runtime, max_tool_iterations=8)` — minimal non-streaming agentic loop.
  - `send(agent_id, content, session_id=None) -> ChatMessage` — loads the agent, validates provider/model split, appends the user message, sends canonical history through the adapter, dispatches allowed tools, and returns the final assistant message.
  - `start_run(agent_id, content, session_id=...) -> Run` — server-facing entry point that requires an existing Session and starts the same execution model in the run manager.

## Phase 3 Server Contract Alignment

- Sessions remain the canonical persisted JSONL history.
- In the public/server contract, creating a new session is an explicit action;
  chat turns should target an already chosen session instead of implicitly
  switching away from the current one.
- A Run is a single execution within a Session and is the unit targeted by
  `stream` and `cancel`.
- At most one Run may be active per Session at a time.
- Multiple Sessions may execute in parallel.
- `send`, `stream`, and `cancel` should remain different access modes over the
  same underlying run execution model.
- Readable `reasoning`, tool calls/results, and assistant outputs are part of
  the visible run timeline; opaque `reasoning_meta` is not.
- Cancellation is best effort: once requested, late non-terminal output is not
  forwarded, new tool dispatch is blocked, and the Run ends as `cancelled`.

## Conventions

- Timestamps are UTC ISO 8601 with an explicit offset.
- UTC timestamps using either `+00:00` or `Z` are accepted when reading persisted messages.
- Session files use `.jsonl` and are append-only during normal chat operation.
- Public/server-facing session identifiers are UUID strings. If lower-level helpers accept custom IDs internally, they must still validate them before path construction.
- Current code can still create a session implicitly when `ChatLoop.send()` is
  called without an existing `session_id`, or create the named session if it
  does not yet exist. This describes current implementation behavior and should
  not be mistaken for the intended public/server product contract.
- Current-turn `reasoning_meta` must be preserved unchanged during tool-use loops. Old `reasoning_meta` is not resent after completed turns by default.
- `agent.model` must be in `<provider>/<model-id>` form. An empty model or missing provider raises `ChatError` before an adapter request.
- The chat loop does not prevalidate model existence in static model resources; unknown model IDs are left for the provider API to reject.
- Tool calls are dispatched only through the runtime tool registry and agent allowlist. Disallowed tools raise before a tool result is appended.
- Adapters returned by runtime are closed after each `ChatLoop.send()` turn when they expose `aclose()`.

## Constraints & Gotchas

- Unknown future fields in session JSON may appear; avoid making chat depend on provider-specific metadata shape.
- Model IDs in messages use user-facing `<provider>/<model-id>` form for traceability, while adapters receive the provider-specific `model_id` part.
- The loop stores user, assistant, and tool messages in session files. The system prompt is assembled for each request rather than appended as normal chat history.
