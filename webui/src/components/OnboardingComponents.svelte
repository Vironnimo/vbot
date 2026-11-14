<script>
  import { onMount, onDestroy } from 'svelte';
  import {
    listExtensions,
    skillInventory,
    setSkillDisabled,
    updateSettings,
  } from '$lib/api.js';
  import {
    createAutosaveParticipant,
    useAutosaveContext,
  } from '$lib/autosave.js';
  import { buildExtensionsUpdate } from '$lib/settingsView/extensions.js';
  import { t } from '$lib/i18n.js';
  import Banner from './ui/Banner.svelte';
  import Button from './ui/Button.svelte';
  import Toggle from './ui/Toggle.svelte';

  let skills = $state([]);
  let extensions = $state([]);
  let loading = $state(true);
  let saving = $state(false);
  let error = $state('');
  let pending = $state(null);
  let disposed = false;
  let request = 0;
  const context = useAutosaveContext();
  const participant = createAutosaveParticipant({
    getSnapshot: () => pending,
    hasChanges: () => pending !== null,
    save: saveSelection,
  });
  const unregister = context.register(participant);

  onMount(() => {
    void load();
  });
  onDestroy(() => {
    disposed = true;
    request += 1;
    unregister();
  });

  async function load() {
    const current = ++request;
    error = '';
    try {
      const [inventory, catalog] = await Promise.all([
        skillInventory(),
        listExtensions(),
      ]);
      if (disposed || current !== request) return;
      skills = (inventory.skills ?? []).filter(
        (entry) => entry.source_kind === 'bundled',
      );
      extensions = (catalog.extensions ?? []).filter(
        (entry) => entry.status !== 'overridden',
      );
    } catch (failure) {
      if (!disposed && current === request) error = failure.message;
    } finally {
      if (!disposed && current === request) loading = false;
    }
  }

  function toggle(kind, id, enabled) {
    pending = { kind, id, disabled: !enabled };
    void participant.runSave('manual');
  }

  async function saveSelection() {
    const change = pending;
    if (!change) return true;
    saving = true;
    error = '';
    try {
      if (change.kind === 'skill') {
        await setSkillDisabled(change.id, change.disabled);
      } else {
        const current = await listExtensions();
        await updateSettings(
          buildExtensionsUpdate(current.settings, {
            toggle: { name: change.id, disabled: change.disabled },
          }),
        );
      }
      if (pending === change) pending = null;
      await load();
      return true;
    } catch (failure) {
      if (!disposed) error = failure.message;
      return false;
    } finally {
      if (!disposed) saving = false;
    }
  }
</script>

<div class="onboarding-components">
  {#if error}
    <Banner variant="error" role="alert">
      {error}
      <Button
        variant="secondary"
        onClick={() => (pending ? participant.runSave('manual') : load())}
        >{t('onboarding.retry')}</Button
      >
    </Banner>
  {/if}
  {#if loading}<p role="status">{t('common.loading')}</p>{/if}
  <h4>{t('onboarding.components.skills')}</h4>
  {#each skills as skill (skill.id)}
    <div class="onboarding-component" data-setup-skill={skill.id}>
      <div>
        <strong>{skill.name}</strong>
        <p>{skill.description}</p>
      </div>
      <Toggle
        checked={!skill.disabled}
        disabled={saving || pending !== null}
        ariaLabel={t('onboarding.components.toggle', { name: skill.name })}
        onChange={(enabled) => toggle('skill', skill.id, enabled)}
      />
    </div>
  {/each}
  <h4>{t('onboarding.components.extensions')}</h4>
  {#each extensions as extension (extension.name)}
    <div class="onboarding-component" data-setup-extension={extension.name}>
      <div>
        <strong>{extension.display_name || extension.name}</strong>
        <p>{extension.description}</p>
        {#if extension.error}<p class="onboarding-help">
            {extension.error}
          </p>{/if}
      </div>
      <Toggle
        checked={!extension.disabled}
        disabled={saving || pending !== null}
        ariaLabel={t('onboarding.components.toggle', {
          name: extension.display_name || extension.name,
        })}
        onChange={(enabled) => toggle('extension', extension.name, enabled)}
      />
    </div>
  {/each}
  {#if saving}<p role="status">{t('common.saving')}</p>{/if}
</div>
