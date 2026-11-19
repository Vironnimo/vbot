<script>
  import { onDestroy, untrack } from 'svelte';

  import { listPermissionProfiles, savePermissionProfile } from '$lib/api.js';
  import {
    createDebouncedAutosave,
    useAutosaveContext,
  } from '$lib/autosave.js';
  import { permissionRulesComplete } from '$lib/permissionRules.js';
  import { createSettingsEditorDraft } from '$lib/settingsEditorDraft.svelte.js';
  import PermissionRulesEditor from '../permissions/PermissionRulesEditor.svelte';
  import Button from '../ui/Button.svelte';
  import { t } from '$lib/i18n.js';
  import SaveStatus from '../ui/SaveStatus.svelte';

  let { profile, onCommit, onRemove, busy = false } = $props();

  // The parent keys this editor by profile name. A profile owns a complete
  // set of rules, so a concurrent edit to that set is reconciled as a unit.
  const name = untrack(() => profile.name);
  const id = $props.id();
  let rules = $state(untrack(() => profile.rules));
  let saving = $state(false);
  let error = $state('');
  const draft = createSettingsEditorDraft({
    getSettings: () => profile,
    fromSettings: (row) => row?.rules ?? null,
    read: () => rules,
    write: (value) => (rules = value),
    toPayload: (value) => ({ permission_rules: value }),
    reload: async () => currentProfile(await listPermissionProfiles()),
    submit: async ({ permission_rules, base }) =>
      currentProfile(
        await savePermissionProfile(
          name,
          permission_rules,
          base.permission_rules,
        ),
      ),
    leafDepth: 0,
    onError: (message) => (error = message),
  });

  function currentProfile(result) {
    return {
      rules: result.profiles.find((item) => item.name === name)?.rules ?? null,
      catalog: result,
    };
  }

  const editor = createDebouncedAutosave({
    getSnapshot: () => ({ name, rules }),
    hasChanges: () => JSON.stringify(rules) !== JSON.stringify(profile.rules),
    save() {
      if (!rules || !permissionRulesComplete(rules)) return false;
      return draft.save({
        onCommit: (row) => onCommit(row.catalog),
        onError: (message) => (error = message),
        setSaving: (active) => (saving = active),
      });
    },
  });
  const unregister = useAutosaveContext().register(editor.participant);
  onDestroy(() => {
    unregister();
    editor.cancelPendingTimer();
  });
  $effect(() => {
    if (saving || !editor.participant.hasChanges()) return;
    editor.scheduleRun();
    return editor.cancelPendingTimer;
  });
</script>

<PermissionRulesEditor
  value={rules}
  profile
  level="default"
  idPrefix={id}
  onChange={(next) => {
    rules = next;
    error = '';
  }}
/>
{#if error}<p role="alert" class="s-row-error">{error}</p>{/if}
<div class="profile-editor__footer">
  <Button variant="tertiary" disabled={busy} onClick={onRemove}
    >{t('permissions.profiles.remove')}</Button
  >
  <SaveStatus
    {saving}
    pending={editor.participant.hasChanges()}
    onClick={() => editor.participant.runSave('manual')}
  />
</div>

<style>
  .profile-editor__footer {
    display: flex;
    align-items: center;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: var(--space-sm);
    padding-top: var(--space-md);
    margin-top: var(--space-md);
    border-top: 1px solid var(--border);
  }
</style>
