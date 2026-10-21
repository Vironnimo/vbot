<script>
  import { onDestroy, untrack } from 'svelte';

  import PermissionRulesEditor from '../permissions/PermissionRulesEditor.svelte';
  import SaveStatus from '../ui/SaveStatus.svelte';
  import {
    createDebouncedAutosave,
    useAutosaveContext,
  } from '$lib/autosave.js';
  import { permissionRulesComplete } from '$lib/permissionRules.js';
  import { createSettingsDraft } from '$lib/settingsSave.js';
  import {
    buildPermissionRulesDefaultPayload,
    normalizePermissionRulesDefaultSettings,
  } from '$lib/settingsView.js';

  const noop = () => {};

  // The shared Permission Rules default (`defaults.agent.permission_rules`):
  // every Agent and Project Agent inherits each part it does not set itself.
  // Without it, Agents have full access.
  let { settings = null, onCommit = noop, onError = noop } = $props();

  let form = $state(
    untrack(() => normalizePermissionRulesDefaultSettings(settings)),
  );
  let saving = $state(false);
  const draft = createSettingsDraft({
    settings: untrack(() => settings),
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

<PermissionRulesEditor
  value={form.permission_rules}
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
