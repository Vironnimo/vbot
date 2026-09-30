<script>
  import { onDestroy, untrack } from 'svelte';

  import Dropdown from '../Dropdown.svelte';
  import InfoHint from '../ui/InfoHint.svelte';
  import SaveStatus from '../ui/SaveStatus.svelte';
  import TextField from '../ui/TextField.svelte';
  import ServiceApiKey from './ServiceApiKey.svelte';
  import {
    createDebouncedAutosave,
    useAutosaveContext,
  } from '$lib/autosave.js';
  import { t } from '$lib/i18n.js';
  import { createSettingsDraft } from '$lib/settingsSave.js';
  import {
    buildWebSearchProviderOptions,
    buildWebSearchSettingsPayload,
    getWebSearchSettings,
    getWebServiceKeys,
  } from '$lib/settingsView.js';

  const noop = () => {};

  let {
    settings = null,
    onCommit = noop,
    onToast = noop,
    onError = noop,
  } = $props();

  // Form is seeded once from the settings prop at mount (untrack avoids a
  // reactive dependency); later commits flow back through saveDisabled.
  let webSearchSettings = $state(untrack(() => getWebSearchSettings(settings)));
  let saving = $state(false);
  const webSearchDraft = createSettingsDraft({
    settings: untrack(() => settings),
    fromSettings: getWebSearchSettings,
    read: () => webSearchSettings,
    write: (next) => (webSearchSettings = next),
    toPayload: buildWebSearchSettingsPayload,
  });

  // Whether each keyed provider's API key is set is a server fact read from
  // the current settings, never part of the draft or its dirty comparison.
  let services = $derived(getWebServiceKeys(settings, 'web_search'));
  let webSearchProviderOptions = $derived(
    buildWebSearchProviderOptions(webSearchSettings, services),
  );
  // Keyless providers (SearXNG, DuckDuckGo) have no service entry.
  let service = $derived(
    services.find((item) => item.id === webSearchSettings.provider) ?? null,
  );
  let dataDirectory = $derived(settings?.general?.data_directory ?? '');
  let saveDisabled = $derived(saving || !webSearchDraftHasChanges());
  const autosaveContext = useAutosaveContext();
  const webSearchAutosave = createDebouncedAutosave({
    getSnapshot: () => ({
      ...webSearchSettings,
      searxng: { ...(webSearchSettings.searxng ?? {}) },
    }),
    hasChanges: webSearchDraftHasChanges,
    save: saveWebSearchSettings,
  });
  const unregisterWebSearchAutosave = autosaveContext.register(
    webSearchAutosave.participant,
  );

  $effect(() => {
    if (saveDisabled) {
      return;
    }

    webSearchAutosave.scheduleRun();

    return () => {
      webSearchAutosave.cancelPendingTimer();
    };
  });

  onDestroy(() => {
    unregisterWebSearchAutosave();
    webSearchAutosave.cancelPendingTimer();
  });

  function webSearchSettingsMatch(left, right) {
    const normalizedLeft = getWebSearchSettings({ web_search: left });
    const normalizedRight = getWebSearchSettings({ web_search: right });

    return (
      normalizedLeft.provider === normalizedRight.provider &&
      normalizedLeft.default_count === normalizedRight.default_count &&
      normalizedLeft.searxng.base_url === normalizedRight.searxng.base_url
    );
  }

  // Dirty state, scheduling and saving all compare the normalized draft (the
  // payload that would be sent) with the persisted values, so a cleared field
  // that normalizes to the stored value is not a pending change.
  function webSearchDraftHasChanges() {
    return !webSearchSettingsMatch(
      webSearchSettings,
      getWebSearchSettings(settings),
    );
  }

  function handleWebSearchProviderChange(provider) {
    webSearchSettings = {
      ...webSearchSettings,
      provider,
    };
    onError('');
  }

  function handleWebSearchDefaultCountChange(next) {
    webSearchSettings = {
      ...webSearchSettings,
      default_count: next,
    };
    onError('');
  }

  function handleWebSearchSearxngBaseUrlChange(event) {
    webSearchSettings = {
      ...webSearchSettings,
      searxng: {
        ...(webSearchSettings.searxng ?? {}),
        base_url: event.currentTarget.value,
      },
    };
    onError('');
  }

  async function saveWebSearchSettings() {
    if (!webSearchDraftHasChanges()) {
      return true;
    }

    return webSearchDraft.save({
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
        {t('settings.webSearch.provider')}
        <InfoHint text={t('settings.webSearch.providerHelp')} />
      </div>
    </div>
    <div class="s-row-control s-row-control--web-search">
      <Dropdown
        id="settings-web-search-provider"
        value={webSearchSettings.provider}
        options={webSearchProviderOptions}
        ariaLabel={t('settings.webSearch.provider')}
        triggerClass="settings-view__dropdown"
        listClass="settings-view__thinking-list"
        onValueChange={handleWebSearchProviderChange}
      />
    </div>
  </div>
  {#if service}
    <ServiceApiKey
      id="settings-web-search-api-key"
      {service}
      {dataDirectory}
      {onCommit}
      {onToast}
      {onError}
    />
  {/if}
  {#if webSearchSettings.provider === 'searxng'}
    <div class="s-row">
      <div class="s-row-info">
        <div class="s-row-label">
          {t('settings.webSearch.searxngBaseUrl')}
          <InfoHint text={t('settings.webSearch.searxngBaseUrlHelp')} />
        </div>
        <div class="s-row-desc">
          {t('settings.webSearch.searxngBaseUrlDescription')}
        </div>
      </div>
      <div class="s-row-control s-row-control--web-search-url">
        <TextField
          id="settings-web-search-searxng-base-url"
          code
          type="url"
          value={webSearchSettings.searxng.base_url}
          placeholder={t('settings.webSearch.searxngBaseUrlPlaceholder')}
          ariaLabel={t('settings.webSearch.searxngBaseUrl')}
          onInput={(_next, event) => handleWebSearchSearxngBaseUrlChange(event)}
        />
      </div>
    </div>
  {/if}
  <div class="s-row">
    <div class="s-row-info">
      <div class="s-row-label">
        {t('settings.webSearch.defaultCount')}
        <InfoHint text={t('settings.webSearch.defaultCountHelp')} />
      </div>
    </div>
    <div class="s-row-control s-row-control--number">
      <TextField
        id="settings-web-search-default-count"
        type="number"
        min="1"
        max="20"
        step="1"
        value={webSearchSettings.default_count}
        ariaLabel={t('settings.webSearch.defaultCount')}
        onInput={(next) => handleWebSearchDefaultCountChange(next)}
      />
    </div>
  </div>
</div>

<div class="s-footer">
  <SaveStatus
    {saving}
    pending={webSearchAutosave.participant.hasChanges()}
    onClick={() => webSearchAutosave.participant.runSave('manual')}
  />
</div>
