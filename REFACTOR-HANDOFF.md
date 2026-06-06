# Refactor Handoff — Large-File Decomposition

**Status:** in progress (Wave 1 done; Wave 2 SettingsView started) · **Owner:** Julian · **Started:** 2026-06-06
**Next action:** continue Wave 2 → `SettingsView.svelte` panel extraction (CSS + Channels
done; see "CONTINUE HERE"). Extract the remaining panels into `settings/` children.

This document is self-contained: a fresh session should be able to continue from it
alone. Read it top to bottom before touching code.

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

- [ ] **`SettingsView.svelte` (4475 → 3063, IN PROGRESS)** ◀── CONTINUE HERE. The
  file is a stack of **independent panels**, each with its own load/save/handler
  triple: Language/Appearance, General, Skill-Dirs, Agent-Defaults, Subagent,
  Compaction, Recall, WebSearch, Debug, TaskModel (specialized_models),
  Providers/Connections, Channels. Plan: one child component per panel under
  `webui/src/components/settings/`; `SettingsView.svelte` becomes a thin nav/panel
  container (target each panel 150–350 lines).

  **Done so far (2026-06-06, gate green ruff n/a · vitest 525/525 · build PASS):**
  - **CSS lifted to global** `webui/src/styles/settings.css` (802 lines), imported
    via `@import './settings.css';` in `webui/src/styles/app.css`. This was the
    enabling step: the shared `.s-*` layout primitives were scoped to
    `SettingsView.svelte`, so a Svelte child component could not use them. They are
    now global (settings-specific names, no bleed). `SettingsView.svelte` no longer
    has a `<style>` block; **new panel children need NO `<style>`** — they reuse the
    global `.s-*` classes (this also fixed the pre-existing voice-panel styling gap).
  - **Channels panel extracted** → `webui/src/components/settings/SettingsChannelsPanel.svelte`
    (558). It is the cleanest seam: fully self-contained (loads its own data via
    `agent.list`/`channel.*` on `onMount`, like the existing `WakewordVoiceSettings`),
    touches none of the shared `settings`/`saving`/`commitSettings` flow. Zero props.
    `SettingsView` just renders `<SettingsChannelsPanel />` in the `{:else if
    activePanelId === 'channels'}` branch; the nav entry stays in `SettingsView`.

  **Validated extraction recipe (follow for the rest):**
  - Child imports `rpc` from `$lib/api.js` (the test mocks that exact module — keep
    importing from there so mocks apply), `t` from `$lib/i18n.js`, helpers from
    `$lib/settingsView.js`. No `<style>` (global classes).
  - `SettingsView.test.js` mounts the **real** `SettingsView` and asserts on DOM
    (class/text/aria) + `rpc` call names — so preserve markup classes, ids,
    aria-labels, and the rpc calls verbatim and the tests stay green with **zero test
    edits** (currently 525/525). `settingsView.test.js` tests pure fns — **do not
    remove/rename existing `settingsView.js` exports**, only add.
  - Panels that load lazily on select (`onMount` in the child) match the established
    voice-panel pattern; the old parent caching (`channelsLoaded` guard) drops out.

  **Remaining panels** (still inline in `SettingsView`): defaults, skills, subagents,
  compaction, recall, web_search, debug, specialized_models, providers, general,
  appearance. Unlike Channels these read the **shared loaded `settings` object**, so
  they need the shared-settings contract below. Note the parent groups code **by kind,
  not by panel** — each panel's pieces are scattered across the script (all `$state`
  together near L330, all `$derived` `…SaveDisabled` near L450, all auto-save `$effect`
  blocks near L560, the `saveXxx`/`clearXxxTimer`/`handleManualXxxSave`/`handleXxxChange`/
  `xxxMatch` fns spread through L800–L1500, plus a line in `applySettings`). So each
  extraction is ~10 small edits in the parent + one new child. Use a Python splice
  script with `assert`-guarded anchors (as with Channels) rather than hand-editing.

  **Validated shared-settings child contract** (worked out, not yet executed):
  props `{ settings, onCommit, onToast, onError }` where the parent wires
  `onCommit={commitSettings}` (updates parent `settings`), `{onToast}` (parent's prop),
  `onError={(msg) => (saveError = msg)}` (the `saveError` banner is **shared**, rendered
  in the parent header above every panel — the child must keep using it, calling
  `onError('')` on field change + at save start, `onError(msg)` on save failure, exactly
  as the inline `saveXxx`/`handleXxxChange` do today). The child owns: form `$state`
  seeded `normalizeXxx(settings)`, a **child-local** `saving` flag (drop the parent
  `loading`/`saving` from `saveDisabled` — the child only mounts when its panel is
  active, so those are unobservable here; behavior-equivalent), `saveDisabled =
  saving || xxxMatch(form, normalizeXxx(settings))`, and the auto-save `$effect`
  **without** the `activePanelId !== 'x'` guard (the child only exists while active):
  `$effect(() => { if (saveDisabled) return; timer = setTimeout(save, 800); return () =>
  clearTimer(); })`, plus `onDestroy(clearTimer)`. `saveXxx` does
  `rpc('settings.update', buildXxxPayload(form))` → `onCommit(next)` → success toast;
  panels that re-seed after save (agentDefaults/recall/web_search/debug) keep that line.
  `debug` also needs an `onDebugEnabledChange` prop. `defaults`/`compaction` additionally
  need the model picker (`availableModels`/`availableConnections`, loaded lazily in the
  parent today via `ensureModelCatalogsLoaded` — either load in the child on mount or
  pass the catalogs down). `providers` is the most coupled (props `providerAuthEvent`/
  `connectProvider`/`disconnectProvider` + exported `handleProviderAuthCompleted` +
  `model.refresh_db` + the header refresh button) — do it last. `specialized_models`
  uses `taskModelSettings.js`, lazy-loads targets/schemas on mount, and has **no test
  coverage** — extract it carefully (build/eslint only safety net). Spec
  `.vorch/specs/webui.md` already says "SettingsView… own Settings panel state"; per §4
  step 5 no inventory is needed, the split is internal. Box stays unchecked until the
  container is thin.

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
