## Plan: Statistics Tab (read-only aggregation over existing session data)

**Goal:** A new `Statistics` WebUI tab with four sub-views — Overview, Usage (Provider/Model), Runs & Errors, Tools — computed entirely on demand from the data already persisted in session JSONL files, with no new backend storage.

> This plan is written to be executed by a fresh session with no prior context. Everything needed is below.

## Orientation (read first)

vBot is a local-first agent harness: one async Python kernel (`core/`) behind a FastAPI server (`server/`), a Svelte WebUI (`webui/`, JavaScript — **no TypeScript**), a pywebview desktop shell, and a CLI. Communication is `POST /api/rpc` (method dispatch) + `/ws` + SSE.

Mandatory project reads before coding (project rule): `.vorch/PROJECT.md` (architecture, conventions, specs index, dev/test commands) and `.vorch/GLOSSARY.md` (terms). For each domain you touch, read its spec under `.vorch/specs/` — the `read:` field on every task below lists the relevant ones. Work directly on `main`; commit per logical unit with conventional-commit messages; run the quality gates before committing.

Conventions that bind this work: constructor dependency injection via `__init__` with `typing.Protocol` interfaces (no globals/service-locator); UTC ISO-8601 timestamps persisted, UI renders in user timezone; structured logging via `LogManager` (`vbot.<domain>` loggers, no `print`); **no legacy/migration branches** (read current format only); every user-visible WebUI string goes through `t(...)` in `webui/src/lib/i18n.js`.

Quality gates (must be green before commit):
```bash
python scripts/quality.py [paths...]            # backend: format → lint → type-check → test
python scripts/quality-frontend.py [paths...]   # frontend: + build
```
Run the app for live verification (see `.vorch/PROJECT.md` → Run, and `.vorch/TESTER.md` for live-testing): `python server/main.py` then open the WebUI; the desktop shell is `python desktop/main.py`.

**Context:**

vBot now generates a lot of data and the user wants a Statistics tab. Hard constraint from the user: **everything must be derivable from currently persisted data — no new backend storage to feed statistics** (a disposable rebuildable cache may come later, explicitly out of scope here).

Data ground-truth, verified against the code (do not re-derive):

- All statistic-relevant facts live in persisted `ChatMessage` JSONL lines ([core/chat/messages.py](../../core/chat/messages.py)). Canonical fields: `id`, `timestamp` (UTC ISO), `role`, `content`, `model`, `reasoning`/`reasoning_meta`, `usage`, `timing`, `tool_calls`, `tool_call_id`, `name`, `error_kind`, `tail_boundary_id`, `run_id`, `status`, `sender`.
- **Roles:** `system`/`user`/`assistant`/`tool`/`note`/`error`/`compaction_checkpoint`/`run_summary`. Agents = directories under `<data_dir>/agents/<id>/`; sessions = `.jsonl` files under `agents/<id>/sessions/`.
- **Runs:** `run_summary` lines carry `run_id`, `status` (`completed`/`failed`/`cancelled`) and `timing.duration_ms` + `started_at`/`completed_at`. This is exact, not estimated — the source for run counts, status distribution, duration percentiles, cancel/fail rate.
- **Model/Provider:** `model` is on every assistant/system message as `<provider>/<model-id>` (optionally `::<connection>[:<account>]`). Provider = segment before the first `/`; strip the `::…` suffix with the existing parser in [core/chat/model_resolution.py](../../core/chat/model_resolution.py) (`parse_bare_model`).
- **Tokens:** `usage` is on **every** assistant message — real provider numbers, or estimated via [chat.py:732](../../core/chat/chat.py#L732) `_apply_usage_estimation` with `estimated: true`. Real and estimated tokens must be tracked and shown **separately** and never summed into one "true" number; report counts of estimated vs measured turns.
- **Tools:** tool messages have `name` + `timing.duration_ms`, and `content` is always the envelope `{ok, error, data, artifacts}` ([core/tools/tools.py:210-236](../../core/tools/tools.py#L210-L236)). `ok: false` → failure; `error.code` → failure category. Reuse the envelope validator in `core/tools/tools.py` rather than re-parsing by hand.
- **Errors:** `error` messages carry `error_kind` (`rate_limit`, `timeout`, `network_error`, `provider_overloaded`, `tool_iterations_exceeded`, `auth_error`, `provider_fatal`, `config_error`, `provider_error`) — the basis for the error breakdown.
- **Compaction:** `compaction_checkpoint` carries `usage.compacted_token_count`.

**Key algorithmic insight — per-run aggregation without `run_id` on every message:** assistant/tool messages carry no `run_id` (only `run_summary` does). To get per-run metrics (tool-calls-per-run, model-per-run, fallback) you **segment each session's message list at `run_summary` boundaries**: a `run_summary` annotates the immediately preceding Assistant Run, so the messages between two consecutive `run_summary` records form one run group. From a run group derive: the run's model(s) (assistant `model` fields), tool-call count, error presence, and — **best-effort fallback detection** — a run group containing ≥2 distinct bare models means a mid-run model switch (this is a derived signal, clearly labeled, NOT the authoritative in-memory `model_fallback_activated` event, which is not persisted).

Not derivable from persisted data (kept OUT of scope, see Scope):

- **Cost (€/tokens-priced):** normalized model catalogs (`resources/models/*.json`) carry no pricing; only `openrouter.raw.json` (a raw dump the app doesn't consume) does.
- **Authoritative fallback / subagent-per-run:** `model_fallback_activated` and `subagent_session_started` are in-memory Run events, not persisted messages. (Derived fallback via model-switch is in scope; subagent attribution is not.)

**Requirements (from the user's list — the Option B subset; verbatim items grouped by tab):**

- *Overview:* total agents; total sessions; total runs; active/running runs; total messages by role (user/assistant/tool/error/note/run_summary); last activity overall; last activity per agent; average run duration; median run duration; error rate per day/week/month; tool-call share per run; run status distribution (completed/failed/cancelled); daily trend (runs + errors).
- *Usage (Provider/Model):* per provider/model — runs, model calls, estimated input tokens, estimated output tokens, total tokens, share of total, average run duration, error rate; top models; top providers; time series (tokens / runs / errors per day). Distinguish persisted real usage from estimated tokens.
- *Runs & Errors:* runs per day / per agent / per session; average run duration; P50/P90/P95 run duration; longest runs; runs with tool calls; runs with fallback (best-effort, derived); run status distribution; cancel rate; failed rate; tool-iterations-limit errors; provider-error vs timeout vs rate-limit. Errors grouped by error_kind / model / provider / agent / session / time; errors per day; top error kinds; top agents/models with errors; rate-limit / timeout / auth / tool / provider-fatal frequency; errors by time of day.
- *Tools:* per tool — call count, success rate, error rate, average duration, P95 duration, top error `code`; tools with frequent errors; tools with long runtimes; tools by agent; tools by session. **Never expose raw tool arguments** — not even hashed in v1.

**Scope:**

- **In:** new read-only `core/statistics/` aggregation domain; one server RPC returning a full statistics report (optional time-window param); new `Statistics` WebUI tab with 4 internal sub-views; i18n; tests; new spec + PROJECT.md index update.
- **Out:** any new persistence or derived cache (on-demand full scan only in v1); cost/pricing; authoritative fallback/subagent-from-events; Logs / Attachments / Skills / Extensions / Memory statistics (future "System Health" surface); a dedicated Sessions sub-tab (data is available; deferred to a later iteration).

**Assumptions & Constraints:**

- **Pure read side.** The aggregator only reads via `ChatSessionManager` (`list_with_metadata`, `get(...).load()`) and the agents service for the agent-id list. It constructs no session paths and writes nothing.
- **Single pass, single payload.** One core aggregation walks every agent → session → message exactly once and produces a structured report; the RPC returns the whole report and the four tabs render slices of it. At current data volume an on-demand full scan is acceptable; if it becomes slow later, a disposable rebuildable cache (the `core/recall/` SQLite-index pattern) is the future path — not now.
- **Time series granularity = day.** Core returns daily buckets keyed by the user-timezone-independent UTC date; week/month rollups and locale rendering happen in the frontend (`activeLocaleTag()` per the webui conventions).
- **Real vs estimated tokens never merged.** Report carries `input_tokens`/`output_tokens` split into measured vs estimated, plus the count of estimated assistant turns, so the UI can badge them.
- **No new runtime persistence schema, no legacy/migration branches** (per PROJECT.md conventions). Validation of the RPC param goes through the existing server validation patterns.
- **Charts are hand-rolled SVG — no charting dependency** (decided with the user). The visuals are small (status donut + daily bars/sparkline); build them as inline SVG with project styling. Do not add a chart library.

### Concrete interfaces & wiring (verified against the code)

Use these exact entry points — they have been checked, do not re-derive or guess names:

- **Runtime services** (`core/runtime/runtime.py`, public properties): `runtime.agents -> AgentStore`, `runtime.chat_sessions -> ChatSessionManager`, `runtime.storage.data_dir -> Path`.
- **Agent listing:** `AgentStore.list() -> list[Agent]` (sorted by id); each `Agent` has `.id`. This is the agent-id source for the scan. The `StatisticsService` should depend on a minimal `Protocol` (e.g. `agent_ids()` or accepting the `AgentStore`), wired from `runtime.agents`.
- **Sessions** (`core/sessions/`): `ChatSessionManager.list_with_metadata(agent_id) -> list[summary]` where each summary has `id`, `created_at`, `last_active_at` plus sidecar fields (derived cheaply from bookend timestamps — use it for created/last-active). `ChatSessionManager.get(agent_id, session_id).load() -> list[ChatMessage]` returns validated canonical messages in append order. Never construct `.jsonl` paths directly.
- **Message model** (`core/chat/messages.py`): `ChatMessage` dataclass with the fields listed in Context. `to_dict()` gives the JSON shape if you need it.
- **`usage` dict fields:** `input_tokens`, `output_tokens` (always present), optional `cache_read_tokens` / `cache_write_tokens`, and `estimated: true` when token counts were estimated rather than provider-reported. **Caveat:** canonical `input_tokens` already includes cached tokens — do NOT add cache fields on top of `input_tokens` (it would double-count). Track cache tokens only as a separate informational figure if shown at all.
- **Model parsing** (`core/chat/model_resolution.py`): `parse_bare_model(model)` strips the optional `::<connection>[:<account>]` suffix. Provider = the segment before the first `/` of the bare model (e.g. `openrouter/anthropic/claude-sonnet-4` → provider `openrouter`).
- **Tool result envelope** (`core/tools/tools.py`): every tool message `content` is JSON `{ok: bool, error: {code, message} | null, data, artifacts}`. There is an envelope validator in that module — reuse it; `ok: false` → failure and `error.code` is the failure category.
- **RPC handler shape** (`server/rpc/operations_methods.py` is the closest sibling pattern): a handler is `def _handler(state: Any, params: JsonObject) -> JsonObject` (may be `async def`); it reads services off `state.runtime.<service>`; lazily build/cache a service on `state` exactly as `_log_viewer(state)` does for `LogViewer`. A methods module exposes `method_handlers() -> dict[str, RpcMethodHandler]`. Register the module by adding it to the import group **and** the registry tuple in `server/rpc/methods.py:build_method_handlers()`.
- **WebUI transport** (`webui/src/lib/api.js`): call `rpc('statistics.report', params)`; transport errors normalize to `ApiClientError`. **Nav registration** lives in `webui/src/App.svelte` — add a `NAVIGATION_ITEMS` entry `{ id, labelKey, labelFallback }`, import the view, and add an `{:else if activeViewId === 'statistics'}` branch (mirror the existing `logs` wiring). Locale via `activeLocaleTag()` from `i18n.js` for all `Intl`/`toLocaleString` calls.

### Milestones

| # | Milestone | Deliverable |
|---|---|---|
| M1 | Core aggregation | `core/statistics/` computes a full `StatisticsReport` from JSONL in one pass; unit-tested against synthetic sessions covering every role, run segmentation, real+estimated usage, tool envelopes, errors. |
| M2 | RPC contract | `statistics.report` returns the report (optional `{ since, until }` window); registered and tested. |
| M3 | WebUI tab | `Statistics` nav item + `StatisticsView.svelte` with Overview / Usage / Runs & Errors / Tools sub-views, pure helpers in `lib/statisticsView.js`, i18n, Vitest. |
| M4 | Docs | `.vorch/specs/statistics.md` + PROJECT.md specs index / core-modules entry. |

### Phase Breakdown

#### Phase 1: Core aggregation domain
**Goal of this phase:** A DI-constructed `StatisticsService` that scans all sessions once and returns a `StatisticsReport` dataclass tree covering all four tabs' needs.
**Can run in parallel with:** none (foundation).

- Implement the domain — read: [.vorch/specs/sessions.md], [.vorch/specs/recall.md] (scan pattern), [.vorch/specs/chat.md] (message shape), [.vorch/specs/models.md] (model-id convention) — files: [core/statistics/__init__.py, core/statistics/statistics.py]
  - `StatisticsService(chat_sessions, agents)` via constructor injection; `agents` is a minimal `Protocol` exposing the agent-id list (wired from the runtime's agents service). No global singletons.
  - One pass: for each agent id → `chat_sessions.list_with_metadata(agent_id)` for session created/last-active + `chat_sessions.get(agent_id, id).load()` for messages. Accumulate:
    - **Totals:** agents, sessions, messages-by-role, last-activity overall + per agent (from `list_with_metadata`).
    - **Runs:** segment each session at `run_summary` boundaries. Per run group capture status, `duration_ms`, distinct bare models (≥2 → derived fallback), tool-call count, error presence. Aggregate to: run count (total/per agent/per session/per day), status distribution, duration mean + P50/P90/P95 (compute percentiles in core), longest runs, runs-with-tool-calls, derived-fallback count, cancel/fail rates.
    - **Usage:** per `(provider, bare-model)` — run count, assistant-call count, summed input/output tokens split measured-vs-estimated, estimated-turn count, error rate, avg run duration; plus daily token/run/error series. Provider = pre-`/` segment after stripping `::…` via `parse_bare_model`.
    - **Errors:** by `error_kind`, by model, by provider, by agent, daily series, by hour-of-day.
    - **Tools:** per tool name — call count, success/error counts (from the `{ok,error}` envelope via the `core/tools/tools.py` validator), avg + P95 `timing.duration_ms`, top `error.code`; plus per-agent and per-session breakdowns. No raw arguments anywhere.
  - Optional `since`/`until` filter applied at message-timestamp granularity.
  - Reuse existing helpers; do not reimplement model parsing or tool-envelope validation.
- Unit tests — files: [tests/core/statistics/test_statistics.py]
  - Synthetic sessions exercising: every role; multi-run sessions (run-summary segmentation); a run group with two distinct models (derived fallback); measured vs `estimated: true` usage kept separate; tool success and failure envelopes with `error.code`; each `error_kind`; empty sessions; `since/until` windowing; percentile correctness (P50/P90/P95).

**Dependencies:** none.
**Done when:** `python scripts/quality.py core/statistics/` is green and the tests assert each report section against hand-computed expected values.

#### Phase 2: Server RPC
**Goal of this phase:** Expose the report over RPC.
**Can run in parallel with:** none (depends on Phase 1; touches the shared registry).

- New methods module — read: [.vorch/specs/server.md], [.vorch/specs/runtime.md] — files: [server/rpc/statistics_methods.py]
  - `statistics.report` handler: optional params `{ since?, until? }` (ISO UTC) validated through the existing server validation approach; reject unknown params like sibling handlers. Build/obtain `StatisticsService` from runtime state (mirror how `operations_methods` lazily builds `LogViewer`), call it, return the report as a JSON-serializable dict. Strip nothing sensitive needed — there is no opaque provider metadata in the report by construction (no raw tool args, no reasoning_meta).
  - `method_handlers()` returns `{ "statistics.report": ... }`.
- Register the module — files: [server/rpc/methods.py]
  - Add `statistics_methods` to the import group and to the registry tuple in `build_method_handlers()`. **Shared file — sequential, no parallel task may touch it.**
- Tests — files: [tests/server/rpc/test_statistics_methods.py]
  - Handler returns the expected shape for a seeded data dir; rejects bad params; empty-data case returns zeroed report without error.

**Dependencies:** Phase 1.
**Done when:** `python scripts/quality.py core/statistics/ server/rpc/statistics_methods.py` green and a dispatched `statistics.report` call returns the report.

#### Phase 3: WebUI Statistics tab
**Goal of this phase:** The four-sub-view tab, rendered from one `statistics.report` call.
**Can run in parallel with:** none as a phase (depends on the Phase 2 contract), but tasks **within** it parallelize where file-scopes are disjoint.
Read for all tasks: [.vorch/specs/webui.md], [.vorch/DESIGN.md].

- Pure display/formatting helpers ⚡ *parallel with the component task* — files: [webui/src/lib/statisticsView.js, webui/src/lib/__tests__/statisticsView.test.js]
  - Number/token/percent/duration formatting (locale via `activeLocaleTag()`), measured-vs-estimated token split rendering, daily→week/month rollup, provider→model grouping for the Usage table, top-N selection, percentile labels. Keep all non-trivial logic here (unit-testable), components stay display-only.
- `StatisticsView.svelte` — files: [webui/src/components/StatisticsView.svelte, webui/src/components/__tests__/StatisticsView.test.js]
  - Loads `statistics.report` once on mount (and a manual refresh control), holds the active sub-view in local state, renders Overview / Usage / Runs & Errors / Tools. Visuals (status donut, daily bars/sparkline) are hand-rolled inline SVG — **no charting dependency** (decided). All strings via `t(...)`. Estimated tokens carry a visible "~ estimated" badge. No raw tool arguments rendered.
- Wire navigation + transport — files: [webui/src/App.svelte, webui/src/lib/api.js]
  - Add the `{ id: 'statistics', labelKey: 'navigation.statistics', labelFallback: 'Statistics' }` nav item, import + route `StatisticsView`. Optional thin `api.js` wrapper only if it improves ergonomics; otherwise call `rpc('statistics.report', …)` directly. **Both shared files — sequential, do after the two tasks above.**
- i18n — files: [webui/src/lib/i18n.js, webui/src/lib/__tests__/i18n.test.js (or the existing i18n test path)]
  - Add `navigation.statistics` and all Statistics copy to every locale object with English fallback; update/extend the i18n key-parity test. **Shared file — sequential.**

**Dependencies:** Phase 2 (RPC contract).
**Done when:** `python scripts/quality-frontend.py` green (format/lint/test/build), and the tab renders all four sub-views from a `statistics.report` response in the running app.

#### Phase 4: Spec & docs
**Goal of this phase:** Document the new domain per project rules.
**Can run in parallel with:** Phase 3 (disjoint files), once the Phase 1/2 contract is settled.

- New spec — **read [.vorch/workflows/spec-workflow.md] first** — files: [.vorch/specs/statistics.md]
  - Factual working notes: the read-only-aggregation boundary, the run-summary segmentation rule, real-vs-estimated token handling, the derived-fallback caveat, the `statistics.report` contract, and what is explicitly NOT derivable (cost, authoritative fallback/subagent). Every claim backed by source/tests.
- Index it — files: [.vorch/PROJECT.md]
  - Add `core/statistics` to the Core modules list and a `.vorch/specs/statistics.md` row to the Specs index table.

**Dependencies:** Phases 1-2 (so the documented contract is real).
**Done when:** the spec exists, follows spec-workflow rules, and PROJECT.md references it.

### Risks & Mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Full scan slow as data grows | Med | Med | Single pass for all aggregates; one RPC call; document the disposable-cache path (recall pattern) as the explicit future step — do not pre-build it. |
| Estimated tokens misread as real spend | Med | High | Never merge measured + estimated; surface both with counts; UI badge. |
| Run segmentation wrong for internal/automation runs (no `user_message_persisted`) | Med | Low | Segment strictly on `run_summary` boundaries (present for all run types); cover internal-run shape in tests. |
| Derived fallback mistaken for authoritative | Low | Med | Label it "derived from in-run model change"; document the caveat in the spec and UI copy. |
| Raw tool arguments leaking into stats | Low | High | Aggregate only tool `name`, `timing`, and `{ok,error.code}`; never read `tool_calls.arguments` into the report; assert in tests. |
| Parallel edits to shared files (`methods.py`, `App.svelte`, `i18n.js`) | Med | Med | Marked sequential; no `⚡` on shared-file tasks. |
