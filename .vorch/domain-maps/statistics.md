# Statistics

Read-only aggregation over persisted Sessions that produces the WebUI Statistics tab's data in one on-demand scan.

## Overview

`core/statistics/` owns the Statistics domain. It computes a full `StatisticsReport`
by scanning the canonical JSONL Sessions that already exist and **adds no
persistence of its own** — every figure is derived from persisted `ChatMessage`
data ([core/chat/messages.py](../../core/chat/messages.py)). This is a hard
constraint from the user: statistics must be derivable from currently persisted
data, with no new backend storage to feed them.

`StatisticsService(chat_sessions, agents)` is constructor-injected (DI via
minimal `Protocol`s, no globals). `report(since=None, until=None)` walks every
agent → session → message exactly once and returns a frozen dataclass tree;
`StatisticsReport.to_dict()` (uses `dataclasses.asdict`) yields the JSON payload
the RPC returns. Out of scope by decision: any new persistence or derived cache,
cost/pricing, authoritative fallback/subagent attribution, and a per-session
sub-tab. A disposable rebuildable cache (the `core/recall/` SQLite-index pattern)
is the documented future path if the full scan becomes slow — it is **not**
pre-built.

## Data Model

The report tree (frozen dataclasses in `statistics.py`, all JSON-native fields):
`StatisticsReport{generated_at, window, overview, usage, runs, errors, tools}`.

- **overview** — totals (agents, sessions, runs, open_run_groups, messages),
  `messages_by_role` (all eight roles, zero-filled), last activity, run-status
  counts, mean + median run duration, tool-call totals, per-agent activity, and
  a `daily_trend` series.
- **usage** — `totals` plus per-`provider` and per-`(provider, bare-model)`
  records and a daily series. Each record splits tokens into
  measured_input/output and estimated_input/output, with `total_tokens`
  (measured + estimated) provided **only** for ranking/share.
- **runs** — status distribution, cancel/failure rate, duration stats
  (count, mean, P50/P90/P95), longest runs, runs-with-tool-calls,
  derived_fallback_runs, per-agent/per-session/per-day counts.
- **errors** — counts by error_kind, provider, model, agent, hour-of-day, and a
  daily series.
- **tools** — per tool name: calls, successes/failures, success/error rate, mean
  + P95 duration, top `error.code`; plus calls-per-agent and busiest sessions.

## Interfaces

- `StatisticsService(chat_sessions: SessionSource, agents: AgentDirectory)`.
  `SessionSource` needs `list_with_metadata(agent_id)` + `get(agent_id, id).load()`
  (satisfied by `ChatSessionManager`); `AgentDirectory` needs `list()` returning
  objects with `.id` (satisfied by `AgentStore`). Wired in the RPC from
  `runtime.chat_sessions` and `runtime.agents`.
- **RPC `statistics.report`** ([server/rpc/statistics_methods.py](../../server/rpc/statistics_methods.py)):
  optional params `{since?, until?}` (ISO-8601, `Z` accepted), validated —
  unknown fields and inverted/malformed windows are rejected with
  `RPC_ERROR_INVALID_REQUEST`. The service is lazily built and cached on RPC
  `state` (mirrors `operations_methods._log_viewer`). Registered in
  [server/rpc/methods.py](../../server/rpc/methods.py).
- **WebUI**: `webui/src/components/StatisticsView.svelte` calls
  `rpc('statistics.report')` once on mount (plus manual refresh) and renders five
  sub-views (Overview / Usage / Runs & errors / Tools / Limits). All formatting/rollup
  logic is in the pure, unit-tested `webui/src/lib/statisticsView.js`; the nav
  entry (`navigation.statistics`) and route live in `webui/src/App.svelte`.
- **The Limits sub-view is NOT part of this domain.** It is presentation-only: it
  lazily calls the providers-domain `provider.usage` RPC (live subscription usage),
  which never touches `StatisticsService` and adds no persistence — it only happens to
  live behind the same tab. See `providers.md` → Provider Usage Probe and `webui.md`.
  The read-only "no new backend storage, no network" invariant below applies to
  `StatisticsService` only.

## Conventions

- **Run-summary segmentation.** Assistant/tool messages carry no `run_id`; only
  `run_summary` does, and it annotates the immediately preceding Assistant Run.
  Per-run aggregates come from segmenting each session's message list at
  `run_summary` boundaries — the messages between two consecutive `run_summary`
  records form one run group. Run counts, status, duration percentiles, and
  cancel/fail rates come straight from `run_summary` records (exact, not
  estimated).
- **Real vs estimated tokens never merge.** `usage.estimated: true` marks
  estimated turns ([core/chat/chat.py](../../core/chat/chat.py) `_apply_usage_estimation`);
  they are accumulated separately from provider-reported usage and the count of
  estimated turns is reported. The UI badges estimated tokens. Canonical
  `input_tokens` already includes cached tokens, so `cache_read_tokens` /
  `cache_write_tokens` are surfaced only as separate informational totals, never
  added on top.
- **Window semantics.** `since`/`until` filter time-derived aggregates at
  message-timestamp granularity. Structural totals (agent and session counts) and
  last-activity are window-independent snapshots.
- **Percentiles** use the nearest-rank method (`rank = ceil(p/100 · n)`),
  deterministic and tested in [tests/core/statistics/test_statistics.py](../../tests/core/statistics/test_statistics.py).
- **No raw tool arguments anywhere.** Only the tool `name`, `timing`, and the
  `{ok, error.code}` result envelope (validated via
  `core.tools.is_tool_result_envelope`) feed the report.

## Constraints & Gotchas

- **Derived fallback is best-effort, not authoritative.** A run group with ≥2
  distinct bare models is reported as `derived_fallback_runs` (an in-run model
  change). The authoritative `model_fallback_activated` event is in-memory and
  not persisted, so it is not available here; the UI labels this as derived.
- **`open_run_groups` is best-effort "active/unterminated".** A trailing message
  group with conversational activity after the last `run_summary` is counted as
  open. This conflates a genuinely running run with a crashed/interrupted one —
  truly-active runs are in-memory (`ChatRunManager`) and out of this read-only scope.
- **Error attribution is by proxy.** `error` messages reject a `model` field, so
  each error is attributed to the last assistant model seen in its session
  (`unknown` when none precedes it) and to that model's provider.
- **Not derivable from persisted data (kept out of scope):** cost/pricing
  (normalized catalogs carry no pricing) and authoritative fallback /
  subagent-per-run attribution.
