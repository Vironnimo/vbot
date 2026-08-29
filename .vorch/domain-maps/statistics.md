# Statistics

Incrementally indexed aggregation over persisted Sessions producing the WebUI Statistics tab without reparsing unchanged transcripts.

## Overview

`core/statistics/` derives every local activity figure from canonical JSONL `ChatMessage` data while `StatisticsIndex` maintains a disposable compact SQLite read model at `<data_dir>/statistics/session-statistics.sqlite`. Reports reconcile source identity/size/mtime and the Sessions-owned append cursor, ingest only validated new tails, prune removed scopes, then aggregate compact records; schema mismatch, replacement, corruption, or invalid cursors rebuild derived state rather than touching JSONL. The normalized limit observations under `statistics/provider-usage/` stay Provider-owned upstream data (`providers/usage.md`) - colocation makes Statistics neither writer nor cache owner. A fork's copied prefix is history, not activity: only records after `fork_source.message_count` index.

`StatisticsService(chat_sessions, agents, projects=None, skill_inventory=None)` is constructor-injected via minimal Protocols. Reporting covers Identity Agents plus every project-scoped Session (project agents under `agent@projekt`, identity bare); composite keys prevent identity/project UUID collisions. Out of scope by decision: cost/pricing, authoritative fallback/subagent attribution, per-session sub-tab.

## Report sections

Frozen dataclass tree `{generated_at, window, overview, usage, runs, compactions, errors, tools, skills}`; aggregation in `statistics.py`, SQLite reconciliation + minimal message projection in `index.py`, skill usage in `skills.py`. The index stores scoped cursors plus compact records holding only aggregation-consumed fields - raw text, Reasoning, arguments/result payloads, Skill content never enter it.

- **overview** - structural totals, role counts split into visible Chat messages (`user` always; `assistant` only with non-blank text - Thinking/Tool-only steps don't count) versus all ten canonical stored roles zero-filled, including internal `history_edit` controls, run-status counts, durations, per-agent activity, daily trend with outcome splits.
- **usage** - totals plus per-provider/per-model records with input/output each split measured vs estimated (`total_tokens` only for ranking); cache section with worst hit-rate sessions (minimum reporting floor) and suspected breaks.
- **runs** - status distribution, cancel/failure/interruption rates, percentiles, longest runs, tool-call stats, derived fallback count, per-agent/session/day counts, Agentic-Loop depth (`agent_messages` visible text vs `model_steps` all responses).
- **compactions** - checkpoint-derived totals, per-session statistics by Strategy, estimated reclaimed context from checkpoints carrying both token sizes (legacy checkpoints count as Compaction but not reclaim), top sessions.
- **errors** - by kind/provider/model/agent/UTC-hour/daily.
- **tools** - per name: calls, Accepted/Rejected rates (`ok:true/false` - rejection is not malfunction), durations, top error codes, busiest sessions. No raw arguments anywhere - name, timing, envelope code only.
- **skills** - one row per current-inventory Skill joined at build time: origins (multiple on cross-scope collisions - usage keys on bare name deliberately), offered (from `seen_skills` sidecar) vs activated (both carrier parsers) vs their intersection, usage rate (`null` only without offer evidence), first/last timestamps, per-agent activations, evidence-backed delete/improve candidates separated from missing-data Skills. Deleted Skills drop out entirely.

## Interfaces

Both report RPCs reconcile through the dedicated two-worker Statistics pool with backpressure; startup warmup runs independently.

- `statistics.report {since?, until?}` validates ISO windows strictly; the service caches on RPC state and offloads reconciliation so filesystem/SQLite work never blocks the loop.
- `statistics.run_activity {since, until}` returns Runs overlapping the requested interval (newest first, cap 200) for temporal correlation with usage changes - explicitly not causality attribution.
- WebUI renders seven sub-views from one mount-time call plus refresh; formatting/rollup logic stays in pure unit-tested `statisticsView.js`. Presentation labels carry the semantic distinctions this map defines (visible vs records, UTC buckets, derived heuristics labelled).
- CLI `vbot statistics compactions` formats the same section without rescanning.
- **Limits crosses two owners:** while visible, the view polls Providers-domain `provider.usage` immediately then every 10 s; `LimitHistory` separately refreshes Provider-owned history every 60 s. Selecting an interval requests the Statistics-owned Run projection. Statistics performs no network access and writes only its disposable index.

## Conventions

- **Run-summary segmentation:** messages between consecutive `run_summary` records form run groups; run counts/status/durations come straight from summaries - exact, not estimated.
- **Canonical-first projection:** JSONL appends succeed independently; Statistics is never a Chat write dependency. Reconciliation consumes raw append-only records, including superseded lineage, so a message edit preserves historical Usage, Runs, Compactions, errors, and Tool activity while adding one `history_edit` record. It applies metadata-only changes without transcript reads, ingests growth from cursor, rebuilds replaced/truncated Sessions, deletes missing scopes; generation-stamped snapshots reuse only when nothing changed.
- **Fork prefixes excluded** from all activity windows via `fork_source.message_count`; structural counts, current offer sidecar, and post-fork messages count normally; invalid hand-edited provenance fails open unsliced.
- **Real vs estimated tokens never merge:** field-level flags feed matching buckets; turn-level `estimated` remains the legacy signal; cached tokens are already inside canonical input so cache figures surface separately, never added on top; reasoning tokens are a measured-output subset.
- **Cache figures require field presence and measured input** - a non-caching provider must never read as 0% hit rate; rates divide by cache-reported input; any estimated-input turn breaks cache comparison.
- Windows filter time-derived aggregates at message-timestamp granularity; structural totals are window-independent snapshots. Percentiles use nearest-rank.
- Offered filters by offering Session's `created_at` (sidecar carries no per-Skill timestamps - deliberate approximation); conversion uses only the per-Session offered+activated intersection, staying within 0-100%.

## Constraints & Gotchas

- **The SQLite index is not a second source of truth:** replace-not-migrate schema version, deletable on failure, must stay fully rebuildable - no user-owned or non-derivable data in it. Source cursors validate the processed prefix; mismatches rebuild rather than repair.
- Derived fallback detection (>=2 distinct models per run group) is best-effort and labelled derived - the authoritative event is in-memory only.
- Suspected cache breaks are a heuristic comparing consecutive measured turns, skipping legitimately explained misses (first turn, checkpoint/takeover boundaries, model switches, >300s gaps, small prompts, post-estimated turns) with a 0.5 read-ratio incident floor - false negatives accepted by design.
- `open_run_groups` conflates running with crashed/interrupted; truly active Runs live in memory outside this projection's authority.
- Error attribution proxies to the last assistant model seen (`unknown` when none).
- Sessions predating `seen_skills` contribute activations but no offers - accepted lower bound, never presented as delete candidates.
