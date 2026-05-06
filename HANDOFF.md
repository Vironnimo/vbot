# Streaming Handoff

Everything you need to know about implementing real token-by-token streaming in vBot. This is the next major feature after the persistent WebSocket connection.

## Current State (What Exists)

### Backend — Non-Streaming Only

The entire chat loop is non-streaming. The critical line is in `core/chat/chat.py`:

```python
# Line 474 in _send_assistant_request:
response = await adapter.send(messages, model_id=model_id, ...)
```

`adapter.send()` waits for the **complete** provider response before returning. The user sees nothing until the entire response is ready.

### Backend — What DOES Exist for Streaming

- `ProviderAdapter.stream()` — Both `OpenAICompatibleAdapter` and `AnthropicAdapter` have working `stream()` methods that return `AsyncIterator[dict]` yielding chunk-by-chunk SSE data from the provider. This is already built and working.
- `Run.emit()` — The event system on `Run` objects can emit events at any time and they're delivered over SSE to the client. This works.
- SSE endpoint `GET /api/runs/{run_id}/events` — Already streams `RunEvent` objects to the frontend. Works.
- `subscribeRunEvents()` in the frontend — Opens an EventSource, receives events, calls `appendRunEvent()`. Works.

### Frontend — Whole-Message Rendering

- `ChatTimeline.svelte` renders messages and events as complete blocks. There is no "growing text" rendering mode.
- `chatState.js` has `appendRunEvent()` which adds events to `runEvents[]`. It replaces/finds duplicates by sequence number but does not accumulate deltas.
- The events currently emitted during a run are whole-message events: `reasoning` (complete thinking text), `assistant_output` (complete assistant text), `tool_call_started` (complete tool call with all arguments).

### Event Flow Today

```
User sends message
  → adapter.send() waits for COMPLETE response
  → ChatLoop gets whole ChatMessage
  → Emits: user_message_persisted, reasoning (whole text), assistant_output (whole text), tool_call_started, tool_call_result
  → If tool_calls: loop again with adapter.send()
  → Emits: run_completed/run_failed
  → SSE delivers all events (most arrive in rapid succession since they were all emitted after the response)
```

The user sees: nothing for seconds, then the entire response appears at once.

## What We Want

### Token-by-Token Streaming

The text should appear character by character (or chunk by chunk) as the provider sends it, exactly like ChatGPT, Claude, etc. This includes:

1. **Assistant text** — The main response text streams token by token
2. **Reasoning/thinking text** — If the model sends thinking blocks, they should stream in real-time too
3. **Tool call names and arguments** — As the model decides to call a tool, the tool name appears, then the arguments build up
4. **Chronological correctness** — Everything appears in the order the provider sends it. If the model thinks first, then writes text, then calls a tool, the user sees exactly that sequence in real-time

### What the User Experience Should Feel Like

- User sends message
- Immediately: a typing/streaming indicator appears
- Tokens appear one by one (or in small chunks)
- If the model goes into a thinking phase: a collapsible "Thinking..." block appears and streams
- If the model calls a tool: tool name appears, then "running...", then the result
- If the model loops (thinks → tool → thinks again): each phase streams live
- When the run is done: final message is persisted to the session and rendered as a normal message

### Compatibility Goal

Streaming must be implemented as a **best-effort capability across providers and models**, not as a narrow happy path for only OpenAI and Anthropic. vBot should support as many configured providers/models as possible:

- If a provider/model streams text deltas, show them live.
- If it streams reasoning deltas, show them live.
- If it streams tool-call fragments, accumulate them live.
- If it only returns complete tool calls, show the tool call when it is complete.
- If a provider/model cannot stream at all or rejects a streaming request, `chat.stream` should gracefully fall back to the existing non-streaming path and still return the final message through the Run/SSE flow.
- Provider-specific gaps should degrade the experience, not break the Run, unless the provider request itself genuinely fails.

This matters because vBot is intended to work with many providers and models: OpenAI, Anthropic, OpenRouter, Groq, Together, local/OpenAI-compatible servers, and future adapters. Do not assume every "OpenAI-compatible" provider behaves identically.

## What We Do NOT Want

- **Do NOT remove `chat.send` (non-streaming)** — Keep it. It's useful for tests, CLI, scripts, and simple responses where streaming is overkill. The RPC method `chat.send` returns the complete assistant message as before.
- **Do NOT remove the existing event types** — `run_started`, `run_completed`, `run_failed`, `run_cancelled` stay exactly as they are. These are terminal/lifecycle events, not content events.
- **Do NOT change the two-channel architecture** — SSE is for per-Run streaming. WebSocket is for app-wide signalling. This was just implemented and is correct.
- **Do NOT stream opaque provider metadata** — The existing rule from `.vorch/specs/server.md`: "Public history, Run, SSE, and WebSocket payloads must strip opaque provider metadata such as `reasoning_meta` recursively." Delta events are no exception.
- **Do NOT persist delta events** — Deltas are ephemeral. Only the final complete `ChatMessage` is persisted to the JSONL session file, exactly as today.

## Architecture: How Streaming Should Work

### Two Modes on the Same Run Infrastructure

| | `chat.send` (non-streaming) | `chat.stream` (streaming) |
|---|---|---|
| RPC method | `POST /api/rpc` with `method: "chat.send"` | `POST /api/rpc` with `method: "chat.stream"` |
| Returns | Complete `ChatMessage` in the RPC response | `run_id` + SSE URL immediately |
| Internal | `ChatLoop._send_assistant_request()` calls `adapter.send()` | `ChatLoop._send_assistant_request()` calls `adapter.stream()` and emits deltas |
| Events | Whole-message events emitted after completion | Delta events emitted during streaming, whole-message events at turn boundaries |
| Session | Message persisted at end of each turn | Same — message persisted at end of each turn |

Fallback behavior: `chat.stream` is the public streaming access mode, but it must not require every provider/model to support true streaming. If the adapter reports that streaming is unsupported or the provider rejects a streaming-only request in a recognizable way, the streaming Run falls back to `adapter.send()`, emits the same whole-message events as today, and completes normally. This keeps the WebUI and Run lifecycle consistent even for models that cannot stream.

The key insight: both modes use the same `Run`, the same `ChatRunManager`, the same SSE endpoint, the same `_send_until_final()` loop. The only difference is what happens inside `_send_assistant_request()`.

`ChatLoop` gets a `streaming` constructor parameter (default `False`). The server delegates use this — `_send_chat` creates a non-streaming `ChatLoop`, `_stream_chat` creates one with `streaming=True`. The mode is determined by the RPC method, not by a user setting.

### Event Design: Delta Events

New event types for streaming content:

| Event Type | Payload | Channel | When |
|---|---|---|---|
| `assistant_output_delta` | `{ "content_delta": " token" }` | SSE only | Each text chunk from the provider |
| `reasoning_delta` | `{ "reasoning_delta": " thinking" }` | SSE only | Each thinking chunk from the provider |
| `tool_call_delta` | `{ "tool_call_id": "...", "name_delta": "read", "arguments_delta": "{\"" }` | SSE only | Each tool call chunk from the provider |
| `tool_call_started` | `{ "tool_call": { "id": "...", "name": "read_file", "arguments": {...} } }` | SSE + WS | When a complete tool call is assembled (stays the same) |
| `assistant_output` | `{ "message": { ... complete message ... } }` | SSE + WS | At the end of a turn, the complete message (stays the same for finalization) |

Decision: we use **separate `_delta` event types** — not a `stream: true` flag on existing event types. The separate types are easier to handle in the frontend (clear distinction between "this is a partial chunk" and "this is the complete thing") and they don't break existing non-streaming consumers.

Decision: **delta events go on SSE only, NOT on WebSocket.** Delta events are too chatty for WebSocket (dozens per second). WebSocket continues to get whole-message `run_output` events (`reasoning`, `assistant_output`, `tool_call_started`, `tool_call_result`). Delta events are NOT added to `RUN_OUTPUT_EVENT_TYPES` or `SERVER_EVENT_TYPES`. A WebSocket client that's only listening for run lifecycle summaries won't be flooded with tokens.

**Important design decision**: The complete `assistant_output` event is still emitted at the end of each turn, AFTER all deltas. This gives the frontend a clear signal: "the streaming for this turn is done, here's the complete message." The frontend can use this to replace its accumulated delta text with the authoritative final message.

Tool-call UX decision: `tool_call_delta` may include raw `arguments_delta` fragments for chronological accumulation, but the normal UI should not show broken partial JSON as if it were final data. Show the tool name/status as soon as possible, display a "preparing arguments"/running state while arguments are incomplete, and show parsed arguments only after the complete `tool_call_started` event is emitted. Raw argument fragments may be kept internally for accumulation or shown only in a debug/details affordance later.

Event type constants live in `core/chat/runs.py` as the single source of truth:
- `ASSISTANT_OUTPUT_DELTA_EVENT = "assistant_output_delta"`
- `REASONING_DELTA_EVENT = "reasoning_delta"`
- `TOOL_CALL_DELTA_EVENT = "tool_call_delta"`

`server/delegates.py` and `webui/src/lib/api.js` reference these constants. No hardcoded strings.

### The Streaming Agentic Loop

This is the hardest part. Currently the loop looks like:

```python
# Current: non-streaming
for _ in range(max_iterations + 1):
    assistant_message = await adapter.send(messages, ...)  # WAIT for complete response
    session.append(assistant_message)  # Persist complete message
    run.emit("assistant_output", {...complete message...})  # Whole event
    if not assistant_message.tool_calls:
        return assistant_message  # Done
    tool_results = await dispatch_tools(...)
    session.append(tool_results)
    messages.extend([assistant_message, tool_results])
```

`_send_assistant_request()` currently has no access to `run` — it only takes `(agent, adapter, model_id, messages, tools)`. For streaming, it needs `run` to emit delta events. The `run` parameter must be added to its signature.

In streaming mode, `_send_assistant_request()` emits ALL events itself — deltas during the stream, and the whole-message events (`reasoning`, `assistant_output`, `tool_call_started`) at the end of the turn. The existing `_emit_assistant_events()` helper is only called in the non-streaming path. The caller (`_send_until_final()`) persists the message and dispatches tools the same way in both modes.

In streaming mode, it needs to become:

```python
# Streaming: accumulate chunks, emit deltas, then finalize
accumulated_content = ""
accumulated_reasoning = ""
accumulated_reasoning_meta = None  # opaque provider data, never public
accumulated_tool_calls = {}  # id -> {name, arguments_str}
current_tool_call_id = None

async for chunk in adapter.stream(messages, ...):
    run.raise_if_cancelled()  # check cancellation mid-stream

    # Each chunk is a NORMALIZED delta dict from the adapter
    if chunk has content_delta:
        accumulated_content += content_delta
        run.emit("assistant_output_delta", {"content_delta": content_delta})
    if chunk has reasoning_delta:
        accumulated_reasoning += reasoning_delta
        run.emit("reasoning_delta", {"reasoning_delta": reasoning_delta})
    if chunk has reasoning_meta:
        # Opaque internal provider data. Preserve but never emit publicly.
        accumulated_reasoning_meta = merge_or_replace_provider_meta(...)
    if chunk has tool_call_delta:
        # Accumulate tool call arguments
        ...accumulate and emit tool_call_delta...

# Streaming turn complete — build the final ChatMessage
assistant_message = ChatMessage.assistant(
    model=model,
    content=accumulated_content or None,
    reasoning=accumulated_reasoning or None,
    reasoning_meta=provider_reasoning_meta,  # preserved for round-tripping
    tool_calls=assembled_tool_calls or None,
)
session.append(assistant_message)
run.emit("assistant_output", {"message": visible_message_dict})  # Final complete event

if not assistant_message.tool_calls:
    return assistant_message

# Tool dispatch, then loop again for the next streaming turn
tool_results = await dispatch_tools(...)
session.append(tool_results)
run.emit("tool_call_result", {...})
messages.extend([assistant_message.to_dict(), tool_results...])
```

**Critical detail**: `reasoning_meta` must be preserved across tool-use loops for provider round-tripping. The streaming accumulator must track it. The visible delta events must NOT include `reasoning_meta` (stripping rule still applies).

Clarification: this does **not** change the existing specs. `.vorch/specs/chat.md` already defines `reasoning_meta` as opaque provider data that chat must not interpret, and `.vorch/specs/server.md` already forbids exposing it in public payloads. Streaming extends that rule: adapters may yield internal metadata deltas or final metadata objects so the ChatLoop can store them on the final `ChatMessage.reasoning_meta`, but these internal chunks are never converted into Run events, SSE payloads, WebSocket payloads, or visible history.

Recommended normalized internal metadata shape:

```python
{ "type": "reasoning_meta", "reasoning_meta": {...} }
```

or, if simpler for an adapter:

```python
{ "type": "finish", "reason": "stop" | "tool_calls", "reasoning_meta": {...} }
```

The ChatLoop only stores this opaque object. It must not branch on provider-specific keys like Anthropic `signature`, OpenRouter `encrypted_content`, or `reasoning_details`. When the next tool-use request is built, the adapter remains responsible for translating the canonical `reasoning_meta` back into the provider wire format.

**Another critical detail**: Tool call arguments arrive as string fragments that must be concatenated. The complete JSON arguments string is only available when the provider signals the end of the tool call. Only then can the arguments be parsed and validated, and `tool_call_started` emitted.

**The `finish` signal**: The adapter yields a `{ "type": "finish", "reason": "stop" | "tool_calls" }` delta when the stream ends. This tells the accumulator whether the response is complete or whether tool calls are coming. Without it, the loop can't distinguish "response ended, no tool calls" from "response ended, tool calls still being assembled." OpenAI encodes this as `choices[0].finish_reason`, Anthropic as `stop_reason` on `message_delta`.

Provider compatibility note: finish reasons are not perfectly standardized. The adapter should normalize known equivalents into `"stop"` or `"tool_calls"`, tolerate unknown non-error finish reasons by treating them as `"stop"` when no tool calls are pending, and preserve enough logging/debug information to diagnose provider-specific behavior. Missing finish events should not lose already accumulated content; the stream end can finalize as `"stop"` if no pending tool call requires more data.

**Cancellation mid-stream**: `run.raise_if_cancelled()` is called between chunks. Late chunks that arrive after cancellation are ignored. The existing `cancel_requested` guard works for this. The implementation should also make sure the provider stream/HTTP response is closed when cancellation is observed, so a cancelled Run does not keep an idle provider connection open until the next chunk arrives.

### Provider Adapter Differences

Each provider streams differently. The adapter's `stream()` method handles this:

- **OpenAI-compatible**: Streams `choices[0].delta.content` for text, `choices[0].delta.tool_calls[i]` for tool call fragments. Each chunk is a JSON dict. The `finish_reason` field (`"stop"` or `"tool_calls"`) becomes the `finish` delta.
- **Anthropic**: Uses `content_block_start`, `content_block_delta`, `content_block_stop` events for both text and tool use. The event types carry the delta information. `message_delta` with `stop_reason` (`"end_turn"` or `"tool_use"`) becomes the `finish` delta. `message_stop` signals stream end.

Decision: **the adapter normalizes deltas.** Each adapter's `stream()` method yields normalized delta dicts, not raw provider chunks. The ChatLoop never sees provider-specific chunk formats. This keeps the chat layer provider-agnostic, which is the whole point of the adapter abstraction.

Normalized delta types:

```
{ "type": "content_delta", "text": " token" }
{ "type": "reasoning_delta", "text": " thinking" }
{ "type": "tool_call_delta", "id": "...", "name_delta": "...", "arguments_delta": "..." }
{ "type": "reasoning_meta", "reasoning_meta": {...} }  # internal only, never public
{ "type": "finish", "reason": "stop" | "tool_calls" }
```

The `reasoning_meta` delta type is internal to the adapter/chat boundary. It is not a Run event type and must never be emitted through SSE or WebSocket.

Provider compatibility rules for adapters:

- Text streaming is optional per provider/model. Absence of text deltas is valid.
- Reasoning streaming is optional. Absence of reasoning deltas is valid even for reasoning-capable models.
- Tool-call streaming is optional. If only complete tool calls are available, yield enough data for the ChatLoop to build the final message and emit `tool_call_started` at completion.
- Some OpenAI-compatible providers omit tool-call IDs in deltas. Generate a stable internal ID when needed so accumulation and later tool results still have a valid `tool_call_id`.
- Some providers send tool-call fragments keyed by index, not ID. Track by index inside the adapter and expose stable IDs to ChatLoop.
- Unknown provider-specific chunk fields should be ignored unless they are required to preserve opaque metadata. Do not leak raw provider chunks upward.

**Anthropic content block tracking**: Anthropic streams numbered content blocks. A typical stream looks like:

```
content_block_start(index=0, type="thinking")
content_block_delta(index=0, thinking_delta="Ich...")
content_block_stop(index=0)
content_block_start(index=1, type="text")
content_block_delta(index=1, text_delta="Die Antwort...")
content_block_stop(index=1)
content_block_start(index=2, type="tool_use", id="tool_123", name="read_file")
content_block_delta(index=2, input_delta='{"path":"')
content_block_delta(index=2, input_delta='file.txt"}')
content_block_stop(index=2)
message_delta(stop_reason="tool_use")
message_stop
```

The adapter tracks block index and type internally via `content_block_start`. When it receives `content_block_delta`, it looks up the current block type to determine whether to emit `reasoning_delta`, `content_delta`, or `tool_call_delta`. The ChatLoop sees zero Anthropic-specific structure.

Currently both adapters' `stream()` methods yield **raw provider-specific chunks**. They must be updated to yield normalized deltas instead.

### Frontend: Delta Accumulation

`chatState.js` needs a new concept: the "streaming buffer." It should preserve chronological order, not only maintain three independent strings. During a streaming run:

```javascript
// New state in sessionState:
streamingItems: [
  { type: "reasoning", content: "..." },
  { type: "assistant", content: "..." },
  { type: "tool_call", id: "...", name: "...", argumentsText: "...", complete: false }
]
```

When a delta event arrives:
- `assistant_output_delta`: append `content_delta` to the current trailing assistant streaming item, or create a new assistant item if the previous streaming item is a different type.
- `reasoning_delta`: append `reasoning_delta` to the current trailing reasoning streaming item, or create a new reasoning item if the previous streaming item is a different type.
- `tool_call_delta`: accumulate into the matching tool-call streaming item by ID. If it does not exist, create it at the current chronological position.

When the final `assistant_output` event arrives (complete message): replace the streaming buffer with the authoritative message. Clear `streamingItems` for that turn.

`visibleTimelineItems()` needs to render:
- If an assistant streaming item exists: show a "growing" assistant message block
- If a reasoning streaming item exists: show a collapsible "Thinking..." block that grows live
- If a tool-call streaming item exists: show the tool name/status as soon as available; do not render incomplete raw JSON arguments in the normal view
- When a complete message event arrives: render it as a normal static message

This ordered-buffer design is required for chronological correctness. Separate top-level strings for reasoning/content/tool calls can accidentally reorder output if a provider interleaves thinking, text, and tool-use blocks.

### Frontend: Auto-Scroll

During streaming, the chat should auto-scroll to the bottom as new tokens arrive. This needs:
- A scroll container ref in `ChatTimeline.svelte`
- After each delta event, check if the user is near the bottom
- If yes, scroll to bottom. If the user has scrolled up, don't force-scroll (they're reading older content).

### Frontend: SSE Event Handling

The current `subscribeRunEvents()` uses named event types via `EventSource.addEventListener(type, ...)`. Add the three new delta types to `RUN_EVENT_TYPES` in `api.js`. The existing pattern extends cleanly.

### SSE Reconnect with Event IDs and `after_sequence`

The SSE endpoint currently calls `run.subscribe()` without an `after_sequence` parameter. This means reconnect replays ALL events. With delta events producing hundreds of entries, this is wasteful.

The SSE endpoint should send each event with `id: <sequence>` and support the standard `Last-Event-ID` reconnect header. Native `EventSource` automatically sends `Last-Event-ID` on reconnect when event IDs were present, which avoids reopening the stream with a dynamically rewritten URL.

The endpoint should also accept an optional `?after_sequence=N` query parameter for symmetry with the WebSocket replay pattern and for clients that reconnect manually. Replay precedence: explicit `after_sequence` query parameter wins; otherwise use `Last-Event-ID`; otherwise replay from the beginning. In all cases the server replays events with `sequence > N`, then streams new events.

The frontend still tracks the highest sequence seen for its own duplicate protection, but it can rely on SSE `id`/`Last-Event-ID` for normal automatic reconnects.

## What We Must NOT Forget

1. **Session persistence is at turn boundaries, not per-delta** — Deltas are never written to the JSONL file. Only the final complete `ChatMessage` is persisted. If the server crashes mid-stream, the user loses the partial response, but the session file is not corrupted.

2. **`reasoning_meta` round-tripping during tool-use loops** — Anthropic requires `thinking` blocks with `signature` fields to be sent back unchanged. If we drop them, the model breaks continuity. The streaming accumulator must preserve `reasoning_meta` across the agentic loop, even though it's never emitted in public events.

3. **Sequence numbers must be monotonically increasing** — Each `RunEvent` has a `sequence` field. Delta events must also have increasing sequence numbers. The SSE client uses sequences for reconnect-replay (via `after_sequence` on the SSE endpoint and WebSocket).

4. **Cancellation must work mid-stream** — `run.cancel()` must immediately stop emitting deltas. Late deltas that arrive after cancellation must be ignored. The existing `cancel_requested` flag and `TERMINAL_EVENT_TYPES` guard already handle this for whole events; delta events need the same guard. Call `run.raise_if_cancelled()` between chunks in the streaming loop.

5. **The final `assistant_output` event is the authoritative message** — After all deltas are streamed, emit the complete message. The frontend should use this to replace its accumulated delta text with the correct final text. This handles edge cases where delta accumulation has tiny differences from the final text (whitespace, encoding).

6. **Empty content is valid** — A model response can have `content: null` with only `tool_calls`. Or only `reasoning` with no `content`. The streaming accumulator must handle all these cases without emitting empty delta events.

7. **The `chat.send` RPC must continue to work unchanged** — Non-streaming mode uses `adapter.send()` and emits whole-message events. It must not be affected by the streaming implementation. The `ChatLoop.streaming` flag branches cleanly.

8. **Tool dispatch happens BETWEEN streaming turns** — After a streaming turn with tool calls, the accumulated message is persisted, tools are dispatched, tool results are persisted, and the next streaming turn begins. The loop structure of `_send_until_final()` stays the same; only `_send_assistant_request()` changes from `send()` to `stream()`.

9. **Both adapters must support streaming deltas** — OpenAI and Anthropic have different streaming formats. Each adapter's `stream()` method must yield normalized deltas. Provider-specific streaming formats stay hidden behind the adapter abstraction. The chat layer only sees normalized deltas.

10. **Delta events stay in `Run._events`** — No compacting or pruning. A typical streaming run produces 100-500 delta events. Memory is trivial — Runs are short-lived. If it ever becomes a problem, compacting can be added later (remove delta events for a turn once the final `assistant_output` event arrives, since the client already replaced them with the authoritative message).

11. **`_send_assistant_request` needs the `run` parameter** — The method currently has no access to `run`, but needs it to emit delta events. The `run` parameter must be added.

12. **Streaming path emits all events itself** — In streaming mode, `_send_assistant_request()` emits deltas during the stream and whole-message events at the end of the turn. The `_emit_assistant_events()` helper is only called in the non-streaming path.

13. **SSE `after_sequence` for reconnect** — The SSE endpoint must accept `?after_sequence=N` so reconnect doesn't replay hundreds of delta events from the start. Same pattern as WebSocket reconnect.

14. **Chunk timeout during streaming** — If the provider sends no chunk for 30 seconds, the streaming request times out. Prevents connections hanging on stalled providers. The timeout resets with each received chunk. On timeout, the run fails with `run_failed`.

15. **Delta events are SSE-only** — Do NOT bridge delta events to WebSocket. WebSocket gets whole-message events only. Delta events are too chatty (`_bridge_run_to_event_bus` must NOT add them to `RUN_OUTPUT_EVENT_TYPES` or `SERVER_EVENT_TYPES`).

16. **Streaming must be best-effort across providers/models** — Do not assume all OpenAI-compatible providers stream the same fields. Gracefully handle missing reasoning deltas, missing tool-call IDs, provider-specific finish reasons, complete-only tool calls, and streaming-unsupported models. Fall back to non-streaming when appropriate.

17. **Ordered frontend streaming buffer** — Use a chronological `streamingItems` buffer rather than unrelated strings for reasoning/content/tool calls. The UI must preserve the order in which the provider emitted visible blocks.

18. **Reasoning metadata remains opaque and internal** — Streaming adapters may yield internal `reasoning_meta` data so the final `ChatMessage` can preserve provider-specific encrypted/signature data for tool-use round-tripping. This data is never emitted as a Run event and never exposed in SSE/WebSocket/history payloads.

19. **Prefer SSE `id`/`Last-Event-ID` for reconnect** — `after_sequence` remains useful and should be supported, but native EventSource reconnect works best when SSE events include `id: <sequence>` and the server honors `Last-Event-ID`.

20. **Do not over-scope the feature** — Implement streaming transport, accumulation, provider normalization, reconnect replay, cancellation, timeout handling, and UI rendering. Do not add unrelated settings screens, new provider-management features, a new tool system, or a different chat architecture unless required for streaming correctness.

## Relevant Files

| File | Role |
|---|---|
| `core/chat/chat.py` | The agentic loop. `_send_assistant_request()` is the key method — needs `run` parameter and must branch on streaming mode. `ChatLoop` gets `streaming` constructor parameter. |
| `core/chat/runs.py` | `Run`, `RunEvent`, `ChatRunManager` — the event system and run coordination. New event type constants for deltas. |
| `core/providers/adapter.py` | `ProviderAdapter` ABC — `stream()` must yield normalized deltas, not raw provider chunks. |
| `core/providers/openai_compatible.py` | OpenAI adapter — `stream()` must yield normalized deltas + `finish` delta from `finish_reason`. Must track tool call accumulation by index. |
| `core/providers/anthropic.py` | Anthropic adapter — `stream()` must track content block index/type and yield normalized deltas + `finish` delta from `stop_reason`. Must preserve `reasoning_meta` internally. |
| `server/delegates.py` | `_stream_chat()` starts the run with streaming `ChatLoop`. `_bridge_run_to_event_bus()` must NOT bridge delta events to WebSocket. `_server_event_from_run_event()` maps delta events for SSE. |
| `server/app.py` | SSE endpoint `GET /api/runs/{run_id}/events` — must accept `after_sequence` query parameter for reconnect replay. |
| `webui/src/lib/chatState.js` | Needs streaming buffer state (`streamingContent`, `streamingReasoning`, `streamingToolCalls`) and accumulation logic in `appendRunEvent`. `visibleTimelineItems()` renders growing text. |
| `webui/src/lib/api.js` | `RUN_EVENT_TYPES` needs the three new delta event types added. |
| `webui/src/components/ChatTimeline.svelte` | Needs "growing text" rendering mode and auto-scroll. |
| `webui/src/components/ChatView.svelte` | `subscribeToRun` may need updates for `after_sequence` on SSE reconnect. |
| `.vorch/specs/chat.md` | Must be updated with streaming event types and delta semantics. |
| `.vorch/specs/server.md` | Must be updated with delta events in data model and SSE-only delta routing. |

## Dependencies / Prerequisites

- **Persistent WebSocket**: DONE. The two-channel architecture (SSE for streaming, WS for signalling) is implemented and working.
- **No other prerequisites**: Streaming is self-contained. It doesn't depend on tools, settings, or other features being built first.

## Priority

Streaming should come before tools or other features. Without streaming, the chat experience feels broken (long wait, then wall of text). With streaming, even simple responses feel responsive and professional. All future features (tools with live feedback, reasoning display, cancel improvements) build on the streaming infrastructure.
