<script>
  import { onDestroy, untrack } from 'svelte';

  import Dropdown from '../Dropdown.svelte';
  import InfoHint from '../ui/InfoHint.svelte';
  import SaveStatus from '../ui/SaveStatus.svelte';
  import ServiceApiKey from './ServiceApiKey.svelte';
  import {
    createDebouncedAutosave,
    useAutosaveContext,
  } from '$lib/autosave.js';
  import { t, tOr } from '$lib/i18n.js';
  import { createSettingsDraft } from '$lib/settingsSave.js';
  import {
    buildWebFetchSettingsPayload,
    getWebFetchSettings,
    getWebServiceKeys,
    webServiceKeyHint,
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
  const webFetchDraft = createSettingsDraft({
    settings: untrack(() => settings),
    fromSettings: getWebFetchSettings,
    read: () => draft,
    write: (next) => (draft = next),
    toPayload: buildWebFetchSettingsPayload,
  });
  const context = useAutosaveContext();
  const autosave = createDebouncedAutosave({
    getSnapshot: () => ({ ...draft }),
    hasChanges,
    save,
  });
  const unregister = context.register(autosave.participant);
  const saveDisabled = $derived(saving || !hasChanges());
  // Whether each service's API key is set is a server fact read from the
  // current settings, never part of the draft or its dirty comparison.
  const services = $derived(getWebServiceKeys(settings, 'web_fetch'));
  const providers = $derived(
    (settings?.web_fetch?.available_providers ?? ['direct']).map((id) => {
      const keyed = services.find((item) => item.id === id);
      return {
        value: id,
        label:
          id === 'direct'
            ? t('settings.webFetch.direct')
            : tOr(`settings.webSearch.providers.${id}`, id),
        ...(keyed ? { secondaryLabel: webServiceKeyHint(keyed) } : {}),
      };
    }),
  );
  const service = $derived(
    services.find((item) => item.id === draft.provider) ?? null,
  );
  const pricingUrl = $derived(
    settings?.web_fetch?.services?.find((item) => item.id === draft.provider)
      ?.pricing_url ?? '',
  );
  const dataDirectory = $derived(settings?.general?.data_directory ?? '');
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

  async function save() {
    if (!hasChanges()) return true;
    return webFetchDraft.save({
      onCommit,
      onError,
      setSaving: (value) => (saving = value),
    });
  }
</script>

<div class="s-group">
  <div class="s-row">
    <div class="s-row-info">
      <div class="s-row-label">
        {t('settings.webFetch.provider')}
        <InfoHint text={t('settings.webFetch.providerHelp')} />
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
    <!-- What opting in means for the chosen service: URL sharing and cost,
         then its API key. -->
    <div class="s-group__block s-group__block--attached s-group__note">
      <p>
        {t('settings.webFetch.cost')}
        {#if pricingUrl}
          <a href={pricingUrl} target="_blank" rel="noreferrer">
            {t('settings.webFetch.pricing')}
            <span aria-hidden="true">↗</span>
          </a>
        {/if}
      </p>
    </div>
    {#if service}
      <ServiceApiKey
        id="settings-web-fetch-api-key"
        {service}
        {dataDirectory}
        {onCommit}
        {onToast}
        {onError}
      />
    {/if}
    <div class="s-row">
      <div class="s-row-info">
        <div class="s-row-label">
          {t('settings.webFetch.mode')}
          <InfoHint text={t('settings.webFetch.modeHelp')} />
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
  {/if}
</div>

<div class="s-footer">
  <SaveStatus
    {saving}
    pending={autosave.participant.hasChanges()}
    onClick={() => autosave.participant.runSave('manual')}
  />
</div>
