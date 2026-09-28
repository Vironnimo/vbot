<script>
  import { onDestroy, untrack } from 'svelte';

  import Dropdown from '../Dropdown.svelte';
  import SaveButton from '../ui/SaveButton.svelte';
  import TextField from '../ui/TextField.svelte';
  import {
    createDebouncedAutosave,
    useAutosaveContext,
  } from '$lib/autosave.js';
  import { t } from '$lib/i18n.js';
  import { runSettingsSave } from '$lib/settingsSave.js';
  import {
    buildWebSearchProviderOptions,
    buildWebSearchSettingsPayload,
    getWebSearchSettings,
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

  let webSearchProviderOptions = $derived(
    buildWebSearchProviderOptions(webSearchSettings),
  );
  // Hosted providers need an API key in the data directory's .env file.
  let keyHint = $derived(webSearchKeyHint(webSearchSettings.provider));
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

  function handleManualWebSearchSettingsSave() {
    if (saving) {
      return;
    }

    if (saveDisabled) {
      onToast({
        title: t('common.alreadySaved'),
        variant: 'success',
      });
      return;
    }

    webSearchAutosave.cancelPendingTimer();
    void webSearchAutosave.participant.runSave('manual');
  }

  async function saveWebSearchSettings(reason) {
    if (!webSearchDraftHasChanges()) {
      return true;
    }

    return runSettingsSave({
      reason,
      onCommit,
      onToast,
      onError,
      setSaving: (value) => (saving = value),
      buildPayload: () => buildWebSearchSettingsPayload(webSearchSettings),
      successTitle: t('settings.webSearch.saveSuccess'),
      getDraftSnapshot: () => webSearchSettings,
      applyResult: (next) => (webSearchSettings = getWebSearchSettings(next)),
    });
  }

  function webSearchKeyHint(provider) {
    switch (provider) {
      case 'brave':
        return t('settings.webSearch.braveKeyHint');
      case 'tavily':
        return t('settings.webSearch.tavilyKeyHint');
      case 'exa':
        return t('settings.webSearch.exaKeyHint');
      case 'serper':
        return t('settings.webSearch.serperKeyHint');
      case 'firecrawl':
        return t('settings.webSearch.firecrawlKeyHint');
      case 'perplexity':
        return t('settings.webSearch.perplexityKeyHint');
      default:
        return '';
    }
  }
</script>

<div class="s-group">
  <div class="s-row">
    <div class="s-row-info">
      <div class="s-row-label">
        {t('settings.webSearch.provider')}
      </div>
      <div class="s-row-desc">
        {t('settings.webSearch.providerDescription')}
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
  {#if keyHint}
    <div class="s-group__block s-group__block--attached s-group__note">
      {keyHint}
    </div>
  {/if}
  {#if webSearchSettings.provider === 'searxng'}
    <div class="s-row">
      <div class="s-row-info">
        <div class="s-row-label">
          {t('settings.webSearch.searxngBaseUrl')}
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
      </div>
      <div class="s-row-desc">
        {t('settings.webSearch.defaultCountDescription')}
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
  <SaveButton
    class="s-save-button s-save-button--inline"
    {saving}
    pending={webSearchAutosave.participant.hasChanges()}
    onClick={handleManualWebSearchSettingsSave}
  />
</div>
