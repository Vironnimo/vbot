# Streaming / Provider Handoff

Scope: exploratory review of provider streaming, provider adapters, and the
OpenAI-compatible / Anthropic base implementations. The code inspected was
primarily in `.worktrees/write-hang-order`, because the IDE tabs point there.
This handoff is written in the main repository root on purpose.

No code changes were made during the review.

## Context

vBot's intended contract is:

- Provider adapters hide raw provider stream formats.
- `adapter.stream()` yields normalized deltas only:
  - `content_delta`
  - `reasoning_delta`
  - `tool_call_delta`
  - internal-only `reasoning_meta`
  - `usage`
  - `finish`
- `core/chat/streaming.py` accumulates those normalized deltas into the final
  canonical assistant message.
- The Run SSE endpoint streams Run events, not provider chunks.
- `reasoning_meta` must be persisted for provider round-tripping but stripped
  from public server/SSE/WebSocket payloads.

Relevant specs already read:

- `.vorch/PROJECT.md`
- `.vorch/GLOSSARY.md`
- `.vorch/specs/providers.md`
- `.vorch/specs/chat.md`
- `.vorch/specs/server.md`
- `.vorch/specs/models.md`

## Main Findings

### 1. Streamed tool argument merging can silently corrupt valid JSON

File:

- `.worktrees/write-hang-order/core/chat/streaming.py`

Relevant code:

- `_ToolCallFragments.append()` around lines 83-90
- `_merge_stream_fragment()` around lines 293-316
- `_should_preserve_leading_quote()` around lines 319-327
- `_should_preserve_leading_backslash()` around lines 330-334

Problem:

`_merge_stream_fragment()` treats any suffix/prefix overlap as duplicate text
and removes the overlapping prefix from the new delta.

That is risky because provider tool-call argument deltas are normally
incremental fragments. Repeated text across a chunk boundary can be legitimate
payload, not duplicated cumulative output.

Repro run from the worktree:

```python
from core.chat.streaming import StreamingAccumulator

acc = StreamingAccumulator()
acc.add_delta({
    "type": "tool_call_delta",
    "id": "c",
    "name_delta": "write",
    "arguments_delta": '{"value":"ab',
})
print(acc.add_delta({
    "type": "tool_call_delta",
    "id": "c",
    "arguments_delta": 'ab"}',
})[0].payload)
print(acc.finalize_assistant_fields().tool_calls)
```

Observed output:

```python
{"tool_call_id": "c", "arguments_delta": '"}'}
[{"id": "c", "name": "write", "arguments": {"value": "ab"}}]
```

Expected final arguments would be:

```python
{"value": "abab"}
```

Why this matters:

- This is silent data corruption.
- It can change file paths, command text, JSON string values, prompts, or any
  tool input.
- The UI may show only the de-duplicated visible delta, making the bug look like
  a provider/model issue rather than local accumulation.

Suggested fix:

- Keep the `delta.startswith(existing)` path only for truly cumulative providers.
- Remove generic suffix/prefix overlap merging for normal incremental deltas.
- In practice:
  - if `not existing`: append delta
  - if `delta.startswith(existing)`: treat as cumulative and emit suffix
  - else: append delta exactly
- Delete the quote/backslash special cases after the generic overlap heuristic
  is removed; they are compensating for the wrong abstraction.

Suggested tests:

- Incremental repeated text must preserve both copies:
  - `{"value":"ab` + `ab"}` -> `{"value":"abab"}`
- Cumulative fragment still emits suffix:
  - `{"path":"` + `{"path":"notes.md"}` -> only `notes.md"}` emitted
- Escaped quote and backslash boundary tests should still pass.

### 2. Provider streams can end without a terminal marker and still finalize as success

Files:

- `.worktrees/write-hang-order/core/providers/openai_compatible.py`
- `.worktrees/write-hang-order/core/providers/anthropic.py`
- `.worktrees/write-hang-order/core/chat/chat.py`

Relevant code:

- OpenAI stream loop around `openai_compatible.py:337`
- Anthropic stream loop around `anthropic.py:356`
- Anthropic `message_stop` break around `anthropic.py:398`
- Chat finalization around `chat.py:1543`

Problem:

The OpenAI-compatible adapter stops when it sees `data: [DONE]`, but it does
not verify that `[DONE]` was actually seen before the response iterator ends.

The Anthropic adapter stops when it sees `message_stop`, but it does not verify
that `message_stop` was actually seen before EOF.

After either adapter ends, the chat loop finalizes whatever has accumulated:

```python
assistant_message = _assistant_message_from_response(
    agent.model,
    accumulator.finalize_assistant_fields().to_response_dict(),
)
```

Why this matters:

- A cleanly closed but truncated stream can be persisted as a complete assistant
  answer.
- It can produce partial tool calls, missing usage, missing final finish reason,
  or incomplete text that looks "done" to the rest of the system.
- The failure is hard to debug later because the final assistant message becomes
  authoritative.

Suggested fix:

- Track terminal marker state in each adapter.
- For OpenAI-compatible streams, require `[DONE]` or another explicitly accepted
  terminal condition.
- For Anthropic streams, require `message_stop`.
- If EOF arrives first, raise a provider/network streaming error rather than
  finalizing success.
- Consider using `NetworkError` for unexpected EOF after stream establishment,
  since this is a transport-level incomplete read.

Suggested tests:

- OpenAI-compatible stream with content but no `[DONE]` raises.
- Anthropic stream with content but no `message_stop` raises.
- A stream with terminal marker still finalizes normally.

### 3. In-band SSE provider errors are ignored by the base adapters

Files:

- `.worktrees/write-hang-order/core/providers/openai_compatible.py`
- `.worktrees/write-hang-order/core/providers/anthropic.py`
- `.worktrees/write-hang-order/core/providers/github_copilot_messages.py`

Relevant code:

- OpenAI chunk parsing around `openai_compatible.py:343`
- OpenAI normalization starts with choices around `openai_compatible.py:393`
- Anthropic event dispatch around `anthropic.py:406`
- Copilot Messages handles errors around `github_copilot_messages.py:140`

Problem:

OpenAI-compatible streaming ignores chunks that do not have usable `choices`.
If a provider sends an SSE event like:

```json
{"error": {"message": "..."}}
```

the adapter effectively yields nothing.

Anthropic streaming dispatches only known content/message events. If the stream
contains:

```json
{"type": "error", "error": {"type": "...", "message": "..."}}
```

the adapter returns `[]` and keeps going until EOF or `message_stop`.

Copilot Messages already does the right thing:

```python
if event_type == "error":
    raise ProviderError(_messages_error_detail(event), retryable=False)
```

Why this matters:

- A real provider-side streaming error can become either a silent no-op or a
  confusing incomplete successful response.
- This undermines fallback logic and user-facing error reporting.

Suggested fix:

- Add in-band error detection to OpenAI-compatible streaming:
  - top-level `error`
  - maybe provider variants such as `type == "error"` if present
- Add `event_type == "error"` handling to Anthropic.
- Raise `ProviderError(..., retryable=False)` unless provider status/shape gives
  a clearer retryable classification.

Suggested tests:

- OpenAI-compatible stream with `data: {"error": {"message": "bad"}}` raises
  `ProviderError`.
- Anthropic stream with `{"type":"error", "error": {"message": "bad"}}` raises
  `ProviderError`.

### 4. Mid-stream read/timeout error handling is inconsistent

Files:

- `.worktrees/write-hang-order/core/providers/openai_compatible.py`
- `.worktrees/write-hang-order/core/providers/anthropic.py`
- `.worktrees/write-hang-order/core/providers/github_copilot.py`

Relevant code:

- OpenAI catches only `httpx.ReadError` around `openai_compatible.py:349`
- Anthropic catches only `httpx.ReadError` around `anthropic.py:400`
- Copilot `_stream_responses()` around `github_copilot.py:234`
- Copilot `_stream_messages()` around `github_copilot.py:252`

Problem:

OpenAI-compatible and Anthropic adapters wrap `httpx.ReadError` during stream
iteration, but not `httpx.TimeoutException` or broader stream-related `httpx`
exceptions that can occur while reading lines.

Copilot endpoint stream helpers do not wrap mid-stream read errors at all.
They close the response in `finally`, but raw `httpx` exceptions can escape.

Why this matters:

- Error classes become inconsistent between endpoints.
- Fallback behavior and user-facing failures become endpoint-dependent.
- The specs say mid-stream errors should propagate clearly as provider/network
  errors, not leak raw provider or httpx details upward.

Suggested fix:

- Introduce a small shared helper or local pattern that catches:
  - `httpx.ReadError`
  - `httpx.TimeoutException`
  - possibly `httpx.HTTPError` for stream iteration only
- Wrap these as `NetworkError` or `ProviderTimeoutError` as appropriate.
- Apply it consistently to:
  - OpenAI-compatible stream iteration
  - Anthropic stream iteration
  - GitHub Copilot `/responses`
  - GitHub Copilot `/v1/messages`

Suggested tests:

- Copilot Responses mid-stream read error raises `NetworkError`.
- Copilot Messages mid-stream read error raises `NetworkError`.
- OpenAI/Anthropic mid-stream timeout gets classified consistently.

## Other Notes

### OpenCode Go worktree differs from main checkout

The main checkout currently has a smaller OpenCode Go exception list:

- `minimax-m2.7`

The worktree has:

- `minimax-m2.7`
- `minimax-m2.5`
- `qwen3.6-plus`
- `qwen3.5-plus`

The worktree also includes `_bound_assistant_reasoning_replay()` to keep only
the latest assistant reasoning for Anthropic-routed OpenCode Go models. That is
important because the spec says stale completed-turn reasoning should not be
resent on later turns, while current-turn reasoning/meta must be preserved
during tool-use loops.

Relevant file:

- `.worktrees/write-hang-order/core/providers/opencode_go.py`

This looks like a targeted mitigation for prompt growth / provider confusion,
but note that `.vorch/specs/providers.md` still mentions only `minimax-m2.7`.
Only Orchestrator should update specs.

### Streaming fallback is narrow and text-message based

File:

- `.worktrees/write-hang-order/core/chat/chat.py`

Relevant code:

- `_is_streaming_fallback_error()` around lines 1736-1740

Current behavior:

```python
def _is_streaming_fallback_error(error: ProviderError) -> bool:
    if error.retryable:
        return False
    message = str(error).lower()
    return all(token in message for token in ("stream", "support"))
```

This means streaming-to-non-streaming fallback only triggers before visible
deltas and only when the provider error text contains both `stream` and
`support`.

This may be intentional, but it is brittle. If provider error wording changes,
fallback will not happen.

### Usage extraction exists and is mostly covered

The code already handles:

- OpenAI stream `usage` chunks when `stream_options.include_usage` is used.
- Anthropic usage split across `message_start` and `message_delta`.
- Accumulator usage finalization.

Relevant files:

- `.worktrees/write-hang-order/core/providers/openai_compatible.py`
- `.worktrees/write-hang-order/core/providers/anthropic.py`
- `.worktrees/write-hang-order/core/chat/streaming.py`

This area looks better covered than stream terminal/error handling.

## Recommended Fix Order

1. Fix `_merge_stream_fragment()` first.
   - This is the highest-risk silent correctness bug.
   - It is local to `core/chat/streaming.py`.

2. Add terminal marker enforcement.
   - OpenAI-compatible: require `[DONE]`.
   - Anthropic: require `message_stop`.
   - Copilot Responses/Messages should be reviewed for equivalent terminal
     semantics too.

3. Add in-band SSE error detection.
   - Copy the Copilot Messages pattern into base OpenAI-compatible and
     Anthropic adapters.

4. Normalize mid-stream exception wrapping.
   - Prefer a shared helper only if it stays small and readable.
   - Otherwise duplicate a clear local pattern in each adapter family.

5. Re-run targeted tests:

```powershell
python scripts/quality.py core/chat/streaming.py core/providers/openai_compatible.py core/providers/anthropic.py core/providers/github_copilot.py tests/core/chat/test_streaming.py tests/core/providers/test_openai_compatible.py tests/core/providers/test_anthropic.py tests/core/providers/test_github_copilot.py tests/core/providers/test_github_copilot_responses.py tests/core/providers/test_github_copilot_messages.py
```

Adjust the path prefix if running inside `.worktrees/write-hang-order`.

## Files Most Likely To Touch

Primary:

- `core/chat/streaming.py`
- `tests/core/chat/test_streaming.py`
- `core/providers/openai_compatible.py`
- `tests/core/providers/test_openai_compatible.py`
- `core/providers/anthropic.py`
- `tests/core/providers/test_anthropic.py`

Secondary:

- `core/providers/github_copilot.py`
- `core/providers/github_copilot_responses.py`
- `core/providers/github_copilot_messages.py`
- `tests/core/providers/test_github_copilot.py`
- `tests/core/providers/test_github_copilot_responses.py`
- `tests/core/providers/test_github_copilot_messages.py`

Potential documentation, Orchestrator-only:

- `.vorch/specs/providers.md`
- `.vorch/specs/chat.md`

## One-Sentence Summary

The architecture is basically right, but the fragile spots are exactly at the
streaming boundary: argument-fragment merging is too clever, stream completion
is trusted without terminal proof, in-band provider errors are under-detected,
and mid-stream exception wrapping differs by endpoint.
