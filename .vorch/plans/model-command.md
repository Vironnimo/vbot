## Plan: `/model` command + per-agent model override

**Goal:** A `/model` slash command persistently changes the current session's agent model between runs — for an identity agent in its own config, for a project (config) agent via a new per-agent override in `project.json` — with `/model reset` to undo, input validated against actually-usable models, and the override shown (with an `x` to clear) on the Projects tab.

**Context:** Today an identity agent's model lives in its own config and is mutable through `agent.update`, but a project/config agent's model comes live from the repo scan, which vBot only reads. There is no vBot-owned place to persistently pin a different model for one project agent — editing the repo is off-limits and the project-wide default has the wrong scope. The decision (with the user) is to store a **per-agent override** in the data-dir `project.json` (never the repo), applied as the **top tier** of the config-agent model chain (override → repo value → project default → global). The change takes effect between runs (the model is resolved at run start), so mid-run stability and thinking/CoT replay are unaffected — those are already model-gated (same-model gate for `full_history`, stale `reasoning_meta` stripped for a different provider) and proven by the existing fallback-model switch.

**Decisions (settled with the user):**
- `/model <value>` is **persistent**, not session-scoped.
- Override **wins** over the repo-declared model (top of the chain).
- Storage: identity agent → its own `model` field; project agent → `project.json` `model_overrides` (data-dir, never the repo).
- `/model reset` undoes: identity → empty model (falls to global default); project → removes the per-agent override (falls back to the repo value).
- Bare `/model` shows the **current model + origin only** (no model list — there are 400+).
- Only **available/usable** models are accepted (provider registered, model in catalog, usable credential).
- Projects tab: per-agent row shows the override (which model) + an `x` button to clear it. No set-from-UI in v1 (setting is via `/model`).

**Scope:**
- In: the `model_overrides` field + validation + store set/clear; resolver chain placement + an `is_model_configured` seam; the `/model` command (show / set / reset); identity vs project routing; the `project.clear_model_override` RPC + override exposure in the team scan; Projects-tab display + clear; i18n; tests; domain-map updates.
- Out: a UI control to *set* an override (command-only in v1); per-agent overrides for temperature/thinking effort (model only); a dedicated CLI subcommand (the CLI sends `/model` as chat text like any accessor); deep validation of a pinned `::connection` suffix beyond the model-level check (see Risks); **any change to thinking/CoT replay handling** — it is already model-gated and safe (same-model gate under `full_history`, stale `reasoning_meta` stripped for a different provider) and the switch only takes effect at the next run boundary, so the replay path needs no work.

**Already-true facts the builder must not re-investigate (verified during planning):**
- The `chat.commands` catalog RPC (`server/rpc/catalog_methods.py` `_list_commands`) **auto-derives** from `CommandDispatcher.BUILT_IN_COMMANDS` (emits `name`/`description`/`argument`/`output`). Adding the `/model` `CommandSpec` reaches the WebUI command autocomplete **for free** — do NOT edit `catalog_methods.py` and do NOT look for a separate hardcoded frontend command list.
- `project.json` is read fresh from disk on **every** resolve (`ProjectStore.get` has no cache; the resolver's only cache is team *membership*). A written override therefore applies on the next run with **no cache invalidation** — do not add any.
- The real `CommandDispatcher` is wired at `core/runtime/runtime.py` (~line 343) with `agent_resolver`, `sessions`, `models`, `providers`, `projects`, `agents`. But unit tests construct it minimally (`CommandDispatcher(ChatRunManager())`), so any new handler that touches those services MUST None-guard them, mirroring `_handle_status`.
- Identity agents need **no** resolver change: their model is their own field, mutated via `runtime.agents.update(agent_id, model=...)`; an empty model already resolves to the global default at read time.
- Command replies are **plain English** strings (see `_handle_stop`/`_handle_help` in `commands.py`), NOT routed through i18n. Only the WebUI strings (Phase 3) go through `i18n.js`.

**Assumptions & Constraints:**
- Single-user-local: a per-agent set/clear read-modify-write on `project.json` needs no cross-process locking beyond the existing atomic replace.
- `model_overrides` is keyed by the scanned `agent_id` (unique per project team after collision resolution).
- No new dependencies.

### Milestones

| # | Milestone | Deliverable |
|---|---|---|
| M1 | Override storage + resolution | `project.json` carries `model_overrides`; resolver applies it as the top model-chain tier; store set/clear + `is_model_configured` seam; backend tests green |
| M2 | `/model` command + RPC | `/model` show/set/reset works for identity and project agents with validation; team scan exposes `model_override`; `project.clear_model_override` RPC; tests green |
| M3 | Projects-tab display + clear | Team rows show the override + an `x` that clears it; i18n; frontend tests + full gates green |

### Phase Breakdown

#### Phase 1: Override storage + resolution
**Goal of this phase:** `project.json` persists a per-agent `model_overrides` map, the resolver applies it above the repo value, and the store can atomically set/clear one entry. Identity agents are unchanged (they already mutate their own model field).
**Can run in parallel with:** none (foundation for M2/M3).

- Add `model_overrides: dict[str, str]` (default empty) to `Project`; thread it through `to_dict`, `project_from_dict` (default `{}`), and `build_project` (new keyword param + a `_validate_model_overrides` helper: a dict of non-empty `agent_id` string → non-empty model string). The model value's *configured-ness* is NOT checked here (file validation is shape-only, mirroring `default_model`). — files: [core/projects/projects.py], tests: [tests/core/projects/test_projects.py]
- ⚡ *parallel with the projects.py task* — Add `"model_overrides"` to `PROJECT_FIELDS` and a `$.model_overrides` shape rule in `validate_project_data` (object whose keys are non-empty strings and values are non-empty strings; absent is valid → defaults to `{}`). NOTE: there is no existing object/dict validator helper (only `_validate_optional_string` / `_validate_optional_string_list`), so add a small dict validator. Shape only — do NOT check the model value for configured-ness here (that is the set-time gate), exactly as `default_model` is only shape-checked. — read: [.vorch/domain-maps/settings.md], files: [core/settings/validation.py], tests: [tests/core/settings/ (project validation test)]
- Store: make `update()` carry `model_overrides` through `build_project` (preserve across unrelated edits — it is NOT added to the generic `allowed_fields`/`project.set` surface); add `set_model_override(project_id, agent_id, model) -> Project` and `clear_model_override(project_id, agent_id) -> Project` doing an atomic read-modify-write (get → copy dict → set/del key → `build_project` preserving identity/timestamps → atomic write). Clearing an absent key is a no-op success. — read: [.vorch/domain-maps/projects.md], files: [core/projects/store.py], tests: [tests/core/projects/test_store.py] — depends on the projects.py task.
- ⚡ *parallel with the store.py task* — Resolver: in `_resolve_model_or_raise`, prepend `project.model_overrides.get(scanned.agent_id, "")` as the first chain candidate (kept behind the same `is_configured` gate as the other tiers, so an override that later becomes unconfigured degrades to the repo value rather than erroring). Add a public `is_model_configured(model: str) -> bool` delegating to the existing `_model_checker` (one rule, no drift) for the command layer to reuse. — read: [.vorch/domain-maps/agent.md], files: [core/projects/resolver.py], tests: [tests/core/projects/test_resolver*.py] — depends on the projects.py task.
- Domain maps: projects.md (the `model_overrides` field + store set/clear methods), agent.md (config-agent model chain now reads override → repo → project default → global), settings.md (the new field's shape rule). — files: [.vorch/domain-maps/projects.md, .vorch/domain-maps/agent.md, .vorch/domain-maps/settings.md]

**Dependencies:** none.
**Done when:** A `project.json` with `model_overrides` round-trips through load/validate/save; the resolver returns the override model for a config agent when configured, the repo value when no override (or override unconfigured); `set_model_override`/`clear_model_override` mutate exactly one entry atomically and leave the rest intact; `is_model_configured` matches the scan's bad-model rule.

#### Phase 2: `/model` command + RPC surface
**Goal of this phase:** `/model` shows / sets / resets the model for the current session's agent, routing identity vs project, validating input against usable models; the team scan exposes each agent's override and the UI can clear it.
**Can run in parallel with:** none (depends on Phase 1).

- Command layer: add `CommandSpec("model", "Show, set, or reset this session's model.", argument="optional", output="action")` to `BUILT_IN_COMMANDS`, register `_handle_model`, and add `"set_model"` to `CommandActionName`. `_handle_model`: bare (argument None) → `CommandHandled(output="transient")` reporting the current model + origin; with argument → `CommandAction(name="set_model", argument=<raw text>)`. The bare-show derives its data with only cheap, fresh reads (no chain replication): resolved model = `resolve_agent(project_id, agent_id).model` (already post-override); on a project session, `projects.get(project_id).model_overrides.get(agent_id)` present → label "local override", else label "from project configuration"; identity session → just the agent's model. Showing the underlying repo model alongside an override is an optional nice-to-have, not required. **None-guard `_agent_resolver`/`_projects`/`_models` exactly like `_handle_status`** so minimal-construction test dispatchers don't crash. `/help` updates automatically (it iterates `BUILT_IN_COMMANDS`); the `chat.commands` autocomplete RPC does too (no catalog edit). Replies are plain English (match `_handle_stop`). — read: [.vorch/domain-maps/chat.md], files: [core/chat/commands.py], tests: [tests/core/chat/test_commands.py]
- ⚡ *parallel with the commands.py task* — Project RPC: join the override into the team scan — `_team_member_response` gains `"model_override"` (the per-agent value from `project.model_overrides`, or `null`), so the scan-preview helpers pass `project` down. Add a `project.clear_model_override` handler (params `project_id`, `agent_id`; calls `ProjectStore.clear_model_override`; returns the updated project + fresh scan) and register it in `method_handlers()`. No team-scan cache invalidation needed (project.json is read fresh per resolve). — read: [.vorch/domain-maps/server.md], files: [server/rpc/project_methods.py], tests: [tests/server/rpc/ (project methods test)] — depends on Phase 1.
- Command execution: add `_handle_set_model_command` in `chat_methods.py` and wire it into the `_handle_command_action` match for `"set_model"`. It parses the argument: a case-insensitive `reset` token → clear; anything else → a model value. Validate a set value with `resolver.is_model_configured(value)` (reject with a clear `invalid_request`-style reply when not configured — the same rule that produces the scan's bad-model finding); when the value carries a `::connection` suffix, additionally reuse `_ensure_model_connection_supported` (in `server/rpc/agent_methods.py`, used by `agent.*` — import it or lift it to a shared spot) so a bad pinned connection is rejected up front. Route: identity session (`project_id is None`) → `runtime.agents.update(agent_id, model=value or "")`; project session → `runtime.projects.set_model_override(project_id, agent_id, value)` / `clear_model_override(...)`. No busy-guard (takes effect on the next run). Return `data: {command: "model", agent_id, model}` (toast-style confirmation). — files: [server/rpc/chat_methods.py], tests: [tests/server/rpc/ (chat methods test)] — depends on the commands.py task (the `set_model` action name) + Phase 1.
- Domain maps: chat.md (add `/model` to the built-ins list + its show/set/reset behavior and identity-vs-project routing), projects.md (the `project.clear_model_override` RPC + `model_override` in the team response). — files: [.vorch/domain-maps/chat.md, .vorch/domain-maps/projects.md]

**Dependencies:** Phase 1 (store methods, `is_model_configured`, the `model_overrides` field).
**Done when:** `/model <usable-model>` in an identity session updates the agent model (next run uses it); in a project session it writes a per-agent override that wins over the repo model on the next run; `/model reset` undoes each respectively; an unusable model is rejected with a helpful reply; bare `/model` reports the current model + origin; `project.clear_model_override` removes one entry and the team scan response carries `model_override`.

#### Phase 3: Projects-tab display + clear
**Goal of this phase:** Each team row on the Projects tab shows whether the agent has a model override and which model, with an `x` to clear it.
**Can run in parallel with:** none (depends on Phase 2 RPC + Phase 1 response field).

- View helper: `projectTeam(scan)` carries `model_override` (string or null) per member; optionally a small label helper. — files: [webui/src/lib/projectsView.js], tests: [webui/src/lib/__tests__/projectsView.test.js]
- ⚡ *parallel with the projectsView.js task* — API wrapper: `clearModelOverride(projectId, agentId)` calling `project.clear_model_override`. — read: [.vorch/domain-maps/webui.md], files: [webui/src/lib/api.js], tests: [webui/src/lib/__tests__/api.test.js]
- Component: in the team listing of `ProjectsView.svelte`, when a member has a `model_override` render a badge ("Model override: <model>") with an `x` button that calls `clearModelOverride` then refreshes the project (re-run `project.show`); add i18n strings. — read: [.vorch/domain-maps/webui.md, .vorch/DESIGN.md], files: [webui/src/components/ProjectsView.svelte, webui/src/lib/i18n.js], tests: [webui/src/components/__tests__/ProjectsView.test.js] — depends on the two tasks above.
- Domain map: webui.md (Projects tab shows + clears a per-agent model override). — files: [.vorch/domain-maps/webui.md]

**Dependencies:** Phase 2 (`project.clear_model_override`, `model_override` in scan), Phase 1 (the field).
**Done when:** A project agent with an override shows the badge + model and the `x` clears it (row updates to the repo value); an agent without an override shows no badge; frontend tests + both full quality gates green.

### Risks & Mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| An override becomes unconfigured later (credential removed) and silently degrades to the repo value | Med | Low | Same `is_configured` gate as every chain tier (consistent behavior); the override is always visible on the Projects tab so the user sees it and can clear it |
| Stale override entry lingers for an agent removed from the repo | Low | Low | Harmless — the resolver checks team membership before model resolution, so a non-team entry never applies; the `x` (or `/model reset`) removes it |
| A pinned `::connection` suffix in an override isn't deeply validated (only model-level `is_model_configured`) | Low | Low | Reuse the existing `agent.*` model-connection support check when a suffix is present; otherwise the chat loop's connection resolution surfaces a clear error at run start |
| Identity `/model reset` to empty with no global default leaves the agent model-less | Low | Low | Same as today's no-model agent; the next run surfaces the existing clear "no model" error |

**Open decisions (resolved here, flag for review):**
- **Dedicated store set/clear methods vs. whole-dict via `project.set`.** Chose dedicated `set_model_override`/`clear_model_override` (atomic per-agent RMW, single seam for both the command and the `x`), keeping `model_overrides` out of the generic `project.set` field surface. Alternative: expose it as a bulk list field — rejected (read-modify-write over RPC, race-prone, two write paths for one field).
- **Override stays behind the `is_configured` gate.** Chose consistency with the existing chain (degrade to repo if the override goes unusable). Alternative: make the override bypass the gate and hard-error — rejected (less forgiving, surprising).
