<script>
  import { onDestroy, untrack } from 'svelte';

  import Dropdown from '../Dropdown.svelte';
  import SaveButton from '../ui/SaveButton.svelte';
  import {
    createDebouncedAutosave,
    useAutosaveContext,
  } from '$lib/autosave.js';
  import { t } from '$lib/i18n.js';
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
          ? t('settings.webFetch.direct', 'Direct (no service)')
          : t(
              `settings.webSearch.providers.${id}`,
              id === 'parallel' ? 'Parallel' : id,
            ),
    })),
  );
  const service = $derived(
    settings?.web_fetch?.services?.find((item) => item.id === draft.provider),
  );
  const modes = $derived([
    {
      value: 'fallback',
      label: t('settings.webFetch.fallback', 'Only when direct fetch fails'),
    },
    {
      value: 'prefer',
      label: t('settings.webFetch.prefer', 'Prefer this service'),
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
      successKey: 'settings.webFetch.saveSuccess',
      successFallback: 'Web fetch settings updated.',
      getDraftSnapshot: () => draft,
      applyResult: (next) => (draft = getWebFetchSettings(next)),
    });
  }

  function manualSave() {
    if (saving) return;
    if (saveDisabled) {
      onToast({
        title: t('common.alreadySaved', 'Already saved'),
        variant: 'success',
      });
      return;
    }
    autosave.cancelPendingTimer();
    void autosave.participant.runSave('manual');
  }
</script>

<div class="s-row">
  <div class="s-row-info">
    <div class="s-row-label">
      {t('settings.webFetch.provider', 'Page extraction service')}
    </div>
    <div class="s-row-desc">
      {t(
        'settings.webFetch.description',
        'Read pages directly, or use an optional service for difficult websites and JavaScript content.',
      )}
    </div>
  </div>
  <div class="s-row-control s-row-control--web-search">
    <Dropdown
      id="settings-web-fetch-provider"
      value={draft.provider}
      options={providers}
      ariaLabel={t('settings.webFetch.provider', 'Page extraction service')}
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
        {t('settings.webFetch.mode', 'When to use it')}
      </div>
      <div class="s-row-desc">
        {t(
          'settings.webFetch.modeDescription',
          'Fallback uses the service for blocked, failed or unreadable pages. Prefer uses it first for page URLs and tries direct fetch if it fails.',
        )}
      </div>
    </div>
    <div class="s-row-control s-row-control--web-search">
      <Dropdown
        id="settings-web-fetch-mode"
        value={draft.mode}
        options={modes}
        ariaLabel={t('settings.webFetch.mode', 'When to use it')}
        triggerClass="settings-view__dropdown"
        listClass="settings-view__thinking-list"
        onValueChange={(value) => change('mode', value)}
      />
    </div>
  </div>
  <div class="s-row s-row--stacked">
    <div class="s-row-info">
      <div class="s-row-desc">
        {t(
          'settings.webFetch.cost',
          'The selected service receives requested URLs and may charge per page. Free allowances and prices vary. Reading or searching an already saved page makes no additional service request.',
        )}
      </div>
      {#if service}
        <div class="s-row-desc">
          {service.configured
            ? t('settings.webFetch.keyPresent', 'API key configured.')
            : t('settings.webFetch.keyMissing', 'API key required:')}
          <code>{service.api_key_env}</code>
          {#if !service.configured}
            {t(
              'settings.webFetch.keyHint',
              'Set this variable in the .env file in the vBot data directory.',
            )}
            <code>{settings?.general?.data_directory ?? ''}</code>
          {/if}
          <a href={service.pricing_url} target="_blank" rel="noreferrer"
            >{t('settings.webFetch.pricing', 'Service pricing')}</a
          >
        </div>
      {/if}
    </div>
  </div>
{/if}

<div class="s-footer">
  <SaveButton
    class="s-save-button s-save-button--inline"
    {saving}
    pending={autosave.participant.hasPending()}
    onClick={manualSave}
  />
</div>
