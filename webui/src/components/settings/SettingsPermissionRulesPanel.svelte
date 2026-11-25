<script>
  import {
    getSettings as reloadDraftSettings,
    updateSettings as submitDraftSettings,
  } from '$lib/api.js';

  import { onDestroy, untrack } from 'svelte';

  import PermissionProfilesPanel from './PermissionProfilesPanel.svelte';
  import { t } from '$lib/i18n.js';
  import PermissionRulesEditor from '../permissions/PermissionRulesEditor.svelte';
  import SaveStatus from '../ui/SaveStatus.svelte';
  import {
    createDebouncedAutosave,
    useAutosaveContext,
  } from '$lib/autosave.js';
  import { permissionRulesComplete } from '$lib/permissionRules.js';
  import { createSettingsEditorDraft } from '$lib/settingsEditorDraft.svelte.js';
  import {
    buildPermissionRulesDefaultPayload,
    normalizePermissionRulesDefaultSettings,
  } from '$lib/settingsView.js';

  const noop = () => {};

  // The shared Permission Rules default (`defaults.agent.permission_rules`):
  // Identity Agents and Project Agents inherit each part they do not set themselves.
  // Unset, it is the restricted profile's rules (settings.get reports them).
  let { settings = null, onCommit = noop, onError = noop } = $props();

  let form = $state(
    untrack(() => normalizePermissionRulesDefaultSettings(settings)),
  );
  let saving = $state(false);
  const draft = createSettingsEditorDraft({
    reload: reloadDraftSettings,
    submit: submitDraftSettings,
    getSettings: () => settings,
    onError: (message) => onError(message),
    fromSettings: normalizePermissionRulesDefaultSettings,
    read: () => form,
    write: (next) => (form = next),
    toPayload: buildPermissionRulesDefaultPayload,
    // A rule set's parts are only meaningful together with the rules a
    // concurrent edit kept; the whole set is one value.
    leafDepth: 1,
  });

  let saveDisabled = $derived(saving || !hasChanges());
  const autosaveContext = useAutosaveContext();
  const autosave = createDebouncedAutosave({
    getSnapshot: () => ({ ...form }),
    hasChanges,
    save,
  });
  const unregisterAutosave = autosaveContext.register(autosave.participant);

  onDestroy(() => {
    unregisterAutosave();
    autosave.cancelPendingTimer();
  });

  $effect(() => {
    if (saveDisabled) return;
    autosave.scheduleRun();
    return () => autosave.cancelPendingTimer();
  });

  function hasChanges() {
    return (
      JSON.stringify(normalizePermissionRulesDefaultSettings(form)) !==
      JSON.stringify(normalizePermissionRulesDefaultSettings(settings))
    );
  }

  function change(rules) {
    form = { permission_rules: rules };
    onError('');
  }

  async function save() {
    if (!hasChanges()) return true;
    // A rule still being typed has no pattern; saving waits for it.
    if (!permissionRulesComplete(form.permission_rules)) return false;
    return draft.save({
      onCommit,
      onError,
      setSaving: (value) => (saving = value),
    });
  }
</script>

<PermissionProfilesPanel />
<div class="s-group">
  <details class="s-disclosure">
    <summary>{t('permissions.profiles.sharedRules')}</summary>
    <div class="s-disclosure__body shared-permission-rules__body">
      <p class="s-subhead__desc">
        {t('permissions.profiles.sharedDescription')}
      </p>
      <PermissionRulesEditor
        value={form.permission_rules}
        inset
        level="default"
        idPrefix="default-permission-rules"
        onChange={change}
      />
      <div class="s-footer">
        <SaveStatus
          {saving}
          pending={autosave.participant.hasChanges()}
          onClick={() => autosave.participant.runSave('manual')}
        />
      </div>
    </div>
  </details>
</div>

<style>
  .shared-permission-rules__body {
    display: grid;
    gap: var(--space-md);
  }
</style>
