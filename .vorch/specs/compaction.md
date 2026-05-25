# Compaction

Context-window management and compaction checkpoint creation for chat Sessions.

## Overview

`core/compaction/` owns the provider-agnostic compaction algorithm, settings type, and strategy/service interface. Compaction is a logical Session operation: it summarizes older closed history into a `compaction_checkpoint` message while preserving a verbatim recent tail. It never rewrites or deletes existing Session JSONL history.

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
- `CompactionService.compact(messages, agent, summary_adapter, summary_model_id, storage, settings) -> ChatMessage` delegates to the strategy and requires a `role: "compaction_checkpoint"` result.
- `CompactionService.should_auto_compact(input_tokens, context_window, threshold) -> bool` evaluates configured threshold ratio.
- `CompactionService.estimate_messages_tokens(messages) -> int` estimates prompt size when provider usage is unavailable.
- `SummarizationStrategy.compact(...)` reads `compaction.md` through the provided storage object, sends one user prompt to the summary adapter with `temperature=0.0` and provider-default thinking effort, and returns `ChatMessage.compaction_checkpoint(...)`.

## Cross-Domain Contracts

- `core/chat/` owns safe invocation points. Auto-compaction runs only after a final assistant response with no pending tool calls or after a complete tool-result cycle.
- `core/sessions/` owns persistence. Compaction appends checkpoint messages to the Session; it never mutates existing records.
- `core/storage/` owns persisted settings and prompt-fragment access. `compaction.md` is allowlisted for backend loading but is not part of the normal system-prompt editor surface.
- `server/` owns manual `/compact` command handling and summary-model adapter resolution for that path.
- WebUI renders `compaction_completed` Run events and persisted `compaction_checkpoint` history as timeline separators, not normal chat bubbles.

## Conventions

- Tail boundaries must start on user messages so preserved history resumes at a complete user turn.
- Compaction must not split unresolved assistant tool-call cycles.
- Tool messages are represented in compaction prompts with `TOOL_RESULT_CONTENT_PLACEHOLDER`; raw tool result content can be large or sensitive and is not copied into the summary prompt.
- Summary adapter responses may be raw provider dicts or adapter-normalized dicts. If an adapter exposes `normalize_response()`, the strategy uses it before extracting summary text.
- `summary_model` fallback behavior is owned by callers because they know the active runtime/provider context. Invalid or unavailable summary models should fall back to the active run model rather than failing the user turn.

## Constraints & Gotchas

- `CompactionService` intentionally accepts adapters, storage, settings, and agent objects from callers instead of reaching into runtime globals.
- The domain depends on canonical `ChatMessage` and `ContentBlock` types from `core.chat`; chat depends on the compaction service only by injection. Avoid introducing a runtime import cycle.
- Existing completed-turn provider reasoning metadata must not be blindly carried into summaries or later provider requests.
- Failed automatic compaction should not fail the active Run; the chat loop logs a warning and continues without compaction.
