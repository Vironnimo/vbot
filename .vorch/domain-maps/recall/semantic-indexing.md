# Semantic Indexing

Read this reference when you change semantic indexing: triggers, passes, batching, failures, backoff and skips, Embedding space changes, `IndexStatus`, or indexer logging. The ownership summary (only `SemanticIndexer` embeds documents, when it runs, the status RPCs) stays in `recall.md` -> Semantic Indexing.

## Lifecycle and triggers

`SemanticIndexer` runs while the selected backend is in `SEMANTIC_RECALL_BACKENDS` (`vector`, `hybrid`; `RecallIntegration` calls `set_enabled` at construction and on every reload) and a `text_embedding` binding is set. Runtime starts its loop task (`recall-semantic-indexer`) through `start_event_loop_service`, so only inside a serving Event Loop and never in safe-startup mode, and closes it before the index.

- Triggers: start (`START_DELAY_S` 5 s), every terminal Run (`RecallIntegration.observe_runs` adds a completion observer to each started Run; coalesced within `RUN_DEBOUNCE_S` 20 s), a periodic sweep (`SWEEP_INTERVAL_S` 600 s) that also catches edits, imports and Channel traffic, a `text_embedding` binding change (`Runtime.apply_settings_change` -> `embedding_binding_changed`), `rebuild`, enabling, and a search that finds waiting Passages (`on_waiting`). Settings changes, rebuild and enabling clear a pending backoff; the others respect it.

## Passes and batching

- A pass refreshes the catalog for every scope with live Sessions (`ChatSessionManager.list_live_scopes`), over conversation and Sub-Agent Sessions only, evicts indexed scopes without live Sessions, then drains `pending_texts` newest first in batches of `BATCH_SIZE` (64 distinct texts, or the smaller `EmbeddingService.document_batch_size()` of a local Model, 8), one embedding call at a time, each followed by one short write. Usage of each stored batch adds to `indexer_state`, with the length of its texts.
- Queue selection and waiting-text estimates start from `pending_vectors`, not the complete Passage catalog. Selection groups narrow hash/ref/timestamp rows before loading the limited batch's text; estimates group precomputed lengths. Keep full text out of these sorts: forcing a queue-first join alone makes a full rebuild more expensive.

## Failures and backoff

- Transient failures (network, timeout, retryable status, 429) back off 30 s doubling to 30 min and honor `Retry-After` (state `retrying`); a permanent rejection (another 400/413/422, a context overflow, or a local Model's `EmbeddingInputTooLongError`) bisects the batch, stores the accepted halves and records each single text that still fails in `skipped_texts` until the space changes or a rebuild; if every text of a multi-text batch fails alone, the provider rejects the request shape, so the pass stops as `error` without skipping. Configuration failures (unusable binding, 401/403, 404 model, unusable response) stop as `error` with the same backoff.
- A local Model (`EmbeddingSpaceIdentity.local`) has no provider to retry: not installed or not ready stops as `local_model_missing`, any other engine failure as `local_engine_failed`; a finished local installation re-triggers the indexer (`add_local_ready_listener`).
- An index failure discards a damaged index (`discard_if_damaged`) and backs off as `index_unavailable`.

## Embedding space changes

A pass whose binding names another space than the pinned header drops every vector first (`reset_vectors`, logged with `reason=binding`), so a change of embedding identity re-embeds everything even when nothing was waiting; equivalent target spellings keep their space (`model_tasks/embeddings.md` -> Interfaces). A dimension that differs from the pinned header re-pins (`use_space`), which requeues everything; a pass that moves space more than `_MAX_SPACE_MOVES` (3) times stops as `space_unstable`. A response from a binding other than the one the pass started with restarts the pass.

## Status and logs

- `IndexStatus` (`semantic_enabled`, `state` in `disabled|unconfigured|idle|indexing|retrying|error`, provider/model, `indexed`/`waiting`/`skipped` Passage counts, `last_error {code, message}`, `next_attempt_at`, `last_completed_at`, `spent` usage, `estimate` of the waiting texts, and while `indexing` the pass's `rate` in texts embedded or skipped per second and `eta_seconds` for the waiting texts at that rate; both stay `null` until the pass finished a batch and ran `RATE_MIN_SECONDS` 5 s, and outside a pass). `status()` and `rebuild()` never create the index file. Listeners get a status on every state change and at most every 2 s while indexing; the server bridges them to the `recall_index_status` event and serves `recall.status`/`recall.rebuild_index` (`server/rpc/recall_methods.py`).
- Rates come from what the space's requests reported (`SpentMeasure`, `recall/passage-index.md`): tokens per character from those that reported tokens, else 1 per `CHARS_PER_TOKEN` 4; USD per token from those that reported tokens and cost, else the Model DB input price, 0 for a local Model. `estimate` applies both to the waiting characters. `spent.cost` is the reported cost plus, at that price, the tokens of requests that reported no cost, or their characters at that rate; `null` without a price. While the index holds unclassified usage (`recall/passage-index.md`), `spent.cost` is the reported cost only when every request reported one, the reported tokens at that price when every request reported tokens and none a cost, else `null`.
- Logs: INFO for pass start/finish (counts, duration, usage), space pinning and rebuild; WARNING once per failure condition (`Paused Recall indexing (code=...)`), INFO on recovery; batches at DEBUG. Never text content.

## Source and tests

- `core/recall/semantic_indexer.py`; Runtime wiring `core/runtime/_recall.py` (`RecallIntegration`)
- `tests/core/recall/test_semantic_indexer.py` covers the indexer (order, status, visibility, bisection, backoff, configuration errors, space thrash, scheduling with injected clocks and zero delays); Runtime wiring (stale files, deletion under any backend, Run nudge) is in `tests/core/runtime/test_runtime_recall.py`.
