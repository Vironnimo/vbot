<script>
  import { untrack } from 'svelte';

  import { deletePermissionProfile, savePermissionProfile } from '$lib/api.js';
  import { useAutosaveContext } from '$lib/autosave.js';
  import { t } from '$lib/i18n.js';
  import {
    permissionProfiles,
    refreshPermissionProfiles,
  } from '$lib/permissionProfiles.js';
  import { permissionProfileLabel } from '$lib/permissionRules.js';
  import Button from '../ui/Button.svelte';
  import ConfirmDialog from '../ui/ConfirmDialog.svelte';
  import TextField from '../ui/TextField.svelte';
  import PermissionProfileEditor from './PermissionProfileEditor.svelte';

  const autosave = useAutosaveContext();
  let profiles = $state.raw([]);
  let editing = $state('');
  let creating = $state(false);
  let newName = $state('');
  let removing = $state(null);
  let busy = $state(false);
  let error = $state('');
  let removeError = $state('');
  let builtins = $derived(profiles.filter((profile) => profile.builtin));
  let custom = $derived(profiles.filter((profile) => !profile.builtin));
  let restricted = $derived(
    profiles.find(
      (profile) => profile.name === 'restricted' && profile.builtin,
    ),
  );
  let exists = $derived(
    profiles.some((profile) => profile.name === newName.trim()),
  );

  // A refresh that is still running, or one that failed, keeps the last
  // committed list. A mutation response is already usable without a reload.
  $effect.pre(() => {
    const snapshot = $permissionProfiles;
    if (!snapshot.loading && !snapshot.error) {
      untrack(() => adopt(snapshot));
    }
  });

  function adopt(snapshot) {
    profiles = snapshot.profiles;
    if (editing && !profiles.some((profile) => profile.name === editing))
      editing = '';
  }

  function committed(snapshot) {
    adopt(snapshot);
    void refreshPermissionProfiles();
  }

  function select(name) {
    return autosave.requestTransition(() => {
      creating = false;
      editing = name;
      error = '';
    });
  }

  function startCreating() {
    return autosave.requestTransition(() => {
      editing = '';
      newName = '';
      creating = true;
      error = '';
    });
  }

  async function create(event) {
    event.preventDefault();
    const name = newName.trim();
    if (!name || exists || !restricted || busy) return;
    busy = true;
    error = '';
    try {
      committed(await savePermissionProfile(name, restricted.rules, null));
      editing = name;
      creating = false;
    } catch (cause) {
      error = cause.message;
      void refreshPermissionProfiles();
    } finally {
      busy = false;
    }
  }

  function requestRemoval(name) {
    return autosave.requestTransition(() => {
      removing =
        profiles.find((profile) => profile.name === name && !profile.builtin) ??
        null;
      removeError = '';
    });
  }

  async function remove() {
    if (!removing || busy) return;
    busy = true;
    try {
      committed(await deletePermissionProfile(removing.name, removing.rules));
      removing = null;
    } catch (cause) {
      removeError = cause.message;
      void refreshPermissionProfiles();
    } finally {
      busy = false;
    }
  }
</script>

<div class="profile-manager">
  <div class="s-group-toolbar">
    <div class="s-row-info">
      <h4 class="s-subhead__title">{t('permissions.profiles.title')}</h4>
      <p class="s-subhead__desc">{t('permissions.profiles.description')}</p>
    </div>
    {#if !creating}
      <Button
        variant="secondary"
        disabled={!restricted || busy}
        onClick={startCreating}>{t('permissions.profiles.add')}</Button
      >
    {/if}
  </div>
  {#if $permissionProfiles.error}
    <p class="s-row-error" role="alert">
      {$permissionProfiles.error}
      <Button variant="tertiary" onClick={refreshPermissionProfiles}
        >{t('common.retry')}</Button
      >
    </p>
  {/if}
  {#if creating}
    <form class="profile-manager__create" onsubmit={create}>
      <TextField
        value={newName}
        ariaLabel={t('permissions.profiles.name')}
        placeholder={t('permissions.profiles.name')}
        disabled={busy}
        onInput={(value) => {
          newName = value;
          error = '';
        }}
      />
      <Button
        variant="primary"
        type="submit"
        loading={busy}
        disabled={!newName.trim() || exists || !restricted}
        >{t('common.create')}</Button
      >
      <Button variant="tertiary" disabled={busy} onClick={() => select('')}
        >{t('common.cancel')}</Button
      >
    </form>
    {#if exists}
      <p class="s-row-error">{t('permissions.profiles.exists')}</p>
    {/if}
  {/if}
  {#if profiles.length}
    <div class="s-group">
      {#each builtins as profile (profile.name)}
        <div class="s-row s-row--compact">
          <div class="s-row-info">
            <div class="s-row-label">
              {permissionProfileLabel(profile.name)}
            </div>
            {#if profile.name === 'restricted'}
              <p class="s-row-desc">
                {t('permissions.profile.restrictedSummary')}
              </p>
            {:else if profile.name === 'full_access'}
              <p class="s-row-desc">{t('permissions.profile.fullSummary')}</p>
            {/if}
          </div>
        </div>
      {/each}
      {#each custom as profile (profile.name)}
        <div class="s-entity">
          <div class="s-entity__head">
            <div class="s-row-info">
              <div class="s-row-label profile-manager__name">
                {profile.name}
              </div>
            </div>
            <Button
              variant="tertiary"
              aria-expanded={editing === profile.name}
              ariaLabel={editing === profile.name
                ? t('permissions.profiles.close')
                : t('permissions.profiles.edit', { name: profile.name })}
              disabled={busy}
              onClick={() =>
                select(editing === profile.name ? '' : profile.name)}
            >
              {editing === profile.name ? t('common.close') : t('common.edit')}
            </Button>
          </div>
          {#if editing === profile.name}
            <div class="s-disclosure__body">
              <PermissionProfileEditor
                {profile}
                onCommit={committed}
                onRemove={() => requestRemoval(profile.name)}
                {busy}
              />
            </div>
          {/if}
        </div>
      {/each}
    </div>
  {/if}
  {#if error}
    <p class="s-row-error" role="alert">{error}</p>
  {/if}
</div>
{#if removing}
  <ConfirmDialog
    title={t('permissions.profiles.deleteTitle', { name: removing.name })}
    body={t('permissions.profiles.deleteBody')}
    confirmLabel={t('common.delete')}
    {busy}
    error={removeError}
    onConfirm={remove}
    onCancel={() => (removing = null)}
  />
{/if}

<style>
  .profile-manager {
    display: grid;
    gap: var(--space-md);
  }
  .profile-manager__name {
    overflow-wrap: anywhere;
  }
  .profile-manager__create {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 8px;
  }
</style>
