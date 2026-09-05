# Tools

Tool metadata registry, Tool Access Policy resolution, provider definitions, context-aware execution, stable result envelopes, display metadata, and async dispatch.

## Overview

`core/tools/` owns canonical contracts, registry, declarative family/activation/constraint metadata, and the Tool Access Policy resolver. Registration compiles immutable JSON Schemas before exposure; a Tool may additionally select a configuration-bound Definition Profile per Agent. Dispatch uses the exact model-facing input schema from that Provider cycle to normalize a copied argument object for common unambiguous Model encodings, normally validates before the handler, and validates successful result data afterward. A deliberately batched Tool may set `handler_validates_arguments=True` when whole-call schema rejection would violate item independence; its handler must then validate the complete root plus every item before that item's side effects. Sibling calls schedule concurrently by default within bounded limits while preserving explicit ordering barriers; expected failures become stable result envelopes. Chat may apply an additional route-specific visibility filter after definitions are built (currently gating `analyze_image` on route image capability plus live binding/wire availability) without touching registration or permissions. Concrete built-in Tool behavior lives in child maps under `.vorch/domain-maps/tools/`.

## Terms

Domain vocabulary. The core Tool term lives in `.vorch/GLOSSARY.md`.

### Readiness
**Definition:** A per-tool, cheap, I/O-free predicate (`Tool.ready`) evaluated at every prompt/tool-definition build; filter order is registered -> policy-allowed -> ready. A not-ready Tool vanishes from System Prompt and provider definitions but stays visible with its explanation in configuration surfaces so persisted policy survives; direct dispatch returns a clean `tool_not_ready` envelope instead of running. A raising predicate counts as not-ready (logged once at warning).
**Not:** A permission - policy answers "may this Agent use it", readiness answers "can it run right now". Also not stored Extension state: the Extensions tab's waiting status is a derived display value.

### Session Grant
**Definition:** A capability Chat derives while building one Session's request state and carries through provider definitions and dispatch. Example: `history` appears only after persisted Compaction state makes it meaningful.
**Not:** A Run-local input, Agent/Project configuration, catalog choice, or permission to address another Session.

### Definition Profile
**Definition:** A model-facing description + input schema selected from stable persisted configuration for an Agent, keyed by an immutable profile key; unchanged configuration must reproduce byte-identical provider definitions, transient state must never select a different profile.
**Not:** The canonical contract, a per-Run schema, readiness, or an execution-time fallback.

## Contracts

- **Result envelope:** `{ ok, error, data, artifacts }` - success uses `error: null`, failure `data: null` plus `error.code`/`error.message`. The top-level key set is exact (`is_tool_result_envelope()`); retry-signalling fields go *inside* `error`: optional `retryable: bool` and `attempts_made: int` tell the Model whether a failure is transient and how many attempts were already made (a tool exhausting its own retries sets `retryable=True` with the real count; validation/fatal failures omit or set false). Network tools share the retry policy in `core/utils/http_status.py` (`is_retryable_status`, `parse_retry_after`) and backoff math via `core/utils/retry.compute_retry_delay`.
- **Timing** is never inside the envelope: completed calls expose a sibling `timing {started_at, completed_at, duration_ms}` (monotonic, non-negative) on Run events and persisted Tool messages - never forwarded to adapters.
- **Rich-media artifact:** `read_media_artifact(...)` builds the one cross-Tool artifact `{kind: "read_media", attachment_id, filename, media_type}` used by `read` (local images) and `web_fetch` (image URLs); Chat resolves it as Run-local content on the correlated Result. New image-returning Tools reuse this builder rather than hand-rolling shapes.
- **Display metadata** (`ToolDisplay`) is presentation-only per-invocation data: typed facts (counts, line ranges, +/- changes) recorded via `ToolContext.add_display_count()`/`add_display_line_changes()`, assembled into a versioned payload that never enters provider definitions or result envelopes. Every built-in registers an explicit display (an intentional no-summary row included); new growth belongs in the Tool-owned profile, not per-name branches in Svelte.
- **ToolContext** carries identity/addressing (`agent_id`, session/run/tool-call ids, nesting depth, iteration number), `workspace`, `cwd`/`effective_cwd` (the working Project repo for Rooted Identity or Project Agents, else Workspace; Memory deliberately ignores it), `project_id` (Session/address ownership, stays `None` for Rooted Identity Agents so bare Sub-Agent targets remain Identity targets; `skill_project_id` separately carries the Project serving Skill activation), cancel hooks wired by the chat dispatcher, the change tracker, and `after_result_persisted(callback)` invoked only after ordered persistence.
- **Concurrency:** same-turn sibling Calls execute concurrently by default within bounded Run/global limits, preserving original order in results and persistence. Only a registered Tool with `parallel_safe=False` is an explicit ordering barrier before and after itself; unknown Tools invoke no handler and create no barrier. Extension hooks serialize their callbacks batch-locally without serializing parallel-safe handlers.
- **Blocking work:** handlers offload blocking sections through the dedicated worker pool (`run_tool_worker` / `offload_tool_handler`) with admission backpressure and cancellation shielding; built-ins choose selectively (wholly offloaded: edit/history/project/glob/grep/memory/session_search/write; split: skill/read/channel_send/web_fetch). Ordinary sync built-ins keep loop-dispatch because some intentionally call loop-owned APIs; sync Extension handlers auto-offload.
- **Policy resolution:** `normalize_tool_access` validates strict persisted policy; `resolve_tool_access` is the single resolver behind prompt/provider definitions and dispatch - mode selection, automatic activation (`session_read` follows `session_search`, Memory mode activates `memory`, grants activate theirs), constraints, `denied` as absolute veto, orphaned followers removed, mode `none` suppresses every activation path.
- **Prompt-block seam:** `ToolPromptBlockRegistry` lets a Tool declare a System Prompt block (`id`/owner `tool:<name>`, rendered only when the Tool is effectively allowlisted); the prompt domain imports no tool classes. Built-in `project` and `subagent` use dynamic blocks for current catalogs.

## Registration and dispatch rules

- Names match `^[A-Za-z][A-Za-z0-9_]{0,63}$`. Registration compiles the canonical Draft 2020-12 contract up front and rejects invalid schemas, unknown families, or incoherent activation metadata before publishing; fixed-shape objects close by default while migrated model-facing schemas set `open_input_schema=True` and leave unknown-field rejection to the handler. Families are comprehension/bulk-action metadata only - membership never implies activation.
- Dispatch authorizes and readiness-checks, normalizes a deep copy of arguments against the cycle's exact input contract, validates unless the registered handler explicitly owns complete argument validation, executes, validates the envelope plus declared result schema, and verifies finite JSON-serializability. Normalization fixes only unambiguous encodings (numeric strings for numeric fields, case-insensitive booleans, `"null"`, JSON-encoded containers, single value wrapped into array) while preserving accepted strings; everything else normally fails before side effects (`invalid_arguments`). Handler-deferred validation is reserved for an independently executed batch whose item errors must not reject valid siblings; it does not weaken the Provider schema or authorize malformed item side effects. An invalid/non-serializable result raises `InvalidToolResultError`, distinct from argument errors, mapped to `invalid_tool_result`.
- An ungranted Session-scoped Tool fails `<tool>_unavailable` before allowlist checks; a matching grant overrides the allowlist. Dispatch is not list-filtered by readiness but re-evaluates live -> `tool_not_ready` envelope (retryable=False) instead of running.
- Provider-visible definitions expose only name/description/schema - never handlers, context, internal flags, or display metadata; Definition Profiles resolve per Agent from stable config (deterministic key, sorted set inputs, transient liveness excluded) and a `None` profile hides the Tool.
- `list_tools` filters registered -> allowed -> ready (readiness last, default off); `provider_definitions`/`prompt_definitions` default `ready_only=True`; `tool.list` intentionally includes not-ready/granted public Tools with full metadata so policy editors can explain states. At this boundary `allowed_tools=None`/`["*"]` mean all normal Tools, `[]` means none - the Project Tool Whitelist is deliberately stricter and rejects `"*"` (see `projects/resolution.md`).
- For a **config (Project) Agent**, the Project Tool Whitelist is the outer ceiling: scanned repository denials narrow it, a vBot override fully replaces the scanned policy but must stay inside the ceiling; `project.set` accepts only registered configurable Tools and keeps previously persisted unavailable names retain-or-remove.
- Internal tools bypass allowlist filtering and enforce their own domain rules. Same-turn failures are per-call envelopes, never Run failures; Run/provider/process/persistence failures stay outside this boundary. Result persistence preserves the Assistant's original call order even when a parallel group finishes out of order.
- Extensions register Tools via `api.register_tool(...)` after declaring families if desired; applied last during bootstrap, then ordinary Tools in every respect. Name collisions skip with diagnosis; invalid declarations isolate without blocking other capabilities.

## Conventions

- When vBot authors a filesystem path for Model consumption in Tool output, render separators as forward slashes (`C:/...`); resolution, persistence, input handling, and OS calls keep native `Path` semantics - never global text normalization.
- Repair representation mistakes before rejecting: normalization handles only schema-guided unambiguous mistakes on a deep copy - no field renaming, no whitespace-only omission, no boolean aliases like `yes`/`1`, no mutation of the original Assistant Tool Call. Exact empty strings in optional object properties omit before validation; requirements, unions/ranges, and unknown properties on closed contracts still fail first; migrated open-schema Tools delegate unknown-field rejection to handlers, which also own authorization/existence/state/constraint checks.
- **Agent-facing definitions follow the design map `tools/designing-agent-tools.md`** - action-based Tools advertise required `action` plus a shared optional superset with handler-owned rejection; single-behavior Tools use direct fields, except deliberately batched independent operations such as `edit(edits[])`. Never add nested `request.operation` shapes, operation-key objects, redundant single-value actions, or inference from unrelated optional combinations; historical persisted calls may retain retired shapes for presentation only.
- Readiness is a separate axis from policy and grants: not-ready stays registered (policy persists), drops from model-facing surfaces, and returns `tool_not_ready` on direct dispatch. Gate-2 of a `tool:<name>` prompt block reads the readiness-filtered definitions, so its block disappears too.
- Display labels are not parameters - do not add arguments like `description` to affect UI chrome; use `ToolDisplay`.
- Tool timing metadata never reaches provider adapters; provider tool messages carry only role, correlation, name, content.
- A `tool:<name>`-owned prompt block still drops when its Tool is not ready (gate 2 reads filtered definitions).

Live Extension catalogs publish atomically through `ExtensionOperations.replace_tools`; ToolRegistry revision changes refresh the next Provider cycle's definitions and contracts. Removing a catalog retires its authority. Definition-profile context includes the exact Agent/Project identity. See `extensions/mcp.md` for connection grants and remote schema changes.

## Constraints & Gotchas

- Non-envelope, non-serializable, or schema-violating successful results reject as `invalid_tool_result` without aborting the Run.
- Disallowed normal Tools fail at dispatch even if a provider asks for them.
- Chat alone derives Session Grants during request building; static restrictions and live denials stay dispatch-only and must never filter provider definitions or mutate prompt assembly.
- Relative paths resolve from `effective_cwd` (Project repo else Workspace); absolute bypass unless a specific Tool forbids. File tools and `bash` use it; `memory` deliberately stays on Workspace (identity home, not project-relative).
- A live per-name denial resolver runs before hooks and handler for every sibling; denial returns `tool_not_allowed` without side effects.

## References

Read these only when your task matches - not by default.

- Any work on the agent-facing part of a Tool - adding, changing, or migrating a definition, description, parameter, or model-facing schema -> `tools/designing-agent-tools.md`
- Changing one concrete built-in Tool -> its per-tool spec (read only the one you change): `tools/read.md`, `tools/edit.md`, `tools/write.md`, `tools/file_state.md` (shared read stamps/mutation locks/atomic replace), `tools/change_tracker.md` (run-delta statistics), `tools/glob.md`, `tools/grep.md`, `tools/web_fetch.md`, `tools/web_search.md`, `tools/bash.md`, `tools/process.md`, `tools/terminal.md` (interactive PTY sessions), `tools/status.md`, `tools/memory.md`, `tools/image.md`, `tools/generated-media.md`, `tools/session_search.md` (+derived `session_read`), `tools/history.md`, `tools/project.md`, `tools/skill.md` (+ `skill_manage` contract owned by `skills.md`), `tools/subagent.md`, `tools/cron.md`, `tools/calendar.md` (calendar domain: `calendar.md`), `tools/channel_send.md`, `tools/speech.md`
