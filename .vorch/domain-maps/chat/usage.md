# Chat Usage

Task-gated reference for canonical per-step and whole-Session token Usage. Read this when changing Provider Usage normalization, estimation, persistence, Run events, Session aggregation, or the WebUI token projection.

## Canonical shape

Assistant `usage` uses canonical `input_tokens` and `output_tokens`, with optional `cache_read_tokens`, `cache_write_tokens`, `reasoning_tokens`, aggregate `estimated`, and field provenance `input_tokens_estimated` / `output_tokens_estimated`. Usage is valid only on Assistant messages. `input_tokens` means the total prompt including cached tokens. Wires that report cache tokens separately must add them into the canonical input total; OpenAI-style cached-token detail is already a subset of its input count. `reasoning_tokens` is a provider-reported subset of `output_tokens`, never an additional token total.

Provider adapters normalize every available wire counter before Chat sees it; normalized streaming Usage may carry either primary counter independently. `_complete_usage_with_estimates` preserves usable counters, treats a reported zero input for a non-empty vBot request as unavailable, fills only a missing/invalid input or output through the structured estimator in `core/utils/tokens.py`, sets the matching field-provenance flag, and sets aggregate `estimated=true` when either field is estimated. Legacy persisted payloads carrying only `estimated=true` mean both primary fields are estimated. The estimator counts provider-relevant text, Content Blocks, Tool Calls, and active reasoning fields while ignoring persistence metadata.

Usage is persisted on finalized Assistant messages but never echoed into later Provider requests. `_message_to_request_dict` strips it from history, and the live Tool-continuation helper does the same while preserving permitted reasoning.

Current Context Usage is a separate server projection shaped as required `{tokens, estimated}` plus optional measured provenance `{provider_input_tokens, provider_output_tokens, estimated_delta_tokens}`. Canonical input anchors the request that just completed, canonical output accounts for the appended Assistant response, each measured field is exposed independently, and only provider-visible messages added after that response need an additional structural estimate. Consumers must not substitute cumulative Session Usage or compare the Provider anchor with a complete transcript estimate and select the larger number: persisted reasoning representations can make those evidence bases diverge sharply without increasing the Provider's actual Context.

After Compaction there is no Provider measurement yet, so Chat builds the exact projected next request and estimates its complete Provider-visible footprint, including the refreshed System Prompt, request-only Context, Summary+Tail, applicable in-run restored fields, and Tool definitions. This direct rebuild projection is stamped onto the checkpoint before append and becomes the shared `context_usage`; it must not be reconstructed as `before - canonical reclaim`, because those values can use different representations and scopes.

## Session aggregation and events

`core/chat/usage.py::aggregate_session_usage(messages)` always produces `{measured_turns, estimated_turns, cache_turns, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens}` and conditionally adds `{reasoning_turns, reasoning_tokens}` after at least one measured output reports a valid Reasoning counter. `measured_turns` counts only fully measured turns; `estimated_turns` counts turns with either estimated primary field. Token totals include each provider-reported field independently, so a partial turn may contribute measured output while its estimated input remains excluded. Cache fields require measured input, while Reasoning requires measured output; their turn counters retain field-presence semantics so consumers can distinguish a reported zero from no Provider reporting.

At the first Model step the loop seeds totals from persisted Session history and advances them after each Assistant append through `add_session_turn_usage`. Every finalized Assistant step emits non-visual `model_step_usage` with the exact step payload, current `session_usage`, and current `context_usage`, including Tool-only steps. Tool Results appended before the next request extend the same Provider anchor by only their estimated request delta for automatic Compaction decisions. `Run.terminal_payload_extras` carries final Session and Context Usage for every terminal outcome; the loop recomputes both from persisted history in `finally` after the run-summary append, logging and swallowing only diagnostic recomputation failure.

`chat.history` returns the full-transcript Session total and latest durable Context projection even when the visible message page is only a slice. Consumers must not derive either value from their locally loaded page. The final `run_completed` payload may additionally carry the last Assistant step's `usage`; failed/cancelled terminal events do not invent a per-step value.

## Streaming and Provider boundaries

Streaming adapters yield normalized `usage` deltas containing at least one non-negative integer primary counter. `StreamingAccumulator` merges partial deltas and forwards optional cache and Reasoning fields only when they are non-negative integers. OpenAI generally supplies final-chunk Usage. Native Anthropic may split input/cache counts at `message_start` and output/Thinking detail at `message_delta`; a compatible Messages gateway may instead repeat a complete input/cache snapshot in the terminal `message_delta`, which replaces the earlier start snapshot. Provider-specific wire shapes stay in the Provider maps.

The WebUI token badge consumes only `context_usage` for its numerator; history seeds it, `model_step_usage` and Compaction events update it during an active Run, and the terminal payload provides final reconciliation. Last-step Usage and cumulative Session Usage remain tooltip diagnostics and never determine the badge. The tooltip prefixes only estimated token fields with `~`, explains which Provider field was omitted, and labels Session turns as fully measured versus carrying estimated fields. Context-window resolution is not owned by Usage; it comes from the active Model/Provider configuration.

## Source and tests

- Aggregation: `core/chat/usage.py`; `tests/core/chat/test_usage.py`.
- Message persistence/estimation: `core/chat/messages.py`; `tests/core/chat/test_chat_loop_usage.py` and `test_messages_primitives.py`.
- Streaming accumulation: `core/chat/streaming.py`; `tests/core/chat/test_streaming.py`.
