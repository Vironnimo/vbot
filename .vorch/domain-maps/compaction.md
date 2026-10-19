# Compaction

Policy-driven Context transformation and checkpoint creation for chat Sessions.

## Overview

`_tail.py` owns provider-safe chronological Tail selection and its internal records; `_tail_types.py` holds the shared Tail sizing and framing contract. `core/compaction/` owns the provider-neutral Engine: evaluates Triggers, asks one Strategy for a Plan, performs zero or one Model call, validates the resulting Projection, returns one self-contained `compaction_checkpoint` - never rewriting or deleting prior canonical Session records. `run_coordination.py` owns Compaction Policy decisions, checkpoint persistence, and manual/automatic Run lifecycle. Chat owns request materialization and recovery through `core/chat/compaction_host.py::ChatCompactionHost`, the implementation of `CompactionRunHost`; the coordinator never imports private Chat symbols or reconstructs Chat request state. Pinned prompt-epoch assembly lives in `core/prompts/pinned_context.py` behind that host; Policy configuration belongs to Settings/Agent/Project/Session layers.

## Data Model & Checkpoint contract

- A resolved Policy is `{enabled, trigger, strategy}`; built-in triggers are `context_ratio{threshold, tokens?}` and `input_tokens{tokens}`. For `context_ratio`, the optional absolute `tokens` cap combines with the ratio and whichever limit is reached first triggers Compaction. Built-in strategies are `summary_tail{tail_tokens, summary_model}` and `continuation`. `CompactionSettings` is the typed runtime view - not a second settings source.
- A `CompactionPlan` is the Strategy/Engine boundary permitting zero or one Model call; the Engine never loops or follows up. The Strategy receives the current effective canonical Context and the corresponding already-built provider request.
- A new checkpoint stores the reference-framed summary in `content`, a canonical message `projection` that starts with the matching `[compaction-summary]` note, the cumulative compacted-token count (both strategies add only newly compacted messages, never the previous checkpoint notes), before/after context tokens and Policy/Strategy provenance (`compaction/strategies-and-checkpoints.md` -> Checkpoint content). Latest checkpoint Projection + later messages = effective Context; hidden history never re-reads into later compaction. Every checkpoint carries a Projection; `ChatMessage` validation rejects one without it.
- Every completed checkpoint ends a prompt epoch: Skill activations end (names-only `[compaction-skills]` reminder), the `[tool-change]`, `[prompt-block-change]` and `[effort-change]` notes drop from the Projection, and the next epoch's Tool, dynamic block and effort pins come from the projected request (`chat/request-building.md` -> Skills and prompt-cache stability, Tool catalog per prompt epoch, Dynamic prompt blocks per prompt epoch, Thinking effort per prompt epoch).
- The Engine records Model-backed work as `usage.model_call = {model, usage}` on the checkpoint and records every Adapter invocation's Usage independently of checkpoint persistence (`compaction/strategies-and-checkpoints.md` -> Usage).

## Strategies

`summary_tail` (`SummarizationStrategy`) summarizes the request prefix before a Tail of whole steps with the Summary Model and keeps the Tail natively after the summary; `continuation` (`ContinuationStrategy`) appends a checkpoint request to the completed active request and asks the active Model, with the same Tool definitions - the cache-preserving shape. The Tail is a chronological suffix of whole steps, so a Tool Call and its Results never split. Mechanics, Tail sizing and safety, summary acceptance: `compaction/strategies-and-checkpoints.md`.

## Policy Resolution

Resolved per compaction decision: Session override -> Agent effective (incl. Project member override) -> global settings. For a temporary Agent the Agent level is the `compaction_policy` of its admitted immutable configuration (for example a Swarm profile's override); without one it inherits global settings (`agent.md`, `test_temporary.py`). Absence inherits dynamically - global changes affect existing Sessions without rewrites; a Policy change triggers nothing itself and never reprocesses hidden history. `compaction.py::effective_compaction_policy()` is the single implementation of this chain: Run coordination and every surface that reports an effective Policy (Session listing and Policy updates, the Agent payload without a Session override, `chat.history` on current reads) use it. Every reported effective Policy is normalized and complete.

## Cross-Domain Contracts

- Runtime constructs one registry-backed service for the one `ChatLoop`. Automatic Compaction runs inside the current Agentic Run; manual `/compact` starts a dedicated Session Run so admission prevents concurrent work and accessors subscribe immediately. Both emit the shared started/completed/aborted lifecycle (`compaction/run-coordination.md` -> Run events). Manual Runs use the same native admission, completion transaction and History Run-completeness projection as Agentic Runs. Checkpoints retain their Run identity; no checkpoint-specific terminal proof is needed.
- Automatic Compaction evaluates once immediately before each needed Model request, after input delivery, Notes, request hooks and media; a final answer triggers none (`chat/run-execution.md` -> Agentic progression and events; detail in `compaction/run-coordination.md` -> Evaluation). Triggers compare the request's Context with the target's Context capacity (the context window or a smaller Provider-reported input limit). Automatic decisions also trigger early, with reason `input_limit` or `output_reservation`, when the request could exceed the Provider's input limit or leave too little room for the active Adapter's minimum output reservation (`providers/request-policy.md` -> Request limits and context).
- The Engine obtains request-context kwargs, prompt-cache affinity included, from the selected target's own Adapter; never copy another Adapter's wire-specific context (`compaction/strategies-and-checkpoints.md` -> Model call).
- Chat's `CompactionRunHost` materializes the exact request snapshot and adapters, prepares refreshed prompt-only inputs (full Skill rescan, catalog + seen-Skill set replacement, Working Project rebuild, Config Agent body refresh - while Model target, Connection, Tool policy, Agent identity, working Project identity, and cwd stay Run-stable), closes adapters, builds and counts the projected request (with a fresh Tool pin), commits the checkpoint with its epoch, and rebuilds after stale results. Compaction coordinates the Strategy call, stamps and conditionally persists the checkpoint, and emits lifecycle completion.
- Every attempt plans and calls Models outside the Session write lock and commits through `ChatSession.commit_compaction`, which writes only while the snapshot cursor is still current (`sessions/history-api.md`). A stale result writes nothing - no checkpoint, no prompt-epoch change; automatic attempts carry concurrently appended notes into the checkpoint and rebuild after other changes (`compaction/run-coordination.md` -> Commit and staleness).
- Automatic checkpoints must reclaim >=4,096 estimated tokens; no per-Run limit - every later safe boundary re-evaluates trigger and usefulness rules. A new-context check before any Model call keeps fixed overhead from causing a paid call per boundary.
- Sessions persists checkpoints plus the optional `compaction_policy` metadata override (each evaluation reads only that value through `metadata_value`); Settings/Agents/Projects validate the same shape as nullable inheritance overrides; server exposes both override and effective values in listings, WebUI edits all scopes.

## Invariants & Gotchas

- Manual `/compact` runs the selected Strategy as the primary active Run and refuses while another Run is active.
- The Chat context-card action requests Compaction inside the exact active Run, bypassing automatic enablement, thresholds and the fresh-Skill deferral but no other rule (`compaction/run-coordination.md` -> Context-card requests).
- A failed automatic attempt or post-compaction request projection never defers the next evaluation: retrying is the only path out of an oversized Context, so every later needed Model-request boundary evaluates trigger and usefulness again from scratch.
- Skill activation state changes only on successful checkpoint append - failed/stale/insufficient attempts retain dedup state and environment grants exactly.
- Prompt-refresh inputs prepare before persistence (stored after-count covers the request actually used). Their pins and seen-Skill set commit in the checkpoint's transaction, so no checkpoint exists without its epoch pins; a failed refresh preparation still commits the checkpoint with the previous pins and seen Skills, except the Tool pin: it always comes from the projected request, because the checkpoint drops the notes the old pin relied on.

## Agent-facing text

This domain owns the Compaction instructions (`resources/prompts/compaction.md`, `compaction-manual.md`, `compaction-continuation.md`, `compaction-continuation-manual.md`) and the checkpoint texts `COMPACTION_REFERENCE_PREFIX`, `COMPACTION_USER_QUOTE_PREFIX` and `SKILL_COMPACTION_GUIDANCE` (`core/compaction/compaction.py`). Before you change, add or delete a sentence of them, read `compaction/agent-facing-text.md`: it records why each sentence exists. Update its row for every sentence you change and add one for every sentence you add. The checkpoint guidance sentence `COMPACTION_CHECKPOINT_GUIDANCE` is Chat's (`chat/agent-facing-text.md`).

## References

Read these only when your task matches - not by default.

- Changing a Strategy, Tail selection, checkpoint content, the User quote, summary acceptance, the Compaction Model call or Compaction Usage -> `compaction/strategies-and-checkpoints.md`
- Changing when automatic Compaction runs, manual Compaction Runs, the context card, commit staleness, failures, limits, logging, the prompt refresh of temporary Agents or the Compaction Run events -> `compaction/run-coordination.md`
- Choosing how Compaction informs the Model (checkpoint content, notes, System Reminders) or adding a kernel-to-Model channel -> `model-communication.md`
