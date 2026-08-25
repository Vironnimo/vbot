# Compaction

Policy-driven Context transformation and checkpoint creation for chat Sessions.

## Overview

`core/compaction/` owns the provider-neutral Engine: evaluates Triggers, asks one Strategy for a Plan, performs zero or one Model call, validates the resulting Projection, returns one self-contained `compaction_checkpoint` - never rewriting or deleting prior Session JSONL. `run_coordination.py` owns the Compaction-run coordination (manual dedicated Run plus automatic safe-boundary attempt, prompt-epoch refresh preparation/commit, post-compaction request projection); the chat loop owns safe execution points and drives the coordinator through its `CompactionRunHost` seam. Pinned prompt-epoch assembly lives in `core/prompts/pinned_context.py`; Policy configuration belongs to Settings/Agent/Project/Session layers.

## Data Model & Checkpoint contract

- A resolved Policy is `{enabled, trigger, strategy}`; built-in triggers are `context_ratio{threshold}` and `input_tokens{tokens}`, built-in strategies `summary_tail{tail_tokens, summary_model}` and `continuation`. `CompactionSettings` is the typed runtime view - not a second settings source.
- A `CompactionPlan` is the Strategy/Engine boundary permitting zero or one Model call; the Engine never loops or follows up. The Strategy receives the current effective canonical Context and the corresponding already-built provider request.
- A new checkpoint stores plain summary in `content`, canonical message `projection`, cumulative compacted-token count, before/after context tokens, and Policy/Strategy provenance. The after count builds and structurally estimates the exact projected request (refreshed System Prompt, request-only Context, retained Summary+Tail, same-Run Reasoning/Tool restoration, complete Tool definitions) - explicitly approximate in the UI, never derived by subtracting reclaim from a differently scoped estimate. The Projection constructor guarantees it starts with the matching `[compaction-summary]` note and strips Assistant reasoning fields/meta/scope: vBot Compaction is textual Summary+Tail state, never Provider-native opaque reasoning tokens. Summary+Tail inserts a model-facing reminder identifying the retained Tail as most recent verbatim activity (ignored as Head content by later passes, still counted in size estimates), and Chat appends an ordinal reminder that the Session-scoped `history` Tool can retrieve hidden messages and original payloads behind deterministic Tail digests. Latest checkpoint Projection + later messages = effective Context; hidden history never re-reads into later compaction. Old boundary-based checkpoints stay valid read-only input materialized in memory.
- Every completed checkpoint also ends one Skill activation epoch: Skill names derive only from post-checkpoint carriers, `[skill-context]` carriers drop from the Projection, retained loading Tool Results become instruction-free digests preserving complete cycles, and one `[compaction-skills]` names-only reminder (replacing any previous) says instructions and environment access are inactive with reload guidance.

## Strategies

- `SummarizationStrategy` slices the already-built provider request immediately before the Tail boundary, appends the Compaction instruction, and calls the resolved Summary Model once. The prefix is never rewritten - System Prompt, previous Summary, messages, Tool Calls/Results exactly as sent. The Projection retains the latest active User exactly once plus chronological Assistant/Tool trajectory, removing only Provider-owned Reasoning and pressure-compacting consumed Tool payloads into bounded previews carrying original message ids when verbatim admission would exceed the 115% soft ceiling. Without a configured `summary_model`, the active adapter/model preserves the prior request as exact prefix enabling provider cache reuse; a configured Summary Model intentionally starts fresh cache and gets reasoning stripped (Provider-owned reasoning cannot cross targets).
- `ContinuationStrategy` requires the completed active provider request, appends one internal reminder asking for checkpoint Context, calls the active Model once with the same Tool definitions, and uses the response directly as Projection - the cache-preserving append-only shape. It waits at mid-tool safe points rather than synthesizing prefixes.
- `find_tail_boundary` walks backward across provider-visible User/Assistant boundaries with a 115% soft ceiling on the tail-token target, keeping Assistant Tool-call cycles atomic (boundaries never split or precede an incomplete cycle). Estimates use Chat's shared estimator (`chat/usage.md`).

## Policy Resolution

Resolved per compaction decision: Session override -> Agent effective (incl. Project member override) -> global settings. Absence inherits dynamically - global changes affect existing Sessions without rewrites; a Policy change triggers nothing itself and never reprocesses hidden history.

## Cross-Domain Contracts

- Runtime constructs one registry-backed service shared by both chat loops. Automatic Compaction runs inside the current Agentic Run; manual `/compact` starts a dedicated Session Run so admission prevents concurrent work and accessors subscribe immediately. Both emit the shared started/completed/aborted lifecycle and identical payloads.
- Auto-compaction evaluates only at safe completed Model boundaries against the same Context projection Run events, History, and WebUI expose. Summary+Tail defers an otherwise eligible attempt when the newest unconsumed Tool batch contains a freshly loaded Skill (the next Model step must receive that Result first), re-evaluating after the consuming Assistant boundary.
- Chat resolves Policy/adapters, supplies the exact sent request snapshot plus continuation to both Strategies, prepares refreshed prompt-only inputs (full Skill rescan, catalog + seen-skills replacement, Working Project rebuild, Config Agent body refresh - while Model target, Connection, Tool policy, Agent identity, working Project identity, and cwd stay Run-stable), builds and counts the projected request, stamps and persists the checkpoint, commits the epoch, emits completion.
- Automatic attempts capture the append cursor under the Session write lock, plan and call Models outside it, then re-acquire briefly to append only if nothing appeared meanwhile - a stale result aborts (`compaction_aborted`, no checkpoint, request state rebuilt so concurrent notes remain available). Projected-request failure likewise aborts.
- Automatic checkpoints must reclaim >=4,096 estimated tokens; no per-Run limit - every later safe boundary re-evaluates trigger and usefulness rules. A checkpoint after a Tool batch can affect the next Model step in the same Run counting restored request-local state; one after final response affects the next Run counting durable neutral Projection.
- Sessions persists checkpoints plus the optional `compaction_policy` metadata override; Settings/Agents/Projects validate the same shape as nullable inheritance overrides; server exposes both override and effective values in listings, WebUI edits all scopes.

## Invariants & Gotchas

- Manual `/compact` runs the selected Strategy as the primary active Run and refuses while another Run is active.
- Automatic Summary+Tail never re-summarizes only its own previous Summary; it advances within long turns once older cycles move to Head, always governed by threshold/usefulness/reclaim floor.
- The latest User message anchors requests: retained with identical id/content across checkpoints until superseded; suffixes starting later insert the anchor ahead of themselves. Pre-invariant checkpoints hiding their User anchor are not rehydrated.
- Failed automatic Compaction settles its started row and leaves the Agentic Run untouched; failed/cancelled manual Compaction terminates its dedicated Run accordingly - neither path appends a checkpoint nor activates `history`.
- Skill activation state changes only on successful checkpoint append - failed/stale/insufficient attempts retain dedup state and environment grants exactly.
- Prompt-refresh inputs prepare before persistence (stored after-count covers the request actually used); metadata commit failure logs without invalidating the checkpoint.
- Do not write new `tail_boundary_id` checkpoints or rewrite old ones - the legacy reader exists solely to preserve persisted history. The removed flat Policy shape has no compatibility parser.
