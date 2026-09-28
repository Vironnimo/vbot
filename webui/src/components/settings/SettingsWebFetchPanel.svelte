<script>
  import { onDestroy, untrack } from 'svelte';

  import Dropdown from '../Dropdown.svelte';
  import SaveButton from '../ui/SaveButton.svelte';
  import {
    createDebouncedAutosave,
    useAutosaveContext,
  } from '$lib/autosave.js';
  import { t, tOr } from '$lib/i18n.js';
  import { runSettingsSave } from '$lib/settingsSave.js';
  import {
    buildWebFetchSettingsPayload,
    getWebFetchSettings,
  } from '$lib/settingsView.js';

  const noop = () => {};
  let {
    settings = null,
    onCommit = noop,
    onToast = noop,
    onError = noop,
  } = $props();
  let draft = $state(untrack(() => getWebFetchSettings(settings)));
  let saving = $state(false);
  const context = useAutosaveContext();
  const autosave = createDebouncedAutosave({
    getSnapshot: () => ({ ...draft }),
    hasChanges,
    save,
  });
  const unregister = context.register(autosave.participant);
  const saveDisabled = $derived(saving || !hasChanges());
  const providers = $derived(
    (settings?.web_fetch?.available_providers ?? ['direct']).map((id) => ({
      value: id,
      label:
        id === 'direct'
          ? t('settings.webFetch.direct')
          : tOr(`settings.webSearch.providers.${id}`, id),
    })),
  );
  const service = $derived(
    settings?.web_fetch?.services?.find((item) => item.id === draft.provider),
  );
  const modes = $derived([
    {
      value: 'fallback',
      label: t('settings.webFetch.fallback'),
    },
    {
      value: 'prefer',
      label: t('settings.webFetch.prefer'),
    },
  ]);

  $effect(() => {
    if (saveDisabled) return;
    autosave.scheduleRun();
    return () => autosave.cancelPendingTimer();
  });
  onDestroy(() => {
    unregister();
    autosave.cancelPendingTimer();
  });

  function hasChanges() {
    const persisted = getWebFetchSettings(settings);
    return (
      draft.provider !== persisted.provider || draft.mode !== persisted.mode
    );
  }

  function change(key, value) {
    draft = { ...draft, [key]: value };
    onError('');
  }

  async function save(reason) {
    if (!hasChanges()) return true;
    return runSettingsSave({
      reason,
      onCommit,
      onToast,
      onError,
      setSaving: (value) => (saving = value),
      buildPayload: () => buildWebFetchSettingsPayload(draft),
      successTitle: t('settings.webFetch.saveSuccess'),
      getDraftSnapshot: () => draft,
      applyResult: (next) => (draft = getWebFetchSettings(next)),
    });
  }

  function manualSave() {
    if (saving) return;
    if (saveDisabled) {
      onToast({
        title: t('common.alreadySaved'),
        variant: 'success',
      });
      return;
    }
    autosave.cancelPendingTimer();
    void autosave.participant.runSave('manual');
  }
</script>

<div class="s-group">
  <div class="s-row">
    <div class="s-row-info">
      <div class="s-row-label">
        {t('settings.webFetch.provider')}
      </div>
      <div class="s-row-desc">
        {t('settings.webFetch.description')}
      </div>
    </div>
    <div class="s-row-control s-row-control--web-search">
      <Dropdown
        id="settings-web-fetch-provider"
        value={draft.provider}
        options={providers}
        ariaLabel={t('settings.webFetch.provider')}
        triggerClass="settings-view__dropdown"
        listClass="settings-view__thinking-list"
        onValueChange={(value) => change('provider', value)}
      />
    </div>
  </div>

  {#if draft.provider !== 'direct'}
    <div class="s-row">
      <div class="s-row-info">
        <div class="s-row-label">
          {t('settings.webFetch.mode')}
        </div>
        <div class="s-row-desc">
          {t('settings.webFetch.modeDescription')}
        </div>
      </div>
      <div class="s-row-control s-row-control--web-search">
        <Dropdown
          id="settings-web-fetch-mode"
          value={draft.mode}
          options={modes}
          ariaLabel={t('settings.webFetch.mode')}
          triggerClass="settings-view__dropdown"
          listClass="settings-view__thinking-list"
          onValueChange={(value) => change('mode', value)}
        />
      </div>
    </div>
    <div class="s-group__block s-group__note">
      <p>
        {t('settings.webFetch.cost')}
      </p>
      {#if service}
        <p>
          {service.configured
            ? t('settings.webFetch.keyPresent')
            : t('settings.webFetch.keyMissing')}
          <code>{service.api_key_env}</code>
          {#if !service.configured}
            {t('settings.webFetch.keyHint')}
            <code>{settings?.general?.data_directory ?? ''}</code>
          {/if}
          <a href={service.pricing_url} target="_blank" rel="noreferrer"
            >{t('settings.webFetch.pricing')}</a
          >
        </p>
      {/if}
    </div>
  {/if}
</div>

<div class="s-footer">
  <SaveButton
    class="s-save-button s-save-button--inline"
    {saving}
    pending={autosave.participant.hasChanges()}
    onClick={manualSave}
  />
</div>
