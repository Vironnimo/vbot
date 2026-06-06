# Refactor Handoff — Large-File Decomposition

**Status:** in progress (Wave 1 done; Wave 2 SettingsView in progress) · **Owner:** Julian · **Started:** 2026-06-06
**Next action:** continue Wave 2 → `SettingsView.svelte` panel extraction. CSS, Channels,
Sub-Agents, Recall, Web Search, Skills, Appearance done. **Extract exactly the next ONE
panel** (see "CONTINUE HERE": Debug), then stop and update this handoff.

This document is self-contained: a fresh session should be able to continue from it
alone. Read it top to bottom before touching code.

> **⚠ WORKFLOW — ONE PANEL PER SESSION (user decision 2026-06-06).** Do **not** extract
> all remaining SettingsView panels in one go. Each session: extract **exactly one** panel
> from the "Remaining panels — one per session" queue below, get the full frontend gate
> green (`python scripts/quality-frontend.py` → vitest 525/525 · build PASS), update this
> handoff (check the panel off, advance "CONTINUE HERE" to the next one, record line
> counts), commit, and stop. The user starts the next session for the next panel.

---

## 0. Orientation (read before doing anything)

This is **vBot**, a local-first agent harness (async Python kernel + FastAPI + Svelte
WebUI + CLI + pywebview desktop). Before working:

1. Read `.vorch/PROJECT.md` — architecture, layers, conventions, quality-gate commands.
2. Read the spec for whatever domain you are about to touch, from `.vorch/specs/`
   (index is in `PROJECT.md`). E.g. WebUI work → `.vorch/specs/webui.md`.
3. This is a **pure structural refactor** effort: move code to separate concerns,
   **never change behavior**. Tests are the safety net — keep them green, and prefer
   not to edit tests (if a test reaches into internals, see the recipe in §5).

Conventions that matter here (from `PROJECT.md`):
- DI via constructor `__init__`; interfaces via `typing.Protocol`. No globals/singletons.
- stdlib → third-party → local imports; remove unused.
- Backend Python 3.11+, ruff + mypy + pytest gated. Frontend Svelte (JS, no TS),
  Vitest. Use the current Python interpreter directly (no venv assumptions).

---

## 1. Goal

Several files have grown past a maintainable size. Split each one along genuine
concern boundaries so no single file carries multiple unrelated responsibilities.

## 2. Working threshold & principle

- Soft limit is **~1000 lines per file** (`PROJECT.md` now says 1000). Files
  comfortably under ~1000 are **not** targets. The split is about separating concerns,
  not hitting a number.
- **Deep modules, few not many** (`PROJECT.md` convention): each domain keeps *one*
  public main file (the deep interface). Internal concerns move into private sibling
  modules; the package `__init__.py` re-exports what callers need. **Do not** fragment
  into many shallow files — extract cohesive units only.

## 3. Out of scope

- **`core/runtime/runtime.py` (~1030)** — long but *flat*: ~210 lines are trivial
  lazy-getter DI `@property` wiring. Splitting hurts the DI overview. **Excluded by
  user decision — do not touch.**
- **Test files** (e.g. `tests/server/test_rpc.py` 4849, `test_chat_loop.py` 3536) — not
  a separate campaign. Tests mirror source and split along with it.
- **Borderline 900–1000 files** (`openai_compatible.py` 977, `channels.py` 940,
  `telegram.py` 912, `settings/validation.py` 910, `SystemPromptView.svelte` 995,
  `CronView.svelte` 968, `LogsView.svelte` 936) — under threshold. Watch list only;
  trim opportunistically if already editing them (Boy-Scout rule).

---

## 4. Per-file process — Definition of Done (FOLLOW EVERY TIME)

For each target file, in order:

1. **Read the file fully** and map its concerns (classes, function clusters, helpers).
2. **Find external dependents** before cutting — see §5 for the exact grep recipe.
   Know what the package `__init__.py` re-exports and who imports module internals.
3. **Extract cohesive units** into private sibling modules. Keep the main file as the
   deep public interface. Preserve every public import path.
4. **Keep `__init__.py` surface identical** (or only widen it). Re-export anything
   callers imported. (The project's "no legacy compatibility" rule is about *data
   formats* — it does not excuse breaking live import paths.)
5. **Update the domain spec — MANDATORY after every refactor.** Follow
   `.vorch/workflows/spec-workflow.md` (the Orchestrator agent, `.opencode/agents/orchestrator.md`,
   owns specs; that workflow is the standard). Spec rules that bite here:
   - A spec is **decision-useful working notes for agents**, *not* architecture
     documentation, API reference, or a file-layout inventory.
   - A pure internal refactor (no behavior/contract/boundary change) usually needs
     **little or no** spec change. Do **not** paste a "module layout" inventory.
   - Add only what changes an agent's decisions: the **import-path boundary** ("import
     from the package, internals are private") and a concise **"where new code goes"**
     decision rule if the split created a new home for a concern (e.g. "new settings
     normalizers go in `settings_normalizers.py`, not `storage.py`").
   - Every claim must be backed by the source you just wrote. Update the Specs index in
     `PROJECT.md` only if a spec is created/renamed/removed (not for in-place edits).
6. **Quality gate green** (see §5 for commands). No behavior change.
7. **Update this handoff**: check the box, record before→after line counts, the new
   files, and the verification result. Keep "START HERE" pointing at the next item.
8. **Commit the item.** Once the gate is green and the handoff is updated, commit the
   refactor as its own logical commit (one per target file / wave item, plus the
   updated handoff). Use a `refactor(<domain>): …` message. This step is mandatory —
   do not leave finished work uncommitted for the next session to discover.

Commit cadence: scaffolding (plan + threshold) and each refactor item get their own
commit on `main`, matching this handoff's structure. Do not bundle multiple wave items
into one commit. Branch only if the user asks.

---

## 5. Verification recipe (commands & how-to)

**Find who depends on a package before splitting it** (bash tool; adjust the name):
```bash
# external importers of the package
grep -rnE 'from core\.<pkg>|import core\.<pkg>|core\.<pkg>\.' --include='*.py' . \
  | grep -v '/core/<pkg>/' | grep -v '__pycache__'
# what the package __init__ re-exports
cat core/<pkg>/__init__.py
# does any TEST import the concrete module (not the package) or patch its members?
grep -nE 'core\.<pkg>\.<module>' tests -r
```
If a test does `import core.<pkg>.<module> as m` and patches `m.<name>`, the split must
**keep that module exposing `<name>`** (re-export it). That lets you split with **zero
test changes** — preferred, because unchanged tests prove behavior is preserved.

**Quality gates** (each runs format → lint → type-check → test):
```bash
python scripts/quality.py core/<pkg>/            # backend module
python scripts/quality-frontend.py webui/src/... # frontend path
```
The backend gate runs the *mirrored* tests only. Also run the **indirect consumers**
you found in the grep, e.g.:
```bash
python -m pytest tests/core/prompts tests/core/runtime tests/server/test_rpc.py -q
```
Finish with an import smoke test:
```bash
python -c "import core.runtime.runtime, server.app; print('ok')"
```

## 6. Reusable split recipe (learned in Wave 1)

- **Pure stateless helpers** (validators/normalizers that only touch args + constants)
  → a free-function module (e.g. `settings_normalizers.py`). Move the constants they
  use with them. Caller methods change `self._x(...)` → `x(...)`.
- **A stateful collaborator** (owns paths/IO) → a class injected via `__init__` and
  delegated to (e.g. `PromptFragmentStore`). The main class keeps thin delegator
  methods so the public API is unchanged.
- **Shared low-level primitives** (atomic temp-file writes, a logger, a background-task
  error logger) → a tiny shared module (`atomic.py`) or the lower-layer module, imported
  by both halves. Avoid duplicating.
- **Errors** → per-package `errors.py` (matches `core/providers/errors.py`,
  `core/chat/errors.py`). Re-import into the main module so old paths still resolve.
- **Circular imports**: a sibling importing another sibling is fine as long as the
  `__init__.py` imports the leaf modules *before* the main module, and you use direct
  submodule imports (`from core.x.leaf import Y`). Lower layers must not import upward.
- **Re-export-only-for-tests**: if a name must stay on a module but is otherwise unused
  there, use `from x import name as name` (ruff treats it as an intentional re-export;
  avoids F401).

---

## 7. Primary targets (> ~1000 lines)

| File | Lines | Core problem |
|---|---|---|
| `webui/src/components/SettingsView.svelte` | 4475 | ~10 independent settings panels in one component |
| `webui/src/components/ChatTimeline.svelte` | 2523 | rendering + scroll logic + date grouping |
| `core/chat/chat.py` | 2486 | data model + tool dispatch + orchestrator + model resolution + events |
| `webui/src/components/AgentsView.svelte` | 2025 | agent list + form + detail in one view |
| `webui/src/lib/chatState.js` | 1927 | session/run state **+** full timeline projection |
| `webui/src/components/DebugView.svelte` | 1700 | trace list + detail + filters |
| `webui/src/components/ChatView.svelte` | 1594 | |
| `webui/src/lib/api.js` | 1227 | RPC client for all domains in one module |
| `cli/main.py` | 1109 | 350-line `parse_args` + dispatchers + output formatters |
| `webui/src/components/ChatComposer.svelte` | 1075 | |
| `core/chat/chat.py`, `core/storage/storage.py`, `core/subagents/subagents.py` | — | (storage & subagents DONE — see Wave 1) |

## 8. Execution order (waves)

Ordering: **low risk + clean seam + good test coverage first**, central/risky last.

### Wave 1 — backend, clean class seams (DONE ✅)

- [x] **`core/storage/storage.py` (1298 → 744)** — DONE 2026-06-06. New files:
  `settings_normalizers.py` (476, stateless validate/normalize fns),
  `prompt_fragments.py` (259, `PromptFragmentStore` owned+delegated by `StorageManager`),
  `errors.py` (9, `StorageError`), `atomic.py` (24, temp-file write/replace).
  `__init__.py` surface unchanged. Spec `.vorch/specs/storage.md` updated (import-path
  boundary + "where new code goes" rule). Gate green (ruff+mypy+111/111) + 384
  indirect-consumer tests green.

- [x] **`core/subagents/subagents.py` (1006 → 713)** — DONE 2026-06-06. Extracted the
  self-contained `SubAgentBatchTracker` state machine → `tracker.py` (316).
  `subagents.py` keeps `SubAgentCoordinator` + spawn/result handlers and re-exports
  `_LOGGER` (`as _LOGGER`) so `tests/core/tools/test_subagent.py` patches still resolve
  — **zero test changes**. Spec `.vorch/specs/subagents.md` updated. Gate green
  (ruff+mypy) + 40 subagent + 120 runtime/websocket tests green.
  - Deferred (optional, not needed for threshold): the ~200-line `_handle_subagent`
    spawn handler could be decomposed internally later.

### Wave 2 — frontend, biggest LOC wins  ◀── START HERE

> Prereq: read `.vorch/specs/webui.md`. Frontend gate is
> `python scripts/quality-frontend.py <path>`. Tests live in
> `webui/src/components/__tests__/` and `webui/src/lib/__tests__/`. No TypeScript.

- [ ] **`SettingsView.svelte` (4475 → 2240, IN PROGRESS)** — one child component per
  panel under `webui/src/components/settings/`; `SettingsView.svelte` becomes a thin
  nav/panel container (target each panel 150–350 lines). **Do one panel per session**
  (see WORKFLOW box at the top). Box stays unchecked until the container is thin.

  **Done (2026-06-06, full gate green each step · vitest 525/525 · build PASS):**
  - **CSS lifted to global** `webui/src/styles/settings.css` (802), imported via
    `@import './settings.css';` in `app.css`. The shared `.s-*` layout primitives were
    scoped to `SettingsView.svelte`; they are now global (settings-specific names, no
    bleed). `SettingsView.svelte` has no `<style>` block; **panel children need NO
    `<style>`** — they reuse the global `.s-*` classes.
  - **Channels** → `SettingsChannelsPanel.svelte` (558). Self-contained, loads its own
    data on `onMount` (`agent.list`/`channel.*`), zero props. Cleanest seam.
  - **Sub-Agents** → `SettingsSubAgentsPanel.svelte` (219). First shared-settings panel;
    validated the contract below incl. all the auto-save behavioral tests.
  - **Recall** → `SettingsRecallPanel.svelte` (156). Dropdown, re-seeds after save.
  - **Web Search** → `SettingsWebSearchPanel.svelte` (203). Dropdown + conditional
    SearXNG URL, re-seeds after save.
  - **Skills** → `SettingsSkillsPanel.svelte` (229). Read-only default-dir row (reads
    `settings`) + add/remove directory list with `newSkillDirectory` child-local state +
    manual & auto-save. `directoriesMatch` moved into the child (it was a parent-local fn,
    not a `settingsView.js` export). Does NOT re-seed after save.
  - **Appearance** → `SettingsAppearancePanel.svelte` (158). Language `<select>`
    (`bind:value` + `handleLanguageChange`) + manual & auto-save; calls `init(language)`
    from `$lib/i18n.js` after a successful save. `isLanguageSaveDisabled` is called with
    `loading: false` (child only mounts when active). Does NOT re-seed after save. Parent
    keeps its own `init(language)` in `applySettings` (still imported).

  **Validated extraction recipe — shared-settings panels (FOLLOW EXACTLY; executed for
  subagents/recall/web_search, zero test edits, 525/525):**
  - **Props `{ settings, onCommit, onToast, onError }`.** Parent wires them in the
    `{:else if activePanelId === 'x'}` branch:
    ```svelte
    <SettingsXxxPanel {settings} onCommit={commitSettings} {onToast}
      onError={(message) => (saveError = message)} />
    ```
    The `saveError` banner is **shared**, rendered in the parent header above every
    panel — the child drives it via `onError('')` on every field change + at save start,
    `onError(msg)` on save failure.
  - **Seed form state once with `untrack`** to avoid the `state_referenced_locally`
    compiler warning: `import { onDestroy, untrack } from 'svelte';` then
    `let form = $state(untrack(() => normalizeXxx(settings)));`. ⚠ Do **not** try to
    silence the warning with a `// svelte-ignore state_referenced_locally` comment —
    eslint's `svelte/no-unused-svelte-ignore` then fails the gate (the warning is
    runtime-only, eslint can't see it). `untrack` is the clean fix.
  - **Child-local `saving`** (`let saving = $state(false)`); drop the parent
    `loading`/`saving` — the child only mounts when its panel is active, so those are
    unobservable (behavior-equivalent). `saveDisabled = $derived(saving || xxxMatch(form,
    normalizeXxx(settings)))`.
  - **Auto-save `$effect` without** the `activePanelId !== 'x'` guard (child only exists
    while active): `$effect(() => { if (saveDisabled) return; autoSaveTimer =
    setTimeout(() => { autoSaveTimer = null; void saveXxx(); }, 800); return () =>
    clearAutoSaveTimer(); });` plus `onDestroy(() => clearAutoSaveTimer());`. Keep
    `AUTO_SAVE_DEBOUNCE_MS = 800` local to the child.
  - **`saveXxx`** does `rpc('settings.update', buildXxxPayload(form))` → `onCommit(next)`
    → success toast. **Re-seed after save** (`form = getXxx(next)`) for the panels that
    did so inline: agentDefaults, recall, web_search, debug. (subagents does NOT re-seed.)
  - **Toasts go through `onToast({ title, variant })` directly** (not the old
    `showSettingsToast`). Manual save: `handleManualXxxSave()` returns early if `saving`;
    if `saveDisabled` it fires `onToast({ title: t('common.alreadySaved','Already
    saved'), variant: 'success' })`; else `clearAutoSaveTimer(); void saveXxx();`.
  - **Imports:** `rpc` from `$lib/api.js` (test mocks that exact module — keep it), `t`
    from `$lib/i18n.js`, helpers from `$lib/settingsView.js`, shared UI components from
    `../` (e.g. `import Dropdown from '../Dropdown.svelte';`). No `<style>`.
  - **Parent cleanup per panel** (~10 small edits): remove the panel's `$state`, its
    `…SaveDisabled` `$derived`, its auto-save `$effect`, the onMount cleanup
    `clear…AutoSaveTimer()` line, the `applySettings` re-seed line, and the `saveXxx` /
    `clear…AutoSaveTimer` / `handleManualXxxSave` / `handleXxxChange` / `xxxMatch` fns,
    plus any now-orphan `…AutoSaveTimer` var and now-unused `settingsView.js` imports.
    The parent groups code **by kind, not by panel**, so the pieces are scattered — grep
    the panel's identifiers and remove each. After removal, grep for leftovers and run
    eslint (catches unused imports/vars).
  - **Test guard:** `SettingsView.test.js` mounts the **real** `SettingsView` and asserts
    on DOM (class/text/aria) + `rpc` call names → preserve markup classes, ids,
    aria-labels, and the rpc calls verbatim. `settingsView.test.js` tests pure fns — do
    not remove/rename existing `settingsView.js` exports, only add.

  **Remaining panels — one per session (ordered queue; simplest first, providers last):**
  1. **Debug** ◀── CONTINUE HERE. checkbox + trace-limit number; **auto-save only, no Save button** (no
     sticky footer). Needs an extra **`onDebugEnabledChange`** prop (parent passes its
     own `onDebugEnabledChange` prop through); call it with the new enabled flag after a
     successful save. Re-seeds after save. `getDebugSettings`/`DEBUG_SETTING_DEFAULTS`
     live inline in `SettingsView` today — move them into the child (or a helper).
  2. **General** — **read-only**, no save/state: two `s-value-box` rows (`serverHostValue`
     via `formatServerHost`, `dataDirectoryValue` via `getDataDirectoryValue`). Trivial;
     pass `{ settings }` only (+ `t`). Removes those two `$derived` from the parent.
  3. **Defaults** — model + fallback-model `SearchableDropdown` + temperature + thinking
     effort. Needs the **model picker** (`availableModels`/`availableConnections`); load
     them in the child on mount (`model.list` + `connection.list`, as `ensureModelCatalogsLoaded`
     does) so `waitForModelCatalogs()` in the test still sees both calls. Re-seeds after
     save. Uses `modelSelection.js` + `buildModelSelectOptions`. Keep ids
     `settings-defaults-model` / `-fallback-model` / `-temperature` / `-thinking-effort`.
  4. **Compaction** — auto checkbox + threshold + tail_tokens + summary-model picker.
     Same model-picker need as Defaults (id `settings-compaction-summary-model`).
     `normalizeCompactionSettings`/`buildCompactionSettingsPayload`/`getCompactionSettings`
     exist in `settingsView.js` (the inline fallbacks in `SettingsView` shadow them — use
     the lib ones in the child).
  5. **Specialized Models** — task-model bindings; uses `taskModelSettings.js`,
     lazy-loads targets/schemas on mount, **no test coverage** (build/eslint only). Many
     own state vars (`taskModel*`) + `updateTaskModelSettings`/`listTaskModelTargets`/
     `getTaskModelOptions` from `$lib/api.js`. Extract carefully.
  6. **Providers** — **most coupled, do last.** Props `providerAuthEvent`/`connectProvider`/
     `disconnectProvider`, the exported `handleProviderAuthCompleted`, `model.refresh_db`
     + the **header refresh button** (currently rendered in the parent's `s-panel-header`,
     gated on `activePanelId === 'providers'`), the device-flow OAuth dialog, and all the
     `oauth*`/`provider*` helpers. Plan the header-button handoff before cutting.

- [ ] **`chatState.js` (1927)** — two concerns: session/run state mutation **+**
  timeline projection (`buildVisibleTimelineItems`, `liveTimelineItems`,
  `historyTimelineItems` & helpers, ~line 473→end). Move the projection half →
  `webui/src/lib/chatTimeline.js`; `chatState.js` keeps session/run state. Clean seam,
  covered by `chatState.test.js` (3389).

- [ ] Then same pattern, in rough size order: **ChatTimeline.svelte** (2523),
  **AgentsView.svelte** (2025), **DebugView.svelte** (1700), **ChatView.svelte** (1594),
  **api.js** (1227 — split RPC client per domain), **ChatComposer.svelte** (1075).

### Wave 3 — central, well-tested core (do last)

- [ ] **`core/chat/chat.py` (2486)** — central but well covered by `test_chat_loop.py`
  (3536). The `core/chat/` package is already partly modular (`streaming.py`,
  `commands.py`, `block_resolver.py`). Four clean cuts:
  - `messages.py` ← `ToolCall` + `ChatMessage` (canonical data model, ~lines 137–406).
  - `tool_dispatch.py` ← `_EmittingToolRegistry` + display helpers (~407–592).
  - `model_resolution.py` ← `parse_model_with_connection`, `_resolve_fallback`,
    connection helpers (~1830–end).
  - `events.py` ← the `_emit_*` helpers (~1646–1733).
  - `chat.py` keeps only `ChatLoop`. Spec: `.vorch/specs/chat.md`.
  - ⚠ Check `tests/core/chat/test_chat_loop.py` for `import core.chat.chat as ...` and
    member patching before cutting (§5) — re-export to keep tests unchanged.

- [ ] **`cli/main.py` (1109)** — `parse_args` alone is ~350 lines of argparse →
  `cli/parser.py`. `print_*` / `_*_output_lines` formatters → `cli/output.py`.
  `dispatch_*` stay or → `cli/dispatch.py`. Spec: `.vorch/specs/cli.md`.
