# Chat

Canonical backend chat messages and chat-loop execution exposed through the
server layer.

## Overview

`core/chat/` owns the provider-agnostic conversation representation used between
the chat layer and provider adapters, plus the chat loop that executes provider
and tool turns. Run lifecycle and queue coordination live in `core/runs/`;
Session persistence lives in `core/sessions/`; compaction/context management
lives in `core/compaction/`; chat should interact with those domains through
their public APIs rather than knowing storage paths or lifecycle internals.
Provider-specific wire details stay in `core/providers/`; chat should assemble
canonical history and request options only. A Session is the persisted chat
container; a Run is one active execution inside that session.

## Data Model

- `ToolCall` — assistant-requested tool invocation: `id`, `name`, `arguments`. Tool-call index is derived from the assistant message order for runtime lifecycle events.
- `ChatMessage` — persisted canonical message with role-specific fields:
  - common: `id`, `timestamp`, `role`
  - `system`: `model`, `content`
  - `user`: `content` (`str` or `list[ContentBlock]`)
  - `assistant`: `model`, nullable `content`, nullable `reasoning`, nullable `reasoning_meta`, nullable `tool_calls`
  - `tool`: `tool_call_id`, `name`, `content`
  - `note`: `content`; kernel-internal background note persisted in the Session, not shown as a normal chat message
  - `compaction_checkpoint`: `content` (summary text), `tail_boundary_id`, optional `usage.compacted_token_count`; persisted summary anchor used to rebuild shorter future request history
  - `error`: `content`, `error_kind`; persisted run-time failure visible in normal history
- `reasoning` is readable thinking text. `reasoning_meta` is opaque provider data and must not be interpreted by chat.
- Activated skill context is persisted as a special internal `note` whose content begins with `[skill-context] `. These notes are not converted to `<system-reminder>` blocks; instead the chat loop restores them as `<skill_content>` user-context messages before provider requests.

## Interfaces

- `ChatMessage.system(content, model)` / `.user(content)` / `.assistant(...)` / `.tool(...)` / `.note(content)` / `.compaction_checkpoint(summary, tail_boundary_id, compacted_token_count)` / `.error(error_kind, content)` — constructors for role-specific messages.
- `ChatMessage.to_dict()` / `ChatMessage.from_dict(data)` — canonical JSON-compatible conversion.
- `error_kind_llm_visible(kind)` — returns whether a persisted error should be embedded into the next provider request.
- Session persistence interfaces live in `.vorch/specs/sessions.md` and are exported from `core.sessions`.
- Run lifecycle types (`Run`, `RunEvent`, `ChatRunManager`, queue items, and Run errors) live in `.vorch/specs/runs.md` and are exported from `core.runs`.
- `RunEvent` — provider-agnostic visible timeline event for one Run. Payloads must not expose opaque provider fields such as `reasoning_meta`.
- `compaction_completed` is a visible Run event carrying `{ message }` after auto-compaction appends a `compaction_checkpoint` during a Run.
- `model_fallback_activated` is a visible Run event with payload `{ from_model, to_model }` emitted when the chat loop switches from the agent's primary model to its configured fallback model within the current Run.
- `core.runs.Run` — active execution state with replayable events, subscription, cancellation request flag, terminal status, and final result/error.
- `core.runs.ChatRunManager` — starts Runs with one active Run per `(agent_id, session_id)`, stores recent Runs by ID, exposes lookup/cancel, supports `cancel_by_session(agent_id, session_id)` for pre-run command handling, allows parallel Runs in different Sessions, and owns the in-memory FIFO queue of pending `QueuedRunItem`s per Session with enqueue/list/remove/update plus automatic drain when the active Run finishes.
- `CommandDispatcher` — shared pre-run slash-command router for pure-text messages. Recognized built-ins return a handled result with optional reply text; unknown slash text falls through so normal chat behavior, including skill activation, stays intact. Current built-ins include `/stop` and `/compact`.
- Streaming Run events: `assistant_output_delta`, `reasoning_delta`, `tool_call_delta`, `tool_call_stdout`, and `tool_call_stderr` are transient visible Run events used for SSE streaming only. They receive normal monotonically increasing Run sequence numbers, are not persisted to JSONL session files, and must not contain opaque `reasoning_meta`.
- Tool lifecycle Run events: `tool_call_started` has payload `{ tool_call: { id, index, name, arguments }, display: { summary, hidden_argument_keys } }`; `tool_call_result` has payload `{ tool_call: { id, index, name }, result }`, where `result` is the stable tool result envelope. Tool failures use `tool_call_result` with `result.ok = false`; there is no public `tool_call_failed` event.
- Error persistence Run event: `error_message_persisted` has the same message payload shape as other output-message events and indicates that a `role: "error"` message was appended to the Session.
- `ChatLoop(runtime, max_tool_iterations=1000, streaming=False, attachment_resolver=None, compaction_service=None)` — agentic loop with non-streaming and streaming modes over the same Run/session/tool dispatch infrastructure. The optional compaction service comes from `core.compaction`.
  - `send(agent_id, content, session_id=None) -> ChatMessage` — loads the agent, validates model and connection, appends the user message, sends canonical history through the adapter, dispatches allowed tools, and returns the final assistant message.
  - `start_run(agent_id, content, session_id=..., internal=False) -> Run` — server-facing entry point that requires an existing Session and starts the same execution model in the run manager. Internal runs persist `content` as a `role: "note"` system reminder rather than a visible `role: "user"` message.
  - `queue_run(agent_id, content, *, session_id, internal=False) -> QueuedRunItem` — validates the same agent/provider/session prerequisites as `start_run(...)`, derives a display preview for the queued message, and delegates busy-session enqueue/start behavior to `ChatRunManager`.
- `core/chat/content_blocks.py` owns `TextBlock`, `MediaBlock`, `FileBlock`, plus dict round-trip helpers for persisted JSONL content lists.
- `core/chat/block_resolver.py` owns last-mile attachment resolution from persisted content blocks to provider-ready dicts just before adapter calls.

## Server Contract Alignment

- Sessions remain the canonical persisted history. The current storage format is
  append-only JSONL owned by `core/sessions/`.
- In the public/server contract, creating a new session is an explicit action;
  chat turns should target an already chosen session instead of implicitly
  switching away from the current one.
- A Run is a single execution within a Session and is the unit targeted by
  `stream` and `cancel`.
- At most one Run may be active per Session at a time.
- Multiple Sessions may execute in parallel.
- Busy-session follow-up work may be enqueued through `ChatRunManager`; queued items are in-memory FIFO per Session and start automatically when the active Run clears.
- `send`, `stream`, and `cancel` should remain different access modes over the
  same underlying run execution model.
- In streaming mode, provider adapters yield normalized deltas that the chat
  loop accumulates into the final canonical assistant message. The final
  message is persisted at the same turn boundary as non-streaming and remains
  authoritative over transient deltas.
- Readable `reasoning`, tool calls/results, and assistant outputs are part of
  the visible run timeline; opaque `reasoning_meta` is not.
- Tool calls from the same assistant turn execute concurrently. The next model
  request waits until every sibling tool call reaches a terminal result.
- Tool result messages are persisted in the assistant's original tool-call order,
  even when lifecycle result events complete and stream in a different order.
- Cancellation is best effort: once requested, late non-terminal output is not
  forwarded, new tool dispatch is blocked or suppressed, and the Run ends as
  `cancelled`.
- Run cancellation also calls the runtime `ProcessManager.cancel_scope(run.id)`
  hook so active host processes started by tools in that Run are killed.
- Built-in slash commands are intercepted before `ChatLoop.start_run()` only
  for pure-text messages. Attachment-bearing or multi-block messages bypass
  command dispatch and continue through the normal Run path.

## Conventions

- Timestamps are UTC ISO 8601 with an explicit offset.
- UTC timestamps using either `+00:00` or `Z` are accepted when reading persisted messages.
- Public/server-facing session identifiers are UUID strings. Storage-level ID
  validation and path construction rules are owned by `core/sessions/`.
- Current code can still create a session implicitly when `ChatLoop.send()` is
  called without an existing `session_id`, or create the named session if it
  does not yet exist. This describes current implementation behavior and should
  not be mistaken for the intended public/server product contract.
- Only user messages may persist `list[ContentBlock]` content. System, assistant,
  tool, note, and error messages remain string-or-null content only.
- Current-turn `reasoning` and `reasoning_meta` must be preserved unchanged during tool-use loops when the same assistant turn continues after tool results. Old completed-turn `reasoning` and `reasoning_meta` are not resent on later turns by default.
- Notes are kernel-internal background events. They remain in JSONL history as `role: "note"` but are embedded into provider requests as synthetic user messages containing one or more `<system-reminder>...</system-reminder>` blocks. Provider adapters must never receive `role: "note"`.
- `role: "compaction_checkpoint"` stays in the Session JSONL history but is never sent directly to providers. When the chat loop sees the latest checkpoint, it rebuilds request history as system prompt + skill context + one synthetic `<system-reminder>` summary message + the verbatim tail starting at `tail_boundary_id`.
- Notes generated during a tool-use turn must not appear between an assistant message with `tool_calls` and that turn's tool-result messages, either in JSONL persistence or in the provider request history. Such notes are deferred until after the last tool result for that assistant turn.
- Auto-compaction is evaluated only at safe turn boundaries: after a final assistant response with no pending tool calls, or after a full tool-result cycle has completed. The preserved tail boundary must always begin at a user-turn boundary; compaction never splits an open tool cycle.
- Failed Runs may append `role: "error"` messages to JSONL history. `error_kind` must be non-empty when writing; unknown future `error_kind` values are accepted on read. LLM-visible error kinds are embedded into later provider requests as `<system-reminder>` blocks; non-visible error kinds stay in history/UI only.
- Skill-context notes are kernel-internal persistence records. They remain in JSONL history as `role: "note"`, are filtered from normal history, and are restored into provider requests as `<skill_content>` context messages rather than `<system-reminder>` blocks.
- User messages can trigger deterministic skill activation before provider requests with `/skill-name` at the start of the message or `$skill-name` anywhere in the message. The original user message is preserved unchanged.
- `$skill-name` is always interpreted as a skill activation hint, never as a built-in command route. If it matches an allowed loadable skill, the chat loop injects that skill's persisted `<skill_content>` context before the original user message; if it does not match, the original message remains unchanged and an internal system reminder explains that the trigger did not match an allowed loadable skill.
- Recognized built-in slash commands (currently `/stop` and `/compact`) are not part of that
  skill-activation path. They are handled earlier by the shared command
  dispatcher; unrecognized slash text still reaches the existing skill-trigger
  logic unchanged.
- Normal server history responses and the standard WebUI timeline must filter out notes; only debug-specific surfaces may expose them intentionally.
- Consecutive notes in loaded history are grouped into one synthetic user message. Notes added while a Run is active are drained before each model request, including follow-up requests after tool results, but note embedding must still preserve immediate assistant-tool adjacency within a tool-call sequence.
- If a Session later continues with a different provider, stale `reasoning_meta`
  from the old provider must never be sent to the new provider.
- `agent.model` must be in `<provider>/<model-id>` form and may optionally carry `::<connection-local-id>` at the end. An empty model or missing provider raises `ChatError` before an adapter request.
- Runtime target resolution parses `agent.model` with `rpartition("::")`. When a suffix is present, the chat loop reconstructs the full connection ID as `<provider>:<connection-local-id>`. Without a suffix, the chat loop falls back to the first usable connection for the model provider in provider-config order.
- Queued display previews are derived from plain-string content or the concatenated text of `TextBlock` items and fall back to `[attachment]` when a queued payload has no text blocks.
- If a retryable `ProviderError` escapes adapter retries and the Agent has a resolvable `fallback_model`, the chat loop may switch to that fallback for the rest of the current Run. The switch emits `model_fallback_activated` and persists a note so the next provider request sees the change as a `<system-reminder>`. The Agent config itself is not mutated, so the next turn starts from the primary model again.
- `fallback_model` follows the same optional `::<connection-local-id>` convention as `model`; fallback resolution uses that suffix when present and otherwise auto-resolves the first usable connection for the fallback provider.
- Run-local model fallback is part of the shared ChatLoop execution path and therefore applies equally to direct and internal Runs that execute through that path.
- The chat loop does not prevalidate model existence in static model resources; unknown model IDs are left for the provider API to reject.
- Surfaces that need the catalog model key rather than the pinned connection form, such as context-window lookup or display helpers, must strip the optional `::<connection-local-id>` suffix first.
- Attachment resolution happens in the chat layer, not inside provider adapters.
  Current-turn `MediaBlock` image attachments become base64 provider-neutral media dicts;
  historical media become text placeholders; `FileBlock` becomes a text note with
  MIME type and local path; `TextBlock` stays embedded text.
- Vision capability checks also happen in the chat layer. An image attachment sent
  to a non-vision model raises a clear `ChatError`; there is no silent fallback.

## Token Usage

- Provider adapters extract token usage from responses: OpenAI maps `prompt_tokens`/`completion_tokens` to canonical `input_tokens`/`output_tokens`; Anthropic maps directly from `usage.input_tokens`/`usage.output_tokens`. If a provider doesn't supply usage (e.g., local providers without usage reporting), the backend falls back to a 4-chars-per-token estimation via `estimate_tokens()` in `core/utils/tokens.py` and marks the result with `"estimated": true`.
- `ChatMessage.assistant()` accepts an optional `usage: JsonObject | None` field (canonical keys: `input_tokens`, `output_tokens`; optional `estimated` boolean). Usage is only valid on assistant messages and is rejected on other roles by `from_dict()`.
- `_message_to_request_dict()` strips `usage`, `reasoning`, and `reasoning_meta` from assistant history before it is sent to provider APIs. Readable `reasoning` still round-trips for reasoning-aware adapters on the active tool-continuation path, but stale completed-turn reasoning must not be resent on later follow-up turns.
- The `run_completed` event payload includes `usage` from the final assistant message when available. Terminal events for failed or cancelled runs do not include usage.
- In streaming mode, usage arrives as a `{"type": "usage", ...}` delta. OpenAI sends it only in the final streaming chunk (with `stream_options.include_usage`). Anthropic splits it across `message_start` (input_tokens) and `message_delta` (output_tokens). The `StreamingAccumulator` collects these deltas; `finalize_assistant_fields()` includes usage in the response dict.
- GitHub Copilot endpoint helpers must emit the same normalized streaming shapes
  as other adapters. Copilot `/responses` usage is normalized from completed
  response events or response payloads. Copilot `/v1/messages` follows the
  Anthropic-like message usage shape when available. Raw provider SSE events and
  opaque reasoning metadata stay inside adapters and must not be exposed as Run
  event payloads.
- Tool calls are dispatched only through the runtime tool registry and agent allowlist. Normal tool execution failures, including disallowed or unknown tools, are appended as failed result envelopes so the assistant can recover.
- Adapters returned by runtime are closed after each `ChatLoop.send()` turn when they expose `aclose()`.

## Constraints & Gotchas

- Unknown future fields in session JSON may appear; avoid making chat depend on provider-specific metadata shape.
- Model IDs in messages use user-facing `<provider>/<model-id>` form for traceability, while adapters receive the provider-specific `model_id` part.
- The loop stores user, assistant, tool, and note messages in session files. The system prompt is assembled for each request rather than appended as normal chat history.
