<script>
  import { onDestroy, untrack } from 'svelte';

  import Dropdown from '../Dropdown.svelte';
  import Button from '../ui/Button.svelte';
  import { rpc } from '$lib/api.js';
  import { t } from '$lib/i18n.js';
  import {
    buildWebSearchProviderOptions,
    buildWebSearchSettingsPayload,
    getWebSearchSettings,
  } from '$lib/settingsView.js';

  const AUTO_SAVE_DEBOUNCE_MS = 800;
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
  let autoSaveTimer = null;

  let webSearchProviderOptions = $derived(
    buildWebSearchProviderOptions(webSearchSettings, t),
  );
  let saveDisabled = $derived(
    saving ||
      webSearchSettingsMatch(webSearchSettings, getWebSearchSettings(settings)),
  );

  $effect(() => {
    if (saveDisabled) {
      return;
    }

    autoSaveTimer = setTimeout(() => {
      autoSaveTimer = null;
      void saveWebSearchSettings();
    }, AUTO_SAVE_DEBOUNCE_MS);

    return () => {
      clearAutoSaveTimer();
    };
  });

  onDestroy(() => {
    clearAutoSaveTimer();
  });

  function clearAutoSaveTimer() {
    if (autoSaveTimer !== null) {
      clearTimeout(autoSaveTimer);
      autoSaveTimer = null;
    }
  }

  function webSearchSettingsMatch(left, right) {
    const normalizedLeft = getWebSearchSettings({ web_search: left });
    const normalizedRight = getWebSearchSettings({ web_search: right });

    return (
      normalizedLeft.provider === normalizedRight.provider &&
      normalizedLeft.searxng.base_url === normalizedRight.searxng.base_url
    );
  }

  function handleWebSearchProviderChange(provider) {
    webSearchSettings = {
      ...webSearchSettings,
      provider,
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

    clearAutoSaveTimer();
    void saveWebSearchSettings();
  }

  async function saveWebSearchSettings() {
    if (saveDisabled) {
      return;
    }

    saving = true;
    onError('');

    try {
      const nextSettings = await rpc(
        'settings.update',
        buildWebSearchSettingsPayload(webSearchSettings),
      );
      onCommit(nextSettings);
      webSearchSettings = getWebSearchSettings(nextSettings);
      onToast({
        title: t(
          'settings.webSearch.saveSuccess',
          'Web search settings updated.',
        ),
        variant: 'success',
      });
    } catch (error) {
      onError(
        `${t('settings.saveError', 'Settings could not be saved.')} ${error.message}`,
      );
    } finally {
      saving = false;
    }
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

{#if webSearchSettings.provider === 'searxng'}
  <div class="s-row">
    <div class="s-row-info">
      <div class="s-row-label">
        {t('settings.webSearch.searxngBaseUrl', 'SearXNG base URL')}
      </div>
      <div class="s-row-desc">
        {t(
          'settings.webSearch.searxngBaseUrlDescription',
          'Base URL of the local or remote SearXNG instance.',
        )}
      </div>
    </div>
    <div class="s-row-control s-row-control--web-search-url">
      <input
        id="settings-web-search-searxng-base-url"
        class="s-input"
        type="url"
        value={webSearchSettings.searxng.base_url}
        placeholder="http://localhost:8888"
        aria-label={t('settings.webSearch.searxngBaseUrl', 'SearXNG base URL')}
        oninput={handleWebSearchSearxngBaseUrlChange}
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
    {saving
      ? t('common.saving', 'Saving…')
      : t('settings.webSearch.save', 'Save')}
  </Button>
</div>
