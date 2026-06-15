# Compaction

Context-window management and compaction checkpoint creation for chat Sessions.

## Overview

`core/compaction/` (`compaction.py`) owns the provider-agnostic compaction algorithm, settings type, and strategy/service interface. Compaction is a logical Session operation: it summarizes older closed history into a `compaction_checkpoint` message while preserving a verbatim recent tail. It never rewrites or deletes existing Session JSONL history.

The chat loop decides when compaction is safe to run. The compaction domain decides how to choose the preserved tail boundary, render pre-tail history for summarization, call the supplied summary adapter, and validate the resulting checkpoint.

## Data Model

- `CompactionSettings` — runtime settings `{ auto, threshold, tail_tokens, summary_model }`.
- `CompactionStrategy` — protocol for implementations that produce a `ChatMessage` checkpoint.
- `CompactionService` — wrapper that runs a strategy, validates the checkpoint role, and exposes threshold/token helpers.
- `SummarizationStrategy` — current strategy that summarizes pre-tail history through a caller-provided provider adapter.
- `CompactionError` — expected domain error for invalid history, invalid strategy output, or invalid summary responses.
- `TOOL_RESULT_CONTENT_PLACEHOLDER` — placeholder used when rendering tool messages into the summary prompt; raw tool result content is omitted from compaction prompts.

## Interfaces

- `find_tail_boundary(messages, tail_tokens) -> str` returns the user-message id where the verbatim preserved tail starts.
- `CompactionService.compact(messages, agent, summary_adapter, summary_model_id, storage, settings, instruction=None) -> ChatMessage` delegates to the strategy and requires a `role: "compaction_checkpoint"` result. `instruction` is the optional manual `/compact` free-text argument forwarded to the strategy.
- `CompactionService.should_auto_compact(input_tokens, context_window, threshold) -> bool` evaluates configured threshold ratio. The chat loop resolves `context_window` through the shared read-side default chain (`resolve_context_window`, see `providers.md`) before calling, so a model whose catalog window is `None` still auto-compacts against a usable window (provider-config default or the global floor) instead of silently disabling. The `<= 0` guard stays as defensive belt-and-suspenders.
- `CompactionService.estimate_messages_tokens(messages) -> int` estimates prompt size when provider usage is unavailable. The estimate counts provider-relevant structured message fields such as content blocks, tool calls, tool result metadata, and reasoning fields; storage-only fields such as ids, timestamps, usage, and timing are ignored.
- `SummarizationStrategy.compact(...)` reads `compaction.md` through the provided storage object, sends one user prompt to the summary adapter with `temperature=0.0` and provider-default thinking effort, and returns `ChatMessage.compaction_checkpoint(...)`.
- **Incremental compaction:** When the supplied history already contains a `compaction_checkpoint`, `compact` summarizes only the messages from that checkpoint's `tail_boundary_id` onward (its previously preserved tail), not the whole session. The previous summary is seeded into the prompt inside `<previous_summary>...</previous_summary>` so the model carries its facts forward, and the new checkpoint's `usage.compacted_token_count` accumulates the prior count plus the newly folded delta. Without a prior checkpoint, the full history is the candidate region.

## Cross-Domain Contracts

- `core/runtime/` wires compaction: `Runtime.start()` constructs both canonical ChatLoops with one shared `CompactionService(SummarizationStrategy())` via constructor injection. No other layer creates or injects the service.
- `core/chat/` owns the **auto-compaction** entry point. The chat loop (`_maybe_auto_compact` in `core/chat/chat.py`) runs it only after a final assistant response with no pending tool calls or after a complete tool-result cycle, and resolves its own summary adapter/model.
- `core/automation/` owns the **manual `/compact`** entry point as a thin bridge. The pure-text command is recognized by `core/chat/commands.py`; accessors dispatch it (server RPC `_handle_compact_command`, the Telegram channel) to `TriggerService.compact_session`, which delegates to `ChatLoop.compact_session(...)`. Manual and auto compaction share the chat loop's single summary-adapter resolution (`ChatLoop._resolve_summary_adapter`). `server/` is RPC dispatch only — no compaction logic lives there.
- **`/compact <instruction>` argument:** the optional free-text argument is threaded `CommandAction(name="compact", argument=...)` → `compact_session(..., instruction)` → `ChatLoop.compact_session(..., instruction)` → `CompactionService.compact(..., instruction)` → `SummarizationStrategy.compact(..., instruction)`. When present, `_build_compaction_prompt` adds it as a `<user_instruction>...</user_instruction>` section (between the prompt fragment and the optional `<previous_summary>`/`<history>` sections). `instruction` defaults to `None` everywhere, so the auto-compaction path is unaffected.
- `core/sessions/` owns persistence. Compaction appends checkpoint messages to the Session; it never mutates existing records.
- `core/storage/` owns persisted settings and prompt-fragment access. `compaction.md` is in `storage.PROMPT_FRAGMENT_NAMES` (backend load/write allowed) but is deliberately excluded from both `prompts.EDITABLE_PROMPT_FRAGMENT_NAMES` (the prompt-editor surface) and `storage.AGENT_PROMPT_FRAGMENT_NAMES` (never Agent-scoped).
- WebUI renders `compaction_completed` Run events and persisted `compaction_checkpoint` history as timeline separators, not normal chat bubbles.

## Conventions

- Tail boundaries must start on user messages so preserved history resumes at a complete user turn.
- Compaction must not split unresolved assistant tool-call cycles.
- Tool messages are represented in compaction prompts with `TOOL_RESULT_CONTENT_PLACEHOLDER`; raw tool result content can be large or sensitive and is not copied into the summary prompt.
- Tail-boundary token estimates must include structured tool-call arguments and content-block payloads rather than only `message.content`, so tool-heavy turns are not undercounted.
- Summary adapter responses may be raw provider dicts or adapter-normalized dicts. If an adapter exposes `normalize_response()`, the strategy uses it before extracting summary text.
- `summary_model` fallback behavior is owned by callers because they know the active runtime/provider context. Invalid or unavailable summary models should fall back to the active run model rather than failing the user turn.

## Constraints & Gotchas

- `CompactionService` intentionally accepts adapters, storage, settings, and agent objects from callers instead of reaching into runtime globals.
- The domain imports canonical `ChatMessage`/`ContentBlock` types from `core.chat` at module load; `core/chat/chat.py` imports `CompactionService` only under `TYPE_CHECKING` plus a lazy local import, and the service is injected. Keep it that way — a module-level `core.chat` ↔ `core.compaction` import in both directions would create a runtime cycle.
- Manual `/compact` (`compact_session`) refuses while a Run is active for the session (returns "Cannot compact while a run is active for this session"); auto-compaction only runs at the chat loop's safe points. Neither path compacts mid-turn.
- Existing completed-turn provider reasoning metadata must not be blindly carried into summaries or later provider requests.
- Failed automatic compaction should not fail the active Run; the chat loop logs a warning and continues without compaction.
- Compaction assumes a context window of **at least ~32k tokens** with the default settings (`threshold=0.8`, `tail_tokens=15_000`). On much smaller windows the preserved tail alone can exceed the trigger threshold so compaction never reduces below it. `tail_tokens` is a floor, not a cap, and is not clamped against the context window — see `.vorch/FLAGGED.md` for the residual edge cases that were intentionally deferred under the 32k assumption.
