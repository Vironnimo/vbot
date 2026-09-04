<script>
  import { onDestroy, untrack } from 'svelte';

  import Dropdown from '../Dropdown.svelte';
  import Button from '../ui/Button.svelte';
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
    buildWebSearchProviderOptions(webSearchSettings, t),
  );
  let saveDisabled = $derived(
    saving ||
      webSearchSettingsMatch(webSearchSettings, getWebSearchSettings(settings)),
  );
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

  function webSearchDraftHasChanges() {
    const persisted = getWebSearchSettings(settings);
    return (
      webSearchSettings.provider !== persisted.provider ||
      String(webSearchSettings.default_count) !==
        String(persisted.default_count) ||
      webSearchSettings.searxng?.base_url !== persisted.searxng.base_url
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
        title: t('common.alreadySaved', 'Already saved'),
        variant: 'success',
      });
      return;
    }

    webSearchAutosave.cancelPendingTimer();
    void webSearchAutosave.participant.runSave('manual');
  }

  async function saveWebSearchSettings() {
    if (!webSearchDraftHasChanges()) {
      return true;
    }

    return runSettingsSave({
      onCommit,
      onToast,
      onError,
      setSaving: (value) => (saving = value),
      buildPayload: () => buildWebSearchSettingsPayload(webSearchSettings),
      successKey: 'settings.webSearch.saveSuccess',
      successFallback: 'Web search settings updated.',
      getDraftSnapshot: () => webSearchSettings,
      applyResult: (next) => (webSearchSettings = getWebSearchSettings(next)),
    });
  }
</script>

<div class="s-row">
  <div class="s-row-info">
    <div class="s-row-label">
      {t('settings.webSearch.provider', 'Search provider')}
    </div>
    <div class="s-row-desc">
      {t(
        'settings.webSearch.providerDescription',
        'Provider used whenever an agent calls web_search.',
      )}
    </div>
  </div>
  <div class="s-row-control s-row-control--web-search">
    <Dropdown
      id="settings-web-search-provider"
      value={webSearchSettings.provider}
      options={webSearchProviderOptions}
      ariaLabel={t('settings.webSearch.provider', 'Search provider')}
      triggerClass="settings-view__dropdown"
      listClass="settings-view__thinking-list"
      onValueChange={handleWebSearchProviderChange}
    />
  </div>
</div>

<div class="s-row">
  <div class="s-row-info">
    <div class="s-row-label">
      {t('settings.webSearch.defaultCount', 'Default result count')}
    </div>
    <div class="s-row-desc">
      {t(
        'settings.webSearch.defaultCountDescription',
        'Number of results a web_search call returns when the agent does not ask for a specific count (1-20).',
      )}
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
      ariaLabel={t('settings.webSearch.defaultCount', 'Default result count')}
      onInput={(next) => handleWebSearchDefaultCountChange(next)}
    />
  </div>
</div>

{#if webSearchSettings.provider === 'brave'}
  <div class="s-row s-row--stacked">
    <div class="s-row-info">
      <div class="s-row-desc">
        {t(
          'settings.webSearch.braveKeyHint',
          'Brave Search requires an API key: set BRAVE_API_KEY in the .env file in the vBot data directory. Without it, every web search fails.',
        )}
      </div>
    </div>
  </div>
{/if}

{#if webSearchSettings.provider === 'tavily'}
  <div class="s-row s-row--stacked">
    <div class="s-row-info">
      <div class="s-row-desc">
        {t(
          'settings.webSearch.tavilyKeyHint',
          'Tavily requires an API key: set TAVILY_API_KEY in the .env file in the vBot data directory. Without it, every web search fails.',
        )}
      </div>
    </div>
  </div>
{/if}

{#if webSearchSettings.provider === 'exa'}
  <div class="s-row s-row--stacked">
    <div class="s-row-info">
      <div class="s-row-desc">
        {t(
          'settings.webSearch.exaKeyHint',
          'Exa requires an API key: set EXA_API_KEY in the .env file in the vBot data directory. Without it, every web search fails.',
        )}
      </div>
    </div>
  </div>
{/if}

{#if webSearchSettings.provider === 'serper'}
  <div class="s-row s-row--stacked">
    <div class="s-row-info">
      <div class="s-row-desc">
        {t(
          'settings.webSearch.serperKeyHint',
          'Serper requires an API key: set SERPER_API_KEY in the .env file in the vBot data directory. Without it, every web search fails.',
        )}
      </div>
    </div>
  </div>
{/if}

{#if webSearchSettings.provider === 'firecrawl'}
  <div class="s-row s-row--stacked">
    <div class="s-row-info">
      <div class="s-row-desc">
        {t(
          'settings.webSearch.firecrawlKeyHint',
          'Firecrawl requires an API key: set FIRECRAWL_API_KEY in the .env file in the vBot data directory. Without it, every web search fails.',
        )}
      </div>
    </div>
  </div>
{/if}

{#if webSearchSettings.provider === 'perplexity'}
  <div class="s-row s-row--stacked">
    <div class="s-row-info">
      <div class="s-row-desc">
        {t(
          'settings.webSearch.perplexityKeyHint',
          'Perplexity requires an API key: set PERPLEXITY_API_KEY in the .env file in the vBot data directory. Without it, every web search fails.',
        )}
      </div>
    </div>
  </div>
{/if}

{#if webSearchSettings.provider === 'searxng'}
  <div class="s-row">
    <div class="s-row-info">
      <div class="s-row-label">
        {t('settings.webSearch.searxngBaseUrl', 'SearXNG base URL')}
      </div>
      <div class="s-row-desc">
        {t(
          'settings.webSearch.searxngBaseUrlDescription',
          'Address of the SearXNG instance to use. SearXNG is a self-hosted metasearch engine — you need to run one yourself or point this at a reachable instance.',
        )}
      </div>
    </div>
    <div class="s-row-control s-row-control--web-search-url">
      <TextField
        id="settings-web-search-searxng-base-url"
        type="url"
        value={webSearchSettings.searxng.base_url}
        placeholder={t(
          'settings.webSearch.searxngBaseUrlPlaceholder',
          'http://localhost:8888',
        )}
        ariaLabel={t('settings.webSearch.searxngBaseUrl', 'SearXNG base URL')}
        onInput={(_next, event) => handleWebSearchSearxngBaseUrlChange(event)}
      />
    </div>
  </div>
{/if}

<div class="s-footer">
  <Button
    variant="primary"
    class="s-save-button s-save-button--inline"
    onClick={handleManualWebSearchSettingsSave}
  >
    {saving ? t('common.saving', 'Saving…') : t('common.save', 'Save')}
  </Button>
</div>
