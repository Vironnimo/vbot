# Tool Access Policy and Visibility

How `core/tools/availability.py` resolves a Tool Access Policy, how opt-in, followers, `fixed` and `live_call` behave, which flags hide a registered Tool from which surfaces, and how listing filters by readiness. The root rule (one resolver, `denied` absolute, opt-in needs a grant) lives in `tools.md` -> Contracts -> Policy resolution; per-Agent policy storage in `agent.md`, Project ceilings in `projects/resolution.md`.

## Policy resolution

`normalize_tool_access` validates strict persisted policy; `resolve_tool_access` is the single resolver behind prompt/provider definitions and dispatch - mode selection, automatic activation (a declared follower is active while any of its sources is, `activation_sources(tool)`; personal Memory mode or the Agent's Project Memory selection activates `memory`; grants activate theirs), constraints, `denied` as absolute veto, orphaned followers removed, mode `none` suppresses every activation path. Memory visibility remains independent of Tool permission (`memory.md`).

`ToolAccess.fixed` (absent from public policy JSON; the Agent store sets it for the built-in Agents, `agent.md`, and an owner may set it for a temporary Agent) makes a `selected` list the whole Tool set: memory-mode and Session-grant Tools activate only when it names them, and nothing configurable outside it is added. Followers still follow the Tools it names, because each belongs to its sources' capability (the edit dialect of `apply_patch`, `background_tasks` beside the shell Tool and `subagent`, an MCP connection's remote Tools), and a denial still vetoes one (`test_availability.py`). Fixed sets therefore never list followers.

Constraint `live_call` (`TOOL_CONSTRAINT_LIVE_CALL`) resolves a Tool only for a policy with `ToolAccess.live_call` set, which is runtime-only like `fixed` and set by the Agent store only for the two built-in Agents of a Live call; the Live Tools (family `live`, `core/tools/live.py`, opt-in) carry it, so no other Agent can get them, whatever its policy names (`model_tasks/live-operator.md` -> Live Tools).

## Opt-in Tools

Configurable Tools may declare `requires_opt_in=True`. They require an explicit name in `tool_access.granted` in addition to ordinary policy permission; All mode and materialized Project ceilings never grant opt-in. None and denials still win, and companions cannot follow an ungranted parent. Missing Tools retain stored grants. `tool.list` exposes the flag for the shared editor; raw wildcard/unfiltered definition and dispatch paths do not authorize opt-in Tools. Coverage: `tests/core/tools/test_availability.py`, `tests/core/tools/test_tools_registry.py` (registration rules), `tests/core/extensions/test_capabilities.py`.

## Followers and retired names

A retired Tool name (such as `grep`, `glob`, `terminal_beta`) left in a persisted policy is an ordinary unknown Tool name, and Project adapters map external grep/glob denials to `search_files` (see `tools/search_files.md`). `edit` and `write` are registered again as followers of `apply_patch` (`tools/edit.md`): a persisted `denied` entry vetoes one, and an `allowed` entry grants nothing without `apply_patch`.

For a config (Project) Agent, the Project Tool Whitelist is the outer ceiling; how scanned repository denials and vBot overrides combine with it, and what `project.set` accepts: `projects/resolution.md` and `projects/configuration.md`.

## Readiness

`Tool.ready` is an optional zero-argument predicate that must be cheap and free of I/O, because it is evaluated at every prompt and Tool-definition build. The filter order is registered -> policy-allowed -> ready. A Tool not ready when a prompt epoch pins its Tool list stays out of that pin and the System Prompt and is announced once it becomes ready; readiness never removes a Tool the Model already knows in the epoch (`chat/request-building.md` -> Tool catalog per prompt epoch). Configuration surfaces keep a not-ready Tool visible with its explanation so persisted policy survives. Dispatch is not list-filtered by readiness but re-evaluates it live: a not-ready Tool returns a clean `tool_not_ready` envelope instead of running, with the Tool's specific `readiness_hint` when supplied and `retryable` from `Tool.readiness_retryable` (a `register` keyword, so also of live Extension Tool declarations; default false, true only when the hint names a retry that can succeed, such as an MCP remote Tool whose connection reconnects). A raising predicate counts as not-ready (logged once at warning).

## Visibility flags and listing

- An ungranted Session-scoped Tool fails `<tool>_unavailable` before allowlist checks; a matching grant still requires dispatch allowlist permission.
- `deferred=True` keeps a Tool registered for policy, metadata, and dispatch while excluding it from Provider definitions and System Prompt Tool lists. Discovery owners deliver selected schemas through ordinary results; catalog changes must not promote deferred Tools implicitly.
- `catalog_visible=False` hides a Tool only from public catalog projections, including Extension capability lists and Project selection. Internal enumeration retains it for policy, collision detection, replacement, fingerprints and teardown; this flag alone neither grants access nor establishes Session scope. Public projections explicitly use `include_catalog_hidden=False`; the default preserves internal enumeration. Coverage: `tests/server/rpc/test_catalog_methods.py`, `tests/core/extensions/test_operations.py`.
- Provider-visible definitions expose only name/description/schema - never handlers, context, internal flags, or display metadata; Definition Profiles resolve per Agent from stable config (deterministic key, sorted set inputs, transient liveness excluded) and a `None` profile hides the Tool.
- `list_tools` filters registered -> allowed -> ready (readiness last, default off); `provider_definitions`/`prompt_definitions` default `ready_only=True`; `tool.list` intentionally includes not-ready/granted public Tools with full metadata so policy editors can explain states. At this boundary `allowed_tools=None`/`["*"]` mean all normal Tools, `[]` means none - the Project Tool Whitelist is deliberately stricter and rejects `"*"` (see `projects/resolution.md`).
- Internal tools bypass allowlist filtering and enforce their own domain rules.
