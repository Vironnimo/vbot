# Plan: Auto-save + repositioned save buttons across WebUI

**Goal:** Replace the "only-save-when-button-clicked" pattern with auto-save (800 ms debounce after last change) in SettingsView, SystemPromptView, and AgentsView, while keeping a manual save button — moved from the panel header top-right to a sticky footer at the bottom of the scroll area.

**Context:** Users complained that save buttons are always in the top-right corner of panel headers. Modern expectation is that changes are persisted automatically. Some users still want a visible save button for trust. The save button should stay but be relocated from the header to a sticky footer. CronView is excluded: its save button lives inside a modal footer (already a natural bottom position), and auto-saving a modal form (especially create mode) would be confusing — no changes there.

**Scope:**
- In: SettingsView (appearance/skills/subagents/compaction panels), SystemPromptView (per-fragment editors), AgentsView (edit-mode form button repositioning only — no auto-save per user decision)
- Out: CronView (no changes), AgentsView auto-save (manual save only, as decided)

---

## Architecture Decisions

### Auto-save trigger (Svelte 5)
Use `$effect` with a per-view debounce timer. The effect reads the current "dirty" derived state; if dirty, it schedules a save after 800 ms. The cleanup function (returned from `$effect`) clears the timer, so rapid edits only trigger one save.

```js
$effect(() => {
  if (activePanelId !== 'subagents') return;
  if (subAgentSettingsSaveDisabled) return; // nothing to save
  const timer = setTimeout(() => { void saveSubAgentSettings(); }, 800);
  return () => clearTimeout(timer);
});
```

### Form state after auto-save — critical
Currently every save function calls `applySettings(nextSettings)` which resets ALL form state (form values + persisted baseline). This would clobber user input if the user edits a field while the auto-save is in flight.

**Fix:** Introduce a lightweight `commitSettings(nextSettings)` helper that only updates `settings = nextSettings` (the persisted baseline used by dirty-check derived values) without touching form fields (`subAgentSettings`, `skillDirectories`, `compactionSettings`, `selectedLanguageId`). The one exception is the language save: after saving the language, call `init(selectedLanguageId)` to apply the new locale immediately.

All four save functions switch from `applySettings(nextSettings)` to `commitSettings(nextSettings)`. `applySettings` is still called by `loadSettings()` on initial mount.

### Save button: always enabled for trust
The save button is always enabled (not disabled-when-clean). Clicking when dirty cancels the pending debounce timer and saves immediately. Clicking when already clean shows a brief `showLocalToast('Already saved', 'success')` feedback. This fulfills the "trust button" requirement.

### Sticky footer
Each panel's save button is wrapped in `.s-sticky-footer` — a `position: sticky; bottom: 0` element inside the scrollable panel body. Background matches the surface so it floats visually above content.

---

## Phases

### Phase 1 — SettingsView (most complex)
**Files:** `webui/src/components/SettingsView.svelte`

- [ ] Add `commitSettings(nextSettings)` helper: sets `settings = nextSettings` only (no form state reset).
- [ ] Change `saveLanguage`, `saveSkillDirectories`, `saveSubAgentSettings`, `saveCompactionSettings` to call `commitSettings` instead of `applySettings`. In `saveLanguage`, also call `init(selectedLanguageId)` after commit.
- [ ] Remove the four save buttons from `s-panel-header` (the top-right ones in the `{#if activePanelId === 'appearance'}` / `skills` / `subagents` / `compaction` blocks). Keep the `s-refresh-button` for providers.
- [ ] Wrap each section's existing inline `s-save-button--inline` button in a `<div class="s-sticky-footer">` and remove `disabled` binding — always enabled. Add click logic: if disabled (clean), call `showLocalToast(t('common.alreadySaved', 'Already saved'), 'success')`, otherwise call save function directly (and clear any pending debounce timer).
- [ ] Add four `$effect` blocks (one per panel) that auto-save after 800 ms whenever the panel-specific dirty state becomes true.
- [ ] Add i18n key `common.alreadySaved` / `'Already saved'`.
- [ ] Add `.s-sticky-footer` CSS: `position: sticky; bottom: 0; padding: 12px 0 0; background: var(--surface);`

**Done when:** Changing a subagents/compaction/skills/language setting and waiting ~1 s saves without clicking; clicking the save button manually saves immediately; clicking when already clean shows "Already saved" toast.

---

### Phase 2 — SystemPromptView (per-fragment auto-save)
**Files:** `webui/src/components/SystemPromptView.svelte`

- [ ] Add a single `$effect` that watches `fragments` for any entry where `isDirty === true` and `!isSaving && !isResetting`. For each dirty fragment, schedule a per-index debounce timer (store in a `Map`); on timer fire, call `saveFragment(index)`.

  > Note: `$effect` in Svelte 5 re-runs when any reactive read inside it changes. Since `fragments` is `$state([])` and we access `fragments[index].isDirty`, the effect re-runs on any fragment change. Store debounce timers outside the effect in a `let autoSaveTimers = new Map()` variable. Clean up all timers in `onMount` cleanup.

- [ ] Move the per-fragment save button out of `sp-fragment-header` → `sp-fragment-actions` and into a `<div class="sp-sticky-footer">` placed after the textarea but inside `sp-fragment`. Make the button always enabled (same "already saved" pattern: if `!fragment.isDirty && !fragment.isSaving`, show `showToast(t('common.alreadySaved', 'Already saved'), 'success')`).
- [ ] Update `showToast` variant to 'success' for already-saved, keep 'error' for failures.
- [ ] Add `.sp-sticky-footer` CSS: `position: sticky; bottom: 0; padding: 8px 0 0; background: var(--bg);`

**Done when:** Typing in a fragment editor and pausing saves automatically (shows "Saved" toast); save button always visible below the textarea.

---

### Phase 3 — AgentsView (button reposition only)
**Files:** `webui/src/components/AgentsView.svelte`

- [ ] Remove the save (`type="submit"`) button from `detail-btns` div inside `detail-top`.
- [ ] Add a `<div class="agent-sticky-footer">` at the bottom of `agent-detail-scroll` containing the save button.
- [ ] Keep the delete button in `detail-btns` (it's a destructive action and should stay near the agent title, not in the save footer).
- [ ] Add `.agent-sticky-footer` CSS: `position: sticky; bottom: 0; padding: 16px 0 4px; background: var(--surface);`

**Done when:** Save button is visible at the bottom of the agent form scroll area, not at the top-right.

---

## Done when (overall)
- All three views above save automatically ~800 ms after the last change
- Save buttons are at the sticky bottom, not in panel headers
- Clicking save when already clean shows "Already saved" toast
- CronView and AgentsView auto-save: untouched (as decided)
- No regressions in existing save/error/loading behavior

## Risks & Mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| `$effect` re-runs on every minor reactive change, flooding the server with saves | Medium | High | The dirty-check guard (`if (saveDisabled) return` or `if (!isDirty) return`) prevents saves when nothing changed; debounce absorbs rapid changes |
| `commitSettings` breaks dirty-check comparisons if normalized form differs from raw `nextSettings` | Low | Medium | `commitSettings` sets `settings = nextSettings`; all dirty-check comparisons already normalize both sides (e.g. `normalizeSubAgentSettings(settings)`) so the comparison stays symmetric |
| Auto-save during initial mount triggers spurious save | Low | Medium | On mount, `applySettings` runs, making form state match `settings`, so `*SaveDisabled = true`; `$effect` guard exits early |
| Svelte 5 `$effect` circular reactivity if save updates state the effect reads | Low | High | Save functions set `saving = true` immediately, which makes `*SaveDisabled = true`, causing the effect to bail out; after save `commitSettings` only updates `settings`, not form values, so no re-entrancy |
