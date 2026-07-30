# Compaction

Policy-driven Context transformation and checkpoint creation for chat Sessions.

## Overview

`core/compaction/` owns the provider-neutral Engine that evaluates Triggers, asks one Strategy for a Plan, performs zero or one Model call, validates the resulting Projection, and returns one self-contained `compaction_checkpoint`. It never rewrites or deletes prior Session JSONL records. The chat loop owns safe execution points and runtime resolution; settings, Agent, Project, and Session layers own persisted Policy configuration.

Core cross-cutting terms (Session, Agent, Model, Tool) live in `.vorch/GLOSSARY.md`.

## Data Model

- A resolved Compaction Policy is `{enabled, trigger, strategy}`. Built-in Triggers are `{type: "context_ratio", threshold}` and `{type: "input_tokens", tokens}`. Built-in Strategies are `{type: "summary_tail", tail_tokens, summary_model}` and `{type: "continuation"}`.
- `CompactionSettings` is the typed runtime view of one resolved Policy. It is not the persisted schema and must not become a second settings source.
- `CompactionPlan` is the Strategy/Engine boundary: optional Model messages and target (`active` or `summary`), ordered messages before/after the returned text, optional zero-call text, and cumulative compacted-token count. The Strategy receives the previous checkpoint's plain Summary separately from newly compactable history. A Plan permits zero or one Model call; the Engine never loops or performs follow-up calls.
- A new `compaction_checkpoint` stores its plain summary in `content`, a canonical message `projection`, cumulative `usage.compacted_token_count`, and Policy/Strategy provenance. The constructor guarantees the Projection starts with the matching `[compaction-summary]` note and removes Assistant `reasoning`, `reasoning_meta`, and internal `reasoning_scope`: vBot Compaction is textual Summary+Tail state, not a Provider-native opaque reasoning token. Assistant `phase` remains semantic history. After persistence, Chat adds an ordinal model-facing reminder that the Session-scoped `history` Tool can retrieve the hidden canonical messages. The latest checkpoint's Projection plus messages appended after that checkpoint is the effective Context; hidden pre-checkpoint JSONL history is never re-read into later compaction. Existing boundary-based checkpoints remain valid read-only input: their stored summary plus the surviving slice from `tail_boundary_id` is materialized in memory as the effective Projection, then any later compaction writes only the new format.

## Interfaces

- `CompactionService.should_auto_compact(...)` selects the configured Trigger from the registry and evaluates it against current input tokens and the resolved Context window.
- `CompactionService.has_new_compactable_context(...)` preflights automatic Strategy usefulness. Summary+Tail returns true when either a new pre-tail message is eligible for summarization or deterministic Tool aging inside the retained tail can reclaim at least 4,096 estimated tokens. Manual Compaction bypasses this preflight.
- `CompactionStrategy.plan(context, settings) -> CompactionPlan` is the extension seam for programmed Strategies. A Strategy receives the current effective canonical messages, an optional exact provider-request snapshot, previous cumulative count, manual instruction, and prompt-fragment storage; it performs no Model I/O itself.
- `CompactionService.compact(...) -> ChatMessage` selects the Strategy, executes at most one planned Model request, assembles the Projection, validates complete Tool cycles, optionally enforces a caller-provided minimum estimated reclaim, and returns the checkpoint.
- `SummarizationStrategy` chooses a complete user-turn tail, sends the previous Summary in `<previous_summary>` and only newly compactable messages in `<history>`, renders Tool Results as bounded redacted structural digests, and calls the configured Summary Model once when history needs summarization. In the retained tail it deterministically ages older completed Tool batches, including oversized Tool Call arguments, while preserving the newest completed batch and every incomplete batch verbatim. When only Tool aging is needed, it reuses the previous Summary or a deterministic current-Run handoff and performs no Summary Model call.
- `ContinuationStrategy` requires the exact active provider request, appends one internal `<system-reminder>` asking for checkpoint Context, calls the active Model once with the same Tool definitions, and uses the one text response directly as the Projection. This append-only request shape is the cache-preserving strategy; any provider cache hit still depends on the provider's exact-prefix rules.
- `find_tail_boundary(messages, tail_tokens)` returns the user-message id at which a Summary+Tail Strategy's verbatim tail begins.

## Policy Resolution

The chat loop resolves a Policy at every compaction decision, in this order: Session metadata override → Agent effective Policy (including a Project member override) → global settings. Absence means inheritance, so later global or Agent changes affect existing Sessions immediately without rewriting metadata. A Policy change never triggers compaction by itself and never reprocesses hidden history; the next normal or manual compaction consumes the existing checkpoint Projection under the newly resolved Policy.

## Cross-Domain Contracts

- `core/runtime/` constructs one registry-backed `CompactionService` shared by the canonical ChatLoops.
- `core/chat/` evaluates auto-compaction only at safe completed Model boundaries, using the greater of current request estimation and available Provider input usage, resolves effective Policy and adapters, supplies the exact active request snapshot for Continuation, appends the checkpoint with History guidance, refreshes prompt-only inputs, and rebuilds Provider Context plus the effective Tool set from one freshly loaded Session snapshot. The post-checkpoint refresh rescans every Skill source, replaces the prompt-epoch Skill catalog and `seen_skills`, rebuilds the Working Project input from the admitted cwd plus the Project's current display name/auto-load list, refreshes a Config Agent's prompt body without replacing the admitted runtime Agent, and reruns normal System Prompt assembly; Model target, Connection, Tool policy, Agent identity, working Project identity, and cwd remain Run-stable. Automatic checkpoints must reclaim at least 4,096 estimated tokens; there is no per-Run Compaction limit, so every later safe boundary evaluates the normal Trigger and Strategy usefulness rules again. A checkpoint after a Tool batch can therefore expose `history` to the next Model request in the same Run; a checkpoint after the final Assistant response exposes it on the next Run.
- `core/sessions/` persists checkpoints and the optional Session Policy override under metadata key `compaction_policy`.
- `core/settings/`, `core/agents/`, and `core/projects/` validate the same complete Policy shape. Agent and Project-member values are nullable inheritance overrides.
- `server/` exposes Session override mutation and returns both `compaction_policy_override` and `compaction_policy_effective` in Session listings. The WebUI edits the same Policy shape globally and at Agent, Project-member, and Session scopes.

## Invariants & Gotchas

- Automatic compaction is enabled/disabled by Policy. Manual `/compact` still executes the selected Strategy but refuses while a Run is active.
- Automatic Summary+Tail never re-summarizes only its previous Summary while the same retained user turn keeps growing. A long Tool-heavy Run becomes compactable only when older completed Tool payloads can cross the reclaim floor; already-aged payloads are idempotent, and threshold evaluation, the reclaim floor, and the per-Run cap prevent blind per-step Compaction.
- A Strategy cannot split or orphan an Assistant Tool-call cycle in its retained Projection. Summary+Tail boundaries begin on User messages, Tool aging changes only canonical copies inside the new checkpoint Projection, and the append-only Session JSONL remains untouched.
- Continuation runs only when the chat loop can provide a completed active request snapshot. At a mid-tool safe point it waits until the final assistant boundary rather than synthesizing a different prefix.
- Compaction is a hard boundary for cross-Run Provider reasoning. A mid-Run rebuild may restore the live Tool cycle's opaque state by message id, but that restoration is request-local and is not written back into the checkpoint Projection. Request-only rich Tool Result media is not restored onto an aged Tool digest.
- Raw Tool Result bodies and Skill instruction bodies are not copied into the Summary+Tail prompt. Tool outcomes remain available as bounded, structured, sensitive-key-redacted digests; Skill contents are restored deterministically by the Session activation contract rather than summarized from their Tool payload.
- Failed automatic compaction logs and leaves the active Run untouched. Manual failures become the existing user-facing `Compaction failed: ...` reply. Neither failure path appends a checkpoint or activates `history`.
- Prompt refresh happens only after the checkpoint persists. A refresh failure logs and keeps the previous prompt snapshots while the successful checkpoint remains valid; manual and automatic Compaction share this rule.
- Summary Model resolution and fallback remain chat-loop responsibilities because only that layer knows the active provider/connection. Continuation always targets the active Model/connection.
- Do not write new `tail_boundary_id` checkpoints or rewrite old ones. The old checkpoint reader exists solely to preserve already persisted Session history under the explicit cumulative-compaction contract. The removed flat `{auto, threshold, tail_tokens, summary_model}` Policy has no compatibility parser.
